"""
Phase 1 / Task 7: missing-required-field handling and evidence integrity.

A schema-required field that no page produced must not discard the rest of the extraction: the
canonical envelope keeps every extracted, grounded field, records each absent required field
(failed validation + review + REQUIRED_FIELD_MISSING) and can never be auto-approved. Genuinely
malformed output and unrecoverable failures still raise. The same behaviour applies to the
structured, dynamic-schema and generic paths.

Real Module A artifacts flow through the real extractor, aggregation, CoordinateTransform, field
assembly and envelope code; only the lowest-level model call is faked (page-aware).
"""

from typing import Any, Dict, List

import pytest

from doc_extractor.contracts_b import MVPDocumentType
from doc_extractor.dynamic_adapter import DynamicExtractionAdapter
from doc_extractor.exceptions import LLMExecutionError
from doc_extractor.extractor import SchemaGuidedExtractor
from doc_extractor.field_assembly import REQUIRED_FIELD_MISSING, assemble_field_entry
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.schemas import InsuranceApplicationSchema
from doc_extractor.vlm_grounding import (
    PageLeaf,
    build_partial_model_from_pages,
    relaxed_schema,
    validate_allowing_missing,
)
from schema_lifecycle.compiler import DynamicSchemaCompiler
from schema_lifecycle.enums import FieldType, SchemaStatus
from schema_lifecycle.metamodel import FieldDefinition, SchemaDefinition

from tests.test_canonical_module_c_orchestration import (  # noqa: F401  (fixtures + helpers)
    _expected_original_bbox,
    _vlm_pipeline,
    casa_run,
    temp_storage,
)
from tests.test_confidence_integrity_and_review_gating import (  # noqa: F401
    NAME_TOKEN,
    PAN_TOKEN,
    _envelope,
    _good_field,
    _ocr_grounded_payload,
    _text_extractor,
)
from tests.test_dynamic_module_c_adapter import lifecycle_manager  # noqa: F401
from tests.test_multipage_extraction import (  # noqa: F401
    GENERIC_PAGES,
    PageAwareVLM,
    VLM_MODEL,
    _doc02_pages,
    _doc02_tokens,
    _run,
    _vlm_pipeline,
)

SCHEMA_ID = "claims_dynamic_schema"


def _schema_def() -> SchemaDefinition:
    return SchemaDefinition(
        schema_id=SCHEMA_ID,
        version=1,
        tenant_id="tenant_alpha",
        domain="BFSI",
        status=SchemaStatus.DRAFT,
        fields=[
            FieldDefinition(name="applicant.full_name", type=FieldType.STRING, required=True),
            FieldDefinition(name="policy_number", type=FieldType.STRING, required=True),
            FieldDefinition(name="issue_date", type=FieldType.DATE, required=True),
            FieldDefinition(name="nominees[].name", type=FieldType.STRING, required=True),
            FieldDefinition(name="nominees[].share_percentage", type=FieldType.DECIMAL, required=False),
            FieldDefinition(name="notes", type=FieldType.STRING, required=False),
        ],
    )


def _box(i: int):
    return [100, 100 * i, 500, 100 * i + 50]            # [x1, y1, x2, y2]


def _leaf(value) -> PageLeaf:
    return PageLeaf(value=value, bbox_norm=None, raw_value=value, uncertain=False)


# Page 1: applicant + notes; page 2: policy number + a nominee; page 3: issue date.
COMPLETE_PAGES: Dict[int, Dict[str, Any]] = {
    1: {"applicant.full_name": {"value": "ANANYA SHARMA", "bbox": _box(1)},
        "notes": {"value": "Renewal", "bbox": _box(2)}},
    2: {"policy_number": {"value": "POL-00123", "bbox": _box(3)},
        "nominees.0.name": {"value": "ROHIT SHARMA", "bbox": _box(4)}},
    3: {"issue_date": {"value": "2024-08-15", "bbox": _box(5)}},
}


def _without(pages: Dict[int, Dict[str, Any]], *paths: str) -> Dict[int, Dict[str, Any]]:
    return {n: {k: v for k, v in page.items() if k not in paths} for n, page in pages.items()}


@pytest.fixture
def three_pages(temp_storage):
    return _run(temp_storage, 3)


def _dynamic(lifecycle_manager, run, monkeypatch, responses, tolerate_failures=False):
    lifecycle_manager.create_schema("tenant_alpha", _schema_def(), author="alice")
    lifecycle_manager.transition_status("tenant_alpha", SCHEMA_ID, 1, SchemaStatus.TESTING, actor="alice")
    vlm = PageAwareVLM(responses).bind(run.refs).install(monkeypatch)
    adapter = DynamicExtractionAdapter(
        manager=lifecycle_manager,
        extractor=SchemaGuidedExtractor(model_name=VLM_MODEL, use_llm=True, allow_fallback=False),
    )
    env = adapter.extract_and_assemble("tenant_alpha", SCHEMA_ID, 1, run.payload, page_images=run.refs)
    return env, vlm


def _missing_flags(env) -> List[str]:
    return sorted(p for f in env.flags if f.code == REQUIRED_FIELD_MISSING for p in f.fields)


# ============================================================================
# Root cause: the validation primitive separates "absent" from "malformed"
# ============================================================================

def test_validation_separates_absent_required_fields_from_malformed_values():
    schema = DynamicSchemaCompiler.compile(_schema_def())

    model, missing = validate_allowing_missing(schema, {"notes": "n", "applicant": {"full_name": "A"}})
    assert sorted(missing) == ["issue_date", "policy_number"]
    assert model.notes == "n" and model.applicant.full_name == "A"          # extracted data survives
    assert model.policy_number is None and model.issue_date is None
    assert isinstance(model, schema)                                         # still the compiled schema

    with pytest.raises(ValueError, match="does not satisfy schema"):         # malformed still fails
        validate_allowing_missing(schema, {"policy_number": "P", "issue_date": "not-a-date-at-all-123"})
    with pytest.raises(ValueError):                                          # wrong type fails even
        validate_allowing_missing(schema, {"nominees": "not-a-list"})        # when fields are absent


def test_a_present_value_is_still_fully_validated_in_the_relaxed_schema():
    schema = DynamicSchemaCompiler.compile(_schema_def())
    relaxed = relaxed_schema(schema)
    assert relaxed is not schema and issubclass(relaxed, schema)
    assert relaxed.model_validate({}).policy_number is None
    with pytest.raises(Exception):
        relaxed.model_validate({"issue_date": "garbage"})


def test_relaxation_covers_repeating_group_items_and_leaves_complete_schemas_unchanged():
    schema = DynamicSchemaCompiler.compile(_schema_def())
    _model, missing = validate_allowing_missing(schema, {"nominees": [{"share_percentage": 40}]})
    assert "nominees.0.name" in missing
    assert relaxed_schema(InsuranceApplicationSchema).model_validate({}).proposer_name is None
    assert relaxed_schema(InsuranceApplicationSchema) is InsuranceApplicationSchema   # nothing required


def test_aggregated_pages_with_a_required_field_on_no_page_return_the_missing_list():
    schema = DynamicSchemaCompiler.compile(_schema_def())
    pages = [(1, {"applicant.full_name": _leaf("A")}), (2, {"notes": _leaf("n")})]
    model, grounding, missing = build_partial_model_from_pages(pages, schema)
    assert sorted(missing) == ["issue_date", "policy_number"]
    assert grounding["applicant.full_name"].page_number == 1 and grounding["notes"].page_number == 2


# ============================================================================
# 1. All required fields present
# ============================================================================

def test_all_required_fields_present_records_nothing_missing(lifecycle_manager, three_pages, monkeypatch):
    env, vlm = _dynamic(lifecycle_manager, three_pages, monkeypatch, COMPLETE_PAGES)

    assert len(vlm.calls) == 3
    assert _missing_flags(env) == []
    assert env.run.provenance.details["required_fields_missing"] == []
    for path in ("applicant.full_name", "policy_number", "issue_date", "nominees.0.name"):
        assert env.fields[path].value is not None and env.fields[path].validation == "passed"
    assert env.fields["issue_date"].evidence.page == 3


# ============================================================================
# 2. One required field missing across all pages / 4. successful fields preserved / 5. needs_review
# ============================================================================

def test_one_required_field_missing_keeps_the_extraction_and_forces_review(
    lifecycle_manager, three_pages, monkeypatch
):
    env, _ = _dynamic(lifecycle_manager, three_pages, monkeypatch, _without(COMPLETE_PAGES, "policy_number"))

    # the extraction is NOT discarded: an envelope is produced ...
    assert env.document_metadata.status == "needs_review"
    # ... the absent field is recorded in every review structure ...
    assert _missing_flags(env) == ["policy_number"]
    absent = env.fields["policy_number"]
    assert absent.value is None and absent.validation == "failed" and absent.review_required is True
    assert absent.null_reason == "absent" and absent.confidence is None and absent.evidence is None
    assert env.run.provenance.details["required_fields_missing"] == ["policy_number"]
    # ... and every successfully grounded field survives with its own page and box
    for path, page, bbox in (
        ("applicant.full_name", 1, _box(1)),
        ("notes", 1, _box(2)),
        ("nominees.0.name", 2, _box(4)),
        ("issue_date", 3, _box(5)),
    ):
        field = env.fields[path]
        assert field.value is not None, path
        assert field.evidence.page == page, path
        assert field.evidence.bbox == pytest.approx(_expected_original_bbox(three_pages.refs[page - 1], bbox)), path


def test_missing_required_field_prevents_approval_even_when_everything_else_is_trusted():
    good = {"a": _good_field("A"), "b": _good_field("B")}
    assert _envelope(dict(good)).document_metadata.status == "auto_approved"           # control

    flags: List = []
    missing = assemble_field_entry(field_name="c", value=None, pages=[], flags=flags, required=True)
    env = _envelope({**good, "c": missing}, flags)

    assert env.document_metadata.status == "needs_review"
    assert [f.code for f in flags] == [REQUIRED_FIELD_MISSING] and flags[0].fields == ["c"]
    assert missing.validation == "failed" and missing.review_required


def test_an_optional_absent_field_is_not_a_missing_required_field():
    flags: List = []
    entry = assemble_field_entry(field_name="x", value=None, pages=[], flags=flags, required=False)
    assert flags == [] and entry.validation == "not_applicable" and entry.review_required is False


# ============================================================================
# 3. Multiple required fields missing
# ============================================================================

def test_multiple_required_fields_missing_are_all_recorded_individually(
    lifecycle_manager, three_pages, monkeypatch
):
    pages = _without(COMPLETE_PAGES, "policy_number", "issue_date", "nominees.0.name")
    pages[2]["nominees.0.share_percentage"] = {"value": "60", "bbox": _box(6)}     # nominee without a name
    env, _ = _dynamic(lifecycle_manager, three_pages, monkeypatch, pages)

    assert _missing_flags(env) == ["issue_date", "nominees.0.name", "policy_number"]
    for path in ("issue_date", "nominees.0.name", "policy_number"):
        assert env.fields[path].validation == "failed" and env.fields[path].value is None
    assert env.fields["nominees.0.share_percentage"].evidence.page == 2           # partial entry kept
    assert env.fields["applicant.full_name"].value == "ANANYA SHARMA"
    assert env.document_metadata.status == "needs_review"
    assert sorted(env.run.provenance.details["required_fields_missing"]) == [
        "issue_date", "nominees.0.name", "policy_number",
    ]


def test_required_field_inside_an_absent_optional_object_is_still_reported(
    lifecycle_manager, three_pages, monkeypatch
):
    env, _ = _dynamic(lifecycle_manager, three_pages, monkeypatch, _without(COMPLETE_PAGES, "applicant.full_name"))
    assert "applicant.full_name" in _missing_flags(env)
    assert env.fields["policy_number"].value == "POL-00123"


# ============================================================================
# 6. Dynamic schemas: text/Ollama-style path and every page missing
# ============================================================================

def test_dynamic_text_path_with_missing_required_fields_is_kept_and_reviewed(
    lifecycle_manager, sample_payload_for_text
):
    lifecycle_manager.create_schema("tenant_alpha", _schema_def(), author="alice")
    lifecycle_manager.transition_status("tenant_alpha", SCHEMA_ID, 1, SchemaStatus.TESTING, actor="alice")

    def caller(payload, schema_cls):
        return {"applicant": {"full_name": "RUPESH KUMAR"}}, "req-1"

    adapter = DynamicExtractionAdapter(
        manager=lifecycle_manager,
        extractor=SchemaGuidedExtractor(
            model_name="ollama/qwen2.5:7b-instruct", use_llm=True, allow_fallback=False, llm_caller=caller
        ),
    )
    env = adapter.extract_and_assemble("tenant_alpha", SCHEMA_ID, 1, sample_payload_for_text)

    assert {"issue_date", "policy_number"} <= set(_missing_flags(env))
    assert env.fields["applicant.full_name"].value == "RUPESH KUMAR"
    assert env.fields["applicant.full_name"].evidence is not None
    assert env.document_metadata.status == "needs_review"


@pytest.fixture
def sample_payload_for_text(temp_storage):
    return _ocr_grounded_payload(temp_storage, [NAME_TOKEN])


def test_dynamic_extraction_with_nothing_found_on_any_page_still_reports_every_required_field(
    lifecycle_manager, three_pages, monkeypatch
):
    env, vlm = _dynamic(lifecycle_manager, three_pages, monkeypatch, {1: {}, 2: {}, 3: {}})

    assert len(vlm.calls) == 3
    assert {"applicant.full_name", "policy_number", "issue_date"} <= set(_missing_flags(env))
    assert env.run.provenance.details["pages_processed"] == [1, 2, 3]     # empty pages are valid
    assert not any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)
    assert env.document_metadata.status == "needs_review"


# ============================================================================
# Genuinely malformed output / unrecoverable failures still raise
# ============================================================================

def test_malformed_value_still_fails_that_page_not_the_missing_field_path(
    lifecycle_manager, three_pages, monkeypatch
):
    pages = _without(COMPLETE_PAGES, "policy_number")
    pages[3] = {"issue_date": {"value": "definitely not a date 99/99/99", "bbox": _box(5)}}
    env, _ = _dynamic(lifecycle_manager, three_pages, monkeypatch, pages)

    # page 3 is a malformed page (coverage failure); policy_number is an absent field (missing)
    failed = env.run.provenance.details["pages_failed"]
    assert [f["page"] for f in failed] == [3]
    assert any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)
    assert {"policy_number", "issue_date"} <= set(_missing_flags(env))


def test_every_page_failing_is_still_an_unrecoverable_error(lifecycle_manager, three_pages, monkeypatch):
    boom = {1: RuntimeError("gpu"), 2: RuntimeError("gpu"), 3: RuntimeError("gpu")}
    with pytest.raises(LLMExecutionError):
        _dynamic(lifecycle_manager, three_pages, monkeypatch, boom)


def test_non_json_and_blank_replies_are_still_failures_not_missing_fields(
    lifecycle_manager, three_pages, monkeypatch
):
    with pytest.raises(LLMExecutionError):
        _dynamic(lifecycle_manager, three_pages, monkeypatch, {1: "not json", 2: "", 3: "[]"})


# ============================================================================
# 7. Structured (static schema) extraction
# ============================================================================

def test_structured_doc01_missing_required_name_keeps_other_fields(casa_run, monkeypatch):
    vlm = PageAwareVLM({1: {"applicant.pan_card_number": {"value": "ABCDE1234F", "bbox": [120, 200, 320, 250]}}})
    vlm.bind(casa_run["page_images"]).install(monkeypatch)

    env = _vlm_pipeline().process(casa_run["payload"], page_images=casa_run["page_images"])

    assert env.fields["applicant.pan_card_number"].value == "ABCDE1234F"
    assert env.fields["applicant.full_name"].value is None
    assert env.fields["applicant.full_name"].validation == "failed"
    assert _missing_flags(env) == ["applicant.full_name"]
    assert env.document_metadata.status == "needs_review"


def test_structured_text_path_missing_required_name_is_flagged_and_reviewed(temp_storage):
    payload = _ocr_grounded_payload(temp_storage, [PAN_TOKEN])
    env = ModuleCPipeline(extractor=_text_extractor({"applicant": {"pan_card_number": "ABCDE1234F"}})).process(payload)

    assert env.fields["applicant.pan_card_number"].value == "ABCDE1234F"
    assert _missing_flags(env) == ["applicant.full_name"]
    assert env.document_metadata.status == "needs_review"


def test_text_path_instructor_response_model_tolerates_absent_required_fields(monkeypatch, temp_storage):
    """Ollama/litellm path: the structured-output model handed to instructor is the relaxed twin."""
    import instructor

    seen: Dict[str, Any] = {}

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create_with_completion(**kwargs):
                    model = kwargs["response_model"]
                    seen["model"] = model
                    return model.model_validate({"notes": "kept"}), None

    monkeypatch.setattr(instructor, "from_litellm", lambda *a, **k: FakeClient)
    schema = DynamicSchemaCompiler.compile(_schema_def())
    payload = _run(temp_storage, 1).payload
    extractor = SchemaGuidedExtractor(
        model_name="ollama/qwen2.5:7b-instruct", use_llm=True, allow_fallback=False
    )
    result = extractor.extract_with_provenance(payload, schema_cls=schema)

    assert issubclass(seen["model"], schema) and seen["model"] is not schema
    assert isinstance(result.payload, schema) and result.payload.notes == "kept"


# ============================================================================
# 8. Generic extraction with missing expected information
# ============================================================================

def test_generic_expected_fields_missing_are_recorded_and_extracted_fields_kept(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    PageAwareVLM(GENERIC_PAGES).bind(run.refs).install(monkeypatch)

    env = _vlm_pipeline().process_generic(
        run.payload, run.refs, "P", document_type="generic", expected_fields=["title", "expiry_date", "terms"]
    )

    assert _missing_flags(env) == ["expiry_date", "terms"]
    for path in ("expiry_date", "terms"):
        assert env.fields[path].value is None and env.fields[path].validation == "failed"
        assert env.fields[path].review_required is True
    assert env.fields["title"].value == "Gold Shield Plan" and env.fields["title"].evidence.page == 1
    assert env.fields["contact"].evidence.page == 2
    assert env.document_metadata.status == "needs_review"


def test_generic_expected_fields_all_present_report_nothing_missing(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    PageAwareVLM(GENERIC_PAGES).bind(run.refs).install(monkeypatch)
    env = _vlm_pipeline().process_generic(
        run.payload, run.refs, "P", document_type="generic", expected_fields=["title", "contact"]
    )
    assert _missing_flags(env) == []
    assert not any(f.code == "NO_FIELDS_EXTRACTED" for f in env.flags)


def test_generic_extraction_that_finds_nothing_is_never_auto_approved(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    PageAwareVLM({}, default={}).bind(run.refs).install(monkeypatch)

    env = _vlm_pipeline().process_generic(run.payload, run.refs, "P", document_type="generic")

    assert env.fields == {}
    assert [f.code for f in env.flags] == ["NO_FIELDS_EXTRACTED"]
    assert env.document_metadata.status == "needs_review"
    assert env.run.provenance.details["pages_processed"] == [1, 2, 3]


def test_empty_envelope_without_any_value_cannot_be_auto_approved():
    assert _envelope({}).document_metadata.status == "needs_review"


# ============================================================================
# 9. Partial coverage combined with missing required fields
# ============================================================================

def test_partial_coverage_and_missing_required_fields_are_both_reported(
    lifecycle_manager, three_pages, monkeypatch
):
    pages = _without(COMPLETE_PAGES, "policy_number")
    pages[3] = RuntimeError("timeout")
    env, _ = _dynamic(lifecycle_manager, three_pages, monkeypatch, pages)

    codes = {f.code for f in env.flags}
    assert {"PARTIAL_PAGE_COVERAGE", REQUIRED_FIELD_MISSING} <= codes
    coverage = next(f for f in env.flags if f.code == "PARTIAL_PAGE_COVERAGE")
    assert "page 3 (RuntimeError: timeout)" in coverage.message
    assert {"policy_number", "issue_date"} <= set(_missing_flags(env))     # issue_date was on failed page 3
    assert env.run.provenance.details["pages_failed"] == [{"page": 3, "reason": "RuntimeError: timeout"}]
    assert env.fields["applicant.full_name"].value == "ANANYA SHARMA"
    assert env.document_metadata.status == "needs_review"


def test_missing_image_reference_is_coverage_failure_plus_missing_field(
    lifecycle_manager, three_pages, monkeypatch
):
    lifecycle_manager.create_schema("tenant_alpha", _schema_def(), author="alice")
    lifecycle_manager.transition_status("tenant_alpha", SCHEMA_ID, 1, SchemaStatus.TESTING, actor="alice")
    PageAwareVLM(COMPLETE_PAGES).bind(three_pages.refs).install(monkeypatch)
    adapter = DynamicExtractionAdapter(
        manager=lifecycle_manager,
        extractor=SchemaGuidedExtractor(model_name=VLM_MODEL, use_llm=True, allow_fallback=False),
    )
    env = adapter.extract_and_assemble(
        "tenant_alpha", SCHEMA_ID, 1, three_pages.payload, page_images=three_pages.refs[:2]
    )   # page 3 has no image reference

    assert env.run.provenance.details["pages_failed"] == [{"page": 3, "reason": "no page image reference supplied"}]
    assert any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)
    assert "issue_date" in _missing_flags(env)
    assert env.document_metadata.status == "needs_review"


# ============================================================================
# 10. Conflicting values combined with missing required fields
# ============================================================================

def test_conflicting_values_and_missing_required_fields_are_both_kept_for_review(
    lifecycle_manager, three_pages, monkeypatch
):
    pages = _without(COMPLETE_PAGES, "policy_number")
    pages[3] = {"issue_date": {"value": "2024-08-15", "bbox": _box(5)},
                "applicant.full_name": {"value": "ANANYA S. SHARMA", "bbox": _box(7)}}
    env, _ = _dynamic(lifecycle_manager, three_pages, monkeypatch, pages)

    conflict = next(f for f in env.flags if f.code == "FIELD_VALUE_CONFLICT")
    assert conflict.fields == ["applicant.full_name"]
    name = env.fields["applicant.full_name"]
    assert name.value == "ANANYA SHARMA" and name.evidence.page == 1                 # never overwritten
    assert [(e.page, e.text) for e in name.additional_evidence] == [(3, "ANANYA S. SHARMA")]
    assert _missing_flags(env) == ["policy_number"]
    assert env.document_metadata.status == "needs_review"


# ============================================================================
# 2 (evidence integrity) / 10: three-page evidence preservation and invariants
# ============================================================================

def _three_page_repeat_run(temp_storage, monkeypatch, responses):
    run = _run(temp_storage, 3)
    PageAwareVLM(responses).bind(run.refs).install(monkeypatch)
    return run, _vlm_pipeline().process(run.payload, page_images=run.refs)


def test_evidence_survives_three_page_aggregation_with_per_page_geometry(temp_storage, monkeypatch):
    responses = {
        1: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [120, 100, 420, 150]},
            "applicant.pan_card_number": {"value": "ABCDE1234F", "bbox": [120, 200, 320, 250]}},
        2: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [130, 110, 430, 160]},
            "applicant.phone_number": {"value": "9876543210", "bbox": [120, 400, 320, 450]}},
        3: {"applicant.full_name": {"value": "RUPESH  KUMAR JR", "bbox": [140, 120, 440, 170]},
            "applicant.pan_card_number": {"value": "ABCDE1234F", "bbox": [130, 210, 330, 260]}},
    }
    run, env = _three_page_repeat_run(temp_storage, monkeypatch, responses)

    name = env.fields["applicant.full_name"]
    assert (name.evidence.page, name.value) == (1, "RUPESH KUMAR")
    # same reading on page 2 and the conflicting reading on page 3 both stay available for review
    assert [(e.page, e.text) for e in name.additional_evidence] == [(2, "RUPESH KUMAR"), (3, "RUPESH  KUMAR JR")]
    assert name.evidence.bbox == pytest.approx(_expected_original_bbox(run.refs[0], [120, 100, 420, 150]))
    assert name.additional_evidence[0].bbox == pytest.approx(_expected_original_bbox(run.refs[1], [130, 110, 430, 160]))
    assert name.additional_evidence[1].bbox == pytest.approx(_expected_original_bbox(run.refs[2], [140, 120, 440, 170]))
    pan = env.fields["applicant.pan_card_number"]
    assert [e.page for e in pan.additional_evidence] == [3]                  # deduplicated, evidence kept
    assert any(f.code == "FIELD_VALUE_CONFLICT" and f.fields == ["applicant.full_name"] for f in env.flags)


def test_every_evidence_reference_names_a_real_page_of_the_document(temp_storage, monkeypatch):
    responses = {
        1: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [120, 100, 420, 150]}},
        2: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [130, 110, 430, 160]}},
        3: {"applicant.phone_number": {"value": "9876543210", "bbox": [120, 400, 320, 450]}},
    }
    run, env = _three_page_repeat_run(temp_storage, monkeypatch, responses)
    real_pages = {p.page_number for p in run.payload.pages}

    seen = 0
    for field in env.fields.values():
        for snippet in ([field.evidence] if field.evidence else []) + list(field.additional_evidence):
            seen += 1
            assert snippet.page in real_pages
            ref = run.refs[snippet.page - 1].page_ref
            x0, y0, x1, y1 = snippet.bbox
            assert 0 <= x0 <= x1 and 0 <= y0 <= y1                            # original-pixel geometry
            assert x1 <= ref.original_width_px * 1.5 and y1 <= ref.original_height_px * 1.5
    assert seen == 3          # name p1 + name p2 (additional) + phone p3


def test_missing_page_reference_can_never_produce_complete_coverage(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    PageAwareVLM({n: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [120, 100, 420, 150]}}
                  for n in (1, 2, 3)}).bind(run.refs).install(monkeypatch)

    env = _vlm_pipeline().process(run.payload, page_images=[run.refs[0], run.refs[2]])

    details = env.run.provenance.details
    assert details["pages_processed"] == [1, 3]
    assert details["pages_failed"] == [{"page": 2, "reason": "no page image reference supplied"}]
    assert any(f.code == "PARTIAL_PAGE_COVERAGE" and "page 2" in f.message for f in env.flags)
    assert env.document_metadata.status == "needs_review"


def test_empty_processed_pages_stay_valid_and_failed_pages_stay_represented(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    PageAwareVLM({1: {}, 2: RuntimeError("oom"),
                  3: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [120, 100, 420, 150]}}}
                 ).bind(run.refs).install(monkeypatch)

    env = _vlm_pipeline().process(run.payload, page_images=run.refs)

    details = env.run.provenance.details
    assert details["pages_attempted"] == [1, 2, 3]
    assert details["pages_processed"] == [1, 3] and details["pages_empty"] == [1]
    assert details["pages_failed"] == [{"page": 2, "reason": "RuntimeError: oom"}]
    assert env.fields["applicant.full_name"].evidence.page == 3
    assert any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)
    assert env.document_metadata.status == "needs_review"


# ============================================================================
# 11. Existing insurance compatibility
# ============================================================================

def test_insurance_doc02_multipage_repeating_groups_unchanged(temp_storage, monkeypatch):
    run = _run(temp_storage, 3, tokens=_doc02_tokens, expect=MVPDocumentType.DOC_02_INSURANCE_APP)
    vlm = PageAwareVLM(_doc02_pages()).bind(run.refs).install(monkeypatch)

    env = _vlm_pipeline().process(run.payload, page_images=run.refs)

    assert len(vlm.calls) == 3
    assert [env.fields[f"nominees.{i}.name"].value for i in range(3)] == [
        "ROHIT SHARMA", "KAVITA SHARMA", "MEERA SHARMA",
    ]
    assert [e.page for e in env.fields["nominees.0.name"].additional_evidence] == [3]
    assert _missing_flags(env) == []              # DOC-02 declares no required fields
