"""
Module B explicit pass-through backend (PRD v1.5 Module B scope: classify, bind, pass image paths).

These tests never import pytesseract and simulate its absence, so they hold in an environment
with neither the Python wrapper nor the Tesseract executable (e.g. a fresh Kaggle image).
"""

import io
import shutil
import tempfile

import numpy as np
import pytest
import cv2

from doc_extractor.config import Settings
from doc_extractor.handoff import ModuleAHandoffPayload
from doc_extractor.malware import MockMalwareScanner
from doc_extractor.parsing import PassThroughParserEngine, build_parser_engine
from doc_extractor.parsing import tesseract as tesseract_module
from doc_extractor.parsing.base import ParserEngineError
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.storage import LocalDiskStorage


@pytest.fixture
def storage():
    with tempfile.TemporaryDirectory() as tmp:
        yield LocalDiskStorage(base_dir=tmp, tenant_id="tenant-pt")


@pytest.fixture
def no_tesseract(monkeypatch):
    """Neither the pytesseract package nor the executable exists."""
    monkeypatch.setattr(tesseract_module, "pytesseract", None)
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)


def _png() -> bytes:
    img = np.full((1400, 1000, 3), 255, dtype=np.uint8)
    cv2.putText(img, "BANK ACCOUNT OPENING FORM", (40, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 4)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _handoff(storage) -> ModuleAHandoffPayload:
    a = ModuleAPipeline(scanner=MockMalwareScanner(), storage=storage, tenant_id="tenant-pt")
    return a.process_document(io.BytesIO(_png()), "scan.png")


def test_default_backend_is_the_explicit_passthrough_and_needs_no_ocr_runtime(storage, no_tesseract):
    assert Settings().ocr_backend == "passthrough"
    pipeline = ModuleBPipeline(storage)          # the bare constructor must work without Tesseract
    meta = pipeline.parser.get_metadata()
    assert isinstance(pipeline.parser, PassThroughParserEngine)
    assert meta.engine_name == "PassThrough-NoOCR"
    assert meta.is_operational is True
    assert meta.supports_optical_confidence is False
    assert "no ocr" in meta.description.lower()


def test_handoff_without_tesseract_preserves_page_images_and_requires_review(storage, no_tesseract):
    pipeline = ModuleBPipeline(storage, tenant_id="tenant-pt")
    handoff = _handoff(storage)
    out = pipeline.process_handoff(handoff)

    assert out.status == "needs_review"                    # insufficient evidence -> review required
    assert out.error is None
    assert out.classification.document_type.value == "UNKNOWN"   # never guesses a type
    assert out.parser_model_name == "PassThrough-NoOCR"
    assert len(out.pages) == 1
    page = out.pages[0]
    assert page.blocks == [] and page.mean_ocr_confidence is None   # nothing read, no invented score

    # the page image survives the hand-off: path on the layout AND the authoritative reference agree
    refs = pipeline.load_page_images(handoff)
    assert page.page_image_path and str(refs[0].image_path) == page.page_image_path
    assert refs[0].page_ref.preprocessed_width_px > 0              # geometry comes from the reference


def test_passthrough_is_not_a_silent_fallback_for_a_configured_ocr_backend(storage, no_tesseract):
    """Choosing Tesseract without it installed fails loudly; it never degrades to pass-through."""
    for backend in ("tesseract", "pdfium_hybrid"):
        with pytest.raises(ParserEngineError) as exc:
            ModuleBPipeline(storage, settings=Settings(ocr_backend=backend))
        assert exc.value.code == "OCR_DEPENDENCY_MISSING"
        assert ".[ocr]" in str(exc.value) and "passthrough" in str(exc.value)   # tells how to opt in, or to stay on the default


def test_none_backend_still_fails_closed_unlike_passthrough(storage, no_tesseract):
    out = ModuleBPipeline(storage, tenant_id="tenant-pt", settings=Settings(ocr_backend="none")).process_handoff(
        _handoff(storage)
    )
    assert out.status == "failed" and out.error["code"] == "PARSER_NOT_CONFIGURED"


def test_factory_builds_passthrough_for_default_settings_without_runtime(no_tesseract):
    assert isinstance(build_parser_engine(), PassThroughParserEngine)
    assert isinstance(build_parser_engine(Settings(ocr_backend="passthrough")), PassThroughParserEngine)
