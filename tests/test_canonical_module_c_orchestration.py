"""
Phase 1 / Task 4: ModuleCPipeline is the single canonical extraction orchestrator (PRD v1.3).

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
from doc_extractor.storage import LocalDiskStorage
from schema_lifecycle.enums import SchemaStatus

# Fixtures reused from the existing dynamic-adapter tests (not redefined here).
from tests.test_dynamic_module_c_adapter import (  # noqa: F401
    build_test_schema_def,
    lifecycle_manager,
    sample_payload,
)

TENANT = "tenant-canon"
VLM_MODEL = "Qwen/Qwen3-VL-8B-Instruct"

CASA_VLM_OUTPUT: Dict[str, Any] = {
    "applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [100, 120, 150, 420]},
    "applicant.pan_card_number": {"value": "ABCDE1234F", "bbox": [200, 120, 250, 320]},
    "applicant.date_of_birth": {"value": "15/08/1990", "bbox": [300, 120, 350, 320]},
    "applicant.phone_number": {"value": "9876543210", "bbox": [400, 120, 450, 320]},
    "account_details.account_type": {"value": "Savings", "bbox": [500, 120, 550, 320]},
    "account_details.initial_deposit_amount": {"value": "Rs. 5,000.00", "bbox": [600, 120, 650, 320]},
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

    def __init__(self, output: Any = None, error: Exception = None):
        self.output = CASA_VLM_OUTPUT if output is None else output
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def install(self, monkeypatch) -> "FakeVLM":
        fake = self

        def generate(self_extractor, image_input, system_prompt, user_text, max_new_tokens):
            fake.calls.append({"image": str(image_input), "system_prompt": system_prompt})
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
    ymin, xmin, ymax, xmax = bbox_norm
    pr = ref.page_ref
    w, h = pr.preprocessed_width_px, pr.preprocessed_height_px
    return CoordinateTransform.map_bbox_preprocessed_to_original(
        bbox=(xmin * w / 1000.0, ymin * h / 1000.0, xmax * w / 1000.0, ymax * h / 1000.0),
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
    grounding = {"applicant.full_name": FieldGrounding(bbox_norm=(100, 120, 150, 420), raw_value="X")}

    plain = vlm_grounding.grounding_to_evidence_hints(grounding, [ref])["applicant.full_name"]["bbox"]
    skew = vlm_grounding.grounding_to_evidence_hints(grounding, [skewed])["applicant.full_name"]["bbox"]

    assert plain != pytest.approx(skew)
    assert skew == pytest.approx(_expected_original_bbox(skewed, (100, 120, 150, 420)))


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
    bad["applicant.pan_card_number"] = {"value": "NOT-A-PAN", "bbox": [200, 120, 250, 320]}
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
    out["applicant.full_name"] = {"value": "RUPESH KUMAR", "bbox": [100, 120, 1500, 9000]}  # pixels, not 0-1000
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
    "title": {"value": "Gold Shield Plan", "bbox": [50, 100, 120, 600]},
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

def _wire_app(monkeypatch, storage):
    monkeypatch.setattr(app, "get_pipelines", lambda settings=None: (_module_a(storage), _module_b(storage)))


def _write_png(tmp_path: Path) -> str:
    path = tmp_path / "casa_form.png"
    path.write_bytes(_png_bytes())
    return str(path)


def test_gradio_process_document_runs_through_module_c_pipeline(temp_storage, tmp_path, monkeypatch):
    fake = FakeVLM().install(monkeypatch)
    _wire_app(monkeypatch, temp_storage)
    process_calls: List[Any] = []
    original = ModuleCPipeline.process

    def spy(self, payload, *args, **kwargs):
        process_calls.append(kwargs.get("page_images"))
        return original(self, payload, *args, **kwargs)

    monkeypatch.setattr(ModuleCPipeline, "process", spy)

    image, status, json_text, classification_md, json_file = app.process_document(
        _write_png(tmp_path), "Structured", "DOC-01", app.AUTO, "English", True
    )
    envelope = json.loads(json_text)

    assert len(process_calls) == 1 and process_calls[0]            # ModuleCPipeline.process got page images
    assert len(fake.calls) == 1                                    # ...and drove the VLM
    assert Path(fake.calls[0]["image"]) == Path(process_calls[0][0].image_path)
    assert envelope["document_metadata"]["status"] in (
        "auto_approved", "needs_review"
    )
    assert f"Status: {envelope['document_metadata']['status'].upper()}" in status
    assert envelope["run"]["provenance"]["mode"] == "genuine_llm"
    assert envelope["fields"]["applicant.full_name"]["value"] == "RUPESH KUMAR"
    assert image is not None and Path(json_file).is_file()


def test_gradio_cpu_mode_also_uses_module_c(temp_storage, tmp_path, monkeypatch):
    _wire_app(monkeypatch, temp_storage)
    process_calls: List[Any] = []
    original = ModuleCPipeline.process

    def spy(self, payload, *args, **kwargs):
        process_calls.append(self.extractor.use_llm)
        return original(self, payload, *args, **kwargs)

    monkeypatch.setattr(ModuleCPipeline, "process", spy)
    _, status, json_text, _, _ = app.process_document(
        _write_png(tmp_path), "Structured", "DOC-01", app.AUTO, "English", False
    )
    assert process_calls == [False]
    assert json.loads(json_text)["run"]["provenance"]["mode"] == "heuristic"
    assert status.startswith("Status: ")


def test_gradio_generic_category_goes_through_process_generic(temp_storage, tmp_path, monkeypatch):
    fake = FakeVLM(output=GENERIC_OUTPUT).install(monkeypatch)
    _wire_app(monkeypatch, temp_storage)
    generic_calls: List[Dict[str, Any]] = []
    original = ModuleCPipeline.process_generic

    def spy(self, payload, page_images, system_prompt, **kwargs):
        generic_calls.append(kwargs)
        return original(self, payload, page_images, system_prompt, **kwargs)

    monkeypatch.setattr(ModuleCPipeline, "process_generic", spy)
    _, status, json_text, _, _ = app.process_document(
        _write_png(tmp_path), "Handwritten", "DOC-03", app.AUTO, "English", True
    )
    envelope = json.loads(json_text)
    assert generic_calls and generic_calls[0]["force_review"] is True
    assert envelope["fields"]["title"]["review_required"] is True
    assert envelope["document_metadata"]["status"] in ("auto_approved", "needs_review")
    assert len(fake.calls) == 1


def test_app_has_no_inline_extraction_or_envelope_construction():
    source = Path(app.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "extract_page_image",
        "CanonicalExtractionEnvelope(",
        '"status": "valid"',
        "calibrated_confidence",
        "_bbox_to_pixels",
        "0.95",
    ):
        assert forbidden not in source, f"app.py must not contain {forbidden!r}"
    assert "ModuleCPipeline" in source


def test_settings_reach_module_b_and_storage(tmp_path):
    settings = Settings(ocr_backend="none", storage_base_dir=tmp_path / "store")
    pipe_a, pipe_b = app.get_pipelines(settings)

    assert pipe_b.parser.get_metadata().engine_name == "NoParser"      # configured, not default Tesseract
    assert pipe_a.settings is settings
    assert Path(pipe_a.storage.base_dir).resolve() == (tmp_path / "store").resolve()
    assert app.get_pipelines(settings) == (pipe_a, pipe_b)              # cached per settings


def test_configured_no_parser_backend_fails_clearly_in_the_app(tmp_path, monkeypatch):
    settings = Settings(ocr_backend="none", storage_base_dir=tmp_path / "store")
    FakeVLM().install(monkeypatch)
    _, status, json_text, _, _ = app.process_document(
        _write_png(tmp_path), "Structured", "DOC-01", app.AUTO, "English", True, settings=settings
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

def test_rest_api_package_has_no_competing_extraction_path():
    api_dir = Path(app.__file__).parent / "api"
    sources = "\n".join(p.read_text(encoding="utf-8") for p in api_dir.rglob("*.py"))
    for forbidden in (
        "extract_page_image",
        "CanonicalExtractionEnvelope",
        "assemble_canonical_envelope",
        "SchemaGuidedExtractor",
        "ModuleCPipeline",
    ):
        assert forbidden not in sources


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
        "applicant.full_name": {"value": "ANANYA SHARMA", "bbox": [700, 100, 760, 500]},
        "required_missing_field": {"value": "PROVIDED", "bbox": [800, 100, 860, 500]},
        "nominees.name": {"value": "ROHIT SHARMA", "bbox": [880, 100, 930, 500]},
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
