"""
Module C Canonical Envelope and Pipeline Orchestrator — 1-Stage Multimodal.
Adheres strictly to PRD §6.3 Canonical Output Envelope, FR-EXT-001..007,
FR-VAL-001..004, FR-CNF-001..003, GR-002, GR-003, GR-027.
"""

from contextlib import contextmanager
import threading
from typing import Any, Dict, Iterator, List, Literal, Optional, Sequence, Tuple, Type, Union
import re
from pydantic import BaseModel

from doc_extractor.contracts_b import ModuleBOutputPayload
from doc_extractor.contracts_c import (
    CanonicalExtractionEnvelope,
    CanonicalOutputEnvelope,
    DocumentMetadata,
    EvidenceSnippet,
    EvidenceModel,
    ExecutionMode,
    ExecutionProvenance,
    ExtractedField,
    ExtractionResult,
    FieldOutput,
    FlagItem,
    PageImageRef,
    RunMetadata,
)
from doc_extractor.vlm_grounding import conflict_flags, grounding_to_evidence_hints
from doc_extractor.extractor import SchemaGuidedExtractor, SCHEMA_MAP, find_evidence_for_value
from doc_extractor.field_assembly import (
    assemble_canonical_envelope,
    assemble_field_entry,
)
from doc_extractor.handoff import CoordinateTransform, PageHandoffRecord
from doc_extractor.validation_rules import (
    normalize_and_format,
    normalize_date_to_iso,
    normalize_phone_number,
    normalize_currency_amount,
    validate_pan,
    validate_aadhaar,
    validate_ifsc,
    validate_chronology,
    validate_arithmetic,
    calculate_calibrated_confidence,
)


class ModuleCPipeline:
    """
    Module C Pipeline Orchestrator.
    Takes ModuleBOutputPayload, extracts structured fields according to schema,
    performs evidence grounding (FR-EXT-004, GR-002), normalizes and validates values (FR-VAL-001..004),
    calculates calibrated confidences (FR-CNF-001), enforces null-if-absent (FR-EXT-003, GR-003),
    applies document-level status gating (FR-CNF-003, GR-027), and emits CanonicalExtractionEnvelope (SCH-011..013).
    """

    def __init__(
        self,
        extractor: Optional[SchemaGuidedExtractor] = None,
        settings: Optional[Any] = None,
        fallback_extractor: Optional[SchemaGuidedExtractor] = None,
    ):
        """
        ``extractor`` is the primary extractor: multimodal (Qwen-VL, reads Module A page
        images) or text (Ollama/litellm/heuristic, reads Module B markdown) behind the same
        ``extract_with_provenance`` interface. ``fallback_extractor`` is tried when the primary
        raises (FR-RTE-003); the result's provenance then records the fallback.
        """
        if extractor is not None:
            self.extractor = extractor
        elif settings is not None:
            self.extractor = SchemaGuidedExtractor.from_settings(settings)
        else:
            self.extractor = SchemaGuidedExtractor()
        self.settings = settings
        self.fallback_extractor = fallback_extractor
        self._evidence_ctx = threading.local()

    @classmethod
    def with_local_llm(
        cls,
        model_name: str = "ollama/qwen2.5:7b-instruct",
        api_base: str = "http://localhost:11434",
        use_llm: bool = True,
        allow_fallback: bool = False,
        timeout_seconds: float = 30.0,
        prompt_version: str = "1.0.0",
    ) -> "ModuleCPipeline":
        """Factory creating a ModuleCPipeline configured for local Ollama Qwen2.5-7B-Instruct."""
        extractor = SchemaGuidedExtractor.for_local_ollama(
            model_name=model_name,
            api_base=api_base,
            use_llm=use_llm,
            allow_fallback=allow_fallback,
            timeout_seconds=timeout_seconds,
            prompt_version=prompt_version,
        )
        return cls(extractor=extractor)

    @contextmanager
    def _evidence_hint_scope(self, hints: Dict[str, Dict[str, Any]]) -> Iterator[None]:
        """Makes per-field grounded evidence available to _create_field_entry for one process() call."""
        previous = getattr(self._evidence_ctx, "hints", None)
        self._evidence_ctx.hints = hints
        try:
            yield
        finally:
            self._evidence_ctx.hints = previous

    @staticmethod
    def _invoke_extractor(
        extractor: Any,
        payload: ModuleBOutputPayload,
        schema_cls: Type[BaseModel],
        page_images: Optional[Sequence[PageImageRef]],
    ) -> Any:
        """Calls one extractor behind the shared interface (page_images only passed when present)."""
        kwargs: Dict[str, Any] = {"schema_cls": schema_cls}
        if page_images:
            kwargs["page_images"] = page_images
        if hasattr(extractor, "extract_with_provenance"):
            return extractor.extract_with_provenance(payload, **kwargs)
        # Legacy extractors exposing only extract(): wrap with whatever provenance they recorded.
        data = extractor.extract(payload, **kwargs)
        return ExtractionResult(
            payload=data,
            provenance=getattr(extractor, "last_provenance", None)
            or ExecutionProvenance(
                mode=ExecutionMode.HEURISTIC,
                model_name=getattr(extractor, "model_name", "unknown"),
                provider="local_extractor",
                prompt_version=getattr(extractor, "prompt_version", "1.0.0"),
                fallback_occurred=False,
            ),
        )

    def run_extraction(
        self,
        payload: ModuleBOutputPayload,
        schema_cls: Type[BaseModel],
        page_images: Optional[Sequence[PageImageRef]] = None,
    ) -> Any:
        """
        The single extraction stage of Module C (shared by static and dynamic schemas).

        Runs the primary extractor (multimodal VLM on the explicit Module A page images, or a
        text/heuristic extractor on Module B markdown). If it raises and a fallback extractor is
        configured, the fallback runs and its provenance records ``fallback_occurred`` (so it is
        never trusted as a clean benchmark run). Without a fallback the original error propagates.
        """
        return self._run_with_fallback(
            lambda extractor: self._invoke_extractor(extractor, payload, schema_cls, page_images),
            lambda result: getattr(result, "provenance", None),
        )

    def _run_with_fallback(self, call: Any, provenance_of: Any) -> Any:
        """Runs ``call(primary)``; on failure runs ``call(fallback)`` and records it in provenance."""
        try:
            return call(self.extractor)
        except Exception as exc:
            if self.fallback_extractor is None or self.fallback_extractor is self.extractor:
                raise
            result = call(self.fallback_extractor)
            provenance = provenance_of(result)
            if provenance is not None:
                provenance.fallback_occurred = True
                provenance.fallback_reason = (
                    f"primary extractor {getattr(self.extractor, 'model_name', 'unknown')} failed: {exc}"
                )
            return result

    def process_generic(
        self,
        payload: ModuleBOutputPayload,
        page_images: Sequence[PageImageRef],
        system_prompt: str,
        document_type: str = "generic",
        force_review: bool = False,
        expected_fields: Optional[Sequence[str]] = None,
    ) -> CanonicalExtractionEnvelope:
        """
        Canonical path for documents without a bound schema (unstructured / handwritten categories).

        The multimodal extractor reads every explicit Module A page image (document order) with a
        category prompt; values are normalized and aggregated across pages, boxes are mapped to
        200-DPI pixels with each page's own CoordinateTransform, and fields/status/confidence come
        from the same ``assemble_field_entry`` / ``assemble_canonical_envelope`` components as
        schema-bound documents. ``force_review`` marks every field for human review (e.g.
        handwriting). Pages that fail or have no image reference are flagged, never dropped silently.

        Completeness: there is no schema here, so ``expected_fields`` (optional dotted paths the
        caller expects) plays the role of required fields - each one no page produced becomes a
        failed, review-required field plus a REQUIRED_FIELD_MISSING flag, while everything that
        WAS extracted is kept. An extraction that yields no values at all is flagged
        NO_FIELDS_EXTRACTED instead of being approved as an empty document.
        """
        selected = self._unique_pages(page_images)
        if not selected:
            raise ValueError("Generic multimodal extraction requires a Module A page image reference.")

        def call(extractor: Any) -> Any:
            if not hasattr(extractor, "extract_generic_pages"):
                raise ValueError("Configured extractor is not multimodal (no extract_generic_pages).")
            return extractor.extract_generic_pages(selected, system_prompt)

        result = self._run_with_fallback(call, lambda r: r.provenance)
        provenance = result.provenance
        values, grounding = result.values, result.grounding
        hints = grounding_to_evidence_hints(grounding, selected)

        flags: List[FlagItem] = []
        fields: Dict[str, ExtractedField] = {}
        for path, value in values.items():
            hint = hints.get(path)
            if hint is None and not isinstance(value, (str, int, float, bool)):
                # Non-scalar value without a model-supplied box cannot be matched to OCR text.
                flags.append(FlagItem(
                    code="EVIDENCE_MISSING",
                    message=f"No grounded visual evidence found in document for field '{path}'.",
                    fields=[path],
                ))
                fields[path] = ExtractedField(
                    value=None, confidence=None, null_reason="absent",
                    review_required=True, validation="failed", evidence=None,
                )
                continue
            fields[path] = assemble_field_entry(
                field_name=path,
                value=value,
                pages=payload.pages,
                flags=flags,
                evidence_hint=hint,
            )
            if force_review:
                fields[path].review_required = True

        for path in expected_fields or []:
            if path not in values and path not in fields:
                fields[path] = assemble_field_entry(
                    field_name=path, value=None, pages=payload.pages, flags=flags, required=True,
                )
        if not values and not expected_fields:
            flags.append(FlagItem(
                code="NO_FIELDS_EXTRACTED",
                message="No values were extracted from any processed page.",
                fields=[],
            ))

        flags.extend(conflict_flags(grounding))
        coverage_flag = self.partial_coverage_flag(
            provenance,
            [p.page_number for p in payload.pages],
            [r.page_number for r in selected],
        )
        if coverage_flag:
            flags.append(coverage_flag)

        return assemble_canonical_envelope(
            document_id=payload.document_id,
            document_type=document_type,
            schema_id=document_type,
            schema_version="1.0.0",
            classification_confidence=payload.classification.classification_confidence,
            page_count=len(payload.pages),
            fields=fields,
            flags=flags,
            run_id=payload.run_id,
            parser_model_name=payload.parser_model_name,
            extractor_model_name=getattr(self.extractor, "model_name", "unknown"),
            prompt_version=getattr(self.extractor, "prompt_version", "1.0.0"),
            provenance=provenance,
            payload_status="processing",
        )

    @staticmethod
    def partial_coverage_flag(
        provenance: Any,
        expected_pages: Union[int, Sequence[int]],
        supplied_pages: Optional[Sequence[int]] = None,
    ) -> Optional[FlagItem]:
        """
        PARTIAL_PAGE_COVERAGE when an image-based extraction did not process every page of the
        document: a page whose VLM call/output failed, or whose image reference was never
        supplied (``supplied_pages``), is recorded in the provenance (``pages_failed``) and named
        in the flag. A page that was processed but had no values is *not* a failure. Returns None
        when every expected page was processed or the extraction was not image-based (text/OCR
        extraction reads all pages from Module B markdown).
        """
        details = getattr(provenance, "details", None)
        processed = details.get("pages_processed") if details is not None else None
        if processed is None:
            return None

        expected = (
            list(range(1, expected_pages + 1)) if isinstance(expected_pages, int)
            else sorted(set(expected_pages))
        )
        failed = list(details.get("pages_failed") or [])
        reasons = {entry["page"]: entry["reason"] for entry in failed}

        if supplied_pages is not None:
            for number in expected:
                if number not in supplied_pages and number not in reasons:
                    reasons[number] = "no page image reference supplied"
                    failed.append({"page": number, "reason": reasons[number]})
            details["pages_failed"] = failed

        unprocessed = [n for n in expected if n not in processed]
        if not unprocessed:
            return None
        detail = ", ".join(f"page {n} ({reasons.get(n, 'not attempted')})" for n in unprocessed)
        return FlagItem(
            code="PARTIAL_PAGE_COVERAGE",
            message=(
                f"Multimodal extraction processed page(s) {sorted(processed)} of {len(expected)}; "
                f"not processed: {detail}."
            ),
            fields=[],
        )

    @staticmethod
    def _unique_pages(page_images: Optional[Sequence[PageImageRef]]) -> List[PageImageRef]:
        """One reference per page number, in original document order."""
        unique: Dict[int, PageImageRef] = {}
        for ref in page_images or []:
            unique.setdefault(ref.page_number, ref)
        return [unique[n] for n in sorted(unique)]

    def _select_page_images(
        self,
        doc_type: str,
        page_images: Optional[Sequence[PageImageRef]],
    ) -> Optional[List[PageImageRef]]:
        """
        Chooses the explicit page images sent to a multimodal extractor: every supplied page, one
        reference per page number, in document order. Returns None when no images were supplied.
        """
        if not page_images:
            return None
        return self._unique_pages(page_images)

    def process(
        self,
        payload: ModuleBOutputPayload,
        page_images: Optional[Sequence[PageImageRef]] = None,
    ) -> CanonicalExtractionEnvelope:
        # Route UNKNOWN or low-confidence classification directly to needs_review (FR-CLS-002)
        if payload.status == "needs_review" or payload.classification.document_type.value == "UNKNOWN":
            return CanonicalExtractionEnvelope(
                document_id=payload.document_id,
                document_metadata=DocumentMetadata(
                    document_type=payload.classification.document_type.value,
                    schema_id=payload.classification.schema_id or "UNKNOWN",
                    schema_version=payload.classification.schema_version or "1.0.0",
                    classification_confidence=payload.classification.classification_confidence,
                    status="needs_review",
                    page_count=len(payload.pages),
                ),
                fields={},
                flags=[FlagItem(code="CLASSIFICATION_UNCERTAIN", message="Document routed to review prior to extraction", fields=[])],
                run=RunMetadata(
                    run_id=payload.run_id,
                    model_versions={"parser": payload.parser_model_name, "extractor": self.extractor.model_name},
                    prompt_version="1.0.0",
                ),
            )

        doc_type = payload.classification.document_type.value
        schema_cls = SCHEMA_MAP.get(doc_type)

        if not schema_cls:
            return CanonicalExtractionEnvelope(
                document_id=payload.document_id,
                document_metadata=DocumentMetadata(
                    document_type=doc_type,
                    schema_id=payload.classification.schema_id or "UNKNOWN",
                    schema_version=payload.classification.schema_version or "1.0.0",
                    classification_confidence=payload.classification.classification_confidence,
                    status="needs_review",
                    page_count=len(payload.pages),
                ),
                fields={},
                flags=[FlagItem(code="NO_SCHEMA_BOUND", message=f"No schema registered for document type {doc_type}", fields=[])],
                run=RunMetadata(
                    run_id=payload.run_id,
                    model_versions={"parser": payload.parser_model_name, "extractor": self.extractor.model_name},
                    prompt_version=self.extractor.prompt_version,
                ),
            )

        # Extract structured data via the single extraction stage (multimodal VLM on the explicit
        # Module A page images, or text/heuristic extraction on Module B markdown).
        selected_images = self._select_page_images(doc_type, page_images)
        extraction_result = self.run_extraction(payload, schema_cls, selected_images)
        extracted_data = extraction_result.payload
        execution_provenance = extraction_result.provenance

        # Model-grounded evidence: normalized 0-1000 boxes -> 200-DPI original pixels via
        # CoordinateTransform (inverts Module A deskew/rotation). Fields without a box fall back
        # to OCR grounding inside assemble_field_entry, then to EVIDENCE_MISSING (GR-002).
        evidence_hints = grounding_to_evidence_hints(
            getattr(extraction_result, "grounding", None) or {}, selected_images or []
        )

        if execution_provenance is None:
            execution_provenance = ExecutionProvenance(
                mode=ExecutionMode.HEURISTIC,
                model_name=self.extractor.model_name,
                provider="local_extractor",
                prompt_version=self.extractor.prompt_version,
                fallback_occurred=False,
            )

        fields_envelope: Dict[str, ExtractedField] = {}
        flags: List[FlagItem] = []

        with self._evidence_hint_scope(evidence_hints):
            if doc_type == "DOC-01":
                self._process_doc_01(extracted_data, payload, fields_envelope, flags)
            elif doc_type == "DOC-02":
                self._process_doc_02(extracted_data, payload, fields_envelope, flags)
            elif doc_type == "DOC-03":
                self._process_doc_03(extracted_data, payload, fields_envelope, flags)

        flags.extend(conflict_flags(getattr(extraction_result, "grounding", None) or {}))
        coverage_flag = self.partial_coverage_flag(
            execution_provenance,
            [p.page_number for p in payload.pages],
            [r.page_number for r in selected_images] if selected_images else None,
        )
        if coverage_flag:
            flags.append(coverage_flag)

        return assemble_canonical_envelope(
            document_id=payload.document_id,
            document_type=payload.classification.document_type.value,
            schema_id=payload.classification.schema_id or doc_type,
            schema_version=payload.classification.schema_version or "1.0.0",
            classification_confidence=payload.classification.classification_confidence,
            page_count=len(payload.pages),
            fields=fields_envelope,
            flags=flags,
            run_id=payload.run_id,
            parser_model_name=payload.parser_model_name,
            extractor_model_name=getattr(self.extractor, "model_name", "unknown"),
            prompt_version=self.extractor.prompt_version,
            provenance=execution_provenance,
            payload_status=payload.status,
        )

    def _process_doc_01(
        self,
        extracted_data: Any,
        payload: ModuleBOutputPayload,
        fields_envelope: Dict[str, ExtractedField],
        flags: List[FlagItem],
    ) -> None:
        """Processes DOC-01 CASA Account Opening Form fields."""
        app = getattr(extracted_data, "applicant", None)
        acc = getattr(extracted_data, "account_details", None)

        # applicant.full_name (Required)
        name_val = getattr(app, "full_name", None) if app else None
        fields_envelope["applicant.full_name"] = self._create_field_entry(
            "applicant.full_name", name_val, payload, flags, required=True
        )

        # applicant.pan_card_number (Optional, format check FR-VAL-002)
        pan_val = getattr(app, "pan_card_number", None) if app else None
        norm_pan, pan_err = validate_pan(pan_val)
        fields_envelope["applicant.pan_card_number"] = self._create_field_entry(
            "applicant.pan_card_number", norm_pan, payload, flags, error=pan_err, required=False
        )

        # applicant.date_of_birth (Optional, ISO normalizer FR-VAL-001)
        dob_val = getattr(app, "date_of_birth", None) if app else None
        norm_dob, dob_err = normalize_date_to_iso(dob_val)
        fields_envelope["applicant.date_of_birth"] = self._create_field_entry(
            "applicant.date_of_birth", norm_dob, payload, flags, error=dob_err, required=False
        )

        # applicant.phone_number (Optional, E.164 normalizer FR-VAL-001)
        phone_val = getattr(app, "phone_number", None) if app else None
        norm_phone, phone_err = normalize_phone_number(phone_val)
        fields_envelope["applicant.phone_number"] = self._create_field_entry(
            "applicant.phone_number", norm_phone, payload, flags, error=phone_err, required=False
        )

        # account_details.account_type (Optional)
        acc_type = getattr(acc, "account_type", None) if acc else None
        fields_envelope["account_details.account_type"] = self._create_field_entry(
            "account_details.account_type", acc_type, payload, flags, required=False
        )

        # account_details.initial_deposit_amount (Optional, decimal currency normalizer)
        dep_val = getattr(acc, "initial_deposit_amount", None) if acc else None
        norm_amt, curr, amt_err = normalize_currency_amount(dep_val)
        fields_envelope["account_details.initial_deposit_amount"] = self._create_field_entry(
            "account_details.initial_deposit_amount", norm_amt, payload, flags, error=amt_err, required=False
        )

        # account_details.currency: derived from the deposit amount, so it can claim no more than
        # the amount field itself (same evidence, confidence, provenance and review state).
        # Without an amount there is nothing to derive a currency from (none is assumed).
        amount_entry = fields_envelope["account_details.initial_deposit_amount"]
        if amount_entry.value is None:
            fields_envelope["account_details.currency"] = ExtractedField(
                value=None,
                confidence=None,
                null_reason="absent",
                review_required=False,
                validation="not_applicable",
                evidence=None,
            )
        else:
            fields_envelope["account_details.currency"] = ExtractedField(
                value=curr,
                confidence=amount_entry.confidence,
                confidence_provenance=amount_entry.confidence_provenance,
                null_reason=None,
                review_required=amount_entry.review_required,
                validation=amount_entry.validation,
                evidence=amount_entry.evidence,
            )

        # Checkboxes (FR-EXT-007)
        opt_in = getattr(acc, "opt_in_services", None) if acc else None
        chq_val = getattr(opt_in, "cheque_book", None) if opt_in else None
        fields_envelope["account_details.opt_in_services.cheque_book"] = self._create_field_entry(
            "account_details.opt_in_services.cheque_book", chq_val, payload, flags, required=False
        )

        deb_val = getattr(opt_in, "debit_card", None) if opt_in else None
        fields_envelope["account_details.opt_in_services.debit_card"] = self._create_field_entry(
            "account_details.opt_in_services.debit_card", deb_val, payload, flags, required=False
        )

        net_val = getattr(opt_in, "internet_banking", None) if opt_in else None
        fields_envelope["account_details.opt_in_services.internet_banking"] = self._create_field_entry(
            "account_details.opt_in_services.internet_banking", net_val, payload, flags, required=False
        )

    def _process_doc_02(
        self,
        extracted_data: Any,
        payload: ModuleBOutputPayload,
        fields_envelope: Dict[str, ExtractedField],
        flags: List[FlagItem],
    ) -> None:
        """Processes DOC-02 Insurance Application Form fields."""
        p_name = getattr(extracted_data, "proposer_name", None)
        fields_envelope["proposer_name"] = self._create_field_entry(
            "proposer_name", p_name, payload, flags, required=True
        )

        sum_ins = getattr(extracted_data, "sum_insured", None)
        norm_sum, curr, sum_err = normalize_currency_amount(sum_ins)
        fields_envelope["sum_insured"] = self._create_field_entry(
            "sum_insured", norm_sum, payload, flags, error=sum_err, required=False
        )

        p_term = getattr(extracted_data, "policy_term_years", None)
        fields_envelope["policy_term_years"] = self._create_field_entry(
            "policy_term_years", p_term, payload, flags, required=False
        )

        # Checkbox medical declarations (FR-EXT-007)
        med = getattr(extracted_data, "medical_declaration", None)
        diab = getattr(med, "has_diabetes", None) if med else None
        fields_envelope["medical_declaration.has_diabetes"] = self._create_field_entry(
            "medical_declaration.has_diabetes", diab, payload, flags, required=False
        )

        hyp = getattr(med, "has_hypertension", None) if med else None
        fields_envelope["medical_declaration.has_hypertension"] = self._create_field_entry(
            "medical_declaration.has_hypertension", hyp, payload, flags, required=False
        )

        tob = getattr(med, "has_tobacco_use", None) if med else None
        fields_envelope["medical_declaration.has_tobacco_use"] = self._create_field_entry(
            "medical_declaration.has_tobacco_use", tob, payload, flags, required=False
        )

        # Repeating groups: Nominees (FR-EXT-005)
        nominees = getattr(extracted_data, "nominees", []) or []
        for idx, nom in enumerate(nominees):
            nom_dict = nom.model_dump() if hasattr(nom, "model_dump") else (nom if isinstance(nom, dict) else {})
            fields_envelope[f"nominees.{idx}.name"] = self._create_field_entry(
                f"nominees.{idx}.name", nom_dict.get("name"), payload, flags, required=False
            )
            fields_envelope[f"nominees.{idx}.relationship"] = self._create_field_entry(
                f"nominees.{idx}.relationship", nom_dict.get("relationship"), payload, flags, required=False
            )
            fields_envelope[f"nominees.{idx}.share_percentage"] = self._create_field_entry(
                f"nominees.{idx}.share_percentage", nom_dict.get("share_percentage"), payload, flags, required=False
            )

        # Check nominee share percentage sum if nominees present
        shares = [
            float(n.get("share_percentage", 0) if isinstance(n, dict) else getattr(n, "share_percentage", 0))
            for n in nominees
            if (n.get("share_percentage") if isinstance(n, dict) else getattr(n, "share_percentage", None)) is not None
        ]
        if shares and len(shares) > 1:
            arith_flag = validate_arithmetic(shares, 100.0)
            if arith_flag:
                flags.append(FlagItem(code="ARITHMETIC_MISMATCH", message="Nominee share percentages do not sum to 100%", fields=["nominees.share_percentage"]))

    def _process_doc_03(
        self,
        extracted_data: Any,
        payload: ModuleBOutputPayload,
        fields_envelope: Dict[str, ExtractedField],
        flags: List[FlagItem],
    ) -> None:
        """Processes DOC-03 Hospital Discharge Summary fields."""
        pat_name = getattr(extracted_data, "patient_name", None)
        fields_envelope["patient_name"] = self._create_field_entry(
            "patient_name", pat_name, payload, flags, required=True
        )

        ip_no = getattr(extracted_data, "ip_number", None)
        fields_envelope["ip_number"] = self._create_field_entry(
            "ip_number", ip_no, payload, flags, required=False
        )

        adm_date = getattr(extracted_data, "admission_date", None)
        norm_adm, adm_err = normalize_date_to_iso(adm_date)
        fields_envelope["admission_date"] = self._create_field_entry(
            "admission_date", norm_adm, payload, flags, error=adm_err, required=False
        )

        dis_date = getattr(extracted_data, "discharge_date", None)
        norm_dis, dis_err = normalize_date_to_iso(dis_date)
        fields_envelope["discharge_date"] = self._create_field_entry(
            "discharge_date", norm_dis, payload, flags, error=dis_err, required=False
        )

        diag = getattr(extracted_data, "diagnosis", None)
        fields_envelope["diagnosis"] = self._create_field_entry(
            "diagnosis", diag, payload, flags, required=False
        )

        # Chronology & Cross-Field Check (FR-VAL-004)
        chronology_flag = validate_chronology(norm_adm, norm_dis)
        if chronology_flag:
            flags.append(FlagItem(**chronology_flag))
            if "admission_date" in fields_envelope:
                fields_envelope["admission_date"].review_required = True
            if "discharge_date" in fields_envelope:
                fields_envelope["discharge_date"].review_required = True

        # Repeating group: Medications (FR-EXT-005)
        meds = getattr(extracted_data, "medications", []) or []
        for idx, m in enumerate(meds):
            m_dict = m.model_dump() if hasattr(m, "model_dump") else (m if isinstance(m, dict) else {})
            fields_envelope[f"medications.{idx}.medicine_name"] = self._create_field_entry(
                f"medications.{idx}.medicine_name", m_dict.get("medicine_name"), payload, flags, required=False
            )
            fields_envelope[f"medications.{idx}.dosage"] = self._create_field_entry(
                f"medications.{idx}.dosage", m_dict.get("dosage"), payload, flags, required=False
            )
            fields_envelope[f"medications.{idx}.frequency"] = self._create_field_entry(
                f"medications.{idx}.frequency", m_dict.get("frequency"), payload, flags, required=False
            )
            fields_envelope[f"medications.{idx}.duration"] = self._create_field_entry(
                f"medications.{idx}.duration", m_dict.get("duration"), payload, flags, required=False
            )

    def _create_field_entry(
        self,
        field_name: str,
        value: Any,
        payload: ModuleBOutputPayload,
        flags: List[FlagItem],
        error: Optional[str] = None,
        required: bool = False,
    ) -> ExtractedField:
        """
        Helper to create a validated ExtractedField with evidence grounding,
        null-if-absent logic (FR-EXT-003, GR-003), and calibrated confidence (FR-CNF-001).
        Delegates to shared assemble_field_entry service, passing model-grounded evidence
        for this field when the multimodal extractor supplied one.
        """
        hints = getattr(self._evidence_ctx, "hints", None) or {}
        return assemble_field_entry(
            field_name=field_name,
            value=value,
            pages=payload.pages,
            flags=flags,
            error=error,
            required=required,
            strict_boolean_evidence=False,  # preserve existing static pipeline compatibility
            evidence_hint=hints.get(field_name),
        )
