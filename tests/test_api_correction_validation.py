"""
Tests for the pure correction validation of Phase 3, Slice 2A (FR-REV-003), PROVISIONAL.

``CorrectionValidator`` re-validates proposed corrections against the EXACT schema version of the extraction, reusing the
existing validators, rule evaluator and schema compiler. Nothing here stores, audits, completes or exposes anything.

Verified: valid and invalid corrections for the static DOC-01 / DOC-02 / DOC-03 rules and for dynamic ``schema_lifecycle``
schemas; exact-version enforcement (no active or latest fallback, no rounding of a version); controlled failures for missing,
malformed or unresolvable schema versions; invalid, unknown, container and duplicate field paths; that the static rule table
agrees with ``ModuleCPipeline`` itself; that the source field set is never mutated; and that no value, evidence text, client
path or exception text reaches a result or a log record. Synthetic data only.
"""

import ast
import inspect
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from doc_extractor.api.services import correction_validation as module
from doc_extractor.api.services.correction_validation import (
    MAX_CORRECTIONS,
    CorrectionCode,
    CorrectionOutcome,
    CorrectionValidator,
    ProposedCorrection,
    parse_exact_version,
)
from doc_extractor.contracts_b import (
    BoundingBox,
    ClassificationOutput,
    LayoutBlock,
    LayoutElementType,
    ModuleBOutputPayload,
    MVPClassificationDomain,
    MVPDocumentType,
    PageLayoutOutput,
)
from doc_extractor.contracts_c import EvidenceSnippet, ExtractedField
from doc_extractor.extractor import SCHEMA_MAP
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.schemas import (
    AccountDetails,
    ApplicantDetails,
    BankDetails,
    CASASchema,
    DischargeSummarySchema,
    InsuranceApplicationSchema,
    NomineeDetails,
)
from doc_extractor.validation_rules import generate_valid_aadhaar
from schema_lifecycle.compiler import DynamicSchemaCompiler
from schema_lifecycle.enums import FieldType, SchemaStatus
from schema_lifecycle.metamodel import FieldDefinition, RuleDefinition, SchemaDefinition
from schema_lifecycle.repository import InMemorySchemaRepository, SchemaSnapshot

TENANT = "tenant_alpha"
OTHER_TENANT = "tenant_beta"
SECRET = "SYNTH-SECRET-VALUE-731"
SECRET_TEXT = "SYNTH-EVIDENCE-TEXT-952"
HOSTILE_PATH = "John Doe 4111"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

VALIDATOR = CorrectionValidator()


def _fc(value, **kw):
    return ExtractedField(value=value, **kw)


def _validate(schema_id, version, corrections, fields=None, validator=VALIDATOR, tenant=TENANT):
    return validator.validate(
        tenant_id=tenant, schema_id=schema_id, schema_version=version, fields=fields or {},
        corrections=[ProposedCorrection(path, value) for path, value in corrections],
    )


def _issue_codes(result):
    return sorted(result.codes)


# ============================================================================
# Exact version parsing
# ============================================================================

@pytest.mark.parametrize("raw, expected", [
    (1, 1), (7, 7), ("1", 1), ("12", 12), ("1.0.0", 1), ("3.0.0", 3), (" 2 ", 2), (" 2.0.0 ", 2), ("999999999", 999999999),
])
def test_exact_versions_are_parsed(raw, expected):
    assert parse_exact_version(raw) == expected


@pytest.mark.parametrize("raw", [
    0, -1, True, False, 1.0, 1.5, None, "", " ", "0", "01", "v1", "V2", "1.2.0", "1.0.1", "1.0", "1.0.0.0", "1.0.0-beta", "1e3",
    "1abc", "1.0.0abc", "١", "1\n2", "x", [1], {"v": 1}, 10**12, "1000000000",
])
def test_anything_that_is_not_exactly_a_canonical_version_is_refused(raw):
    """``SchemaVersionAdapter.to_canonical`` would round "1.2.0" and "1abc" down to 1: here they are refused, not guessed."""
    assert parse_exact_version(raw) is None


# ============================================================================
# Static schemas: DOC-01, DOC-02, DOC-03 (version 1.0.0)
# ============================================================================

def test_valid_doc01_corrections_pass_with_the_validators_module_c_uses():
    result = _validate("DOC-01", "1.0.0", [
        ("applicant.full_name", "SYNTH NAME"),
        ("applicant.pan_card_number", "abcde1234f"),
        ("applicant.date_of_birth", "22/06/1985"),
        ("applicant.phone_number", "9876543210"),
        ("applicant.aadhaar_number", generate_valid_aadhaar()),
        ("account_details.initial_deposit_amount", "Rs. 5,000"),
        ("bank_details.ifsc_code", "abcd0123456"),
        ("account_details.opt_in_services.cheque_book", True),
    ])
    assert result.outcome is CorrectionOutcome.VALID and result.is_valid and result.issues == ()
    assert result.schema_id == "DOC-01" and result.schema_version == 1
    assert len(result.validated_paths) == 8


@pytest.mark.parametrize("path, value, code", [
    ("applicant.pan_card_number", "12345ABCDE", "INVALID_PAN_FORMAT"),
    ("applicant.date_of_birth", "not a date", "INVALID_DATE_FORMAT"),
    ("applicant.phone_number", "12", "INVALID_PHONE_FORMAT"),
    ("applicant.aadhaar_number", "123", "INVALID_AADHAAR_LENGTH"),
    ("applicant.aadhaar_number", "234567890126", "INVALID_AADHAAR_CHECKSUM"),
    ("account_details.initial_deposit_amount", "abc", "INVALID_AMOUNT_FORMAT"),
    ("bank_details.ifsc_code", "BAD", "INVALID_IFSC_FORMAT"),
])
def test_invalid_doc01_corrections_fail_with_the_fixed_codes_of_the_validators(path, value, code):
    result = _validate("DOC-01", 1, [(path, value)])
    assert result.outcome is CorrectionOutcome.INVALID and not result.is_valid
    assert result.issues[0].code == code and result.issues[0].paths == (path,)
    assert str(value) not in repr(result)


def test_a_required_field_cannot_be_nulled_but_an_optional_one_can():
    for blank in (None, "", "   "):
        assert _validate("DOC-01", 1, [("applicant.full_name", blank)]).codes == ("REQUIRED_FIELD_MISSING",)
    assert _validate("DOC-01", 1, [("applicant.pan_card_number", None)]).is_valid
    assert _validate("DOC-02", 1, [("proposer_name", "")]).codes == ("REQUIRED_FIELD_MISSING",)
    assert _validate("DOC-03", 1, [("patient_name", None)]).codes == ("REQUIRED_FIELD_MISSING",)


@pytest.mark.parametrize("path, value", [
    ("applicant.pan_card_number", 12345), ("applicant.date_of_birth", 20250101), ("applicant.phone_number", 9876543210),
    ("bank_details.ifsc_code", True), ("applicant.aadhaar_number", 1.5),
])
def test_a_text_field_given_a_non_string_is_a_type_error_not_a_silent_pass(path, value):
    """``validate_pan(12345)`` returns (None, None) (no error): the validator would let it through as 'absent'."""
    assert _validate("DOC-01", 1, [(path, value)]).codes == ("INVALID_VALUE_TYPE",)


def test_doc01_scalar_kinds_come_from_the_real_schema_model():
    assert _validate("DOC-01", 1, [("account_details.opt_in_services.debit_card", "yes")]).codes == ("INVALID_VALUE_TYPE",)
    assert _validate("DOC-01", 1, [("account_details.opt_in_services.debit_card", False)]).is_valid


def test_doc02_amounts_shares_terms_and_checkboxes():
    assert _validate("DOC-02", 1, [("sum_insured", "Rs. 10,00,000")]).is_valid
    assert _validate("DOC-02", 1, [("sum_insured", "abc")]).codes == ("INVALID_AMOUNT_FORMAT",)
    assert _validate("DOC-02", 1, [("policy_term_years", 5)]).is_valid
    assert _validate("DOC-02", 1, [("policy_term_years", "ten")]).codes == ("INVALID_VALUE_TYPE",)
    assert _validate("DOC-02", 1, [("policy_term_years", 5.5)]).codes == ("INVALID_VALUE_TYPE",)
    assert _validate("DOC-02", 1, [("medical_declaration.has_diabetes", "no")]).codes == ("INVALID_VALUE_TYPE",)
    assert _validate("DOC-02", 1, [("nominees.0.share_percentage", "abc")]).codes == ("INVALID_VALUE_TYPE",)


def test_doc02_nominee_shares_are_checked_as_module_c_checks_them():
    fields = {"nominees.0.share_percentage": _fc(60.0), "nominees.1.share_percentage": _fc(40.0)}
    ok = _validate("DOC-02", 1, [("nominees.1.share_percentage", 40)], fields)
    assert ok.is_valid
    bad = _validate("DOC-02", 1, [("nominees.1.share_percentage", 30)], fields)
    assert bad.outcome is CorrectionOutcome.INVALID
    assert bad.issues[0].code == "ARITHMETIC_MISMATCH" and bad.issues[0].paths == ("nominees.1.share_percentage",)
    assert _validate("DOC-02", 1, [("nominees.1.share_percentage", "40")], fields).is_valid           # numeric text counts
    assert _validate("DOC-02", 1, [("nominees.1.share_percentage", "30")], fields).codes == ("ARITHMETIC_MISMATCH",)
    # a single nominee is never compared (as in Module C); an unrelated correction does not trigger the rule
    assert _validate("DOC-02", 1, [("nominees.0.share_percentage", 10)], {"nominees.0.share_percentage": _fc(60.0)}).is_valid
    assert _validate("DOC-02", 1, [("proposer_name", "SYNTH")], {**fields, "nominees.1.share_percentage": _fc(1.0)}).is_valid


def test_doc03_chronology_uses_the_existing_rule_and_the_extracted_context():
    fields = {"admission_date": _fc("2026-01-10"), "discharge_date": _fc("2026-01-05")}
    still_bad = _validate("DOC-03", 1, [("discharge_date", "2026-01-06")], fields)
    assert still_bad.issues[0].code == "CHRONOLOGY_MISMATCH" and still_bad.issues[0].paths == ("discharge_date",)
    assert _validate("DOC-03", 1, [("discharge_date", "2026-01-12")], fields).is_valid
    both = _validate("DOC-03", 1, [("admission_date", "20/01/2026"), ("discharge_date", "2026-01-12")], fields)
    assert both.issues[0].code == "CHRONOLOGY_MISMATCH" and both.issues[0].paths == ("admission_date", "discharge_date")
    assert _validate("DOC-03", 1, [("admission_date", "garbage")], fields).codes == ("INVALID_DATE_FORMAT",)
    assert _validate("DOC-03", 1, [("discharge_date", None)], fields).is_valid                       # nothing to compare


def test_a_violation_that_no_correction_touches_is_outside_the_result():
    fields = {"admission_date": _fc("2026-01-10"), "discharge_date": _fc("2026-01-05")}
    assert _validate("DOC-03", 1, [("patient_name", "SYNTH")], fields).is_valid


def test_nominee_and_medication_positions_are_accepted_up_to_three_ascii_digits():
    assert _validate("DOC-02", 1, [("nominees.12.name", "SYNTH")]).is_valid
    assert _validate("DOC-03", 1, [("medications.0.dosage", "500mg")]).is_valid
    for bad in (
        "nominees.1234.name", "nominees.\u0661.name", "nominees.x.name", "nominees..name", "nominees.name.0", "nominees.name",
        "nominees.0.1.name", "nominees.0.name.0", "medical_declaration.0.has_diabetes",
    ):
        result = _validate("DOC-02", 1, [(bad, "x")])
        assert result.outcome is CorrectionOutcome.INVALID and result.codes[0] in ("INVALID_FIELD_PATH", "UNKNOWN_FIELD_PATH"), bad


# ---- the static rule table is pinned against ModuleCPipeline itself -------------------------------------------------------

def _payload():
    block = LayoutBlock(
        block_id="b1", element_type=LayoutElementType.PARAGRAPH, text="x", ocr_confidence=0.9,
        bbox_preprocessed=BoundingBox(x0=0, y0=0, x1=10, y1=10), bbox_original=BoundingBox(x0=0, y0=0, x1=10, y1=10),
        reading_order_index=1,
    )
    page = PageLayoutOutput(page_number=1, width_preprocessed=100, height_preprocessed=100, blocks=[block])
    return ModuleBOutputPayload.model_construct(
        document_id="doc_synth", pages=[page],
        classification=ClassificationOutput.model_construct(
            document_type=MVPDocumentType.DOC_01_CASA, classification_confidence=0.9,
            domain=MVPClassificationDomain.BFSI if hasattr(MVPClassificationDomain, "BFSI") else None,
        ),
    )


def _run_pipeline(doc_type, data):
    """Run Module C's real per-schema processing with ``_create_field_entry`` capturing (name, value, error, required)."""
    captured, flags = {}, []
    pipeline = object.__new__(ModuleCPipeline)

    def capture(name, value, payload, flags_, error=None, required=False):
        captured[name] = (value, error, required)
        return ExtractedField(value=value)

    pipeline._create_field_entry = capture
    fields = {}
    if doc_type == "DOC-01":
        pipeline._process_doc_01(data, None, fields, flags)
        pipeline._process_doc_01_v15_fields(data, None, fields, flags)
    elif doc_type == "DOC-02":
        pipeline._process_doc_02(data, None, fields, flags)
    else:
        pipeline._process_doc_03(data, None, fields, flags)
    return captured, flags


def test_the_static_field_errors_equal_the_errors_module_c_itself_produces():
    bad = CASASchema(
        applicant=ApplicantDetails(pan_card_number="12345ABCDE", date_of_birth="not a date", phone_number="12", aadhaar_number="123"),
        account_details=AccountDetails(initial_deposit_amount=None),
        bank_details=BankDetails(ifsc_code="BAD"),
    )
    captured, _ = _run_pipeline("DOC-01", bad)
    for path, value in (
        ("applicant.pan_card_number", "12345ABCDE"), ("applicant.date_of_birth", "not a date"), ("applicant.phone_number", "12"),
        ("applicant.aadhaar_number", "123"), ("bank_details.ifsc_code", "BAD"),
    ):
        pipeline_error = captured[path][1]
        assert pipeline_error is not None
        assert _validate("DOC-01", 1, [(path, value)]).codes == (pipeline_error,), path
    good = CASASchema(applicant=ApplicantDetails(pan_card_number="ABCDE1234F", date_of_birth="2026-01-01", phone_number="9876543210"))
    captured, _ = _run_pipeline("DOC-01", good)
    assert captured["applicant.pan_card_number"][1] is None and captured["applicant.phone_number"][1] is None
    assert _validate("DOC-01", 1, [("applicant.pan_card_number", "ABCDE1234F"), ("applicant.phone_number", "9876543210")]).is_valid


def test_the_static_required_fields_equal_the_ones_module_c_requires():
    for doc_type, data in (
        ("DOC-01", CASASchema()), ("DOC-02", InsuranceApplicationSchema()), ("DOC-03", DischargeSummarySchema()),
    ):
        captured, _ = _run_pipeline(doc_type, data)
        pipeline_required = {name for name, (_v, _e, required) in captured.items() if required}
        assert pipeline_required == set(module._STATIC_REQUIRED[doc_type]), doc_type
        for path in captured:
            if "." in path and path.split(".")[1].isdigit():
                continue
            blank = _validate(doc_type, 1, [(path, None)])
            assert (blank.codes == ("REQUIRED_FIELD_MISSING",)) == (path in pipeline_required), (doc_type, path)


def test_the_cross_field_codes_equal_module_cs_flags():
    _, flags = _run_pipeline("DOC-03", DischargeSummarySchema(admission_date="2026-02-01", discharge_date="2026-01-01"))
    assert [f.code for f in flags] == ["CHRONOLOGY_MISMATCH"]
    mine = _validate("DOC-03", 1, [("discharge_date", "2026-01-01")], {"admission_date": _fc("2026-02-01")})
    assert mine.codes == ("CHRONOLOGY_MISMATCH",)
    shares = InsuranceApplicationSchema(nominees=[NomineeDetails(share_percentage=60.0), NomineeDetails(share_percentage=30.0)])
    _, flags = _run_pipeline("DOC-02", shares)
    assert [f.code for f in flags] == ["ARITHMETIC_MISMATCH"]
    mine = _validate("DOC-02", 1, [("nominees.1.share_percentage", 30.0)], {"nominees.0.share_percentage": _fc(60.0)})
    assert mine.codes == ("ARITHMETIC_MISMATCH",)


def test_every_static_validator_path_is_a_declared_schema_path():
    from doc_extractor.api.contracts.extraction import schema_field_paths

    for doc_type, table in module._STATIC_VALIDATORS.items():
        declared = schema_field_paths(SCHEMA_MAP[doc_type])
        assert set(table) <= declared, doc_type
        assert set(module._STATIC_REQUIRED[doc_type]) <= declared


# ============================================================================
# Exact schema version, controlled failures
# ============================================================================

def test_a_static_schema_is_available_only_in_version_one():
    assert _validate("DOC-01", "1.0.0", [("applicant.full_name", "x")]).is_valid
    assert _validate("DOC-01", 1, [("applicant.full_name", "x")]).is_valid
    for version in (2, "2.0.0", 99):
        result = _validate("DOC-01", version, [("applicant.full_name", "x")])
        assert result.outcome is CorrectionOutcome.REJECTED and result.codes == ("SCHEMA_VERSION_NOT_FOUND",)
        assert result.schema_version is None and result.validated_paths == ()


@pytest.mark.parametrize("version, code", [
    (None, "SCHEMA_VERSION_REQUIRED"), ("", "SCHEMA_VERSION_REQUIRED"), ("  ", "SCHEMA_VERSION_REQUIRED"),
    ("1.2.0", "SCHEMA_VERSION_INVALID"), ("v1", "SCHEMA_VERSION_INVALID"), (0, "SCHEMA_VERSION_INVALID"),
    (True, "SCHEMA_VERSION_INVALID"), ("latest", "SCHEMA_VERSION_INVALID"), ("active", "SCHEMA_VERSION_INVALID"),
])
def test_a_missing_or_malformed_version_is_a_controlled_rejection_never_a_guess(version, code):
    for schema_id in ("DOC-01", "custom_schema"):
        result = _validate(schema_id, version, [("applicant.full_name", "x")])
        assert result.outcome is CorrectionOutcome.REJECTED and result.codes == (code,)
        assert result.validated_paths == () and result.schema_version is None


@pytest.mark.parametrize("schema_id, code", [
    (None, "SCHEMA_ID_REQUIRED"), ("", "SCHEMA_ID_REQUIRED"), ("  ", "SCHEMA_ID_REQUIRED"),
    ("a/b", "SCHEMA_ID_INVALID"), ("..", "SCHEMA_ID_INVALID"), ("x" * 65, "SCHEMA_ID_INVALID"), (5, "SCHEMA_ID_INVALID"),
    ("John Doe 4111", "SCHEMA_ID_INVALID"),
])
def test_a_missing_or_malformed_schema_id_is_a_controlled_rejection_and_is_not_echoed(schema_id, code):
    result = _validate(schema_id, 1, [("x", 1)])
    assert result.outcome is CorrectionOutcome.REJECTED and result.codes == (code,)
    assert result.schema_id is None and "John" not in repr(result)


def test_a_rejection_never_echoes_even_a_well_formed_schema_id():
    for version in (None, "1.2.0", 99):
        result = _validate("SYNTH-SECRET-VALUE-731", version, [])
        assert result.outcome is CorrectionOutcome.REJECTED and result.schema_id is None and SECRET not in repr(result)


def test_a_blank_tenant_and_a_bad_field_context_are_controlled_rejections():
    for tenant in (None, "", "  ", 5):
        assert _validate("DOC-01", 1, [], tenant=tenant).codes == ("TENANT_REQUIRED",)
    result = VALIDATOR.validate(tenant_id=TENANT, schema_id="DOC-01", schema_version=1, fields=None, corrections=[])   # type: ignore[arg-type]
    assert result.outcome is CorrectionOutcome.REJECTED and result.codes == ("FIELD_CONTEXT_INVALID",)
    result = VALIDATOR.validate(tenant_id=TENANT, schema_id="DOC-01", schema_version=1, fields={"a": {"value": 1}}, corrections=[])  # type: ignore[dict-item]
    assert result.codes == ("FIELD_CONTEXT_INVALID",)


def test_no_corrections_against_an_exact_schema_is_vacuously_valid():
    result = _validate("DOC-01", 1, [])
    assert result.is_valid and result.validated_paths == ()


# ---- dynamic schemas -----------------------------------------------------------------------------------------------------

def _definition(schema_id, version, tenant, *, strict_pan, status=SchemaStatus.APPROVED):
    fields = [
        FieldDefinition(name="full_name", type=FieldType.STRING, required=True),
        FieldDefinition(name="pan", type=FieldType.STRING, format="^[A-Z]{5}[0-9]{4}[A-Z]$" if strict_pan else None),
        FieldDefinition(name="age", type=FieldType.INTEGER),
        FieldDefinition(name="consent", type=FieldType.BOOLEAN),
        FieldDefinition(name="tier", type=FieldType.ENUM, allowed_values=["gold", "silver"]),
        FieldDefinition(name="start_date", type=FieldType.DATE),
        FieldDefinition(name="end_date", type=FieldType.DATE),
        FieldDefinition(name="deposit", type=FieldType.DECIMAL),
        FieldDefinition(name="holder", type=FieldType.OBJECT, fields=[FieldDefinition(name="city", type=FieldType.STRING)]),
    ]
    rules = [
        RuleDefinition(id="R1", expression="start_date <= end_date", target_fields=["start_date", "end_date"],
                       error_code="CHRONOLOGY_MISMATCH", description=f"quotes {SECRET}"),
        RuleDefinition(id="R2", expression="deposit >= 1000", target_fields=["deposit"], error_code="BELOW_MINIMUM"),
        RuleDefinition(id="R3", expression="age >= 18", target_fields=["age"], error_code="bad code with spaces"),
    ]
    return SchemaDefinition(schema_id=schema_id, version=version, status=status, tenant_id=tenant, fields=fields, rules=rules)


def _snapshot(schema_id, version, tenant, *, strict_pan, status=SchemaStatus.APPROVED):
    return SchemaSnapshot(
        schema_id=schema_id, tenant_id=tenant, version=version, status=status,
        definition=_definition(schema_id, version, tenant, strict_pan=strict_pan, status=status),
        author="synthetic_author", created_at=NOW,
    )


class _SpySource:
    """A schema source that records every call and FORBIDS the active, latest and list lookups."""

    def __init__(self, repository):
        self._repository = repository
        self.calls = []

    def get_snapshot(self, schema_id, version, tenant_id):
        self.calls.append(("get_snapshot", schema_id, version, tenant_id))
        return self._repository.get_snapshot(schema_id, version, tenant_id)

    def get_active_version(self, *a, **k):
        raise AssertionError("the active version must never be consulted")

    def get_latest_version(self, *a, **k):
        raise AssertionError("the latest version must never be consulted")

    def list_versions(self, *a, **k):
        raise AssertionError("versions must never be listed")


@pytest.fixture
def dynamic():
    repository = InMemorySchemaRepository()
    repository.save_snapshot(_snapshot("synth_schema", 1, TENANT, strict_pan=False))
    repository.save_snapshot(_snapshot("synth_schema", 2, TENANT, strict_pan=True))              # the newer, stricter version
    repository.save_snapshot(_snapshot("other_schema", 1, OTHER_TENANT, strict_pan=False))
    spy = _SpySource(repository)
    return spy, CorrectionValidator(spy)


def test_dynamic_validation_uses_exactly_the_requested_version_and_never_the_newer_one(dynamic):
    spy, validator = dynamic
    lax = _validate("synth_schema", 1, [("pan", "not-a-pan")], validator=validator)
    strict = _validate("synth_schema", 2, [("pan", "not-a-pan")], validator=validator)
    assert lax.is_valid and lax.schema_version == 1                                              # version 1 has no format
    assert strict.outcome is CorrectionOutcome.INVALID and strict.codes == ("INVALID_PAN_FORMAT",) and strict.schema_version == 2
    assert [c[:3] for c in spy.calls] == [("get_snapshot", "synth_schema", 1), ("get_snapshot", "synth_schema", 2)]
    assert all(call[3] == TENANT for call in spy.calls)


def test_a_missing_version_does_not_fall_back_to_another_version(dynamic):
    spy, validator = dynamic
    for version in (3, 99, "3.0.0"):
        result = _validate("synth_schema", version, [("pan", "ABCDE1234F")], validator=validator)
        assert result.outcome is CorrectionOutcome.REJECTED and result.codes == ("SCHEMA_VERSION_NOT_FOUND",)
        assert result.validated_paths == () and result.schema_version is None
    assert {call[2] for call in spy.calls} == {3, 99}                                           # only the exact lookups, nothing else


def test_an_unrounded_version_string_is_refused_before_any_lookup(dynamic):
    spy, validator = dynamic
    result = _validate("synth_schema", "2.5.0", [("pan", "x")], validator=validator)
    assert result.codes == ("SCHEMA_VERSION_INVALID",) and spy.calls == []                     # "2.5.0" is NOT version 2


def test_another_tenants_schema_is_unreachable(dynamic):
    spy, validator = dynamic
    result = _validate("other_schema", 1, [("pan", "x")], validator=validator)
    assert result.outcome is CorrectionOutcome.REJECTED and result.codes == ("SCHEMA_VERSION_NOT_FOUND",)
    assert _validate("other_schema", 1, [("pan", "x")], validator=validator, tenant=OTHER_TENANT).is_valid


def test_without_a_schema_source_only_the_static_schemas_are_available():
    result = _validate("synth_schema", 1, [("pan", "x")])
    assert result.outcome is CorrectionOutcome.REJECTED and result.codes == ("SCHEMA_SOURCE_UNAVAILABLE",)
    assert _validate("DOC-01", 1, [("applicant.full_name", "x")]).is_valid


def test_a_static_id_is_never_looked_up_in_the_schema_source(dynamic):
    spy, validator = dynamic
    assert _validate("DOC-01", 1, [("applicant.full_name", "x")], validator=validator).is_valid
    assert spy.calls == []


def test_a_source_fault_is_a_controlled_rejection_and_its_text_is_never_exposed(caplog):
    class Broken:
        def get_snapshot(self, *a):
            raise RuntimeError(f"db password {SECRET}")

    with caplog.at_level(logging.DEBUG):
        result = _validate("synth_schema", 1, [("pan", "x")], validator=CorrectionValidator(Broken()))
    assert result.outcome is CorrectionOutcome.REJECTED and result.codes == ("SCHEMA_RESOLUTION_FAILED",)
    assert SECRET not in repr(result) and SECRET not in caplog.text and "password" not in caplog.text
    assert "exception=RuntimeError" in caplog.text


@pytest.mark.parametrize("field, wrong", [("version", 9), ("schema_id", "different"), ("tenant_id", OTHER_TENANT)])
def test_a_source_that_returns_a_different_schema_is_a_resolution_failure(field, wrong):
    snapshot = _snapshot("synth_schema", 1, TENANT, strict_pan=False)
    forged = type("Forged", (), {"schema_id": snapshot.schema_id, "version": snapshot.version, "tenant_id": snapshot.tenant_id,
                                 "definition": snapshot.definition})()
    setattr(forged, field, wrong)

    class Source:
        def get_snapshot(self, *a):
            return forged

    result = _validate("synth_schema", 1, [("pan", "x")], validator=CorrectionValidator(Source()))
    assert result.outcome is CorrectionOutcome.REJECTED and result.codes == ("SCHEMA_RESOLUTION_FAILED",)


def test_a_snapshot_that_cannot_be_compiled_is_a_resolution_failure():
    class Source:
        def get_snapshot(self, *a):
            return type("S", (), {"schema_id": "synth_schema", "version": 1, "tenant_id": TENANT, "definition": object()})()

    result = _validate("synth_schema", 1, [("pan", "x")], validator=CorrectionValidator(Source()))
    assert result.outcome is CorrectionOutcome.REJECTED and result.codes == ("SCHEMA_RESOLUTION_FAILED",)


def test_the_snapshots_lifecycle_status_is_not_a_condition(dynamic):
    spy, validator = dynamic
    repository = InMemorySchemaRepository()
    for version, status in ((1, SchemaStatus.RETIRED), (2, SchemaStatus.DRAFT)):
        repository.save_snapshot(_snapshot("status_schema", version, TENANT, strict_pan=True, status=status))
    local = CorrectionValidator(repository)
    assert _validate("status_schema", 1, [("pan", "ABCDE1234F")], validator=local).is_valid
    assert _validate("status_schema", 2, [("pan", "ABCDE1234F")], validator=local).is_valid


def test_dynamic_field_checks(dynamic):
    _, validator = dynamic
    ok = _validate("synth_schema", 2, [
        ("full_name", "SYNTH"), ("pan", "ABCDE1234F"), ("age", "42"), ("consent", True), ("tier", "gold"),
        ("start_date", "01/02/2026"), ("end_date", "2026-03-01"), ("deposit", "Rs. 5,000"), ("holder.city", "SYNTH CITY"),
    ], validator=validator)
    assert ok.is_valid, ok.issues
    for path, value, code in (
        ("age", "abc", "INVALID_VALUE_TYPE"), ("age", 4.5, "INVALID_VALUE_TYPE"), ("age", True, "INVALID_VALUE_TYPE"),
        ("consent", "yes", "INVALID_VALUE_TYPE"), ("consent", 1, "INVALID_VALUE_TYPE"),
        ("tier", "bronze", "VALUE_NOT_ALLOWED"), ("start_date", "garbage", "INVALID_DATE_FORMAT"),
        ("pan", "bad", "INVALID_PAN_FORMAT"), ("deposit", "abc", "INVALID_VALUE_TYPE"),
    ):
        result = _validate("synth_schema", 2, [(path, value)], validator=validator)
        assert result.codes[0] == code, (path, value, result.codes)
    assert _validate("synth_schema", 2, [("full_name", None)], validator=validator).codes == ("REQUIRED_FIELD_MISSING",)
    assert _validate("synth_schema", 2, [("tier", None)], validator=validator).is_valid


def test_dynamic_cross_field_rules_run_on_the_merged_context_and_report_codes_only(dynamic):
    _, validator = dynamic
    fields = {"end_date": _fc("2026-01-01"), "deposit": _fc(5000.0), "age": _fc(30)}
    result = _validate("synth_schema", 2, [("start_date", "2026-02-01")], fields, validator=validator)
    assert result.outcome is CorrectionOutcome.INVALID
    assert result.issues[0].code == "CHRONOLOGY_MISMATCH" and result.issues[0].paths == ("start_date",)
    assert SECRET not in repr(result)                                                          # the rule description is dropped
    assert _validate("synth_schema", 2, [("start_date", "2025-12-01")], fields, validator=validator).is_valid
    below = _validate("synth_schema", 2, [("deposit", 10)], fields, validator=validator)
    assert below.codes == ("BELOW_MINIMUM",) and below.issues[0].paths == ("deposit",)


def test_a_rule_code_that_is_not_a_token_is_replaced_by_the_fixed_fallback(dynamic):
    _, validator = dynamic
    result = _validate("synth_schema", 2, [("age", 10)], validator=validator)
    assert result.codes == ("RULE_VIOLATION",)                                                 # "bad code with spaces" never leaves


def test_only_rules_that_concern_a_corrected_field_are_reported(dynamic):
    _, validator = dynamic
    fields = {"start_date": _fc("2026-05-01"), "end_date": _fc("2026-01-01"), "age": _fc(3)}      # two violations already present
    assert _validate("synth_schema", 2, [("full_name", "SYNTH")], fields, validator=validator).is_valid
    assert _validate("synth_schema", 2, [("end_date", "2026-02-01")], fields, validator=validator).codes == ("CHRONOLOGY_MISMATCH",)


def test_an_object_field_is_not_a_correction_target(dynamic):
    _, validator = dynamic
    result = _validate("synth_schema", 2, [("holder", "x")], validator=validator)
    assert result.outcome is CorrectionOutcome.INVALID and result.codes == ("FIELD_NOT_CORRECTABLE",)
    assert result.issues[0].paths == ("holder",)


# ---- dynamic numeric corrections: the comparison rules judge the NORMALIZED number (review finding F2) ----------------------

@pytest.mark.parametrize("value", ["₹500", "INR 500", "USD 500", "$500", "₹999", "£1", "₹ 999", 500, "500", "500.00", 999.99, 0])
def test_a_formatted_deposit_below_the_minimum_is_judged_by_its_number_not_skipped(dynamic, value):
    """``deposit >= 1000``: a currency symbol or code must not bypass the comparison (it used to: float("₹500") failed)."""
    _, validator = dynamic
    result = _validate("synth_schema", 2, [("deposit", value)], validator=validator)
    assert result.outcome is CorrectionOutcome.INVALID
    assert result.codes == ("BELOW_MINIMUM",) and result.issues[0].paths == ("deposit",), (value, result.issues)
    assert result.validated_paths == ("deposit",) and str(value) not in repr(result)


@pytest.mark.parametrize("value", ["₹1,500", "INR 1,500", "USD 2000", "$1,000", "₹1,000", "1,000", 1000, "1000", "1500.50", 5000.0])
def test_a_formatted_deposit_at_or_above_the_minimum_passes(dynamic, value):
    _, validator = dynamic
    result = _validate("synth_schema", 2, [("deposit", value)], validator=validator)
    assert result.outcome is CorrectionOutcome.VALID and result.issues == (), (value, result.issues)
    assert result.validated_paths == ("deposit",)


@pytest.mark.parametrize("value", ["1,0", "1,7", "10", 10, 17, "17"])
def test_a_formatted_age_below_the_minimum_is_judged_by_its_number(dynamic, value):
    """``age >= 18`` (the rule's code is not a token, so the fixed fallback is reported): "1,0" is ten, not unjudged."""
    _, validator = dynamic
    result = _validate("synth_schema", 2, [("age", value)], validator=validator)
    assert result.outcome is CorrectionOutcome.INVALID
    assert result.codes == ("RULE_VIOLATION",) and result.issues[0].paths == ("age",), (value, result.issues)


@pytest.mark.parametrize("value", ["1,8", "2,5", "18", 18, 42, "42"])
def test_a_formatted_age_at_or_above_the_minimum_passes(dynamic, value):
    _, validator = dynamic
    assert _validate("synth_schema", 2, [("age", value)], validator=validator).is_valid, value


def test_normalization_and_validation_failures_are_still_rejected_with_their_own_codes(dynamic):
    """Unchanged: a value that cannot be normalized, or is not a number, is refused by its own code (not by a rule)."""
    _, validator = dynamic
    for path, value, code in (
        ("deposit", "₹abc", "INVALID_AMOUNT_FORMAT"),          # a symbol present, normalization fails: the validator error code
        ("deposit", "Rs. 500", "INVALID_VALUE_TYPE"),          # no recognized symbol, not a number
        ("deposit", "abc", "INVALID_VALUE_TYPE"),
        ("age", "₹abc", "INVALID_AMOUNT_FORMAT"),
        ("age", 4.5, "INVALID_VALUE_TYPE"),                    # a decimal for an integer field
    ):
        result = _validate("synth_schema", 2, [(path, value)], validator=validator)
        assert result.codes[0] == code and result.issues[0].paths == (path,), (path, value, result.issues)
        if isinstance(value, str):                              # a text that is not a number cannot be compared by any rule
            assert "BELOW_MINIMUM" not in result.codes and "RULE_VIOLATION" not in result.codes, (path, value, result.codes)


def test_the_rules_receive_the_normalized_number_for_numeric_fields_and_the_raw_value_otherwise(dynamic, monkeypatch):
    """Direct proof of the rule context: numbers for integer and decimal fields, the raw reading for every other field."""
    import doc_extractor.rule_evaluator as evaluator

    seen = {}
    real = evaluator.evaluate_dynamic_rules

    def capture(rules, flat_dict, fields_envelope, flags):
        seen.update(flat_dict)
        return real(rules=rules, flat_dict=flat_dict, fields_envelope=fields_envelope, flags=flags)

    monkeypatch.setattr(evaluator, "evaluate_dynamic_rules", capture)
    _, validator = dynamic
    original = {"end_date": _fc("2026-03-01"), "deposit": _fc(5000.0)}
    snapshot = {key: field.model_dump() for key, field in original.items()}
    result = _validate("synth_schema", 2, [
        ("deposit", "₹1,500"), ("age", "1,8"), ("start_date", "01/02/2026"), ("full_name", "  Synth Name  "), ("consent", True),
    ], original, validator=validator)
    assert result.is_valid, result.issues
    assert seen["deposit"] == 1500.0 and isinstance(seen["deposit"], float)
    assert seen["age"] == 18 and isinstance(seen["age"], int) and not isinstance(seen["age"], bool)
    assert seen["start_date"] == "01/02/2026"                                  # a date stays the reviewer's raw reading
    assert seen["full_name"] == "  Synth Name  " and seen["consent"] is True and seen["end_date"] == "2026-03-01"
    assert {key: field.model_dump() for key, field in original.items()} == snapshot       # the source field set is untouched


def test_a_plain_numeric_correction_is_unchanged_by_the_normalization_context(dynamic, monkeypatch):
    import doc_extractor.rule_evaluator as evaluator

    seen = {}
    real = evaluator.evaluate_dynamic_rules
    monkeypatch.setattr(
        evaluator, "evaluate_dynamic_rules",
        lambda rules, flat_dict, fields_envelope, flags: (seen.update(flat_dict), real(rules, flat_dict, fields_envelope, flags))[1],
    )
    _, validator = dynamic
    assert _validate("synth_schema", 2, [("deposit", 2500), ("age", "30")], validator=validator).is_valid
    assert seen["deposit"] == 2500 and seen["age"] in ("30", 30)               # whatever a plain number was, it still compares


def test_a_validator_error_keeps_the_raw_value_in_the_rule_context(dynamic, monkeypatch):
    """When normalization reports an error the value is not replaced (the correction is rejected by its own code)."""
    import doc_extractor.rule_evaluator as evaluator

    seen = {}
    real = evaluator.evaluate_dynamic_rules
    monkeypatch.setattr(
        evaluator, "evaluate_dynamic_rules",
        lambda rules, flat_dict, fields_envelope, flags: (seen.update(flat_dict), real(rules, flat_dict, fields_envelope, flags))[1],
    )
    _, validator = dynamic
    result = _validate("synth_schema", 2, [("deposit", "₹abc")], validator=validator)
    assert result.codes[0] == "INVALID_AMOUNT_FORMAT" and seen["deposit"] == "₹abc"


def test_a_numeric_field_whose_format_check_fails_keeps_the_raw_value_in_the_rule_context(monkeypatch):
    """Normalization to a number succeeded ("1,0" -> 10) but the schema's format check then failed: the number is NOT used."""
    import doc_extractor.rule_evaluator as evaluator

    seen = {}
    real = evaluator.evaluate_dynamic_rules
    monkeypatch.setattr(
        evaluator, "evaluate_dynamic_rules",
        lambda rules, flat_dict, fields_envelope, flags: (seen.update(flat_dict), real(rules, flat_dict, fields_envelope, flags))[1],
    )
    fields = [FieldDefinition(name="qty", type=FieldType.INTEGER, format="^[A-Z]+$")]          # no number can match this format
    rule = RuleDefinition(id="Q1", expression="qty >= 18", target_fields=["qty"], error_code="QTY_TOO_LOW")
    validator, _ = _group_validator(fields, [rule])
    result = _validate("grp", 1, [("qty", "1,0")], validator=validator)
    assert result.outcome is CorrectionOutcome.INVALID and result.codes[0] == "INVALID_FORMAT"
    assert seen["qty"] == "1,0"                                                    # raw, because the validator reported an error


def test_a_formatted_date_still_triggers_the_chronology_rule(dynamic):
    """Non-numeric behavior is unchanged: the raw date is normalized by the chronology rule itself."""
    _, validator = dynamic
    fields = {"end_date": _fc("2026-01-01")}
    result = _validate("synth_schema", 2, [("start_date", "05/02/2026")], fields, validator=validator)
    assert result.codes == ("CHRONOLOGY_MISMATCH",) and result.issues[0].paths == ("start_date",)
    assert _validate("synth_schema", 2, [("start_date", "05/12/2025")], fields, validator=validator).is_valid


def test_static_numeric_corrections_are_unchanged():
    assert _validate("DOC-02", 1, [("sum_insured", "₹5,00,000"), ("proposer_name", "SYNTH")]).is_valid
    assert _validate("DOC-02", 1, [("sum_insured", "abc")]).codes == ("INVALID_AMOUNT_FORMAT",)
    f = {"nominees.0.share_percentage": _fc(60.0), "nominees.1.share_percentage": _fc(40.0)}
    assert _validate("DOC-02", 1, [("nominees.1.share_percentage", "40")], f).is_valid
    assert _validate("DOC-02", 1, [("nominees.1.share_percentage", "50")], f).codes == ("ARITHMETIC_MISMATCH",)


# ---- dynamic repeating groups: the compiler's own shape is authoritative (review finding F1) -----------------------------------

def _amount_label():
    return [FieldDefinition(name="amount", type=FieldType.DECIMAL), FieldDefinition(name="label", type=FieldType.STRING)]


_GROUP_FORMS = {
    # Every declaration the compiler turns into a REPEATING GROUP of objects ...
    "items_list": lambda: [FieldDefinition(name="items", type=FieldType.ARRAY, items=_amount_label())],
    "items_object": lambda: [FieldDefinition(name="items", type=FieldType.ARRAY,
                                             items=FieldDefinition(name="item", type=FieldType.OBJECT, fields=_amount_label()))],
    "items_is_array": lambda: [FieldDefinition(name="items", type=FieldType.STRING, is_array=True, items=_amount_label())],
    "items_bracket": lambda: [FieldDefinition(name="items[]", type=FieldType.ARRAY),
                              FieldDefinition(name="items[].amount", type=FieldType.DECIMAL),
                              FieldDefinition(name="items[].label", type=FieldType.STRING)],
}
_GROUP_DATA = {"items": [{"amount": 10.0, "label": "a"}, {"amount": 20.0, "label": "b"}]}


def _group_validator(fields, rules=()):
    definition = SchemaDefinition(schema_id="grp", version=1, status=SchemaStatus.APPROVED, tenant_id=TENANT,
                                  fields=fields, rules=list(rules))
    repository = InMemorySchemaRepository()
    repository.save_snapshot(SchemaSnapshot(schema_id="grp", tenant_id=TENANT, version=1, status=SchemaStatus.APPROVED,
                                            definition=definition, author="synthetic_author", created_at=NOW))
    return CorrectionValidator(repository), definition


def _code_of(validator, path, value="5"):
    result = _validate("grp", 1, [(path, value)], validator=validator)
    return "ok" if result.is_valid else result.codes[0]


@pytest.mark.parametrize("form", sorted(_GROUP_FORMS))
def test_indexed_dynamic_object_array_paths_are_correctable(form):
    validator, _ = _group_validator(_GROUP_FORMS[form]())
    for path in ("items.0.amount", "items.1.label", "items.7.amount", "items.999.label"):
        assert _code_of(validator, path, "5" if path.endswith("amount") else "x") == "ok", (form, path)
    result = _validate("grp", 1, [("items.0.amount", "5"), ("items.1.amount", "6")], validator=validator)
    assert result.is_valid and result.validated_paths == ("items.0.amount", "items.1.amount")


@pytest.mark.parametrize("form", sorted(_GROUP_FORMS))
def test_unindexed_and_misplaced_repeating_group_paths_are_refused(form):
    validator, _ = _group_validator(_GROUP_FORMS[form]())
    for path in ("items.amount", "items.label", "items.0.0.amount", "items.amount.0", "items.0.amount.0", "items.1000.amount",
                 "items.x.amount", "items.0.nope", "0.items.amount"):
        assert _code_of(validator, path) == "UNKNOWN_FIELD_PATH", (form, path)
    for path in ("items", "items.0"):                                           # the group itself and one entry are containers
        assert _code_of(validator, path) == "FIELD_NOT_CORRECTABLE", (form, path)


def _orders():
    return [FieldDefinition(name="orders", type=FieldType.ARRAY, items=[
        FieldDefinition(name="id", type=FieldType.STRING),
        FieldDefinition(name="lines", type=FieldType.ARRAY, items=[FieldDefinition(name="amount", type=FieldType.DECIMAL)]),
    ])]


def _holder():
    return [FieldDefinition(name="holder", type=FieldType.OBJECT, fields=[
        FieldDefinition(name="lines", type=FieldType.ARRAY, items=[FieldDefinition(name="amount", type=FieldType.DECIMAL)]),
    ])]


def test_nested_repeating_groups_need_a_position_after_each_group():
    validator, _ = _group_validator(_orders())
    for path, value in (("orders.0.id", "o1"), ("orders.3.id", "o1"), ("orders.0.lines.0.amount", "5"),
                        ("orders.3.lines.2.amount", "5"), ("orders.12.lines.999.amount", "5")):
        assert _code_of(validator, path, value) == "ok", path
    for path in ("orders.id", "orders.lines.amount", "orders.0.lines.amount", "orders.lines.0.amount", "orders.0.0.lines.0.amount",
                 "orders.0.lines.0.0.amount", "orders.amount"):
        assert _code_of(validator, path) == "UNKNOWN_FIELD_PATH", path
    for path in ("orders", "orders.0", "orders.0.lines"):                       # a group or an entry of the outer group
        assert _code_of(validator, path) == "FIELD_NOT_CORRECTABLE", path
    assert _code_of(validator, "orders.0.lines.1") == "UNKNOWN_FIELD_PATH"      # an entry of a nested group: refused all the same


def test_a_repeating_group_inside_an_object_is_validated_the_same_way():
    validator, _ = _group_validator(_holder())
    assert _code_of(validator, "holder.lines.0.amount") == "ok"
    assert _code_of(validator, "holder.lines.amount") == "UNKNOWN_FIELD_PATH"
    assert _code_of(validator, "holder.0.lines.0.amount") == "UNKNOWN_FIELD_PATH"
    assert _code_of(validator, "holder") == "FIELD_NOT_CORRECTABLE" and _code_of(validator, "holder.lines") == "FIELD_NOT_CORRECTABLE"


def test_a_field_declared_as_an_array_with_fields_is_a_plain_object_as_the_compiler_builds_it():
    """
    ``type=ARRAY`` with ``fields=`` is compiled as ONE nested object (``fields`` wins), so extraction produces ``items.amount``:
    the validator follows the compiler (the authority on the extraction's shape), not a guess from the declared type.
    """
    validator, definition = _group_validator([FieldDefinition(name="items", type=FieldType.ARRAY, fields=_amount_label())])
    compiled = DynamicSchemaCompiler.compile(definition)
    assert sorted(DynamicSchemaCompiler.flatten(compiled.model_validate({"items": {"amount": 1.0, "label": "a"}}))) == [
        "items.amount", "items.label"]
    assert _code_of(validator, "items.amount") == "ok" and _code_of(validator, "items.label", "x") == "ok"
    assert _code_of(validator, "items.0.amount") == "UNKNOWN_FIELD_PATH"


@pytest.mark.parametrize("name, fields, data", [
    *[(form, _GROUP_FORMS[form], _GROUP_DATA) for form in sorted(_GROUP_FORMS)],
    ("nested", _orders, {"orders": [{"id": "o1", "lines": [{"amount": 1.0}, {"amount": 2.0}]}, {"id": "o2", "lines": [{"amount": 3.0}]}]}),
    ("in_object", _holder, {"holder": {"lines": [{"amount": 1.0}, {"amount": 2.0}]}}),
    ("plain_object", lambda: [FieldDefinition(name="items", type=FieldType.ARRAY, fields=_amount_label())],
     {"items": {"amount": 1.0, "label": "a"}}),
])
def test_every_path_a_real_extraction_produces_is_correctable_and_its_unindexed_form_is_not(name, fields, data):
    """Parity with the compiler: the keys of a flattened, compiled model instance are exactly the correctable paths."""
    validator, definition = _group_validator(fields())
    compiled = DynamicSchemaCompiler.compile(definition)
    flat = DynamicSchemaCompiler.flatten(compiled.model_validate(data))
    assert flat
    for path, raw in flat.items():
        value = 5 if isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool) else "x"      # decimals compile to Decimal
        assert _code_of(validator, path, value) == "ok", (name, path)
        unindexed = ".".join(part for part in path.split(".") if not part.isdigit())
        if unindexed != path:
            assert _code_of(validator, unindexed, value) == "UNKNOWN_FIELD_PATH", (name, unindexed)


_SUM_RULE = RuleDefinition(
    id="sum_items", expression="sum(items[].amount) == total", target_fields=["items[].amount", "total"],
    error_code="ARITHMETIC_MISMATCH", description=f"quotes {SECRET}",
)


def _sum_fields():
    return [FieldDefinition(name="total", type=FieldType.DECIMAL),
            FieldDefinition(name="items", type=FieldType.ARRAY, items=[FieldDefinition(name="amount", type=FieldType.DECIMAL)])]


_SUM_CONTEXT = {"total": _fc(30.0), "items.0.amount": _fc(10.0), "items.1.amount": _fc(20.0)}


def test_arithmetic_validation_runs_across_a_repeating_group():
    validator, _ = _group_validator(_sum_fields(), [_SUM_RULE])
    broken = _validate("grp", 1, [("items.1.amount", 25)], _SUM_CONTEXT, validator=validator)
    assert broken.outcome is CorrectionOutcome.INVALID and broken.codes == ("ARITHMETIC_MISMATCH",)
    assert broken.issues[0].paths == ("items.1.amount",) and SECRET not in repr(broken)
    assert _validate("grp", 1, [("items.1.amount", 20)], _SUM_CONTEXT, validator=validator).is_valid
    # the merged context: two corrections that together restore the sum
    assert _validate("grp", 1, [("items.0.amount", 5), ("items.1.amount", 25)], _SUM_CONTEXT, validator=validator).is_valid
    # correcting the declared total is judged against the entries of the group
    total = _validate("grp", 1, [("total", 99)], _SUM_CONTEXT, validator=validator)
    assert total.codes == ("ARITHMETIC_MISMATCH",) and total.issues[0].paths == ("total",)
    assert _validate("grp", 1, [("total", "₹30")], _SUM_CONTEXT, validator=validator).is_valid
    # a formatted entry is compared by its number
    assert _validate("grp", 1, [("items.1.amount", "₹25")], _SUM_CONTEXT, validator=validator).codes == ("ARITHMETIC_MISMATCH",)
    assert _validate("grp", 1, [("items.1.amount", "₹20")], _SUM_CONTEXT, validator=validator).is_valid


def test_an_added_entry_of_the_group_takes_part_in_the_sum():
    validator, _ = _group_validator(_sum_fields(), [_SUM_RULE])
    result = _validate("grp", 1, [("items.2.amount", 5)], _SUM_CONTEXT, validator=validator)
    assert result.codes == ("ARITHMETIC_MISMATCH",) and result.issues[0].paths == ("items.2.amount",)


def test_an_existing_violation_in_the_group_is_not_reported_for_an_unrelated_correction():
    validator, _ = _group_validator(
        _sum_fields() + [FieldDefinition(name="note", type=FieldType.STRING)], [_SUM_RULE]
    )
    broken_context = {**_SUM_CONTEXT, "total": _fc(999.0)}
    assert _validate("grp", 1, [("note", "x")], broken_context, validator=validator).is_valid


def test_arithmetic_validation_runs_across_a_nested_repeating_group():
    rule = RuleDefinition(
        id="sum_lines", expression="sum(orders[].lines[].amount) == total", target_fields=["orders[].lines[].amount", "total"],
        error_code="ARITHMETIC_MISMATCH",
    )
    validator, _ = _group_validator(_orders() + [FieldDefinition(name="total", type=FieldType.DECIMAL)], [rule])
    context = {"total": _fc(6.0), "orders.0.lines.0.amount": _fc(1.0), "orders.0.lines.1.amount": _fc(2.0),
               "orders.1.lines.0.amount": _fc(3.0)}
    assert _validate("grp", 1, [("orders.1.lines.0.amount", 3)], context, validator=validator).is_valid
    broken = _validate("grp", 1, [("orders.1.lines.0.amount", 4)], context, validator=validator)
    assert broken.codes == ("ARITHMETIC_MISMATCH",) and broken.issues[0].paths == ("orders.1.lines.0.amount",)


def test_scalar_array_elements_stay_non_correctable():
    """Unchanged: a scalar array (and every element of it) is a container, so it is refused with the same fixed code."""
    for fields in (
        [FieldDefinition(name="tags", type=FieldType.ARRAY, item_type=FieldType.STRING)],
        [FieldDefinition(name="tags", type=FieldType.ARRAY, items=FieldDefinition(name="t", type=FieldType.STRING))],
    ):
        validator, definition = _group_validator(fields)
        assert DynamicSchemaCompiler.compile(definition).__field_metadata__["tags"].field_type is FieldType.ARRAY
        for path in ("tags", "tags.0", "tags.5"):
            result = _validate("grp", 1, [(path, "x")], validator=validator)
            assert result.outcome is CorrectionOutcome.INVALID and result.codes == ("FIELD_NOT_CORRECTABLE",), path
            assert result.validated_paths == ()
        for path in ("tags.0.x", "tags.x", "tags.0.0"):
            assert _code_of(validator, path, "x") == "UNKNOWN_FIELD_PATH", path


def test_static_repeating_group_paths_are_unchanged():
    for schema, good, bad in (
        ("DOC-02", ("nominees.0.name", "nominees.12.share_percentage"), ("nominees.name", "nominees.0.0.name", "nominees.name.0")),
        ("DOC-03", ("medications.0.medicine_name",), ("medications.medicine_name",)),
    ):
        for path in good:
            assert _validate(schema, 1, [(path, 10 if "share" in path else "x")]).is_valid, path
        for path in bad:
            assert _validate(schema, 1, [(path, "x")]).codes == ("UNKNOWN_FIELD_PATH",), path
    assert _validate("DOC-02", 1, [("nominees", "x")]).codes == ("FIELD_NOT_CORRECTABLE",)
    assert _validate("DOC-03", 1, [("medications.0", "x")]).codes == ("FIELD_NOT_CORRECTABLE",)


# ============================================================================
# Field paths, correction shapes
# ============================================================================

@pytest.mark.parametrize("path", [
    "", " ", "a b", HOSTILE_PATH, "applicant.", ".applicant", "applicant..full_name", "applicant/full_name", "../x", "a" * 129,
    "applicant.full_name\n", "applicant.full_name ", "applicant\x00", "applicant.١", "applicant.full-name", "applicant[0]",
    None, 5, b"x",
])
def test_malformed_field_paths_are_refused_and_never_echoed(path):
    result = _validate("DOC-01", 1, [(path, "x")])
    assert result.outcome is CorrectionOutcome.INVALID and result.codes == ("INVALID_FIELD_PATH",)
    assert result.issues[0].paths == () and result.validated_paths == ()
    if isinstance(path, str) and path.strip():
        assert path not in repr(result)


@pytest.mark.parametrize("path", ["applicant.nope", "nope", "applicant.full_name.extra", "nominees.0.name", "Applicant.full_name"])
def test_well_formed_paths_the_schema_does_not_declare_are_unknown_and_never_echoed(path):
    result = _validate("DOC-01", 1, [(path, "x")])
    assert result.codes == ("UNKNOWN_FIELD_PATH",) and result.issues[0].paths == ()
    assert f"'{path}'" not in repr(result)                                                         # the client's path is not echoed
    assert result.validated_paths == ()


def test_container_paths_are_not_correctable():
    for path in ("applicant", "account_details", "account_details.opt_in_services"):
        result = _validate("DOC-01", 1, [(path, "x")])
        assert result.codes == ("FIELD_NOT_CORRECTABLE",) and result.issues[0].paths == (path,)
    assert _validate("DOC-02", 1, [("nominees", "x")]).codes == ("FIELD_NOT_CORRECTABLE",)


def test_a_duplicate_correction_is_refused():
    result = _validate("DOC-01", 1, [("applicant.full_name", "A"), ("applicant.full_name", "B")])
    assert result.codes == ("DUPLICATE_CORRECTION",) and result.issues[0].paths == ("applicant.full_name",)
    assert result.validated_paths == ("applicant.full_name",)


def test_non_correction_items_and_oversized_requests_are_refused():
    result = VALIDATOR.validate(tenant_id=TENANT, schema_id="DOC-01", schema_version=1, fields={},
                                corrections=[("applicant.full_name", "x")])                    # type: ignore[list-item]
    assert result.codes == ("INVALID_CORRECTION",)
    many = [ProposedCorrection(f"nominees.{i % 100}.name", "x") for i in range(MAX_CORRECTIONS + 1)]
    result = VALIDATOR.validate(tenant_id=TENANT, schema_id="DOC-02", schema_version=1, fields={}, corrections=many)
    assert result.outcome is CorrectionOutcome.INVALID and result.codes == ("TOO_MANY_CORRECTIONS",)
    result = VALIDATOR.validate(tenant_id=TENANT, schema_id="DOC-02", schema_version=1, fields={}, corrections="x")   # type: ignore[arg-type]
    assert result.codes == ("TOO_MANY_CORRECTIONS",)


@pytest.mark.parametrize("value, code", [
    ({"a": 1}, "INVALID_VALUE_TYPE"), (["a"], "INVALID_VALUE_TYPE"), (b"bytes", "INVALID_VALUE_TYPE"), (object(), "INVALID_VALUE_TYPE"),
    (float("nan"), "INVALID_VALUE_TYPE"), (float("inf"), "INVALID_VALUE_TYPE"), ("x" * 2049, "VALUE_TOO_LONG"),
])
def test_non_scalar_and_oversized_values_are_refused_without_echo(value, code):
    result = _validate("DOC-03", 1, [("diagnosis", value)])
    assert result.codes == (code,) and result.issues[0].paths == ("diagnosis",)
    assert "xxxx" not in repr(result)


def test_a_failure_in_one_correction_does_not_hide_the_others():
    result = _validate("DOC-01", 1, [
        ("applicant.pan_card_number", "bad"), ("applicant.full_name", "SYNTH"), ("applicant.phone_number", "12"), ("nope", 1),
    ])
    assert sorted(result.codes) == ["INVALID_PAN_FORMAT", "INVALID_PHONE_FORMAT", "UNKNOWN_FIELD_PATH"]
    assert result.validated_paths == ("applicant.pan_card_number", "applicant.full_name", "applicant.phone_number")


def test_validation_is_deterministic():
    corrections = [("applicant.pan_card_number", "bad"), ("applicant.date_of_birth", "x"), ("applicant.full_name", None)]
    assert _validate("DOC-01", 1, corrections) == _validate("DOC-01", 1, corrections)


# ============================================================================
# No mutation, no sensitive values
# ============================================================================

def test_the_source_field_set_is_never_mutated(dynamic):
    _, validator = dynamic
    evidence = EvidenceSnippet(page=1, bbox=(1.0, 2.0, 3.0, 4.0), text=SECRET_TEXT)
    fields = {
        "start_date": _fc("2026-05-01", evidence=evidence), "end_date": _fc("2026-01-01", review_required=False, validation="passed"),
        "deposit": _fc(5.0, review_required=False, validation="passed"), "age": _fc(3, review_required=False),
        "full_name": _fc(SECRET, review_required=False),
    }
    before = {key: field.model_dump_json() for key, field in fields.items()}
    identities = {key: id(field) for key, field in fields.items()}
    result = _validate("synth_schema", 2, [("deposit", 10), ("start_date", "2027-01-01"), ("age", 1)], fields, validator=validator)
    assert result.outcome is CorrectionOutcome.INVALID                                         # the rules did run (and flagged)
    assert "CHRONOLOGY_MISMATCH" in result.codes                                               # ...and the evaluator sets review_required on ITS copy
    assert {key: field.model_dump_json() for key, field in fields.items()} == before
    assert {key: id(field) for key, field in fields.items()} == identities
    assert fields["end_date"].review_required is False and fields["end_date"].validation == "passed"
    # static path too
    static = {"admission_date": _fc("2026-02-01", evidence=evidence), "discharge_date": _fc("2026-01-01")}
    snapshot = {key: field.model_dump_json() for key, field in static.items()}
    _validate("DOC-03", 1, [("discharge_date", "2025-01-01")], static)
    assert {key: field.model_dump_json() for key, field in static.items()} == snapshot


def test_corrections_are_not_mutated_and_the_context_is_not_aliased():
    nested = {"a": [1]}
    corrections = [ProposedCorrection("diagnosis", "SYNTH")]
    _validate("DOC-03", 1, [])
    VALIDATOR.validate(tenant_id=TENANT, schema_id="DOC-03", schema_version=1, fields={}, corrections=corrections)
    assert corrections == [ProposedCorrection("diagnosis", "SYNTH")] and nested == {"a": [1]}


def test_results_and_logs_never_contain_values_evidence_or_hostile_text(dynamic, caplog):
    _, validator = dynamic
    evidence = EvidenceSnippet(page=1, bbox=(1.0, 2.0, 3.0, 4.0), text=SECRET_TEXT)
    fields = {"full_name": _fc(SECRET, evidence=evidence), "end_date": _fc("2026-01-01"), "pan": _fc(f"OLD-{SECRET}")}
    results = []
    with caplog.at_level(logging.DEBUG):
        results.append(_validate("synth_schema", 2, [("pan", f"NEW-{SECRET}"), ("start_date", "2027-01-01"), (HOSTILE_PATH, SECRET),
                                                      ("full_name", None), ("age", f"abc-{SECRET}")], fields, validator=validator))
        results.append(_validate("DOC-01", 1, [("applicant.pan_card_number", f"BAD-{SECRET}"), ("applicant.nope", SECRET)]))
        results.append(_validate("DOC-02", 1, [("nominees.0.share_percentage", f"x{SECRET}"), ("sum_insured", SECRET)]))
        results.append(_validate("synth_schema", 9, [("pan", SECRET)], validator=validator))
        results.append(_validate(SECRET, 1, []))
        results.append(_validate("DOC-01", SECRET, []))
    for result in results:
        rendered = repr(result) + json.dumps([i.code for i in result.issues]) + str(result)
        for sensitive in (SECRET, SECRET_TEXT, HOSTILE_PATH, "OLD-", "NEW-"):
            assert sensitive not in rendered, sensitive
    assert SECRET not in caplog.text and SECRET_TEXT not in caplog.text and HOSTILE_PATH not in caplog.text


def test_the_module_logs_only_constant_messages_without_values():
    tree = ast.parse(inspect.getsource(module))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "logger"]
    assert calls
    for call in calls:
        assert isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
        for arg in call.args[1:]:
            assert ast.unparse(arg) == "type(exc).__name__"                                    # the exception CLASS only
    assert not any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print" for n in ast.walk(tree))


def test_the_result_type_exposes_no_value_fields():
    from dataclasses import fields as dataclass_fields

    from doc_extractor.api.services.correction_validation import CorrectionIssue, CorrectionValidationResult

    assert {f.name for f in dataclass_fields(CorrectionValidationResult)} == {
        "outcome", "issues", "schema_id", "schema_version", "validated_paths"}
    assert {f.name for f in dataclass_fields(CorrectionIssue)} == {"code", "paths"}


def test_no_active_or_latest_lookup_exists_in_the_module():
    tree = ast.parse(inspect.getsource(module))
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & {"get_active_version", "get_latest_version", "resolve_active_schema", "list_versions", "to_canonical",
                         "bind_test_schema", "resolve_approved_schema"}
    assert "get_snapshot" in called


def test_a_validator_code_that_is_not_a_token_is_replaced_by_the_fixed_fallback():
    """A reused validator can only return its own fixed codes today; the fallback guards any future free text."""
    spec = module._FieldSpec(
        required=False, container=False, text_only=False, normalize=lambda value: (value, f"free text {SECRET}"),
    )

    class Schema(module._Schema):
        schema_id, version = "synthetic", 1

        def field(self, path):
            return spec

    problem = CorrectionValidator._check_one(Schema(), ProposedCorrection("x", "v"), set(), {})
    assert problem == ("INVALID_VALUE", "x")
    spec_ok = module._FieldSpec(required=False, container=False, text_only=False, normalize=lambda value: (value, "SYNTH_CODE"))
    Schema.field = lambda self, path: spec_ok
    assert CorrectionValidator._check_one(Schema(), ProposedCorrection("x", "v"), set(), {}) == ("SYNTH_CODE", "x")


def test_a_validator_that_raises_is_a_fixed_code_and_its_text_is_never_logged(caplog):
    def boom(value):
        raise RuntimeError(f"validator secret {SECRET}")

    spec = module._FieldSpec(required=False, container=False, text_only=False, normalize=boom)

    class Schema(module._Schema):
        schema_id, version = "synthetic", 1

        def field(self, path):
            return spec

    with caplog.at_level(logging.DEBUG):
        problem = CorrectionValidator._check_one(Schema(), ProposedCorrection("x", "v"), set(), {})
    assert problem == ("INVALID_VALUE", "x") and SECRET not in caplog.text and "exception=RuntimeError" in caplog.text
