"""
ModuleCPipeline.process_routed_pages: the adapter-aware canonical entry (PRD v1.5).

The app (or any caller) reads each page with the routed specialist; Module C then normalizes, grounds,
validates, gates and assembles the envelope. These tests pin that Module C does this WITHOUT further
inference, with the same results as the direct VLM path, and with per-page failure/evidence rules intact.
"""

import io
import json
from typing import Any, Dict, List

import pytest

from doc_extractor.contracts_c import ExecutionMode, ExecutionProvenance
from doc_extractor.extractor import SchemaGuidedExtractor
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.routed_pages import RoutedPageResult, RoutedResultsExtractor
from doc_extractor import vlm_grounding

from tests.test_canonical_module_c_orchestration import (  # noqa: F401  (fixtures + helpers)
    CASA_VLM_OUTPUT,
    FakeVLM,
    _expected_original_bbox,
    _isolate_environment,
    _module_a,
    _module_b,
    _tiff_bytes,
    _vlm_pipeline,
    casa_run,
    temp_storage,
)

GROUNDED = {"grounded": True, "model_used": "vlm-test", "adapter": "vlm", "fallback_occurred": False}
UNGROUNDED = {"grounded": False, "model_used": "ocr+text-test", "adapter": "ocr_text", "fallback_occurred": False}


def _prov(model="vlm-test", provider="transformers_qwen_vl", **kw) -> ExecutionProvenance:
    return ExecutionProvenance(mode=ExecutionMode.GENUINE_LLM, model_name=model, provider=provider,
                               fallback_occurred=False, **kw)


def _result(page, raw, route=GROUNDED, rotation=0, error=None) -> RoutedPageResult:
    return RoutedPageResult(page_number=page, raw=raw, provenance=_prov(route["model_used"]) if not error else None,
                            route=dict(route), rotation=rotation, error=error)


def _pipeline() -> ModuleCPipeline:
    return ModuleCPipeline(extractor=SchemaGuidedExtractor(use_llm=False))


def _three_pages(storage):
    a = _module_a(storage)
    handoff = a.process_document(io.BytesIO(_tiff_bytes(3)), "scan.tiff")
    b = _module_b(storage)
    return b.process_handoff(handoff), b.load_page_images(handoff)


def _first_page(casa_run):
    return casa_run["payload"], casa_run["page_images"]


def test_module_c_assembles_routed_results_without_any_model_call(casa_run, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("process_routed_pages must not run a model")

    monkeypatch.setattr(SchemaGuidedExtractor, "_generate_from_image", forbidden)
    monkeypatch.setattr(SchemaGuidedExtractor, "extract_page_image", forbidden)
    payload, refs = _first_page(casa_run)

    env = _pipeline().process_routed_pages(payload, [_result(1, CASA_VLM_OUTPUT)], refs, schema_bound=True)

    assert env.fields["applicant.full_name"].value == "RUPESH KUMAR"
    assert env.fields["applicant.full_name"].evidence.bbox == pytest.approx(
        _expected_original_bbox(refs[0], CASA_VLM_OUTPUT["applicant.full_name"]["bbox"]))
    assert env.document_metadata.status == "needs_review"          # VLM confidence is unavailable: never auto-approved


def test_routed_assembly_matches_the_direct_vlm_path_field_for_field(casa_run, monkeypatch):
    FakeVLM().install(monkeypatch)
    payload, refs = _first_page(casa_run)
    direct = _vlm_pipeline().process(payload, page_images=refs)
    routed = _pipeline().process_routed_pages(payload, [_result(1, CASA_VLM_OUTPUT)], refs, schema_bound=True)

    assert json.loads(routed.model_dump_json())["fields"] == json.loads(direct.model_dump_json())["fields"]
    assert routed.document_metadata.status == direct.document_metadata.status
    assert [f.code for f in routed.flags] == [f.code for f in direct.flags]


def test_the_dynamic_path_keeps_every_label_and_grounds_it(casa_run):
    payload, refs = _first_page(casa_run)
    raw = {"title": {"value": "Gold Shield Plan", "bbox": [100, 50, 600, 120]}, "tagline": {"value": None, "bbox": None}}
    env = _pipeline().process_routed_pages(payload, [_result(1, raw)], refs, document_type="generic", force_review=True)

    assert env.document_metadata.document_type == "generic"
    assert env.fields["title"].value == "Gold Shield Plan" and env.fields["title"].review_required is True
    assert env.fields["title"].evidence.bbox == pytest.approx(_expected_original_bbox(refs[0], [100, 50, 600, 120]))


def test_pages_are_read_in_document_order_whatever_order_they_arrive_in(temp_storage):
    payload, refs = _three_pages(temp_storage)
    pages = {1: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [120, 100, 420, 150]}},
             2: {"applicant.pan_card_number": {"value": "ABCDE1234F", "bbox": [120, 200, 320, 250]}},
             3: {"applicant.full_name": {"value": "RAKESH KUMAR", "bbox": [120, 100, 420, 150]}}}
    shuffled = [_result(3, pages[3]), _result(1, pages[1]), _result(2, pages[2]), _result(1, {})]  # dup page 1 ignored
    env = _pipeline().process_routed_pages(payload, shuffled, refs, schema_bound=True)

    details = env.run.provenance.details
    assert details["pages_attempted"] == [1, 2, 3] and details["pages_processed"] == [1, 2, 3]
    name = env.fields["applicant.full_name"]
    assert name.value == "RUPESH KUMAR" and name.evidence.page == 1                       # first reading wins
    assert any(f.code == "FIELD_VALUE_CONFLICT" for f in env.flags)                        # page 3 disagrees
    assert env.fields["applicant.pan_card_number"].evidence.page == 2


def test_a_failed_page_is_recorded_and_the_other_pages_survive(temp_storage):
    payload, refs = _three_pages(temp_storage)
    results = [_result(1, CASA_VLM_OUTPUT), _result(2, None, error="RuntimeError: simulated model failure"),
               _result(3, {"applicant.phone_number": {"value": "9876543210", "bbox": [120, 400, 320, 450]}})]
    env = _pipeline().process_routed_pages(payload, results, refs, schema_bound=True)

    details = env.run.provenance.details
    assert details["pages_failed"] == [{"page": 2, "reason": "RuntimeError: simulated model failure"}]
    assert details["pages_processed"] == [1, 3]
    assert any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)
    assert env.fields["applicant.phone_number"].value == "+919876543210" or env.fields["applicant.phone_number"].value
    assert env.document_metadata.status == "needs_review"


def test_when_no_page_can_be_processed_the_error_surfaces(casa_run):
    payload, refs = _first_page(casa_run)
    with pytest.raises(ValueError, match="No page could be processed"):
        _pipeline().process_routed_pages(payload, [_result(1, None, error="RuntimeError: boom")], refs, schema_bound=True)
    with pytest.raises(ValueError, match="No page could be processed"):
        _pipeline().process_routed_pages(payload, [], refs, schema_bound=True)


def test_a_page_without_an_image_reference_is_flagged_not_dropped(temp_storage):
    payload, refs = _three_pages(temp_storage)
    env = _pipeline().process_routed_pages(
        payload, [_result(1, CASA_VLM_OUTPUT), _result(2, {}), _result(3, {})], [refs[0], refs[2]], schema_bound=True)
    flag = next(f for f in env.flags if f.code == "PARTIAL_PAGE_COVERAGE")
    assert "page 2" in flag.message and "no page image reference supplied" in flag.message


def test_a_route_without_boxes_withholds_its_values_and_flags_evidence_missing(casa_run):
    payload, refs = _first_page(casa_run)
    raw = {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [120, 100, 420, 150]}}   # box must be ignored
    env = _pipeline().process_routed_pages(payload, [_result(1, raw, route=UNGROUNDED)], refs, schema_bound=True)

    field = env.fields["applicant.full_name"]
    assert field.value is None and field.evidence is None and field.review_required is True
    assert any(f.code == "EVIDENCE_MISSING" and "applicant.full_name" in f.fields for f in env.flags)
    details = env.run.provenance.details
    assert details["ungrounded_pages"] == [1]
    assert details["unverified_readings"] == {"1": {"applicant.full_name": "RUPESH KUMAR"}}   # review aid only
    assert env.document_metadata.status == "needs_review"


def test_mixed_routes_keep_grounded_pages_and_withhold_only_the_ungrounded_ones(temp_storage):
    payload, refs = _three_pages(temp_storage)
    results = [_result(1, CASA_VLM_OUTPUT),
               _result(2, {"applicant.first_name": {"value": "RUPESH", "bbox": None}}, route=UNGROUNDED)]
    env = _pipeline().process_routed_pages(payload, results, [refs[0], refs[1]], schema_bound=True)
    assert env.fields["applicant.full_name"].value == "RUPESH KUMAR"        # page 1: VLM, grounded
    assert env.fields["applicant.first_name"].value is None                 # page 2: no boxes, withheld
    assert env.run.provenance.details["ungrounded_pages"] == [2]


@pytest.mark.parametrize("clockwise", [90, 180, 270])
def test_boxes_read_on_a_turned_page_map_back_through_the_stored_pages_geometry(casa_run, clockwise):
    payload, refs = _first_page(casa_run)
    seen = (120, 100, 420, 150)                                     # relative to the UPRIGHT image
    raw = {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": list(seen)}}
    env = _pipeline().process_routed_pages(payload, [_result(1, raw, rotation=clockwise)], refs, schema_bound=True)
    expected = _expected_original_bbox(refs[0], vlm_grounding.unrotate_bbox_norm(seen, clockwise))
    assert env.fields["applicant.full_name"].evidence.bbox == pytest.approx(expected)


def test_a_fallback_in_any_page_is_recorded_in_the_provenance(casa_run):
    payload, refs = _first_page(casa_run)
    prov = _prov(fallback_reason="routed model ocr+text failed: boom")
    prov.fallback_occurred = True
    result = RoutedPageResult(page_number=1, raw=CASA_VLM_OUTPUT, provenance=prov,
                              route={**GROUNDED, "fallback_occurred": True})
    env = _pipeline().process_routed_pages(payload, [result], refs, schema_bound=True)
    assert env.run.provenance.fallback_occurred is True and "boom" in env.run.provenance.fallback_reason


def test_the_replay_extractor_reports_the_models_that_actually_read_the_pages():
    replay = RoutedResultsExtractor([_result(1, {}, route=GROUNDED), _result(2, {}, route=UNGROUNDED)], "9.9.9")
    assert replay.model_name == "vlm-test, ocr+text-test" and replay.prompt_version == "9.9.9"
    assert replay.multimodal is True


def test_the_v15_casa_fields_flow_through_validation(casa_run):
    payload, refs = _first_page(casa_run)
    box = [100, 100, 500, 150]
    raw = {
        "applicant.full_name": {"value": "RUPESH KUMAR", "bbox": box},
        "applicant.first_name": {"value": "RUPESH", "bbox": box},
        "applicant.last_name": {"value": "KUMAR", "bbox": box},
        "applicant.aadhaar_number": {"value": "1234 5678 9012", "bbox": box},       # fails the Verhoeff checksum
        "applicant.gross_annual_income": {"value": "1-5 Lakhs", "bbox": box},
        "bank_details.account_number": {"value": "000123456789", "bbox": box},
        "bank_details.ifsc_code": {"value": "HDFC0001234", "bbox": box},
        "bank_details.pin_code": {"value": "560001", "bbox": box},
    }
    env = _pipeline().process_routed_pages(payload, [_result(1, raw)], refs, schema_bound=True)

    assert env.fields["applicant.first_name"].value == "RUPESH" and env.fields["applicant.last_name"].value == "KUMAR"
    assert env.fields["applicant.gross_annual_income"].value == "1-5 Lakhs"
    assert env.fields["bank_details.account_number"].value == "000123456789"          # leading zeros preserved
    assert env.fields["bank_details.ifsc_code"].validation == "passed"
    assert env.fields["applicant.aadhaar_number"].validation == "failed"
    assert env.fields["applicant.aadhaar_number"].review_required is True
    assert "bank_details.micr_code" in env.fields and env.fields["bank_details.micr_code"].value is None
