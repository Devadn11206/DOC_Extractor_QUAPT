"""
Shared, Stateless Field Assembly & Canonical Envelope Construction Service.
Strictly adheres to PRD §6.3 (SCH-011, SCH-012, SCH-013), §5.4 (FR-EXT-003, FR-EXT-004),
§5.5 (FR-VAL-001..004), and §5.6 (FR-CNF-001..003, GR-002, GR-003, GR-027).
Shared by both static ModuleCPipeline and dynamic DynamicExtractionAdapter.
"""

from typing import Any, Dict, List, Literal, Optional, Tuple, Union
import re

from doc_extractor.contracts_b import ConfidenceProvenance, PageLayoutOutput
from doc_extractor.contracts_c import (
    CanonicalExtractionEnvelope,
    DocumentMetadata,
    EvidenceSnippet,
    ExecutionProvenance,
    ExtractedField,
    FlagItem,
    RunMetadata,
)
from doc_extractor.extractor import find_evidence_for_value
from doc_extractor.validation_rules import calibrate_field_confidence

# Existing default per-field review threshold (FR-CNF-002; overridable per schema field).
DEFAULT_FIELD_CONFIDENCE_THRESHOLD = 0.75

# Flag code for a schema-required field that no page produced (forces needs_review).
REQUIRED_FIELD_MISSING = "REQUIRED_FIELD_MISSING"

# Confidence sources that can never authorize automatic approval on their own.
NON_AUTHORIZING_PROVENANCE = frozenset({
    ConfidenceProvenance.UNAVAILABLE,
    ConfidenceProvenance.UNCALIBRATED,
})


ILLEGIBLE_INDICATORS = (
    "[illegible]",
    "[unreadable]",
    "illegible",
    "unreadable",
    "smudged",
    "blurred",
    "???",
)


def find_null_reason_signal(
    field_name: str,
    pages: List[PageLayoutOutput],
) -> Literal["absent", "illegible"]:
    """
    PRD FR-EXT-003: Derive distinct null reason (absent vs illegible) from document signals.
    Inspects layout elements, degraded checkbox marks ('[?]'), low optical confidence,
    and text tokens indicating illegibility near field name/label.
    """
    clean_field = field_name.split(".")[-1].lower()
    field_tokens = [t for t in re.split(r"[^a-z0-9]+", clean_field) if len(t) >= 3]
    if not field_tokens:
        field_tokens = [clean_field]

    for page in pages:
        # Checkboxes with uncertain / degraded state
        for cb in page.checkboxes:
            if cb.state == "[?]" or cb.is_uncertain:
                cb_id = cb.mark_id.lower()
                if clean_field in cb_id or any(t in cb_id for t in field_tokens):
                    return "illegible"

        # Layout blocks with illegible indicators or optical confidence < 0.35
        for block in page.blocks:
            text_low = block.text.lower()
            matches_field = (
                clean_field in text_low
                or any(re.search(r"\b" + re.escape(t) + r"\b", text_low) for t in field_tokens)
            )
            if matches_field:
                if any(ind in text_low for ind in ILLEGIBLE_INDICATORS):
                    return "illegible"
                if block.ocr_confidence is not None and block.ocr_confidence < 0.35:
                    return "illegible"

    return "absent"


def assemble_field_entry(
    field_name: str,
    value: Any,
    pages: List[PageLayoutOutput],
    flags: List[FlagItem],
    error: Optional[str] = None,
    required: bool = False,
    confidence_threshold: float = DEFAULT_FIELD_CONFIDENCE_THRESHOLD,
    evidence_required: bool = True,
    explicit_null_reason: Optional[Literal["absent", "illegible"]] = None,
    strict_boolean_evidence: bool = True,
    evidence_hint: Optional[Dict[str, Any]] = None,
    model_confidence: Optional[float] = None,
) -> ExtractedField:
    """
    Assembles a validated ExtractedField envelope with evidence grounding (FR-EXT-004, GR-002),
    null-if-absent / illegible logic (FR-EXT-003, GR-003), and calibrated confidence (FR-CNF-001).

    Confidence integrity: the score is built only from measured signals - the evidence's optical
    OCR confidence and, when the caller supplies one, a trustworthy ``model_confidence`` (no
    extractor currently provides one, so it defaults to None and is never assumed to be 1.0).
    Without any signal the field's confidence is None with provenance ``unavailable``, and an
    unavailable score cannot meet ``confidence_threshold``, so the field requires review and
    can never contribute to automatic approval.

    ``evidence_hint`` optionally supplies already-grounded evidence (same keys as
    ``find_evidence_for_value`` results: page, bbox in 200-DPI pixels, text, ocr_confidence),
    e.g. from a multimodal extractor after CoordinateTransform. When absent, evidence is
    searched in the parsed OCR layout as before. Evidence flagged ``uncertain`` (uncertain model
    reading, degraded checkbox, ambiguous character grid) forces review.
    """
    # 1. Null Value Handling (FR-EXT-003, GR-003)
    if value is None:
        null_reason = explicit_null_reason or find_null_reason_signal(field_name, pages)
        # Required nulls fail validation and require review.
        # Optional illegible fields require review; optional absent fields are not_applicable.
        if required:
            review_required = True
            validation_status: Literal["passed", "failed", "not_applicable"] = "failed"
            # The absence is recorded explicitly (the rest of the extraction is kept) and the
            # flag keeps the document out of automatic approval.
            flags.append(FlagItem(
                code=REQUIRED_FIELD_MISSING,
                message=f"Required field '{field_name}' was not found in the document.",
                fields=[field_name],
            ))
        elif null_reason == "illegible":
            review_required = True
            validation_status = "failed"
        else:
            review_required = False
            validation_status = "not_applicable"

        # No value means no score to report: confidence is unavailable (not 0.0, not invented).
        return ExtractedField(
            value=None,
            confidence=None,
            null_reason=null_reason,
            review_required=review_required,
            validation=validation_status,
            evidence=None,
        )

    # 2. Evidence Grounding (FR-EXT-004, GR-002)
    evidence_dict = evidence_hint or find_evidence_for_value(value, pages, field_name=field_name)
    evidence_model: Optional[EvidenceSnippet] = None
    optical_conf: Optional[float] = None
    evidence_uncertain = False
    additional_evidence: List[EvidenceSnippet] = []

    if evidence_dict:
        evidence_model = EvidenceSnippet(
            page=evidence_dict["page"],
            bbox=tuple(evidence_dict["bbox"]),
            text=evidence_dict["text"],
        )
        optical_conf = evidence_dict.get("ocr_confidence")
        evidence_uncertain = bool(evidence_dict.get("uncertain"))
        # Page-specific evidence from other pages (same value repeated, or conflicting readings)
        additional_evidence = [
            EvidenceSnippet(page=h["page"], bbox=tuple(h["bbox"]), text=h["text"])
            for h in (evidence_dict.get("additional") or [])
        ]
    elif evidence_required and (not isinstance(value, bool) or strict_boolean_evidence):
        # GR-002: Non-grounded non-null values must not be emitted.
        # Raise EVIDENCE_MISSING flag and force value to null requiring review.
        flags.append(FlagItem(
            code="EVIDENCE_MISSING",
            message=f"No grounded visual evidence found in document for field '{field_name}'.",
            fields=[field_name],
        ))
        return ExtractedField(
            value=None,
            confidence=None,
            null_reason="absent",
            review_required=True,
            validation="failed",
            evidence=None,
        )

    # 3. Validation Status
    has_validation_failed = (error is not None)
    validation_status = "failed" if has_validation_failed else "passed"

    # 4. Calibrated Confidence (FR-CNF-001): measured signals only; None when unavailable
    confidence, confidence_provenance = calibrate_field_confidence(
        ocr_confidence=optical_conf,
        model_confidence=model_confidence,
        validation_passed=not has_validation_failed,
        has_evidence=(evidence_model is not None),
    )

    # 5. Threshold & Review Flagging (FR-CNF-002, GR-009)
    # Review required if validation failed, confidence is unavailable or below the field's
    # threshold, the extraction state is uncertain, or a required value is missing.
    review_required = (
        has_validation_failed
        or confidence is None
        or (confidence < confidence_threshold)
        or (required and value is None)
        or evidence_uncertain
    )

    return ExtractedField(
        value=value,
        confidence=confidence,
        confidence_provenance=confidence_provenance,
        null_reason=None,
        review_required=review_required,
        validation=validation_status,
        evidence=evidence_model,
        additional_evidence=additional_evidence,
    )


def assemble_canonical_envelope(
    document_id: str,
    document_type: str,
    schema_id: str,
    schema_version: Union[str, int],
    classification_confidence: float,
    page_count: int,
    fields: Dict[str, ExtractedField],
    flags: List[FlagItem],
    run_id: str,
    parser_model_name: str,
    extractor_model_name: str,
    prompt_version: str = "1.0.0",
    provenance: Optional[ExecutionProvenance] = None,
    payload_status: str = "processing",
) -> CanonicalExtractionEnvelope:
    """
    Applies document-level status gating (FR-CNF-003, GR-027) and constructs
    the CanonicalExtractionEnvelope adhering strictly to PRD §6.3 (SCH-011..013).

    A document is auto-approved only if none of the following holds (SCH-012 ``needs_review``):
    - the document type is UNKNOWN, or the upstream payload already requires review;
    - no field carries a value at all (nothing was extracted);
    - any flag was raised (EVIDENCE_MISSING, PARTIAL_PAGE_COVERAGE, rule failures, ...);
    - any field requires review or failed validation (missing required value, failed business
      rule, uncertain extraction state, confidence below the field threshold);
    - any extracted value lacks an authorizing confidence: unavailable (None) or merely
      uncalibrated/nominal scores cannot authorize automatic approval.
    """
    unidentified = document_type == "UNKNOWN"
    # A document from which nothing was extracted has nothing to approve.
    nothing_extracted = not any(f.value is not None for f in fields.values())
    confidence_unavailable = any(
        f.value is not None
        and (f.confidence is None or f.confidence_provenance in NON_AUTHORIZING_PROVENANCE)
        for f in fields.values()
    )
    requires_review = (
        unidentified
        or nothing_extracted
        or payload_status == "needs_review"
        or len(flags) > 0
        or confidence_unavailable
        or any(f.review_required or f.validation == "failed" for f in fields.values())
    )

    if payload_status == "failed":
        final_status: Literal["processing", "auto_approved", "needs_review", "reviewed", "rejected", "failed"] = "failed"
    else:
        final_status = "needs_review" if requires_review else "auto_approved"

    return CanonicalExtractionEnvelope(
        document_id=document_id,
        document_metadata=DocumentMetadata(
            document_type=document_type,
            schema_id=schema_id,
            schema_version=schema_version,
            classification_confidence=classification_confidence,
            status=final_status,
            page_count=page_count,
        ),
        fields=fields,
        flags=flags,
        run=RunMetadata(
            run_id=run_id,
            model_versions={
                "parser": parser_model_name,
                "extractor": extractor_model_name,
            },
            prompt_version=prompt_version,
            provenance=provenance,
        ),
    )
