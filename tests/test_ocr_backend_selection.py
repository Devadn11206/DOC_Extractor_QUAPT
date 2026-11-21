"""
Tests for settings-driven OCR backend selection in the normal Module B path.

Covers:
- Default configuration selects the Tesseract backend.
- Explicit backend selection (tesseract / pdfium_hybrid / pdfium / none) reuses the
  existing adapters.
- Explicit no-parser mode and unconfigured adapters are reported non-operational.
- Missing pytesseract / Tesseract executable / language pack / invalid configuration
  fail closed with actionable errors.
- ModuleBPipeline without an injected parser builds one from settings and receives
  real tokens, blocks, markdown and page info (mocked runtime + live Tesseract smoke).
"""

import io
from pathlib import Path
import tempfile
from typing import List

import cv2
import numpy as np
from PIL import Image
import pytest

pytesseract = pytest.importorskip(
    "pytesseract", reason="optional OCR extra not installed (pip install -e '.[ocr]')"
)
from doc_extractor.config import Settings, load_settings
from doc_extractor.exceptions import ConfigurationError
from doc_extractor.handoff import ModuleAHandoffPayload
from doc_extractor.malware import MockMalwareScanner
from doc_extractor.parsing import (
    NoParserEngine,
    build_parser_engine,
    check_tesseract_available,
)
from doc_extractor.parsing.adapter import LocalOCRParserAdapter
from doc_extractor.parsing.base import ParserEngineError
from doc_extractor.parsing.pdfium import HybridPdfParserAdapter, PdfiumTextParserAdapter
from doc_extractor.parsing import tesseract as tesseract_module
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.storage import LocalDiskStorage


# ----------------------------------------------------------------------------
# Fixtures and helpers
# ----------------------------------------------------------------------------

@pytest.fixture
def temp_storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield LocalDiskStorage(base_dir=tmpdir, tenant_id="tenant-ocr")


@pytest.fixture
def fake_tesseract_runtime(monkeypatch):
    """Simulates a healthy Tesseract install without needing the real binary."""
    monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "5.5.3")
    monkeypatch.setattr(pytesseract, "get_languages", lambda config="": ["eng", "osd"])


def _tesseract_runtime_available() -> bool:
    try:
        check_tesseract_available()
        return True
    except ParserEngineError:
        return False


requires_tesseract = pytest.mark.skipif(
    not _tesseract_runtime_available(),
    reason="Local Tesseract binary/pytesseract not available",
)


def _text_png_bytes() -> bytes:
    """A white page with large, crisp printed text."""
    img = np.full((1400, 1000, 3), 255, dtype=np.uint8)
    cv2.putText(img, "BANK ACCOUNT", (60, 200), cv2.FONT_HERSHEY_SIMPLEX, 2.4, (0, 0, 0), 5)
    cv2.putText(img, "OPENING FORM", (60, 400), cv2.FONT_HERSHEY_SIMPLEX, 2.4, (0, 0, 0), 5)
    cv2.putText(img, "CASA APPLICATION", (60, 600), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), 5)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _handoff_from_png(storage: LocalDiskStorage, data: bytes) -> ModuleAHandoffPayload:
    pipeline_a = ModuleAPipeline(
        scanner=MockMalwareScanner(), storage=storage, tenant_id="tenant-ocr"
    )
    return pipeline_a.process_document(io.BytesIO(data), "scan.png")


def _handoff_from_image_only_pdf(storage: LocalDiskStorage, data: bytes) -> ModuleAHandoffPayload:
    """Wraps the PNG into a PDF page that has no embedded text layer."""
    pdf_buf = io.BytesIO()
    Image.open(io.BytesIO(data)).convert("RGB").save(pdf_buf, format="PDF", resolution=200.0)
    pdf_buf.seek(0)
    pipeline_a = ModuleAPipeline(
        scanner=MockMalwareScanner(), storage=storage, tenant_id="tenant-ocr"
    )
    return pipeline_a.process_document(pdf_buf, "scan.pdf")


# ----------------------------------------------------------------------------
# 1. Configuration
# ----------------------------------------------------------------------------

def test_default_settings_select_the_explicit_passthrough_backend():
    s = Settings()
    assert s.ocr_backend == "passthrough"
    assert s.ocr_tesseract_lang == "eng"
    assert s.ocr_tesseract_config == ""
    assert s.ocr_tesseract_cmd is None


def test_invalid_backend_is_rejected_by_configuration():
    with pytest.raises(ConfigurationError):
        load_settings({"ocr_backend": "paddle"})


@pytest.mark.parametrize("bad_lang", ["", "eng;rm -rf", "eng++hin", "../eng", "eng hin"])
def test_invalid_tesseract_language_is_rejected(bad_lang):
    with pytest.raises(ConfigurationError):
        load_settings({"ocr_tesseract_lang": bad_lang})


def test_factory_rejects_unsupported_backend_that_bypassed_validation():
    settings = Settings.model_construct(ocr_backend="paddle")
    with pytest.raises(ConfigurationError) as exc_info:
        build_parser_engine(settings)
    assert "paddle" in str(exc_info.value)


# ----------------------------------------------------------------------------
# 2. Backend selection reuses existing adapters
# ----------------------------------------------------------------------------

def test_tesseract_backend_selects_operational_tesseract_adapter(fake_tesseract_runtime):
    parser = build_parser_engine(Settings(ocr_backend="tesseract"))
    assert isinstance(parser, LocalOCRParserAdapter)
    meta = parser.get_metadata()
    assert meta.engine_name == "Tesseract-OCR"
    assert meta.engine_version == "5.5.3"  # detected from runtime, not hardcoded
    assert meta.is_operational is True
    assert parser.is_operational() is True


def test_tesseract_version_comes_from_runtime(monkeypatch):
    monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "9.9.9")
    monkeypatch.setattr(pytesseract, "get_languages", lambda config="": ["eng"])
    parser = LocalOCRParserAdapter.with_tesseract()
    assert parser.get_metadata().engine_version == "9.9.9"


def test_explicit_tesseract_language_and_config_are_applied(fake_tesseract_runtime, monkeypatch):
    monkeypatch.setattr(pytesseract, "get_languages", lambda config="": ["eng", "hin", "osd"])
    captured = {}

    def fake_image_to_data(image, output_type=None, lang=None, config=None):
        captured["lang"], captured["config"] = lang, config
        return {"text": [], "left": [], "top": [], "width": [], "height": [], "conf": [], "level": []}

    monkeypatch.setattr(pytesseract, "image_to_data", fake_image_to_data)
    settings = Settings(
        ocr_backend="tesseract", ocr_tesseract_lang="eng+hin", ocr_tesseract_config="--psm 6"
    )
    parser = build_parser_engine(settings)
    parser._raw_extractor(np.full((50, 50, 3), 255, dtype=np.uint8))
    assert captured == {"lang": "eng+hin", "config": "--psm 6"}


def test_explicit_pdfium_backend_selects_native_pdf_adapter():
    parser = build_parser_engine(Settings(ocr_backend="pdfium"))
    assert isinstance(parser, PdfiumTextParserAdapter)
    assert parser.is_operational() is True


def test_explicit_hybrid_backend_wraps_tesseract_fallback(fake_tesseract_runtime):
    parser = build_parser_engine(Settings(ocr_backend="pdfium_hybrid"))
    assert isinstance(parser, HybridPdfParserAdapter)
    assert isinstance(parser.native_parser, PdfiumTextParserAdapter)
    assert isinstance(parser.fallback_parser, LocalOCRParserAdapter)
    assert parser.fallback_parser.get_metadata().engine_name == "Tesseract-OCR"
    assert parser.is_operational() is True


# ----------------------------------------------------------------------------
# 3. No-parser mode / unconfigured parsers are never "operational"
# ----------------------------------------------------------------------------

def test_no_parser_mode_is_reported_non_operational():
    parser = build_parser_engine(Settings(ocr_backend="none"))
    assert isinstance(parser, NoParserEngine)
    assert parser.is_operational() is False
    meta = parser.get_metadata()
    assert meta.is_operational is False
    assert meta.engine_name == "NoParser"
    assert "No parser configured" in meta.description


def test_no_parser_mode_refuses_to_parse(temp_storage):
    parser = NoParserEngine()
    pipeline = ModuleBPipeline(storage=temp_storage, parser=parser, tenant_id="tenant-ocr")
    out = pipeline.process_handoff(_handoff_from_png(temp_storage, _text_png_bytes()))
    assert out.status == "failed"
    assert out.error["code"] == "PARSER_NOT_CONFIGURED"
    assert "ocr_backend" in out.error["message"]
    assert out.parser_model_name == "NoParser"


def test_adapter_without_extractor_is_not_operational():
    adapter = LocalOCRParserAdapter()
    assert adapter.is_operational() is False
    assert adapter.get_metadata().is_operational is False


def test_adapter_without_extractor_never_returns_empty_pages(temp_storage):
    adapter = LocalOCRParserAdapter()
    pipeline = ModuleBPipeline(storage=temp_storage, parser=adapter, tenant_id="tenant-ocr")
    out = pipeline.process_handoff(_handoff_from_png(temp_storage, _text_png_bytes()))
    assert out.status == "failed"
    assert out.error["code"] == "PARSER_UNAVAILABLE"
    assert "no OCR extractor is configured" in out.error["message"]


# ----------------------------------------------------------------------------
# 4. Fail-closed runtime checks with actionable errors
# ----------------------------------------------------------------------------

def test_missing_tesseract_binary_fails_closed_with_actionable_error(monkeypatch):
    def not_found():
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", not_found)
    with pytest.raises(ParserEngineError) as exc_info:
        build_parser_engine(Settings(ocr_backend="tesseract"))
    assert exc_info.value.code == "OCR_BINARY_MISSING"
    msg = str(exc_info.value)
    assert "brew install tesseract" in msg and "apt-get install tesseract-ocr" in msg
    assert "ocr_tesseract_cmd" in msg


def test_configured_tesseract_module_b_pipeline_fails_clearly_when_tesseract_missing(monkeypatch, temp_storage):
    def not_found():
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", not_found)
    with pytest.raises(ParserEngineError) as exc_info:
        ModuleBPipeline(storage=temp_storage, tenant_id="tenant-ocr", settings=Settings(ocr_backend="tesseract"))
    assert exc_info.value.code == "OCR_BINARY_MISSING"


def test_missing_pytesseract_package_fails_closed(monkeypatch):
    monkeypatch.setattr(tesseract_module, "pytesseract", None)
    with pytest.raises(ParserEngineError) as exc_info:
        check_tesseract_available()
    assert exc_info.value.code == "OCR_DEPENDENCY_MISSING"
    assert "pip install" in str(exc_info.value)


def test_unusable_tesseract_binary_fails_closed(monkeypatch):
    def broken():
        raise OSError("exec format error")

    monkeypatch.setattr(pytesseract, "get_tesseract_version", broken)
    with pytest.raises(ParserEngineError) as exc_info:
        check_tesseract_available()
    assert exc_info.value.code == "OCR_BINARY_UNUSABLE"


def test_missing_language_pack_fails_closed(monkeypatch):
    monkeypatch.setattr(pytesseract, "get_tesseract_version", lambda: "5.5.3")
    monkeypatch.setattr(pytesseract, "get_languages", lambda config="": ["eng", "osd"])
    with pytest.raises(ParserEngineError) as exc_info:
        build_parser_engine(Settings(ocr_backend="tesseract", ocr_tesseract_lang="eng+hin"))
    assert exc_info.value.code == "OCR_LANGUAGE_MISSING"
    assert "hin" in str(exc_info.value)


def test_configured_tesseract_cmd_that_does_not_exist_fails_closed():
    original_cmd = pytesseract.pytesseract.tesseract_cmd
    settings = Settings(ocr_backend="tesseract", ocr_tesseract_cmd="/nonexistent/path/to/tesseract")
    with pytest.raises(ParserEngineError) as exc_info:
        build_parser_engine(settings)
    assert exc_info.value.code == "OCR_BINARY_MISSING"
    assert "/nonexistent/path/to/tesseract" in str(exc_info.value)
    assert pytesseract.pytesseract.tesseract_cmd == original_cmd  # global state untouched on failure


def test_hybrid_backend_also_fails_closed_without_tesseract(monkeypatch):
    def not_found():
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "get_tesseract_version", not_found)
    with pytest.raises(ParserEngineError) as exc_info:
        build_parser_engine(Settings(ocr_backend="pdfium_hybrid"))
    assert exc_info.value.code == "OCR_BINARY_MISSING"


# ----------------------------------------------------------------------------
# 5. ModuleBPipeline builds its parser from settings and receives real OCR output
# ----------------------------------------------------------------------------

def test_pipeline_default_parser_comes_from_settings(fake_tesseract_runtime, temp_storage):
    default_pipeline = ModuleBPipeline(storage=temp_storage, tenant_id="tenant-ocr")
    assert isinstance(default_pipeline.parser, LocalOCRParserAdapter)
    assert default_pipeline.parser.is_operational() is True

    none_pipeline = ModuleBPipeline(
        storage=temp_storage, tenant_id="tenant-ocr", settings=Settings(ocr_backend="none")
    )
    assert isinstance(none_pipeline.parser, NoParserEngine)
    assert none_pipeline.parser.is_operational() is False


def test_injected_parser_takes_precedence_over_settings(temp_storage):
    injected = LocalOCRParserAdapter(engine_name="Injected", raw_extractor=lambda img: [])
    pipeline = ModuleBPipeline(
        storage=temp_storage,
        parser=injected,
        tenant_id="tenant-ocr",
        settings=Settings(ocr_backend="none"),
    )
    assert pipeline.parser is injected


def test_configured_pipeline_with_mocked_tesseract_receives_tokens_blocks_and_pages(
    fake_tesseract_runtime, monkeypatch, temp_storage
):
    words: List[str] = ["BANK", "ACCOUNT", "OPENING", "FORM"]
    monkeypatch.setattr(
        pytesseract,
        "image_to_data",
        lambda *a, **k: {
            "text": words,
            "left": [60, 260, 520, 760],
            "top": [100, 100, 100, 100],
            "width": [180, 240, 220, 150],
            "height": [60, 60, 60, 60],
            "conf": [96, 95, 94, 97],
            "level": [5, 5, 5, 5],
        },
    )
    pipeline = ModuleBPipeline(storage=temp_storage, tenant_id="tenant-ocr", settings=Settings(ocr_backend="tesseract"))
    out = pipeline.process_handoff(_handoff_from_png(temp_storage, _text_png_bytes()))

    assert out.status in ("processing", "needs_review")
    assert out.error is None
    assert out.parser_model_name == "Tesseract-OCR"
    assert len(out.pages) == 1
    page = out.pages[0]
    assert page.page_number == 1
    assert [b.text for b in page.blocks] == words
    assert page.mean_ocr_confidence is not None
    for word in words:
        assert word in out.combined_markdown


# ----------------------------------------------------------------------------
# 6. Live smoke tests against the real local Tesseract binary (skipped if absent)
# ----------------------------------------------------------------------------

@requires_tesseract
def test_live_tesseract_pipeline_produces_nonempty_parse_from_image(temp_storage):
    pipeline = ModuleBPipeline(storage=temp_storage, tenant_id="tenant-ocr", settings=Settings(ocr_backend="tesseract"))
    assert pipeline.parser.get_metadata().is_operational is True

    out = pipeline.process_handoff(_handoff_from_png(temp_storage, _text_png_bytes()))

    assert out.status != "failed", out.error
    assert out.parser_model_name == "Tesseract-OCR"
    assert len(out.pages) == 1
    page = out.pages[0]
    assert len(page.blocks) >= 4
    assert page.mean_ocr_confidence is not None and 0.0 < page.mean_ocr_confidence <= 1.0
    text = out.combined_markdown.upper()
    assert "BANK" in text and "ACCOUNT" in text and "CASA" in text

    for block in page.blocks:
        assert block.text.strip()
        bb = block.bbox_original
        assert 0 <= bb.x0 < bb.x1 and 0 <= bb.y0 < bb.y1


@requires_tesseract
def test_live_hybrid_backend_falls_back_to_ocr_for_image_only_pdf(temp_storage):
    pipeline = ModuleBPipeline(
        storage=temp_storage,
        tenant_id="tenant-ocr",
        settings=Settings(ocr_backend="pdfium_hybrid"),
    )
    out = pipeline.process_handoff(_handoff_from_image_only_pdf(temp_storage, _text_png_bytes()))

    assert out.status != "failed", out.error
    assert out.parser_model_name == "PDFium+OCR-Hybrid"
    text = out.combined_markdown.upper()
    assert "BANK" in text and "ACCOUNT" in text


@requires_tesseract
def test_live_no_parser_backend_fails_closed_on_real_document(temp_storage):
    pipeline = ModuleBPipeline(
        storage=temp_storage, tenant_id="tenant-ocr", settings=Settings(ocr_backend="none")
    )
    out = pipeline.process_handoff(_handoff_from_png(temp_storage, _text_png_bytes()))
    assert out.status == "failed"
    assert out.error["code"] == "PARSER_NOT_CONFIGURED"
    assert out.pages == []
