"""
ModuleCPipeline is the canonical processing and envelope-assembly layer (PRD v1.3 / v1.5).

The only thing faked is the lowest-level model call (SchemaGuidedExtractor._generate_from_image),
so the real extract_page_image -> JSON parsing -> normalize_and_format -> CoordinateTransform ->
assemble_field_entry -> assemble_canonical_envelope chain executes on real Module A artifacts.
No GPU, torch or transformers are required.
"""

import io
import json
from pathlib import Path
import tempfile
from typing import Any, Dict, List

import cv2
import numpy as np
import pytest
from PIL import Image

from doc_extractor import app
from doc_extractor import vlm_grounding
from doc_extractor.config import Settings
from doc_extractor.contracts_b import MVPDocumentType
from doc_extractor.contracts_c import ExecutionMode, PageImageRef
from doc_extractor.dynamic_adapter import DynamicExtractionAdapter
from doc_extractor.exceptions import LLMExecutionError
from doc_extractor.extractor import SchemaGuidedExtractor
from doc_extractor.handoff import CoordinateTransform
from doc_extractor.malware import MockMalwareScanner
from doc_extractor.parsing.adapter import LocalOCRParserAdapter
from doc_extractor.parsing.base import RawOCRToken
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.routing import RoutedExtractor
from doc_extractor.storage import LocalDiskStorage
from schema_lifecycle.enums import SchemaStatus

# Fixtures reused from the existing dynamic-adapter tests (not redefined here).
from tests.test_dynamic_module_c_adapter import (  # noqa: F401
    build_test_schema_def,
    lifecycle_manager,
    sample_payload,
)

TENANT = app.TENANT_ID
VLM_MODEL = "Qwen/Qwen3-VL-8B-Instruct"

CASA_VLM_OUTPUT: Dict[str, Any] = {
    "applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [120, 100, 420, 150]},
    "applicant.pan_card_number": {"value": "ABCDE1234F", "bbox": [120, 200, 320, 250]},
    "applicant.date_of_birth": {"value": "15/08/1990", "bbox": [120, 300, 320, 350]},
    "applicant.phone_number": {"value": "9876543210", "bbox": [120, 400, 320, 450]},
    "account_details.account_type": {"value": "Savings", "bbox": [120, 500, 320, 550]},
    "account_details.initial_deposit_amount": {"value": "Rs. 5,000.00", "bbox": [120, 600, 320, 650]},
}


# ----------------------------------------------------------------------------
# Fixtures / helpers
# ----------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch):
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("VLLM_API_BASE", raising=False)
    app._PIPELINES.clear()
    app._ROUTED = None
    yield
    app._PIPELINES.clear()
    app._ROUTED = None


@pytest.fixture
def temp_storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield LocalDiskStorage(base_dir=tmpdir, tenant_id=TENANT)


class FakeVLM:
    """Replaces only SchemaGuidedExtractor._generate_from_image and records every call."""

    def __init__(self, output: Any = None, error: Exception = None, fail_on: Any = ()):
        self.output = CASA_VLM_OUTPUT if output is None else output
        self.error = error
        self.fail_on = set(fail_on)              # 1-based indexes of EXTRACTION calls that raise
        self.calls: List[Dict[str, Any]] = []
        self.extraction_calls = 0

    def install(self, monkeypatch) -> "FakeVLM":
        fake = self

        def generate(self_extractor, image_input, system_prompt, user_text, max_new_tokens, max_pixels=None):
            fake.calls.append({"image": str(image_input), "system_prompt": system_prompt, "max_pixels": max_pixels})
            if "You classify document images" not in system_prompt:
                fake.extraction_calls += 1
                if fake.extraction_calls in fake.fail_on:
                    raise RuntimeError("simulated model failure")
            if fake.error is not None:
                raise fake.error
            return json.dumps(fake.output)

        monkeypatch.setattr(SchemaGuidedExtractor, "_generate_from_image", generate)
        return self


def _casa_ocr_tokens(img: np.ndarray) -> List[RawOCRToken]:
    return [
        RawOCRToken(text="BANK ACCOUNT OPENING FORM", bbox_preprocessed=(50, 50, 400, 100), confidence=0.98),
        RawOCRToken(text="CASA Application", bbox_preprocessed=(50, 110, 300, 150), confidence=0.97),
        RawOCRToken(text="Customer Information Form CIF Number", bbox_preprocessed=(50, 160, 450, 200), confidence=0.96),
        RawOCRToken(text="Initial Deposit Amount", bbox_preprocessed=(50, 210, 300, 250), confidence=0.95),
        RawOCRToken(text="Cheque Book and Debit Card", bbox_preprocessed=(50, 260, 350, 300), confidence=0.94),
    ]


def _png_bytes() -> bytes:
    img = np.full((1400, 1000, 3), 255, dtype=np.uint8)
    cv2.putText(img, "CASA", (60, 200), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), 4)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _tiff_bytes(pages: int) -> bytes:
    images = [Image.new("RGB", (1000, 1400), color=(255, 255, 255)) for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, format="TIFF", save_all=True, append_images=images[1:])
    return buf.getvalue()


def _module_b(storage: LocalDiskStorage) -> ModuleBPipeline:
    return ModuleBPipeline(
        storage=storage,
        parser=LocalOCRParserAdapter(raw_extractor=_casa_ocr_tokens),
        tenant_id=TENANT,
    )


def _module_a(storage: LocalDiskStorage) -> ModuleAPipeline:
    return ModuleAPipeline(scanner=MockMalwareScanner(), storage=storage, tenant_id=TENANT)


@pytest.fixture
def casa_run(temp_storage):
    """Real Module A handoff + Module B payload + explicit page-image references (1 page)."""
    pipe_a = _module_a(temp_storage)
    handoff = pipe_a.process_document(io.BytesIO(_png_bytes()), "casa.png")
    pipe_b = _module_b(temp_storage)
    payload_b = pipe_b.process_handoff(handoff)
    assert payload_b.classification.document_type == MVPDocumentType.DOC_01_CASA
    return {
        "storage": temp_storage,
        "handoff": handoff,
        "payload": payload_b,
        "page_images": pipe_b.load_page_images(handoff),
    }


def _vlm_pipeline(**extractor_kwargs) -> ModuleCPipeline:
    return ModuleCPipeline(
        extractor=SchemaGuidedExtractor(model_name=VLM_MODEL, use_llm=True, allow_fallback=False, **extractor_kwargs)
    )


def _expected_original_bbox(ref: PageImageRef, bbox_norm) -> tuple:
    x1, y1, x2, y2 = bbox_norm                      # canonical [x1, y1, x2, y2] on 0-1000 (PRD v1.5)
    pr = ref.page_ref
    w, h = pr.preprocessed_width_px, pr.preprocessed_height_px
    return CoordinateTransform.map_bbox_preprocessed_to_original(
        bbox=(x1 * w / 1000.0, y1 * h / 1000.0, x2 * w / 1000.0, y2 * h / 1000.0),
        original_width=pr.original_width_px,
        original_height=pr.original_height_px,
        applied_rotation_deg=pr.applied_rotation_deg,
        applied_deskew_deg=pr.applied_deskew_deg,
    )


# ----------------------------------------------------------------------------
# 1. ModuleCPipeline is the real path; the VLM is genuinely invoked
# ----------------------------------------------------------------------------

def test_module_c_runs_vlm_extraction_through_run_extraction(casa_run, monkeypatch):
    fake = FakeVLM().install(monkeypatch)
    stages: List[str] = []
    original = ModuleCPipeline.run_extraction

    def spy(self, *args, **kwargs):
        stages.append("run_extraction")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ModuleCPipeline, "run_extraction", spy)

    env = _vlm_pipeline().process(casa_run["payload"], page_images=casa_run["page_images"])

    assert stages == ["run_extraction"]
    assert len(fake.calls) == 1  # the multimodal extraction method was genuinely invoked
    assert "applicant.full_name" in fake.calls[0]["system_prompt"]  # schema-bound VLM prompt
    assert env.fields["applicant.full_name"].value == "RUPESH KUMAR"
    prov = env.run.provenance
    assert prov.mode == ExecutionMode.GENUINE_LLM
    assert prov.model_name == VLM_MODEL
    assert prov.details["input_modality"] == "image"
    assert prov.details["pages_processed"] == [1]


def test_real_module_a_image_reference_reaches_the_vlm(casa_run, monkeypatch):
    fake = FakeVLM().install(monkeypatch)
    _vlm_pipeline().process(casa_run["payload"], page_images=casa_run["page_images"])

    sent = Path(fake.calls[0]["image"])
    handoff_page = casa_run["handoff"].pages[0]
    expected = casa_run["storage"].resolve_uri_to_path(handoff_page.preprocessed_storage_uri)

    assert sent == expected
    assert sent.is_file()
    assert "preprocessed" in sent.name
    assert casa_run["page_images"][0].page_ref.preprocessed_storage_uri == handoff_page.preprocessed_storage_uri


def test_page_layout_output_has_no_image_reference_so_it_must_be_explicit(casa_run):
    page_layout = casa_run["payload"].pages[0]
    assert not hasattr(page_layout, "preprocessed_storage_uri")  # why PageImageRef exists
    assert isinstance(casa_run["page_images"][0], PageImageRef)


# ----------------------------------------------------------------------------
# 2. Normalization, CoordinateTransform and evidence are invoked at their stages
# ----------------------------------------------------------------------------

def test_normalization_stage_is_invoked_and_values_are_typed(casa_run, monkeypatch):
    FakeVLM().install(monkeypatch)
    seen: List[str] = []
    original = vlm_grounding.normalize_and_format

    def spy(field_name, raw_value):
        seen.append(field_name)
        return original(field_name, raw_value)

    monkeypatch.setattr(vlm_grounding, "normalize_and_format", spy)

    env = _vlm_pipeline().process(casa_run["payload"], page_images=casa_run["page_images"])

    assert {"full_name", "pan_card_number", "date_of_birth", "initial_deposit_amount"} <= set(seen)
    assert env.fields["account_details.initial_deposit_amount"].value == 5000.0  # "Rs. 5,000.00"
    assert env.fields["applicant.date_of_birth"].value == "1990-08-15"           # ISO-8601


def test_coordinate_transform_maps_model_boxes_to_original_200dpi_pixels(casa_run, monkeypatch):
    FakeVLM().install(monkeypatch)
    calls: List[Dict[str, Any]] = []
    original = CoordinateTransform.map_bbox_preprocessed_to_original

    def spy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(CoordinateTransform, "map_bbox_preprocessed_to_original", staticmethod(spy))

    env = _vlm_pipeline().process(casa_run["payload"], page_images=casa_run["page_images"])

    assert calls, "CoordinateTransform must be used for model-supplied boxes"
    ref = casa_run["page_images"][0]
    ev = env.fields["applicant.full_name"].evidence
    assert ev is not None and ev.page == 1
    expected = _expected_original_bbox(ref, CASA_VLM_OUTPUT["applicant.full_name"]["bbox"])
    assert ev.bbox == pytest.approx(expected)


def test_deskew_and_rotation_are_inverted_not_just_scaled(casa_run):
    from doc_extractor.contracts_c import FieldGrounding

    ref = casa_run["page_images"][0]
    skewed = PageImageRef(
        page_number=1,
        image_path=ref.image_path,
        page_ref=ref.page_ref.model_copy(update={"applied_deskew_deg": 4.0}),
    )
    grounding = {"applicant.full_name": FieldGrounding(bbox_norm=(120, 100, 420, 150), raw_value="X")}

    plain = vlm_grounding.grounding_to_evidence_hints(grounding, [ref])["applicant.full_name"]["bbox"]
    skew = vlm_grounding.grounding_to_evidence_hints(grounding, [skewed])["applicant.full_name"]["bbox"]

    assert plain != pytest.approx(skew)
    assert skew == pytest.approx(_expected_original_bbox(skewed, (120, 100, 420, 150)))


def test_no_hardcoded_ocr_confidence_for_model_read_text(casa_run, monkeypatch):
    FakeVLM().install(monkeypatch)
    hints_seen: List[Dict[str, Any]] = []
    original = vlm_grounding.grounding_to_evidence_hints

    def spy(grounding, page_images):
        hints = original(grounding, page_images)
        hints_seen.extend(hints.values())
        return hints

    monkeypatch.setattr("doc_extractor.pipeline_c.grounding_to_evidence_hints", spy)
    _vlm_pipeline().process(casa_run["payload"], page_images=casa_run["page_images"])

    assert hints_seen and all(h["ocr_confidence"] is None for h in hints_seen)


# ----------------------------------------------------------------------------
# 3. The canonical envelope - not the caller - determines status
# ----------------------------------------------------------------------------

def test_status_is_computed_by_the_canonical_envelope(casa_run, monkeypatch):
    fake = FakeVLM().install(monkeypatch)
    pipeline = _vlm_pipeline()

    clean = pipeline.process(casa_run["payload"], page_images=casa_run["page_images"])
    assert clean.document_metadata.status in ("auto_approved", "needs_review")
    assert clean.document_metadata.status != "valid"
    assert clean.document_metadata.status == (
        "needs_review"
        if any(f.review_required or f.validation == "failed" for f in clean.fields.values())
        else "auto_approved"
    )

    bad = dict(CASA_VLM_OUTPUT)
    bad["applicant.pan_card_number"] = {"value": "NOT-A-PAN", "bbox": [120, 200, 320, 250]}
    fake.output = bad
    failed = pipeline.process(casa_run["payload"], page_images=casa_run["page_images"])
    assert failed.fields["applicant.pan_card_number"].validation == "failed"
    assert failed.document_metadata.status == "needs_review"


def test_uncertain_model_reading_forces_review(casa_run, monkeypatch):
    out = {k: dict(v) for k, v in CASA_VLM_OUTPUT.items()}
    out["applicant.full_name"]["uncertain"] = True
    FakeVLM(output=out).install(monkeypatch)

    env = _vlm_pipeline().process(casa_run["payload"], page_images=casa_run["page_images"])
    assert env.fields["applicant.full_name"].review_required is True
    assert env.document_metadata.status == "needs_review"


def test_value_without_grounding_is_nulled_and_flagged_gr002(casa_run, monkeypatch):
    out = {k: dict(v) for k, v in CASA_VLM_OUTPUT.items()}
    out["applicant.full_name"] = {"value": "RUPESH KUMAR", "bbox": None}  # no box, not in OCR text
    FakeVLM(output=out).install(monkeypatch)

    env = _vlm_pipeline().process(casa_run["payload"], page_images=casa_run["page_images"])
    assert env.fields["applicant.full_name"].value is None
    assert any(f.code == "EVIDENCE_MISSING" and "applicant.full_name" in f.fields for f in env.flags)
    assert env.document_metadata.status == "needs_review"


def test_out_of_grid_bbox_is_treated_as_ungrounded(casa_run, monkeypatch):
    out = {k: dict(v) for k, v in CASA_VLM_OUTPUT.items()}
    out["applicant.full_name"] = {"value": "RUPESH KUMAR", "bbox": [120, 100, 9000, 1500]}  # pixels, not 0-1000
    FakeVLM(output=out).install(monkeypatch)

    env = _vlm_pipeline().process(casa_run["payload"], page_images=casa_run["page_images"])
    assert env.fields["applicant.full_name"].evidence is None


# ----------------------------------------------------------------------------
# 4. Text (Ollama) extraction stays available behind the same interface
# ----------------------------------------------------------------------------

def test_multimodal_is_inferred_from_the_model():
    assert SchemaGuidedExtractor().multimodal is True
    assert SchemaGuidedExtractor(model_name="Qwen/Qwen2.5-VL-7B-Instruct").multimodal is True
    assert SchemaGuidedExtractor.for_local_ollama().multimodal is False
    assert SchemaGuidedExtractor(model_name="Qwen2.5-7B-Instruct").multimodal is False
    assert SchemaGuidedExtractor(model_name=VLM_MODEL, multimodal=False).multimodal is False


def test_text_extractor_ignores_page_images_and_never_calls_the_vlm(casa_run, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("text extractor must not invoke the VLM")

    monkeypatch.setattr(SchemaGuidedExtractor, "_generate_from_image", forbidden)
    text_calls: List[str] = []

    def text_caller(payload, schema_cls):
        text_calls.append(payload.combined_markdown)
        return {"applicant": {"full_name": "BANK ACCOUNT OPENING FORM"}}, "req-text-1"

    extractor = SchemaGuidedExtractor(
        model_name="ollama/qwen2.5:7b-instruct", use_llm=True, allow_fallback=False, llm_caller=text_caller
    )
    env = ModuleCPipeline(extractor=extractor).process(casa_run["payload"], page_images=casa_run["page_images"])

    assert text_calls and "CASA" in text_calls[0]
    assert env.run.provenance.request_id == "req-text-1"
    assert "pages_processed" not in env.run.provenance.details
    assert not any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)


def test_heuristic_extractor_still_works_without_images(casa_run):
    env = ModuleCPipeline(extractor=SchemaGuidedExtractor(use_llm=False)).process(casa_run["payload"])
    assert env.run.provenance.mode == ExecutionMode.HEURISTIC


def test_fallback_extractor_runs_when_primary_vlm_fails(casa_run, monkeypatch):
    FakeVLM(error=RuntimeError("CUDA out of memory")).install(monkeypatch)
    fallback_calls: List[int] = []

    def text_caller(payload, schema_cls):
        fallback_calls.append(1)
        return {"applicant": {"full_name": "CASA Application"}}, "req-fallback"

    primary = SchemaGuidedExtractor(model_name=VLM_MODEL, use_llm=True, allow_fallback=False)
    fallback = SchemaGuidedExtractor(
        model_name="ollama/qwen2.5:7b-instruct", use_llm=True, allow_fallback=False, llm_caller=text_caller
    )
    env = ModuleCPipeline(extractor=primary, fallback_extractor=fallback).process(
        casa_run["payload"], page_images=casa_run["page_images"]
    )

    assert fallback_calls
    prov = env.run.provenance
    assert prov.fallback_occurred is True
    assert "CUDA out of memory" in prov.fallback_reason
    assert prov.is_trusted_for_benchmark() is False


def test_failed_vlm_without_fallback_fails_closed(casa_run, monkeypatch):
    FakeVLM(error=RuntimeError("CUDA out of memory")).install(monkeypatch)
    with pytest.raises(LLMExecutionError):
        _vlm_pipeline().process(casa_run["payload"], page_images=casa_run["page_images"])


# ----------------------------------------------------------------------------
# 5. Single-page scope is explicit, never silent
# ----------------------------------------------------------------------------

def test_multipage_document_reads_every_page_in_order(temp_storage, monkeypatch):
    # (Task 6) the VLM path is no longer single-page: every page image is read, in order.
    fake = FakeVLM().install(monkeypatch)
    pipe_a = _module_a(temp_storage)
    handoff = pipe_a.process_document(io.BytesIO(_tiff_bytes(2)), "casa.tiff")
    pipe_b = _module_b(temp_storage)
    payload = pipe_b.process_handoff(handoff)
    refs = pipe_b.load_page_images(handoff)
    assert len(payload.pages) == 2 and len(refs) == 2

    env = _vlm_pipeline().process(payload, page_images=refs)

    assert [Path(c["image"]) for c in fake.calls] == [refs[0].image_path, refs[1].image_path]
    assert env.run.provenance.details["pages_processed"] == [1, 2]
    assert not any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)


def test_vlm_extraction_fails_closed_when_no_page_can_be_processed(casa_run, monkeypatch):
    FakeVLM(error=RuntimeError("boom")).install(monkeypatch)
    extractor = SchemaGuidedExtractor(model_name=VLM_MODEL, use_llm=True)
    from doc_extractor.schemas import CASASchema

    with pytest.raises(ValueError, match="No page could be processed"):
        extractor._extract_vlm_internal(CASASchema, casa_run["page_images"] * 2)


def test_select_page_images_keeps_every_supplied_page(casa_run):
    pipeline = ModuleCPipeline(extractor=SchemaGuidedExtractor(use_llm=False))
    refs = casa_run["page_images"]
    assert pipeline._select_page_images("DOC-01", refs) == refs
    assert pipeline._select_page_images("DOC-01", None) is None


# ----------------------------------------------------------------------------
# 6. Generic (unstructured / handwritten) categories go through Module C too
# ----------------------------------------------------------------------------

GENERIC_OUTPUT = {
    "title": {"value": "Gold Shield Plan", "bbox": [100, 50, 600, 120]},
    "tagline": {"value": "Protection for life", "bbox": None},
}


def test_process_generic_uses_canonical_components(casa_run, monkeypatch):
    fake = FakeVLM(output=GENERIC_OUTPUT).install(monkeypatch)
    env = _vlm_pipeline().process_generic(
        casa_run["payload"], casa_run["page_images"], "GENERIC PROMPT", document_type="generic", force_review=True
    )
    assert fake.calls[0]["system_prompt"] == "GENERIC PROMPT"
    assert env.fields["title"].value == "Gold Shield Plan"
    assert env.fields["title"].evidence.bbox == pytest.approx(
        _expected_original_bbox(casa_run["page_images"][0], GENERIC_OUTPUT["title"]["bbox"])
    )
    assert env.fields["title"].review_required is True            # force_review (handwriting)
    assert env.fields["tagline"].value is None                    # ungrounded -> GR-002
    assert any(f.code == "EVIDENCE_MISSING" for f in env.flags)
    assert env.document_metadata.status == "needs_review"


# ----------------------------------------------------------------------------
# 7. The Gradio application delegates to ModuleCPipeline and propagates Settings
# ----------------------------------------------------------------------------

VLM_ONLY_TABLE: Dict[str, Any] = {
    "version": "test-1",
    "default_model": VLM_MODEL,
    "routes": {c: {"default": VLM_MODEL} for c in ("structured", "unstructured", "handwritten")},
    "alt_engines": {"ocr_text": {"adapter": "ocr_text", "ocr_model": "zai-org/GLM-OCR",
                                 "text_model": "Qwen/Qwen3-4B-Instruct-2507"}},
}


def _wire_app(monkeypatch, storage, table=None):
    """Real Module A/B on temp storage; every category routed to the (fake-backed) vision model."""
    monkeypatch.setattr(app, "get_pipelines", lambda settings=None: (_module_a(storage), _module_b(storage)))
    app._ROUTED = RoutedExtractor(table=table or VLM_ONLY_TABLE, allow_fallback=False)


def _write_png(tmp_path: Path) -> str:
    path = tmp_path / "casa_form.png"
    path.write_bytes(_png_bytes())
    return str(path)


def _write_tiff(tmp_path: Path, pages: int) -> str:
    path = tmp_path / "scan.tiff"
    path.write_bytes(_tiff_bytes(pages))
    return str(path)


def _run_app(path, *choices, settings=None, engine="Follow routing table"):
    """Drains the streaming generator; returns (final 6-tuple, every streamed update)."""
    updates = list(app.process_document(path, *choices, engine_choice=engine, settings=settings))
    return updates[-1], updates


def _spy_routed(monkeypatch) -> List[Any]:
    calls: List[Any] = []
    original = ModuleCPipeline.process_routed_pages

    def spy(self, payload, page_results, page_images, **kwargs):
        calls.append({"results": list(page_results), "refs": list(page_images), **kwargs})
        return original(self, payload, page_results, page_images, **kwargs)

    monkeypatch.setattr(ModuleCPipeline, "process_routed_pages", spy)
    return calls


def test_app_routes_each_page_then_module_c_assembles_the_envelope(temp_storage, tmp_path, monkeypatch):
    fake = FakeVLM().install(monkeypatch)
    _wire_app(monkeypatch, temp_storage)
    calls = _spy_routed(monkeypatch)

    (gallery, status, json_text, md, json_file, steps_md), updates = _run_app(
        _write_png(tmp_path), "Structured", "DOC-01", app.AUTO, "English"
    )
    envelope = json.loads(json_text)

    assert len(calls) == 1 and calls[0]["schema_bound"] is True            # ModuleCPipeline built the envelope
    (result,) = calls[0]["results"]
    assert result.page_number == 1 and result.route["grounded"] is True and result.error is None
    assert len(fake.calls) == 1                                           # one model call: nothing read twice
    field = envelope["fields"]["applicant.full_name"]
    assert field["value"] == "RUPESH KUMAR"
    assert field["evidence"]["bbox"] == pytest.approx(
        _expected_original_bbox(calls[0]["refs"][0], CASA_VLM_OUTPUT["applicant.full_name"]["bbox"])
    )
    assert envelope["document_metadata"]["status"] == "needs_review"      # VLM confidence is unavailable
    assert envelope["document_metadata"]["document_category"] == "structured"
    assert envelope["run"]["routing"][0]["model_used"] == VLM_MODEL
    assert envelope["run"]["confidence_calibrated"] is False
    assert len(gallery) == 1 and "Page 1" in steps_md and Path(json_file).is_file()
    assert status.startswith("Status: NEEDS_REVIEW")
    assert len(updates) >= 4 and all(len(u) == 6 for u in updates)        # live streamed updates incl. steps panel


def test_app_dynamic_default_uses_the_generic_path_and_forces_review_for_handwriting(
    temp_storage, tmp_path, monkeypatch
):
    fake = FakeVLM(output=GENERIC_OUTPUT).install(monkeypatch)
    _wire_app(monkeypatch, temp_storage)
    calls = _spy_routed(monkeypatch)

    (_, _, json_text, md, _, _), _ = _run_app(_write_png(tmp_path), "Handwritten", app.AUTO, app.AUTO, "English")
    envelope = json.loads(json_text)

    assert calls[0]["schema_bound"] is False and calls[0]["force_review"] is True
    assert envelope["document_metadata"]["document_type"] == "generic"
    assert envelope["fields"]["title"]["review_required"] is True
    assert envelope["fields"]["title"]["value"] == "Gold Shield Plan"
    extraction = [c for c in fake.calls if "You classify document images" not in c["system_prompt"]]
    assert len(extraction) == 1


def test_ocr_text_route_has_no_boxes_so_values_are_withheld_and_flagged(temp_storage, tmp_path, monkeypatch):
    from doc_extractor.contracts_c import ExecutionProvenance

    FakeVLM().install(monkeypatch)
    _wire_app(monkeypatch, temp_storage)

    class StubOCRText:
        kind = "ocr_text"
        model_label = "stub-ocr + stub-text"

        def extract(self, image_path, system_prompt):
            prov = ExecutionProvenance(mode=ExecutionMode.GENUINE_LLM, model_name=self.model_label,
                                       provider="transformers_ocr_text", fallback_occurred=False,
                                       details={"ocr_chars": 16})
            # even a (hallucinated) box from a route that cannot ground must never become evidence
            return {"title": {"value": "Gold Shield Plan", "bbox": [10, 10, 900, 90]}}, prov, {
                "grounded": False, "models": {}, "ocr_text_preview": "Gold Shield Plan"}

    monkeypatch.setattr(RoutedExtractor, "get_adapter", lambda self, route: StubOCRText())

    (_, status, json_text, _, _, _), _ = _run_app(
        _write_png(tmp_path), "Unstructured", app.AUTO, app.AUTO, "English",
        engine="GLM-OCR + Qwen3-4B-Instruct text (experimental)",
    )
    envelope = json.loads(json_text)

    assert envelope["fields"]["title"]["value"] is None and envelope["fields"]["title"]["evidence"] is None
    missing = [f for f in envelope["flags"] if f["code"] == "EVIDENCE_MISSING"]
    assert missing and "title" in missing[0]["fields"]                       # PRD GR-002 / FR-EXT-004
    details = envelope["run"]["provenance"]["details"]
    assert details["unverified_readings"]["1"]["title"] == "Gold Shield Plan"   # review aid, not a field value
    assert envelope["document_metadata"]["status"] == "needs_review"
    assert "values withheld" in status


def test_a_failed_page_is_reported_and_the_other_pages_are_kept(temp_storage, tmp_path, monkeypatch):
    FakeVLM(fail_on={2}).install(monkeypatch)
    _wire_app(monkeypatch, temp_storage)

    (gallery, _, json_text, _, _, _), _ = _run_app(
        _write_tiff(tmp_path, 3), "Structured", "DOC-01", app.AUTO, "English"
    )
    envelope = json.loads(json_text)

    details = envelope["run"]["provenance"]["details"]
    assert [f["page"] for f in details["pages_failed"]] == [2] and details["pages_processed"] == [1, 3]
    assert any(f["code"] == "PARTIAL_PAGE_COVERAGE" for f in envelope["flags"])
    assert envelope["fields"]["applicant.full_name"]["value"] == "RUPESH KUMAR"
    assert envelope["document_metadata"]["status"] == "needs_review"
    assert len(gallery) == 3                                              # every page is shown


def test_rotated_pages_are_read_upright_and_boxes_map_back_to_the_stored_page(temp_storage, tmp_path, monkeypatch):
    fake = FakeVLM().install(monkeypatch)
    _wire_app(monkeypatch, temp_storage)
    monkeypatch.setattr(RoutedExtractor, "classify", lambda self, *a, **k: {
        "category": "structured", "document_type": None, "subtype": None, "language": "en",
        "confidence": None, "method": "vlm", "router_model": "stub", "rotation": 90})
    calls = _spy_routed(monkeypatch)

    (gallery, _, json_text, _, _, _), _ = _run_app(_write_png(tmp_path), app.AUTO, app.AUTO, app.AUTO, app.AUTO)
    envelope = json.loads(json_text)

    assert Path(fake.calls[0]["image"]).name.startswith("upright_90_")    # the model read the upright copy
    ref = calls[0]["refs"][0]
    seen_box = CASA_VLM_OUTPUT["applicant.full_name"]["bbox"]            # relative to the UPRIGHT image
    expected = _expected_original_bbox(ref, vlm_grounding.unrotate_bbox_norm(tuple(seen_box), 90))
    assert envelope["fields"]["applicant.full_name"]["evidence"]["bbox"] == pytest.approx(expected)
    w, h = ref.page_ref.original_width_px, ref.page_ref.original_height_px
    assert gallery[0][0].size == (h, w)                                   # shown upright (turned like the model's input)


def test_module_c_owns_envelope_assembly_and_the_app_holds_no_extraction_logic():
    """Replaces the vacuous REST-package guard: ownership is checked where it actually lives."""
    src = Path(app.__file__).parent
    assembly_owners = {"pipeline_c.py", "field_assembly.py", "dynamic_adapter.py", "contracts_c.py"}
    model_call_owners = {"extractor.py", "adapters.py"}
    offenders = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(t in text for t in ("CanonicalExtractionEnvelope(", "assemble_canonical_envelope(", "assemble_field_entry(")):
            if path.name not in assembly_owners:
                offenders.append(f"{path.name}: builds envelope/fields outside Module C")
        if ".extract_page_image(" in text and path.name not in model_call_owners:
            offenders.append(f"{path.name}: calls the model directly")
    assert offenders == []

    app_source = Path(app.__file__).read_text(encoding="utf-8")
    for forbidden in ("normalize_and_format", "assemble_", "extract_page_image", "CanonicalExtractionEnvelope(",
                      "calibrated_confidence", "_bbox_to_pixels", "CoordinateTransform"):
        assert forbidden not in app_source, f"app.py must not contain {forbidden!r}"
    assert "process_routed_pages" in app_source and "ModuleCPipeline" in app_source


def test_settings_reach_module_b_and_storage(tmp_path):
    settings = Settings(ocr_backend="none", storage_base_dir=tmp_path / "store")
    pipe_a, pipe_b = app.get_pipelines(settings)

    assert pipe_b.parser.get_metadata().engine_name == "NoParser"      # configured, not the default
    assert pipe_a.settings is settings
    assert Path(pipe_a.storage.base_dir).resolve() == (tmp_path / "store").resolve()
    assert app.get_pipelines(settings) == (pipe_a, pipe_b)              # cached per settings


def test_default_app_pipelines_need_no_tesseract(tmp_path):
    pipe_a, pipe_b = app.get_pipelines(Settings(storage_base_dir=tmp_path / "store"))
    assert pipe_b.parser.get_metadata().engine_name == "PassThrough-NoOCR"


def test_configured_no_parser_backend_fails_clearly_in_the_app(tmp_path, monkeypatch):
    settings = Settings(ocr_backend="none", storage_base_dir=tmp_path / "store")
    FakeVLM().install(monkeypatch)
    (_, status, json_text, _, _, _), _ = _run_app(
        _write_png(tmp_path), "Structured", "DOC-01", app.AUTO, "English", settings=settings
    )
    assert status.startswith("Module B failed: PARSER_NOT_CONFIGURED")
    assert "error" in json.loads(json_text)


def test_create_demo_applies_settings(monkeypatch):
    settings = Settings(ocr_backend="none")
    monkeypatch.setattr(app, "gr", _FakeGradio())
    app.create_demo(settings=settings)
    assert app.get_settings() is settings


class _FakeGradio:
    """Minimal stand-in so create_demo can run without the optional gradio package."""

    class _Any:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def click(self, *a, **k): return None

    def __getattr__(self, name):
        return self._Any


# ----------------------------------------------------------------------------
# 8. API and DynamicExtractionAdapter contracts
# ----------------------------------------------------------------------------

def test_dynamic_adapter_contract_unchanged_without_page_images(lifecycle_manager, sample_payload):
    lifecycle_manager.create_schema("tenant_alpha", build_test_schema_def(), author="alice")
    lifecycle_manager.transition_status("tenant_alpha", "kyc_dynamic_schema", 1, SchemaStatus.TESTING, actor="alice")

    def mock_caller(payload, schema_cls):
        return {"applicant": {"full_name": "ANANYA SHARMA"}, "required_missing_field": "X"}, "req-dyn"

    extractor = SchemaGuidedExtractor(use_llm=True, allow_fallback=False, llm_caller=mock_caller)
    adapter = DynamicExtractionAdapter(manager=lifecycle_manager, extractor=extractor)
    env = adapter.extract_and_assemble("tenant_alpha", "kyc_dynamic_schema", 1, sample_payload)

    assert env.document_metadata.schema_version == 1
    assert env.run.provenance.request_id == "req-dyn"
    assert env.fields["applicant.full_name"].value == "ANANYA SHARMA"


def test_dynamic_adapter_delegates_to_module_c_and_uses_vlm_grounding(
    lifecycle_manager, sample_payload, temp_storage, monkeypatch
):
    lifecycle_manager.create_schema("tenant_alpha", build_test_schema_def(), author="alice")
    lifecycle_manager.transition_status("tenant_alpha", "kyc_dynamic_schema", 1, SchemaStatus.TESTING, actor="alice")

    # Real Module A page image for explicit page-image references.
    handoff = _module_a(temp_storage).process_document(io.BytesIO(_png_bytes()), "kyc.png")
    refs = _module_b(temp_storage).load_page_images(handoff)

    vlm_output = {
        "applicant.full_name": {"value": "ANANYA SHARMA", "bbox": [100, 700, 500, 760]},
        "required_missing_field": {"value": "PROVIDED", "bbox": [100, 800, 500, 860]},
        "nominees.name": {"value": "ROHIT SHARMA", "bbox": [100, 880, 500, 930]},
    }
    fake = FakeVLM(output=vlm_output).install(monkeypatch)
    delegated: List[bool] = []
    original = ModuleCPipeline.run_extraction

    def spy(self, *args, **kwargs):
        delegated.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ModuleCPipeline, "run_extraction", spy)

    extractor = SchemaGuidedExtractor(model_name=VLM_MODEL, use_llm=True, allow_fallback=False)
    adapter = DynamicExtractionAdapter(manager=lifecycle_manager, extractor=extractor)
    env = adapter.extract_and_assemble(
        "tenant_alpha", "kyc_dynamic_schema", 1, sample_payload, page_images=refs
    )

    assert delegated == [True]
    assert len(fake.calls) == 1 and Path(fake.calls[0]["image"]) == refs[0].image_path
    ev = env.fields["applicant.full_name"].evidence
    # Evidence is the transformed VLM box, not the OCR block (10, 50, 200, 70) in sample_payload.
    assert ev.bbox == pytest.approx(_expected_original_bbox(refs[0], vlm_output["applicant.full_name"]["bbox"]))
    assert env.fields["nominees.0.name"].value == "ROHIT SHARMA"
    assert env.run.provenance.details["pages_processed"] == [1]
    assert env.document_metadata.schema_version == 1
