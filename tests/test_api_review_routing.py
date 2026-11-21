"""
Tests for review routing and review-task draft generation (FR-CNF-003), Phase 3 / Slice 2B-prep, PROVISIONAL, pure logic.

``route_extraction`` decides AUTO_APPROVE or NEEDS_REVIEW from a canonical extraction envelope with the one shared gate
(``field_assembly.assess_review_requirement``) and, for a review, builds a ``ReviewTaskDraft`` of the flagged fields only.

Verified: the decision for all-passing, low-confidence, validation-failure, missing-required and multi-field extractions; that the
draft names only the flagged fields, with fixed codes and the same public identifiers as the API-004 response, and holds no
value, evidence text, flag message or document type; that no draft exists for a successful extraction; that missing or
contradictory metadata fails toward review and never approves; identity consistency; determinism; that the envelope is never
modified; safe logging and sanitized failures; that no second gate exists and that nothing is stored, no clock is read and no
endpoint exists. Synthetic data and the real envelope models and assembly functions.
"""

import ast
import copy
import inspect
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from doc_extractor.api.contracts.extraction import ExtractionResponse, _FieldNames
from doc_extractor.api.contracts.records import ReviewTaskRecord, ReviewTaskStatus
from doc_extractor.api.services import correction_validation
from doc_extractor.api.services import review_routing as module
from doc_extractor.api.services.review_routing import (
    FlaggedField,
    ReviewRoutingDecision,
    ReviewRoutingError,
    ReviewRoutingOutcome,
    ReviewTaskDraft,
    RoutingErrorCode,
    RoutingReason,
    route_extraction,
)
from doc_extractor.contracts_b import (
    BoundingBox,
    ClassificationOutput,
    ConfidenceProvenance,
    LayoutBlock,
    LayoutElementType,
    ModuleBOutputPayload,
    MVPClassificationDomain,
    MVPDocumentType,
    PageLayoutOutput,
)
from doc_extractor.contracts_c import CanonicalExtractionEnvelope, EvidenceSnippet, ExtractedField, FlagItem
from doc_extractor.field_assembly import ReviewReason, assemble_canonical_envelope, assemble_field_entry
from doc_extractor.pipeline_c import ModuleCPipeline

TENANT = "tenant_alpha"
DOC = "doc_synth_1"
NOW = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
SECRET = "SYNTH-SECRET-VALUE-731"
SECRET_EVIDENCE = "SYNTH-EVIDENCE-TEXT-952"
SECRET_MESSAGE = "SYNTH-FLAG-MESSAGE-318"
OCR = ConfidenceProvenance.OCR


def _evidence(text=SECRET_EVIDENCE, ocr=0.97):
    return {"page": 1, "bbox": [0.0, 0.0, 10.0, 10.0], "text": text, "ocr_confidence": ocr}


def _entry(name, value="X", *, ocr=0.97, error=None, required=False, flags=None):
    """A field built by the REAL assembly function (threshold, validation and review indicators are the production ones)."""
    flags = flags if flags is not None else []
    return assemble_field_entry(name, value, [], flags, error=error, required=required, evidence_hint=_evidence(str(value), ocr))


def _good(value="X"):
    return ExtractedField(
        value=value, confidence=0.97, confidence_provenance=OCR, review_required=False, validation="passed",
        evidence=EvidenceSnippet(page=1, bbox=(0.0, 0.0, 10.0, 10.0), text=SECRET_EVIDENCE),
    )


def _envelope(fields, flags=(), document_type="DOC-01", document_id=DOC, payload_status="processing"):
    return assemble_canonical_envelope(
        document_id=document_id, document_type=document_type, schema_id=document_type, schema_version="1.0.0",
        classification_confidence=0.9, page_count=1, fields=fields, flags=list(flags), run_id="run_synth",
        parser_model_name="parser", extractor_model_name="extractor", payload_status=payload_status,
    )


def _with_status(envelope, status):
    return envelope.model_copy(update={"document_metadata": envelope.document_metadata.model_copy(update={"status": status})})


def _route(envelope, tenant=TENANT, document=DOC, now=NOW):
    return route_extraction(envelope, tenant_id=tenant, document_id=document, now=now)


def _all_passing():
    return _envelope({
        "applicant.full_name": _entry("applicant.full_name", SECRET),
        "applicant.pan_card_number": _entry("applicant.pan_card_number", "ABCDE1234F"),
        "applicant.phone_number": _entry("applicant.phone_number", "9876543210"),
    })


def _paths(decision):
    return [item.path for item in decision.draft.flagged_fields]


# ============================================================================
# The decision
# ============================================================================

def test_an_extraction_whose_fields_all_pass_is_auto_approved_without_a_draft():
    envelope = _all_passing()
    assert envelope.document_metadata.status == "auto_approved"
    decision = _route(envelope)
    assert decision.outcome is ReviewRoutingOutcome.AUTO_APPROVE and not decision.needs_review
    assert decision.reasons == () and decision.draft is None


def test_optional_fields_that_are_absent_do_not_block_approval():
    absent = _entry("account_details.account_type", None)
    assert absent.validation == "not_applicable" and absent.review_required is False
    envelope = _envelope({"applicant.full_name": _entry("applicant.full_name"), "account_details.account_type": absent})
    assert _route(envelope).outcome is ReviewRoutingOutcome.AUTO_APPROVE


def test_one_low_confidence_field_requires_review_and_only_it_is_flagged():
    envelope = _envelope({
        "applicant.full_name": _entry("applicant.full_name"),
        "applicant.pan_card_number": _entry("applicant.pan_card_number", "ABCDE1234F", ocr=0.40),
        "applicant.phone_number": _entry("applicant.phone_number", "9876543210"),
    })
    assert envelope.fields["applicant.pan_card_number"].review_required is True and envelope.document_metadata.status == "needs_review"
    decision = _route(envelope)
    assert decision.outcome is ReviewRoutingOutcome.NEEDS_REVIEW and decision.reasons == ("FIELD_REVIEW_REQUIRED",)
    assert _paths(decision) == ["applicant.pan_card_number"]
    assert decision.draft.flagged_fields[0].reasons == ("FIELD_REVIEW_REQUIRED",) and decision.draft.flagged_fields[0].flag_codes == ()
    assert decision.draft.document_reasons == () and decision.draft.flag_codes == ()


def test_a_confidence_exactly_at_the_field_threshold_is_not_flagged_and_just_below_is():
    """The gate reads the ``review_required`` indicator the assembly set from the field's own threshold: it applies none itself."""
    at = _envelope({"a": _entry("a", ocr=0.75)})
    below = _envelope({"a": _entry("a", ocr=0.74)})
    assert _route(at).outcome is ReviewRoutingOutcome.AUTO_APPROVE
    assert _route(below).outcome is ReviewRoutingOutcome.NEEDS_REVIEW


def test_a_per_field_threshold_that_the_assembly_applied_is_respected_not_replaced():
    strict = assemble_field_entry("a", "X", [], [], evidence_hint=_evidence("X", 0.90), confidence_threshold=0.95)
    lax = assemble_field_entry("b", "X", [], [], evidence_hint=_evidence("X", 0.60), confidence_threshold=0.50)
    decision = _route(_envelope({"a": strict, "b": lax}))
    assert decision.outcome is ReviewRoutingOutcome.NEEDS_REVIEW and _paths(decision) == ["field_1"]       # only the strict one


def test_one_validation_failure_requires_review():
    failing = _entry("applicant.pan_card_number", "12345ABCDE", error="INVALID_PAN_FORMAT")
    assert failing.validation == "failed"
    decision = _route(_envelope({"applicant.full_name": _entry("applicant.full_name"), "applicant.pan_card_number": failing}))
    assert decision.needs_review and _paths(decision) == ["applicant.pan_card_number"]
    assert decision.draft.flagged_fields[0].reasons == ("FIELD_REVIEW_REQUIRED", "FIELD_VALIDATION_FAILED")


def test_a_validation_failure_alone_is_enough_even_if_the_review_indicator_was_not_set():
    field = ExtractedField(value="X", confidence=0.97, confidence_provenance=OCR, review_required=False, validation="failed",
                           evidence=EvidenceSnippet(page=1, bbox=(0.0, 0.0, 1.0, 1.0), text="e"))
    decision = _route(_envelope({"applicant.full_name": _good(), "applicant.pan_card_number": field}))
    assert decision.needs_review and _paths(decision) == ["applicant.pan_card_number"]
    assert decision.draft.flagged_fields[0].reasons == ("FIELD_VALIDATION_FAILED",)


def test_a_missing_required_field_requires_review_and_carries_its_flag_code():
    flags = []
    envelope = _envelope({
        "applicant.full_name": _entry("applicant.full_name", None, required=True, flags=flags),
        "applicant.pan_card_number": _entry("applicant.pan_card_number", "ABCDE1234F"),
    }, flags)
    decision = _route(envelope)
    assert decision.needs_review and _paths(decision) == ["applicant.full_name"]
    flagged = decision.draft.flagged_fields[0]
    assert flagged.flag_codes == ("REQUIRED_FIELD_MISSING",) and "FIELD_VALIDATION_FAILED" in flagged.reasons
    assert decision.draft.flag_codes == ("REQUIRED_FIELD_MISSING",) and decision.draft.document_reasons == ("FLAGS_RAISED",)


def test_several_flagged_fields_are_listed_in_the_envelope_order_and_the_others_are_not():
    flags = []
    envelope = _envelope({
        "applicant.full_name": _entry("applicant.full_name"),
        "applicant.date_of_birth": _entry("applicant.date_of_birth", "x", ocr=0.30),
        "applicant.phone_number": _entry("applicant.phone_number", "9876543210"),
        "applicant.pan_card_number": _entry("applicant.pan_card_number", "BAD", error="INVALID_PAN_FORMAT"),
        "bank_details.ifsc_code": _entry("bank_details.ifsc_code", "ABCD0123456"),
        "applicant.aadhaar_number": _entry("applicant.aadhaar_number", None, required=True, flags=flags),
    }, flags)
    decision = _route(envelope)
    assert _paths(decision) == ["applicant.date_of_birth", "applicant.pan_card_number", "applicant.aadhaar_number"]
    for clean in ("applicant.full_name", "applicant.phone_number", "bank_details.ifsc_code"):
        assert clean not in _paths(decision)
    assert len(decision.draft.flagged_fields) == 3


def test_a_value_without_a_confidence_score_cannot_be_auto_approved():
    unscored = ExtractedField(value="X", confidence=None, review_required=False, validation="passed")
    decision = _route(_envelope({"applicant.full_name": _good(), "applicant.pan_card_number": unscored}))
    assert decision.needs_review and decision.reasons == ("CONFIDENCE_NOT_AUTHORIZING",)
    assert _paths(decision) == ["applicant.pan_card_number"]


def test_a_nominal_uncalibrated_score_cannot_be_auto_approved():
    nominal = ExtractedField(value="X", confidence=0.99, review_required=False, validation="passed")      # provenance defaults to uncalibrated
    assert nominal.confidence_provenance is ConfidenceProvenance.UNCALIBRATED
    decision = _route(_envelope({"applicant.full_name": nominal}))
    assert decision.needs_review and decision.reasons == ("CONFIDENCE_NOT_AUTHORIZING",)


@pytest.mark.parametrize("fields, flags, document_type, reasons", [
    ({"applicant.full_name": _good()}, [], "UNKNOWN", ("DOCUMENT_TYPE_UNKNOWN",)),
    ({}, [], "DOC-01", ("NOTHING_EXTRACTED",)),
    ({"applicant.full_name": _good()}, [FlagItem(code="PARTIAL_PAGE_COVERAGE")], "DOC-01", ("FLAGS_RAISED",)),
])
def test_a_document_level_reason_alone_requires_a_review_with_a_draft_that_has_no_flagged_field(fields, flags, document_type, reasons):
    decision = _route(_envelope(fields, flags, document_type=document_type))
    assert decision.needs_review and decision.reasons == reasons
    assert decision.draft.document_reasons == reasons and decision.draft.flagged_fields == ()


def test_the_recorded_upstream_review_status_is_honoured_when_nothing_else_explains_it():
    envelope = _envelope({"applicant.full_name": _good()}, payload_status="needs_review")
    assert envelope.document_metadata.status == "needs_review"
    decision = _route(envelope)
    assert decision.needs_review and decision.reasons == ("UPSTREAM_NEEDS_REVIEW",) and decision.draft.flagged_fields == ()


def test_the_upstream_reason_is_not_attributed_when_the_fields_explain_the_review():
    envelope = _envelope({"applicant.full_name": _entry("applicant.full_name", ocr=0.3)})
    assert envelope.document_metadata.status == "needs_review"
    assert "UPSTREAM_NEEDS_REVIEW" not in _route(envelope).reasons


def test_routing_always_agrees_with_the_status_the_assembler_recorded():
    """The same gate on the same inputs: for every assembled envelope the outcome is the recorded status."""
    cases = [
        _all_passing(),
        _envelope({"a": _entry("a", ocr=0.2)}),
        _envelope({"a": _entry("a", error="E")}),
        _envelope({"a": _good()}, [FlagItem(code="X")]),
        _envelope({"a": _good()}, document_type="UNKNOWN"),
        _envelope({"a": _good()}, payload_status="needs_review"),
        _envelope({}),
    ]
    for envelope in cases:
        decision = _route(envelope)
        assert decision.needs_review == (envelope.document_metadata.status == "needs_review")


# ============================================================================
# Missing or contradictory metadata fails toward review
# ============================================================================

def _constructed(**overrides):
    values = dict(value="X", confidence=0.97, confidence_provenance=OCR, review_required=False, validation="passed", evidence=None,
                  null_reason=None, additional_evidence=[])
    values.update(overrides)
    return ExtractedField.model_construct(**values)                       # bypasses the model validators, as corrupted data can


@pytest.mark.parametrize("overrides", [
    {"confidence": 1.7}, {"confidence": -0.1}, {"confidence": float("nan")}, {"confidence": float("inf")},
    {"confidence": True}, {"confidence": "0.9"}, {"confidence": [0.9]},
    {"validation": "ok"}, {"validation": None}, {"validation": 1},
    {"review_required": None}, {"review_required": "no"}, {"review_required": 0},
    {"confidence_provenance": "ocr"}, {"confidence_provenance": None},
])
def test_a_field_with_untrustworthy_indicators_is_flagged_and_never_approved(overrides):
    envelope = _envelope({"applicant.full_name": _good(), "applicant.pan_card_number": _good("ABCDE1234F")})
    envelope.fields["applicant.pan_card_number"] = _constructed(**overrides)
    decision = _route(envelope)
    assert decision.needs_review and "FIELD_METADATA_INVALID" in decision.reasons
    assert _paths(decision) == ["applicant.pan_card_number"]
    assert "FIELD_METADATA_INVALID" in decision.draft.flagged_fields[0].reasons


def test_a_field_that_is_not_an_extracted_field_is_flagged_and_does_not_crash_the_decision():
    envelope = _envelope({"applicant.full_name": _good(), "applicant.pan_card_number": _good()})
    envelope.fields["applicant.pan_card_number"] = {"value": SECRET, "confidence": 0.99}       # type: ignore[assignment]
    decision = _route(envelope)
    assert decision.needs_review and _paths(decision) == ["applicant.pan_card_number"] and SECRET not in repr(decision)


def test_every_field_untrustworthy_is_a_review_never_an_approval():
    envelope = _envelope({"applicant.full_name": _good()})
    envelope.fields["applicant.full_name"] = _constructed(confidence=2.0)
    decision = _route(envelope)
    assert decision.needs_review and "FIELD_METADATA_INVALID" in decision.reasons


def test_a_recorded_auto_approval_that_the_gate_contradicts_is_routed_to_review():
    envelope = _with_status(_envelope({"applicant.full_name": _entry("applicant.full_name", ocr=0.2)}), "auto_approved")
    decision = _route(envelope)
    assert decision.needs_review and "STATUS_CONFLICT" in decision.reasons and "FIELD_REVIEW_REQUIRED" in decision.reasons
    assert decision.draft.document_reasons == ("STATUS_CONFLICT",)
    unknown = _with_status(_envelope({"a": _good()}, document_type="UNKNOWN"), "auto_approved")
    assert "STATUS_CONFLICT" in _route(unknown).reasons


def test_a_recorded_auto_approval_with_a_corrupted_field_is_a_conflict_too():
    envelope = _with_status(_envelope({"applicant.full_name": _good()}), "auto_approved")
    envelope.fields["applicant.full_name"] = _constructed(validation="ok")
    assert set(_route(envelope).reasons) >= {"STATUS_CONFLICT", "FIELD_METADATA_INVALID"}


def test_a_recorded_review_status_with_a_clean_document_is_still_a_review():
    envelope = _with_status(_all_passing(), "needs_review")
    decision = _route(envelope)
    assert decision.needs_review and decision.reasons == ("UPSTREAM_NEEDS_REVIEW",)


@pytest.mark.parametrize("status", ["failed", "processing", "reviewed", "rejected"])
def test_only_an_extraction_awaiting_a_routing_decision_is_routed(status):
    with pytest.raises(ReviewRoutingError) as caught:
        _route(_with_status(_all_passing(), status))
    assert caught.value.code == RoutingErrorCode.STATUS_NOT_ROUTABLE


def test_a_failed_payload_is_refused_not_routed_to_review():
    envelope = _envelope({"a": _good()}, payload_status="failed")
    assert envelope.document_metadata.status == "failed"
    with pytest.raises(ReviewRoutingError) as caught:
        _route(envelope)
    assert caught.value.code == RoutingErrorCode.STATUS_NOT_ROUTABLE


def _unreadable_variants():
    meta = _all_passing().document_metadata
    run = _all_passing().run
    build = CanonicalExtractionEnvelope.model_construct
    return {
        "not an envelope (dict)": {"document_id": DOC},
        "not an envelope (None)": None,
        "not an envelope (str)": "envelope",
        "flags that are not flags": build(document_id=DOC, document_metadata=meta, fields={"a": _good()}, flags=["x"], run=run),
        "flags that are not a list": build(document_id=DOC, document_metadata=meta, fields={"a": _good()}, flags=None, run=run),
        "fields that are not a mapping": build(document_id=DOC, document_metadata=meta, fields=[("a", _good())], flags=[], run=run),
        "a non-string field key": build(document_id=DOC, document_metadata=meta, fields={1: _good()}, flags=[], run=run),
        "a flag whose fields are not a list": build(
            document_id=DOC, document_metadata=meta, fields={"a": _good()}, flags=[FlagItem.model_construct(code="X", message=None, fields="a")], run=run),
    }


@pytest.mark.parametrize("name", sorted(_unreadable_variants()))
def test_an_extraction_result_that_cannot_be_read_is_refused_not_guessed(name):
    with pytest.raises(ReviewRoutingError) as caught:
        _route(_unreadable_variants()[name])
    assert caught.value.code == RoutingErrorCode.INVALID_ENVELOPE


# ============================================================================
# The draft: flagged fields only, codes and identifiers only
# ============================================================================

def _hostile_envelope():
    """Secrets everywhere an envelope can carry content: a value, evidence text, a flag message, a model-chosen field key."""
    flags = [
        FlagItem(code="ARITHMETIC_MISMATCH", message=f"mismatch {SECRET_MESSAGE}", fields=[f"Total {SECRET}", "ok_field"]),
        FlagItem(code=f"quotes {SECRET}", message=SECRET_MESSAGE, fields=[f"Total {SECRET}"]),
        FlagItem(code="lower_case_code", message=SECRET_MESSAGE),
    ]
    fields = {
        f"Total {SECRET}": ExtractedField(
            value=SECRET, confidence=0.3, confidence_provenance=OCR, review_required=True, validation="failed",
            evidence=EvidenceSnippet(page=1, bbox=(0.0, 0.0, 1.0, 1.0), text=SECRET_EVIDENCE),
            additional_evidence=[EvidenceSnippet(page=2, bbox=(0.0, 0.0, 1.0, 1.0), text=SECRET_EVIDENCE)],
        ),
        "ok_field": _good(SECRET),
    }
    return _envelope(fields, flags, document_type="UNKNOWN")


def test_the_draft_holds_no_value_no_evidence_text_no_message_and_no_model_chosen_key():
    decision = _route(_hostile_envelope())
    assert decision.needs_review
    rendered = " ".join((repr(decision), decision.model_dump_json(), repr(decision.draft), decision.draft.model_dump_json()))
    for secret in (SECRET, SECRET_EVIDENCE, SECRET_MESSAGE, "Total", "quotes", "lower_case_code", "mismatch"):
        assert secret not in rendered, secret
    assert _paths(decision) == ["field_1", "field_2"]                                  # positional ids; ok_field is flagged by a flag
    assert decision.draft.flag_codes == ("ARITHMETIC_MISMATCH", "RULE_VIOLATION")      # the non-token codes became the fixed fallback


def test_a_flag_code_that_is_not_an_upper_case_token_becomes_the_fixed_fallback():
    for code in ("lower", "has space", "9STARTS_WITH_DIGIT", "A", "X" * 65, "", SECRET, None, 7):
        flag = FlagItem.model_construct(code=code, message=None, fields=[])
        envelope = _envelope({"a": _good()})
        envelope.flags.append(flag)
        assert _route(envelope).draft.flag_codes == ("RULE_VIOLATION",), code


def test_the_code_token_shape_equals_the_one_of_the_correction_validation():
    assert module._CODE_TOKEN.pattern == correction_validation._CODE_TOKEN.pattern


def test_the_flag_codes_of_a_field_are_those_of_the_flags_that_name_it():
    flags = [
        FlagItem(code="CHRONOLOGY_MISMATCH", fields=["admission_date", "discharge_date"]),
        FlagItem(code="OTHER_RULE", fields=["discharge_date"]),
        FlagItem(code="CHRONOLOGY_MISMATCH", fields=["admission_date"]),                # a repeated code is listed once
    ]
    envelope = _envelope({
        "patient_name": _entry("patient_name"), "admission_date": _entry("admission_date", "2026-05-15"),
        "discharge_date": _entry("discharge_date", "2026-05-10"),
    }, flags, document_type="DOC-03")
    decision = _route(envelope)
    by_path = {item.path: item for item in decision.draft.flagged_fields}
    assert set(by_path) == {"admission_date", "discharge_date"}
    assert by_path["admission_date"].flag_codes == ("CHRONOLOGY_MISMATCH",)
    assert by_path["discharge_date"].flag_codes == ("CHRONOLOGY_MISMATCH", "OTHER_RULE")
    assert decision.draft.flag_codes == ("CHRONOLOGY_MISMATCH", "OTHER_RULE")


def test_a_field_named_only_by_a_flag_is_flagged_even_when_its_own_indicators_pass():
    flags = [FlagItem(code="CUSTOM_RULE", fields=["applicant.full_name"])]
    decision = _route(_envelope({"applicant.full_name": _good(), "applicant.pan_card_number": _good("ABCDE1234F")}, flags))
    assert _paths(decision) == ["applicant.full_name"]
    flagged = decision.draft.flagged_fields[0]
    assert flagged.reasons == () and flagged.flag_codes == ("CUSTOM_RULE",)


def test_a_flag_that_names_a_schema_path_without_its_own_field_entry_is_kept_as_a_flagged_path():
    flags = [FlagItem(code="ARITHMETIC_MISMATCH", message="shares", fields=["nominees.share_percentage"])]
    envelope = _envelope({"proposer_name": _good(), "nominees.0.share_percentage": _good(60)}, flags, document_type="DOC-02")
    decision = _route(envelope)
    assert _paths(decision) == ["nominees.share_percentage"]
    assert decision.draft.flagged_fields[0].flag_codes == ("ARITHMETIC_MISMATCH",)


def test_a_flag_reference_that_cannot_be_mapped_safely_is_dropped_but_its_code_is_kept():
    flags = [FlagItem(code="RULE_X", message=SECRET, fields=[f"free text {SECRET}", "not_in_schema"])]
    decision = _route(_envelope({"applicant.full_name": _good(), "applicant.pan_card_number": _good()}, flags))
    assert decision.needs_review and decision.draft.flagged_fields == () and decision.draft.flag_codes == ("RULE_X",)
    assert decision.draft.document_reasons == ("FLAGS_RAISED",) and SECRET not in repr(decision)


ARABIC_ONE = chr(0x0661)         # Unicode digits (built with chr()) that a ``\d`` index check would accept
FULLWIDTH_ONE = chr(0xFF11)
UNMAPPABLE = [
    f"nominees.{ARABIC_ONE}.name", f"nominees.{FULLWIDTH_ONE}.name", "nominees.0\n.name", "nominees.0 .name", "nominees.0\r\n.name",
    "nominees..name", "nominees.1234.name", "nominees." + "0." * 70 + "name", f"free text {SECRET}", "", "\n", "x" * 400,
]


def _dropping_envelope(references):
    """DOC-02: ``nominees.0.name`` is flagged by the gate (low OCR); the flag also names ``references``."""
    flags = [FlagItem(code="RULE_X", message=SECRET_MESSAGE, fields=["nominees.0.name", *references, "nominees.share_percentage"])]
    return _envelope({"proposer_name": _entry("proposer_name"), "nominees.0.name": _entry("nominees.0.name", ocr=0.2)}, flags,
                     document_type="DOC-02")


@pytest.mark.parametrize("reference", UNMAPPABLE)
def test_an_unmappable_reference_is_dropped_and_the_valid_ones_still_route(reference):
    decision = _route(_dropping_envelope([reference]))
    assert decision.needs_review
    assert _paths(decision) == ["nominees.0.name", "nominees.share_percentage"]
    by_path = {item.path: item for item in decision.draft.flagged_fields}
    assert by_path["nominees.0.name"].flag_codes == ("RULE_X",) and by_path["nominees.0.name"].reasons          # reasons kept
    assert by_path["nominees.share_percentage"].flag_codes == ("RULE_X",)
    assert decision.draft.flag_codes == ("RULE_X",)                                  # the flag's own code is kept
    text = repr(decision)
    assert SECRET not in text and SECRET_MESSAGE not in text and "free text" not in text


def test_every_reference_unmappable_still_routes_with_the_flag_code_and_the_gate_reasons():
    decision = _route(_dropping_envelope(UNMAPPABLE).model_copy(update={"flags": [FlagItem(code="RULE_X", message="m", fields=UNMAPPABLE)]}))
    assert decision.needs_review and decision.draft.flag_codes == ("RULE_X",)
    assert _paths(decision) == ["nominees.0.name"] and decision.draft.flagged_fields[0].flag_codes == ()
    assert decision.draft.flagged_fields[0].reasons                                      # the low-confidence field keeps its gate reason


def test_valid_references_keep_their_identifiers_unaltered_beside_unmappable_ones():
    valid = ["nominees.0.name", "nominees.12.name", "nominees.999.share_percentage", "nominees.share_percentage"]
    flags = [FlagItem(code="RULE_X", message="m", fields=[*UNMAPPABLE[:4], *valid, *UNMAPPABLE[4:]])]
    decision = _route(_envelope({"nominees.0.name": _entry("nominees.0.name", ocr=0.2)}, flags, document_type="DOC-02"))
    assert _paths(decision) == ["nominees.0.name", "nominees.12.name", "nominees.999.share_percentage", "nominees.share_percentage"]


def test_the_over_long_but_canonical_looking_reference_is_dropped_by_the_routing_guard_itself():
    """Every segment an index: the API-004 mapping calls it canonical, the draft cannot hold it (128 characters): dropped, not fatal."""
    reference = "nominees." + "0." * 70 + "name"
    envelope = _dropping_envelope([reference])
    assert len(reference) > module.MAX_PATH_LENGTH
    assert _FieldNames.for_envelope(envelope).public_reference(reference) == reference
    assert reference not in _paths(_route(envelope)) and _paths(_route(envelope))


def test_a_reference_that_is_not_text_is_dropped_too():
    bad = FlagItem.model_construct(code="RULE_X", message="m", fields=["nominees.0.name", 7, None, b"nominees.0.name", ("a",), {"a": 1}])
    decision = _route(_envelope({"nominees.0.name": _entry("nominees.0.name", ocr=0.2)}, [], document_type="DOC-02").model_copy(update={"flags": [bad]}))
    assert _paths(decision) == ["nominees.0.name"] and decision.draft.flagged_fields[0].flag_codes == ("RULE_X",)


@pytest.mark.parametrize("reference", UNMAPPABLE[:7])
def test_the_draft_and_the_api_response_drop_the_same_references(reference):
    envelope = _dropping_envelope([reference])
    assert set(ExtractionResponse.from_envelope(envelope).flags[0].fields) == {"nominees.0.name", "nominees.share_percentage"}
    assert set(_paths(_route(envelope))) == {"nominees.0.name", "nominees.share_percentage"}


def test_dropping_a_reference_logs_nothing_and_leaves_the_envelope_unmodified(caplog):
    envelope = _dropping_envelope(UNMAPPABLE)
    before = envelope.model_dump_json()
    with caplog.at_level(logging.DEBUG):
        _route(envelope)
    assert caplog.records == [] and envelope.model_dump_json() == before


def test_an_unmappable_reference_does_not_turn_an_approval_into_a_review_or_the_reverse():
    clean = _envelope({"applicant.full_name": _entry("applicant.full_name")}, [], document_type="DOC-01")
    assert _route(clean).outcome is ReviewRoutingOutcome.AUTO_APPROVE
    flagged = _envelope({"applicant.full_name": _entry("applicant.full_name", ocr=0.2)}, [FlagItem(code="RULE_X", message="m", fields=UNMAPPABLE)])
    assert _route(flagged).needs_review


def test_a_genuine_routing_failure_is_still_fail_closed_with_unmappable_references_present(monkeypatch):
    envelope = _dropping_envelope(UNMAPPABLE)

    def broken(self, reference):
        raise RuntimeError(f"fault {SECRET}")

    monkeypatch.setattr(_FieldNames, "public_reference", broken)
    with pytest.raises(ReviewRoutingError) as caught:
        _route(envelope)
    assert caught.value.code == RoutingErrorCode.ROUTING_FAILED and SECRET not in str(caught.value)


@pytest.mark.parametrize("kwargs, code", [
    ({"document": "doc_other"}, RoutingErrorCode.DOCUMENT_ID_MISMATCH),
    ({"tenant": ""}, RoutingErrorCode.TENANT_REQUIRED),
    ({"now": datetime(2026, 3, 1)}, RoutingErrorCode.INVALID_TIMESTAMP),
])
def test_the_other_refusals_are_unchanged_when_the_flags_hold_unmappable_references(kwargs, code):
    with pytest.raises(ReviewRoutingError) as caught:
        _route(_dropping_envelope(UNMAPPABLE), **kwargs)
    assert caught.value.code == code


def test_a_non_routable_status_is_still_refused_with_unmappable_references_present():
    with pytest.raises(ReviewRoutingError) as caught:
        _route(_with_status(_dropping_envelope(UNMAPPABLE), "failed"))
    assert caught.value.code == RoutingErrorCode.STATUS_NOT_ROUTABLE


def test_a_flagged_field_still_refuses_an_unsafe_identifier_directly():
    for path in (f"nominees.{ARABIC_ONE}.name", "nominees.0\n.name", "x" * 129, "", "a b"):
        with pytest.raises(ValidationError):
            FlaggedField(path=path, flag_codes=("RULE_X",))
    assert FlaggedField(path="nominees.0.name", flag_codes=("RULE_X",)).path == "nominees.0.name"


def test_the_draft_identifiers_equal_the_api_004_response_identifiers_for_the_same_envelope():
    """One mapping: whatever a reviewer sees in the extraction response is exactly what a task names."""
    for envelope in (_hostile_envelope(),
                     _envelope({"Total Due": _entry("Total Due", ocr=0.2), "Name": _entry("Name"), "Sum": _entry("Sum", error="E")}, document_type="UNKNOWN"),
                     _envelope({"applicant.full_name": _entry("applicant.full_name", ocr=0.2), "applicant.pan_card_number": _entry("applicant.pan_card_number")})):
        response = ExtractionResponse.from_envelope(envelope)
        flagged_in_response = [path for path, public in response.fields.items() if public.review_required or public.validation == "failed"]
        decision = _route(envelope)
        assert set(flagged_in_response) <= set(_paths(decision))
        for path in _paths(decision):
            assert path in response.fields or path in {p for flag in response.flags for p in flag.fields}


def test_the_draft_has_exactly_the_documented_fields_and_none_of_the_unprovided_ones():
    names = set(ReviewTaskDraft.model_fields)
    assert names == {"tenant_id", "document_id", "status", "created_at", "flagged_fields", "flag_codes", "document_reasons"}
    for absent in ("task_id", "priority", "sla_due_at", "slaDueAt", "document_type", "values", "evidence", "messages", "extracted_values"):
        assert absent not in names
    assert set(FlaggedField.model_fields) == {"path", "reasons", "flag_codes"}


def test_the_draft_is_unassigned_timestamped_and_carries_the_stated_identity():
    decision = _route(_envelope({"applicant.full_name": _entry("applicant.full_name", ocr=0.2)}))
    draft = decision.draft
    assert draft.status is ReviewTaskStatus.UNASSIGNED and draft.created_at == NOW
    assert draft.tenant_id == TENANT and draft.document_id == DOC


def test_the_draft_is_compatible_with_the_existing_review_task_record_contract():
    """Identity, state and time map onto ``ReviewTaskRecord``; ``flaggedFields`` and ``slaDueAt`` are NOT modelled there (yet)."""
    draft = _route(_envelope({"applicant.full_name": _entry("applicant.full_name", ocr=0.2)})).draft
    record = ReviewTaskRecord(task_id="task_synth", tenant_id=draft.tenant_id, document_id=draft.document_id, status=draft.status,
                              created_at=draft.created_at, updated_at=draft.created_at)
    assert record.status is ReviewTaskStatus.UNASSIGNED and record.assigned_reviewer_id is None and record.completed_at is None
    assert not {"flagged_fields", "flaggedFields", "sla_due_at", "slaDueAt"} & set(ReviewTaskRecord.model_fields)


def test_the_draft_and_decision_are_frozen_and_validated():
    decision = _route(_envelope({"applicant.full_name": _entry("applicant.full_name", ocr=0.2)}))
    with pytest.raises(Exception):
        decision.draft.tenant_id = "other"
    with pytest.raises(Exception):
        decision.outcome = ReviewRoutingOutcome.AUTO_APPROVE
    good = dict(tenant_id=TENANT, document_id=DOC, created_at=NOW, flagged_fields=(FlaggedField(path="a", reasons=("FIELD_REVIEW_REQUIRED",)),))
    assert ReviewTaskDraft(**good).status is ReviewTaskStatus.UNASSIGNED
    for bad in (
        {**good, "tenant_id": ""}, {**good, "document_id": ""}, {**good, "created_at": datetime(2026, 1, 1)},
        {**good, "status": ReviewTaskStatus.CLAIMED}, {**good, "status": ReviewTaskStatus.COMPLETED},
        {**good, "flagged_fields": ()},                                                                  # no reason at all
        {**good, "flagged_fields": (FlaggedField(path="a", reasons=("FIELD_REVIEW_REQUIRED",)),) * 2},    # a field flagged twice
        {**good, "flag_codes": ("lower",)}, {**good, "document_reasons": ("NOT_A_KNOWN_REASON",)},
        {**good, "flagged_fields": (FlaggedField(path="a", flag_codes=("UNLISTED_CODE",)),)},            # not a code of the document
        {**good, "extra": 1}, {**good, "task_id": "t1"}, {**good, "document_type": "DOC-01"},
    ):
        with pytest.raises(Exception):
            ReviewTaskDraft(**bad)
    for bad_field in ({"path": "has space"}, {"path": ""}, {"path": "a" * 129}, {"path": "a..b"}, {"path": "a", "reasons": ()},
                      {"path": "a", "reasons": ("FIELD_REVIEW_REQUIRED", "FIELD_REVIEW_REQUIRED")}, {"path": "a", "reasons": ("nope",)}):
        with pytest.raises(Exception):
            FlaggedField(**bad_field)


def test_a_decision_must_match_its_outcome():
    draft = _route(_envelope({"a": _entry("a", ocr=0.2)})).draft
    with pytest.raises(Exception):
        ReviewRoutingDecision(outcome=ReviewRoutingOutcome.AUTO_APPROVE, draft=draft)
    with pytest.raises(Exception):
        ReviewRoutingDecision(outcome=ReviewRoutingOutcome.AUTO_APPROVE, reasons=("FIELD_REVIEW_REQUIRED",))
    with pytest.raises(Exception):
        ReviewRoutingDecision(outcome=ReviewRoutingOutcome.NEEDS_REVIEW, reasons=("FIELD_REVIEW_REQUIRED",))        # no draft
    with pytest.raises(Exception):
        ReviewRoutingDecision(outcome=ReviewRoutingOutcome.NEEDS_REVIEW, draft=draft)                                # no reason
    assert ReviewRoutingDecision(outcome=ReviewRoutingOutcome.AUTO_APPROVE).draft is None


def test_no_draft_exists_for_a_successful_extraction_in_any_shape():
    passing = [
        _all_passing(),
        _envelope({"a": _good()}),
        _envelope({"a": _good(), "b": _entry("b", None)}),
        _envelope({"a": _good(0), "b": _good(False), "c": _good("")}),         # falsy values are still values
    ]
    for envelope in passing:
        decision = _route(envelope)
        assert decision.outcome is ReviewRoutingOutcome.AUTO_APPROVE and decision.draft is None and decision.reasons == ()


# ============================================================================
# Real pipeline
# ============================================================================

def _doc03_payload(admission, discharge):
    block = LayoutBlock(
        block_id="b1", element_type=LayoutElementType.PARAGRAPH, text="Patient: RAJESH SHARMA", ocr_confidence=0.95,
        bbox_preprocessed=BoundingBox(x0=10, y0=10, x1=100, y1=30), bbox_original=BoundingBox(x0=10, y0=10, x1=100, y1=30),
        reading_order_index=0,
    )
    markdown = f"Patient: RAJESH SHARMA\nAdmission Date: {admission}\nDischarge Date: {discharge}"
    page = PageLayoutOutput(page_number=1, markdown_content=markdown, blocks=[block], mean_ocr_confidence=0.95)
    return ModuleBOutputPayload(
        run_id="run_synth", document_id=DOC, tenant_id=TENANT, status="processing",
        classification=ClassificationOutput(
            domain=MVPClassificationDomain.HEALTHCARE, document_type=MVPDocumentType.DOC_03_DISCHARGE_SUMMARY,
            classification_confidence=0.96, confidence_provenance=ConfidenceProvenance.RULE_HEURISTIC, schema_id="DOC-03",
            schema_version="1.0.0",
        ),
        pages=[page], combined_markdown=markdown, parser_model_name="mock-layout-parser", classifier_model_name="mock-classifier",
        processing_time_ms=1.0, created_at=NOW.isoformat(), completed_at=NOW.isoformat(),
    )


def test_the_real_module_c_pipeline_envelope_is_routed_with_its_chronology_flag():
    envelope = ModuleCPipeline().process(_doc03_payload("2026-05-15", "2026-05-10"))
    assert envelope.document_metadata.status == "needs_review"
    decision = _route(envelope)
    assert decision.needs_review and "FLAGS_RAISED" in decision.reasons
    assert "CHRONOLOGY_MISMATCH" in decision.draft.flag_codes
    flagged = {item.path: item for item in decision.draft.flagged_fields}
    assert {"admission_date", "discharge_date"} <= set(flagged)
    assert all("CHRONOLOGY_MISMATCH" in flagged[p].flag_codes for p in ("admission_date", "discharge_date"))
    assert "RAJESH" not in decision.model_dump_json() and "2026-05" not in decision.model_dump_json()


def test_the_real_module_c_pipeline_outcome_always_equals_its_recorded_status():
    for admission, discharge in (("2026-05-10", "2026-05-15"), ("2026-05-15", "2026-05-10")):
        envelope = ModuleCPipeline().process(_doc03_payload(admission, discharge))
        assert _route(envelope).needs_review == (envelope.document_metadata.status == "needs_review")


# ============================================================================
# Identity, determinism, immutability
# ============================================================================

def test_the_draft_carries_exactly_the_stated_tenant_and_document_and_the_envelope_must_match_the_document():
    envelope = _envelope({"applicant.full_name": _entry("applicant.full_name", ocr=0.2)}, document_id="doc_other")
    draft = _route(envelope, tenant="tenant_beta", document="doc_other").draft
    assert (draft.tenant_id, draft.document_id) == ("tenant_beta", "doc_other")
    with pytest.raises(ReviewRoutingError) as caught:
        _route(envelope, document=DOC)                                                  # the envelope belongs to another document
    assert caught.value.code == RoutingErrorCode.DOCUMENT_ID_MISMATCH


@pytest.mark.parametrize("tenant", [None, "", "   ", "\t\n", 7, b"t", ["t"], {"t": 1}])
def test_a_missing_or_malformed_tenant_is_refused(tenant):
    with pytest.raises(ReviewRoutingError) as caught:
        _route(_all_passing(), tenant=tenant)
    assert caught.value.code == RoutingErrorCode.TENANT_REQUIRED


@pytest.mark.parametrize("document", [None, "", "   ", 7, b"d", ["d"]])
def test_a_missing_or_malformed_document_identifier_is_refused(document):
    with pytest.raises(ReviewRoutingError) as caught:
        _route(_all_passing(), document=document)
    assert caught.value.code == RoutingErrorCode.DOCUMENT_ID_REQUIRED


@pytest.mark.parametrize("now", [None, "2026-03-01T12:00:00Z", 1_700_000_000, datetime(2026, 3, 1, 12, 0), NOW.date()])
def test_the_creation_time_must_be_a_timezone_aware_datetime(now):
    with pytest.raises(ReviewRoutingError) as caught:
        _route(_all_passing(), now=now)
    assert caught.value.code == RoutingErrorCode.INVALID_TIMESTAMP


def test_the_identity_is_never_taken_from_the_envelope_or_echoed_by_a_refusal():
    hostile_tenant, hostile_document = f"tenant {SECRET}", f"doc {SECRET}"
    envelope = _envelope({"a": _good()}, document_id="doc_a")
    for kwargs in ({"tenant": "  "}, {"document": hostile_document}, {"document": hostile_document, "tenant": hostile_tenant}):
        with pytest.raises(ReviewRoutingError) as caught:
            _route(envelope, **kwargs)
        assert SECRET not in str(caught.value) and SECRET not in repr(caught.value) and SECRET not in caught.value.code
        assert caught.value.__cause__ is None


def test_the_output_is_deterministic():
    envelope = _hostile_envelope()
    first = _route(envelope)
    assert first == _route(envelope) and first.model_dump_json() == _route(envelope).model_dump_json()
    assert first == _route(copy.deepcopy(envelope)) and first == _route(CanonicalExtractionEnvelope.model_validate(envelope.model_dump()))
    shifted = _route(envelope, now=NOW + timedelta(hours=5))
    assert shifted.draft.created_at == NOW + timedelta(hours=5)
    assert shifted.model_copy(update={"draft": first.draft}) == first                        # nothing else depends on the time


def test_the_source_envelope_is_never_modified():
    variants = [_hostile_envelope(), _all_passing(), _envelope({"a": _entry("a", ocr=0.2), "b": _good()}, [FlagItem(code="X", fields=["a"])])]
    corrupted = _envelope({"applicant.full_name": _good(), "applicant.pan_card_number": _good()})
    corrupted.fields["applicant.pan_card_number"] = _constructed(confidence=9.9)
    variants.append(corrupted)
    for envelope in variants:
        before = envelope.model_dump()
        identities = {key: id(field) for key, field in envelope.fields.items()}, [id(flag) for flag in envelope.flags]
        _route(envelope)
        assert envelope.model_dump() == before
        assert ({key: id(field) for key, field in envelope.fields.items()}, [id(flag) for flag in envelope.flags]) == identities
        assert list(envelope.fields) == list(before["fields"])                              # the order is untouched too


# ============================================================================
# Safe logging, sanitized failures, scope
# ============================================================================

def test_routing_logs_nothing_for_any_input(caplog):
    with caplog.at_level(logging.DEBUG):
        for envelope in (_hostile_envelope(), _all_passing()):
            _route(envelope)
        for bad in (None, {"x": SECRET}):
            with pytest.raises(ReviewRoutingError):
                _route(bad)
    assert caplog.records == []


def test_an_unexpected_internal_fault_is_a_sanitized_error_and_only_its_class_is_logged(caplog, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError(f"database password {SECRET}")

    monkeypatch.setattr(module, "_decide", broken)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ReviewRoutingError) as caught:
            _route(_all_passing())
    assert caught.value.code == RoutingErrorCode.ROUTING_FAILED and str(caught.value) == "Review routing failed."
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True
    assert SECRET not in caplog.text and "password" not in caplog.text and "exception=RuntimeError" in caplog.text
    assert [record.getMessage() for record in caplog.records] == ["Review routing fault (reason=routing_failed, exception=RuntimeError)"]


def test_the_error_codes_have_fixed_messages_that_carry_no_input():
    assert ReviewRoutingError("anything else").code == RoutingErrorCode.ROUTING_FAILED
    for code, message in module._MESSAGES.items():
        error = ReviewRoutingError(code)
        assert error.code == code and str(error) == message
        assert not any(character in message for character in "{}%$")


def test_the_logging_calls_are_constant_messages_with_only_an_exception_class():
    tree = ast.parse(inspect.getsource(module))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "logger"]
    assert len(calls) == 1
    call = calls[0]
    assert isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
    assert [ast.unparse(arg) for arg in call.args[1:]] == ["type(exc).__name__"]


def test_there_is_one_gate_and_this_module_adds_no_threshold_policy_or_second_rule():
    import doc_extractor.field_assembly as assembly

    assert module.assess_review_requirement is assembly.assess_review_requirement
    source = inspect.getsource(module)
    for forbidden in ("0.75", "0.5", "DEFAULT_FIELD_CONFIDENCE_THRESHOLD", "NON_AUTHORIZING_PROVENANCE", "confidence_threshold"):
        assert forbidden not in source, forbidden
    tree = ast.parse(source)
    for node in ast.walk(tree):                                                           # a confidence is never compared with a threshold
        if isinstance(node, ast.Compare):
            names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if "confidence" in names:
                assert all(isinstance(op, (ast.IsNot, ast.LtE, ast.GtE, ast.Lt, ast.Gt)) for op in node.ops)     # "is not None" and the [0, 1] range
                assert {c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, float)} <= {0.0, 1.0}
    assert set(RoutingReason.GATE) == {member.value for member in ReviewReason}


def test_the_module_has_no_side_effect_and_reads_no_clock():
    tree = ast.parse(inspect.getsource(module))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden in ("doc_extractor.api.ports", "doc_extractor.api.services.review_task_service", "doc_extractor.api.dependencies",
                      "doc_extractor.storage", "fastapi", "starlette", "sqlite3", "sqlalchemy", "requests", "httpx", "os", "subprocess"):
        assert not any(name == forbidden or name.startswith(forbidden + ".") for name in imported), forbidden
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"now", "utcnow", "today", "time", "write", "save", "create", "update_status", "send"}
        assert not (isinstance(node, ast.Name) and node.id in {"utc_now", "open", "print"})


def test_nothing_else_in_the_api_uses_this_module_yet_so_no_endpoint_or_webhook_exists():
    api = Path(inspect.getsourcefile(module)).parents[1]
    users = [path.name for path in api.rglob("*.py") if path.name != "review_routing.py" and "review_routing" in path.read_text(encoding="utf-8")]
    assert users == []
