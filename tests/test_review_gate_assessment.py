"""
Tests for ``field_assembly.assess_review_requirement``: the document-level review gate (FR-CNF-003, GR-027), extracted from
``assemble_canonical_envelope`` so that the assembler and any other caller share ONE implementation.

Verified: each clause of the gate on its own; that the assembler's ``needs_review`` / ``auto_approved`` status is exactly the
gate's verdict, over an exhaustive grid compared with the ORIGINAL inline expression (pinned below as a transcription of the code
before the extraction); that the assessment is pure (the inputs are not modified) and holds codes only. Synthetic data only.
"""

import copy
import itertools

import pytest

from doc_extractor.contracts_b import ConfidenceProvenance
from doc_extractor.contracts_c import EvidenceSnippet, ExtractedField, FlagItem
from doc_extractor.field_assembly import (
    NON_AUTHORIZING_PROVENANCE,
    ReviewAssessment,
    ReviewReason,
    assemble_canonical_envelope,
    assess_review_requirement,
)

OCR = ConfidenceProvenance.OCR


def _field(value="x", confidence=0.9, provenance=OCR, review=False, validation="passed"):
    kwargs = {"value": value, "confidence": confidence, "review_required": review, "validation": validation}
    if confidence is not None:
        kwargs["confidence_provenance"] = provenance
    if value is not None:
        kwargs["evidence"] = EvidenceSnippet(page=1, bbox=(0.0, 0.0, 1.0, 1.0), text="evidence")
    return ExtractedField(**kwargs)


def _assess(fields, flags=(), document_type="DOC-01", payload_status="processing"):
    return assess_review_requirement(document_type, fields, list(flags), payload_status)


def _assemble(fields, flags=(), document_type="DOC-01", payload_status="processing"):
    return assemble_canonical_envelope(
        document_id="doc", document_type=document_type, schema_id=document_type, schema_version="1.0.0",
        classification_confidence=0.9, page_count=1, fields=fields, flags=list(flags), run_id="run",
        parser_model_name="parser", extractor_model_name="extractor", payload_status=payload_status,
    )


def test_a_clean_document_has_no_reason():
    result = _assess({"a": _field(), "b": _field()})
    assert result == ReviewAssessment() and not result.requires_review
    assert result.document_reasons == () and result.field_reasons == ()


@pytest.mark.parametrize("kwargs, reason", [
    ({"document_type": "UNKNOWN"}, ReviewReason.DOCUMENT_TYPE_UNKNOWN),
    ({"payload_status": "needs_review"}, ReviewReason.UPSTREAM_NEEDS_REVIEW),
    ({"flags": [FlagItem(code="ANY_FLAG")]}, ReviewReason.FLAGS_RAISED),
])
def test_each_document_level_clause_is_a_review_reason(kwargs, reason):
    result = _assess({"a": _field()}, **kwargs)
    assert result.requires_review and result.document_reasons == (reason,) and result.field_reasons == ()


def test_a_document_with_nothing_extracted_has_nothing_to_approve():
    for fields in ({}, {"a": _field(value=None, confidence=None, validation="not_applicable")}):
        result = _assess(fields)
        assert result.requires_review and result.document_reasons == (ReviewReason.NOTHING_EXTRACTED,)


@pytest.mark.parametrize("field, reasons", [
    (_field(review=True), (ReviewReason.FIELD_REVIEW_REQUIRED,)),
    (_field(validation="failed"), (ReviewReason.FIELD_VALIDATION_FAILED,)),
    (_field(review=True, validation="failed"), (ReviewReason.FIELD_REVIEW_REQUIRED, ReviewReason.FIELD_VALIDATION_FAILED)),
    (_field(confidence=None), (ReviewReason.CONFIDENCE_NOT_AUTHORIZING,)),                            # a value without a score
    (_field(provenance=ConfidenceProvenance.UNCALIBRATED), (ReviewReason.CONFIDENCE_NOT_AUTHORIZING,)),   # a nominal score
])
def test_each_field_level_clause_flags_exactly_that_field(field, reasons):
    result = _assess({"ok": _field(), "bad": field})
    assert result.requires_review and result.document_reasons == ()
    assert result.field_reasons == (("bad", reasons),)


def test_authorizing_provenances_do_not_flag_a_field():
    for provenance in (ConfidenceProvenance.OCR, ConfidenceProvenance.MODEL, ConfidenceProvenance.RULE_HEURISTIC,
                       ConfidenceProvenance.HUMAN_OVERRIDE):
        assert provenance not in NON_AUTHORIZING_PROVENANCE
        assert not _assess({"a": _field(provenance=provenance)}).requires_review


def test_an_empty_valued_field_without_a_score_is_not_flagged_by_the_confidence_clause():
    result = _assess({"a": _field(), "b": _field(value=None, confidence=None, validation="not_applicable")})
    assert not result.requires_review


def test_field_reasons_follow_the_order_of_the_fields():
    fields = {"z": _field(review=True), "a": _field(), "m": _field(validation="failed")}
    assert [key for key, _ in _assess(fields).field_reasons] == ["z", "m"]


def test_the_assessment_is_pure_and_holds_codes_only():
    fields = {"a": _field(value="SYNTH-SECRET-731", review=True)}
    flags = [FlagItem(code="C", message="quotes SYNTH-SECRET-731", fields=["a"])]
    before = ([f.model_dump() for f in fields.values()], [f.model_dump() for f in flags])
    result = _assess(fields, flags)
    assert ([f.model_dump() for f in fields.values()], [f.model_dump() for f in flags]) == before
    assert "SYNTH-SECRET-731" not in repr(result) and _assess(fields, flags) == result
    with pytest.raises(Exception):
        result.document_reasons = ()                                                       # frozen


# ---- the assembler uses the gate, and its behavior is unchanged ------------------------------------------------------------

def _original_inline_gate(document_type, fields, flags, payload_status):
    """TRANSCRIPTION of the expression ``assemble_canonical_envelope`` contained before the gate was extracted from it."""
    unidentified = document_type == "UNKNOWN"
    nothing_extracted = not any(f.value is not None for f in fields.values())
    confidence_unavailable = any(
        f.value is not None and (f.confidence is None or f.confidence_provenance in NON_AUTHORIZING_PROVENANCE)
        for f in fields.values()
    )
    return (
        unidentified
        or nothing_extracted
        or payload_status == "needs_review"
        or len(flags) > 0
        or confidence_unavailable
        or any(f.review_required or f.validation == "failed" for f in fields.values())
    )


_STATES = [
    lambda: _field(value=None, confidence=None, validation="not_applicable"),
    lambda: _field(),
    lambda: _field(review=True),
    lambda: _field(validation="failed"),
    lambda: _field(confidence=None),
    lambda: _field(provenance=ConfidenceProvenance.UNCALIBRATED),
    lambda: _field(value=None, confidence=None, review=True, validation="failed"),
]


def test_the_gate_and_the_assembler_equal_the_original_inline_rule_over_an_exhaustive_grid():
    cases = 0
    for first, second in itertools.product(range(len(_STATES)), repeat=2):
        for document_type, payload_status, flag_count in itertools.product(("DOC-01", "UNKNOWN"), ("processing", "needs_review"), (0, 1)):
            fields = {"a": _STATES[first](), "b": _STATES[second]()}
            flags = [FlagItem(code="SOME_FLAG", fields=["a"])] * flag_count
            expected = _original_inline_gate(document_type, fields, flags, payload_status)
            assert _assess(fields, flags, document_type, payload_status).requires_review is expected
            status = _assemble(copy.deepcopy(fields), flags, document_type, payload_status).document_metadata.status
            assert status == ("needs_review" if expected else "auto_approved"), (first, second, document_type, payload_status, flag_count)
            cases += 1
    assert cases == 7 * 7 * 8


def test_a_failed_payload_stays_failed_whatever_the_gate_says():
    assert _assemble({"a": _field()}, payload_status="failed").document_metadata.status == "failed"
    assert _assemble({"a": _field(review=True)}, payload_status="failed").document_metadata.status == "failed"


def test_the_assembler_delegates_to_the_gate_and_holds_no_copy_of_it():
    import inspect

    source = inspect.getsource(assemble_canonical_envelope)
    assert "assess_review_requirement(" in source
    for fragment in ("NON_AUTHORIZING_PROVENANCE", "nothing_extracted", "unidentified", "confidence_unavailable"):
        assert fragment not in source, fragment
