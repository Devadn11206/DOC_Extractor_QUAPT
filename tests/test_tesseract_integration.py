"""
Focused Unit and Integration Tests for Tesseract OCR Integration (Phase A).

Covers:
- normalize_tesseract_confidence: standard scaling, clamping, negative values, NaNs, strings.
- tesseract_raw_extractor:
  - Successful OCR token conversion from word-level dictionary output.
  - Bounding-box correctness in preprocessed 200-DPI coordinate space.
  - Confidence normalization to [0.0, 1.0].
  - Empty OCR output handling (blank pages / whitespace).
  - Invalid confidence value handling (falling back to None).
  - Error propagation on OCR runtime failure (fail-closed, never silently returning empty).
  - Input buffer validation.
- LocalOCRParserAdapter integration:
  - Dependency injection via raw_extractor=tesseract_raw_extractor.
  - LocalOCRParserAdapter.with_tesseract factory method.
  - Coordinate inversion to original 200-DPI space via CoordinateTransform.
- ModuleBPipeline end-to-end integration:
  - Execution with Tesseract parser resulting in valid classification and layout.
  - Fail-closed handling when OCR execution raises an engine error.
- Live test on synthetic in-memory image (exercising real local Tesseract binary).
"""

import io
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, List
import cv2
import numpy as np
from PIL import Image
import pytest

pytesseract = pytest.importorskip(
    "pytesseract", reason="optional OCR extra not installed (pip install -e '.[ocr]')"
)
from doc_extractor.contracts_b import (
    BoundingBox,
    ModuleAPageRef,
    MVPDocumentType,
    PageLayoutOutput,
)
from doc_extractor.exceptions import DocumentProcessingError
from doc_extractor.handoff import ModuleAHandoffPayload
from doc_extractor.loader_b import LoadedPageImage
from doc_extractor.malware import MockMalwareScanner
from doc_extractor.parsing.adapter import LocalOCRParserAdapter
from doc_extractor.parsing.base import ParserEngineError, RawOCRToken
from doc_extractor.parsing.tesseract import (
    TesseractExtractor,
    normalize_tesseract_confidence,
    tesseract_raw_extractor,
)
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.storage import LocalDiskStorage


@pytest.fixture
def temp_storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = LocalDiskStorage(base_dir=tmpdir, tenant_id="tenant-tesseract-test")
        yield storage


@pytest.fixture
def dummy_loaded_page():
    page_ref = ModuleAPageRef(
        page_number=1,
        original_artifact_id="art_orig",
        preprocessed_artifact_id="art_prep",
        original_storage_uri="storage://orig.png",
        preprocessed_storage_uri="storage://prep.png",
        original_width_px=800,
        original_height_px=600,
        preprocessed_width_px=800,
        preprocessed_height_px=600,
        dpi=200,
        applied_rotation_deg=0,
        applied_deskew_deg=0.0,
        metadata_storage_uri="storage://meta.json",
    )
    img = np.full((600, 800, 3), 255, dtype=np.uint8)
    return LoadedPageImage(
        page_number=1,
        image_bgr=img,
        width_px=800,
        height_px=600,
        dpi=200,
        file_path=Path("dummy.png"),
        page_ref=page_ref,
    )


def _create_synthetic_handoff(storage: LocalDiskStorage, tenant_id: str = "tenant-tesseract-test") -> ModuleAHandoffPayload:
    pipeline_a = ModuleAPipeline(
        scanner=MockMalwareScanner(),
        storage=storage,
        tenant_id=tenant_id,
    )
    img = Image.new("RGB", (800, 600), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return pipeline_a.process_document(buf, "test_doc.png")


# ============================================================================
# 1. Confidence Normalization Tests
# ============================================================================

def test_normalize_tesseract_confidence_valid():
    assert normalize_tesseract_confidence(100) == 1.0
    assert normalize_tesseract_confidence(95) == 0.95
    assert normalize_tesseract_confidence(0) == 0.0
    assert normalize_tesseract_confidence(87.5) == 0.875
    assert normalize_tesseract_confidence("92") == 0.92
    assert normalize_tesseract_confidence("45.25") == 0.4525
    # Out-of-bounds positive values clamped to 1.0
    assert normalize_tesseract_confidence(120) == 1.0


def test_normalize_tesseract_confidence_invalid_or_missing():
    # Tesseract convention for undetermined / non-word confidence
    assert normalize_tesseract_confidence(-1) is None
    assert normalize_tesseract_confidence("-1") is None
    assert normalize_tesseract_confidence(-1.0) is None
    assert normalize_tesseract_confidence(-50) is None
    # None, NaNs, infinities, non-numeric strings
    assert normalize_tesseract_confidence(None) is None
    assert normalize_tesseract_confidence(float("nan")) is None
    assert normalize_tesseract_confidence(float("inf")) is None
    assert normalize_tesseract_confidence(float("-inf")) is None
    assert normalize_tesseract_confidence("invalid") is None
    assert normalize_tesseract_confidence("") is None
    assert normalize_tesseract_confidence([]) is None


# ============================================================================
# 2. Token Conversion and Bounding Box Correctness
# ============================================================================

def test_tesseract_raw_extractor_successful_conversion(monkeypatch):
    mock_data = {
        "level": [1, 2, 3, 4, 5, 5],
        "page_num": [1, 1, 1, 1, 1, 1],
        "block_num": [0, 1, 1, 1, 1, 1],
        "par_num": [0, 0, 1, 1, 1, 1],
        "line_num": [0, 0, 0, 1, 1, 1],
        "word_num": [0, 0, 0, 0, 1, 2],
        "left": [0, 10, 10, 10, 50, 180],
        "top": [0, 20, 20, 20, 100, 100],
        "width": [800, 400, 400, 400, 120, 90],
        "height": [600, 150, 150, 50, 30, 30],
        "conf": [-1, -1, -1, -1, 95, 88],
        "text": ["", "", "", "", "APPLICANT", "NAME"],
    }

    monkeypatch.setattr(pytesseract, "image_to_data", lambda *args, **kwargs: mock_data)

    img = np.full((600, 800, 3), 255, dtype=np.uint8)
    tokens = tesseract_raw_extractor(img)

    assert len(tokens) == 2

    # Token 1
    t1 = tokens[0]
    assert t1.text == "APPLICANT"
    assert t1.bbox_preprocessed == (50.0, 100.0, 170.0, 130.0)
    assert t1.confidence == 0.95
    assert t1.reading_order_hint == 0

    # Token 2
    t2 = tokens[1]
    assert t2.text == "NAME"
    assert t2.bbox_preprocessed == (180.0, 100.0, 270.0, 130.0)
    assert t2.confidence == 0.88
    assert t2.reading_order_hint == 1


def test_tesseract_raw_extractor_bounding_box_correctness(monkeypatch):
    mock_data = {
        "text": ["DOC01", "DEGENERATE_W", "DEGENERATE_H", "VALID"],
        "left": [25, 100, 100, 150],
        "top": [40, 50, 50, 60],
        "width": [75, 0, 50, 80],       # second has width=0 -> should be skipped
        "height": [35, 40, 0, 40],      # third has height=0 -> should be skipped
        "conf": [92, 90, 90, 94],
        "level": [5, 5, 5, 5],
    }

    monkeypatch.setattr(pytesseract, "image_to_data", lambda *args, **kwargs: mock_data)

    img = np.full((300, 400, 3), 255, dtype=np.uint8)
    tokens = tesseract_raw_extractor(img)

    # Degenerate boxes are safely filtered
    assert len(tokens) == 2
    assert tokens[0].text == "DOC01"
    assert tokens[0].bbox_preprocessed == (25.0, 40.0, 100.0, 75.0)
    assert tokens[1].text == "VALID"
    assert tokens[1].bbox_preprocessed == (150.0, 60.0, 230.0, 100.0)


# ============================================================================
# 3. Empty and Invalid Output Handling
# ============================================================================

def test_tesseract_raw_extractor_empty_output(monkeypatch):
    # Blank image output with only level 1 container and empty strings
    mock_data = {
        "level": [1],
        "left": [0],
        "top": [0],
        "width": [800],
        "height": [600],
        "conf": [-1],
        "text": ["   "],
    }
    monkeypatch.setattr(pytesseract, "image_to_data", lambda *args, **kwargs: mock_data)

    img = np.full((600, 800, 3), 255, dtype=np.uint8)
    tokens = tesseract_raw_extractor(img)
    assert tokens == []


def test_tesseract_raw_extractor_invalid_confidence_values(monkeypatch):
    mock_data = {
        "text": ["WORD_UNDETERMINED", "WORD_NAN", "WORD_STR_CONF"],
        "left": [10, 100, 200],
        "top": [10, 10, 10],
        "width": [50, 50, 50],
        "height": [20, 20, 20],
        "conf": [-1, "NaN", "bad_value"],
        "level": [5, 5, 5],
    }
    monkeypatch.setattr(pytesseract, "image_to_data", lambda *args, **kwargs: mock_data)

    img = np.full((100, 400, 3), 255, dtype=np.uint8)
    tokens = tesseract_raw_extractor(img)

    assert len(tokens) == 3
    for t in tokens:
        assert t.confidence is None


def test_tesseract_raw_extractor_input_validation():
    # Empty / None buffer raises ParserEngineError
    with pytest.raises(ParserEngineError) as exc_info:
        tesseract_raw_extractor(None)
    assert exc_info.value.code == "EMPTY_IMAGE_BUFFER"

    with pytest.raises(ParserEngineError) as exc_info2:
        tesseract_raw_extractor(np.array([]))
    assert exc_info2.value.code == "EMPTY_IMAGE_BUFFER"


def test_tesseract_raw_extractor_malformed_output(monkeypatch):
    # Tesseract output missing required keys
    monkeypatch.setattr(pytesseract, "image_to_data", lambda *args, **kwargs: {"corrupt": []})

    img = np.full((100, 100, 3), 255, dtype=np.uint8)
    with pytest.raises(ParserEngineError) as exc_info:
        tesseract_raw_extractor(img)
    assert exc_info.value.code == "MALFORMED_OCR_OUTPUT"


# ============================================================================
# 4. Engine Runtime Failure Handling (Requirement 9)
# ============================================================================

def test_tesseract_raw_extractor_runtime_failure_raises_explicit_error(monkeypatch):
    def failing_image_to_data(*args, **kwargs):
        raise RuntimeError("Tesseract binary crashed or exited with non-zero status")

    monkeypatch.setattr(pytesseract, "image_to_data", failing_image_to_data)

    img = np.full((100, 100, 3), 255, dtype=np.uint8)
    with pytest.raises(ParserEngineError) as exc_info:
        tesseract_raw_extractor(img)

    assert exc_info.value.code == "OCR_EXTRACTION_FAILED"
    assert "Tesseract binary crashed" in exc_info.value.message


# ============================================================================
# 5. LocalOCRParserAdapter Integration
# ============================================================================

def test_adapter_with_injected_tesseract_extractor(dummy_loaded_page, monkeypatch):
    mock_data = {
        "text": ["BANK", "ACCOUNT", "OPENING"],
        "left": [20, 120, 240],
        "top": [50, 50, 50],
        "width": [80, 100, 90],
        "height": [25, 25, 25],
        "conf": [96, 94, 95],
        "level": [5, 5, 5],
    }
    monkeypatch.setattr(pytesseract, "image_to_data", lambda *args, **kwargs: mock_data)

    # Dependency injection: pass tesseract_raw_extractor as raw_extractor
    adapter = LocalOCRParserAdapter(
        engine_name="Tesseract-OCR",
        engine_version="5.5.3",
        raw_extractor=tesseract_raw_extractor,
    )

    result = adapter.parse_page(dummy_loaded_page)

    assert isinstance(result, PageLayoutOutput)
    assert result.page_number == 1
    assert len(result.blocks) == 3
    assert result.mean_ocr_confidence == 0.95
    assert "BANK" in result.markdown_content
    assert "ACCOUNT" in result.markdown_content
    assert "OPENING" in result.markdown_content


def test_adapter_factory_with_tesseract(dummy_loaded_page, monkeypatch):
    mock_data = {
        "text": ["INSURANCE", "APPLICATION"],
        "left": [10, 150],
        "top": [20, 20],
        "width": [120, 140],
        "height": [30, 30],
        "conf": [90, 92],
        "level": [5, 5],
    }
    monkeypatch.setattr(pytesseract, "image_to_data", lambda *args, **kwargs: mock_data)

    # Use class factory method
    adapter = LocalOCRParserAdapter.with_tesseract(
        engine_version="5.5.3", lang="eng", config="--psm 6", verify=False
    )
    meta = adapter.get_metadata()
    assert meta.engine_name == "Tesseract-OCR"
    assert meta.engine_version == "5.5.3"
    assert meta.is_operational is True

    result = adapter.parse_page(dummy_loaded_page)
    assert len(result.blocks) == 2
    assert result.blocks[0].text == "INSURANCE"
    assert result.blocks[1].text == "APPLICATION"


def test_adapter_tesseract_runtime_failure_propagates_parser_engine_error(dummy_loaded_page, monkeypatch):
    def broken_image_to_data(*args, **kwargs):
        raise RuntimeError("Low-level OS socket error executing Tesseract")

    monkeypatch.setattr(pytesseract, "image_to_data", broken_image_to_data)

    adapter = LocalOCRParserAdapter.with_tesseract(verify=False)
    with pytest.raises(ParserEngineError) as exc_info:
        adapter.parse_page(dummy_loaded_page)

    assert exc_info.value.code == "OCR_EXTRACTION_FAILED"


# ============================================================================
# 6. ModuleBPipeline Integration Tests
# ============================================================================

def test_module_b_pipeline_with_tesseract_success(temp_storage, monkeypatch):
    mock_data = {
        "text": [
            "BANK", "ACCOUNT", "OPENING", "FORM",
            "CASA", "APPLICATION",
            "CIF", "NUMBER",
            "INITIAL", "DEPOSIT",
            "CHEQUE", "BOOK",
            "DEBIT", "CARD",
        ],
        "left": [50, 150, 280, 410, 50, 160, 50, 120, 50, 150, 50, 150, 50, 150],
        "top": [50, 50, 50, 50, 110, 110, 170, 170, 230, 230, 290, 290, 350, 350],
        "width": [90, 110, 110, 90, 90, 150, 60, 90, 90, 100, 90, 70, 80, 70],
        "height": [30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
        "conf": [98, 97, 96, 98, 97, 95, 96, 95, 94, 95, 96, 95, 94, 96],
        "level": [5] * 14,
    }
    monkeypatch.setattr(pytesseract, "image_to_data", lambda *args, **kwargs: mock_data)

    handoff = _create_synthetic_handoff(temp_storage)

    # Inject Tesseract adapter into ModuleBPipeline
    tesseract_parser = LocalOCRParserAdapter.with_tesseract(verify=False)
    pipeline_b = ModuleBPipeline(
        storage=temp_storage,
        parser=tesseract_parser,
        tenant_id="tenant-tesseract-test",
    )

    out_payload = pipeline_b.process_handoff(handoff)

    assert out_payload.status == "processing"
    assert out_payload.parser_model_name == "Tesseract-OCR"
    assert out_payload.classification.document_type == MVPDocumentType.DOC_01_CASA
    assert out_payload.classification.schema_id == "DOC-01"
    assert len(out_payload.pages) == 1
    assert len(out_payload.pages[0].blocks) == 14
    assert "BANK" in out_payload.combined_markdown
    assert "ACCOUNT" in out_payload.combined_markdown
    assert "CASA" in out_payload.combined_markdown
    assert "APPLICATION" in out_payload.combined_markdown


def test_module_b_pipeline_with_tesseract_runtime_failure_fails_closed(temp_storage, monkeypatch):
    def failing_image_to_data(*args, **kwargs):
        raise RuntimeError("Tesseract segmentation fault")

    monkeypatch.setattr(pytesseract, "image_to_data", failing_image_to_data)

    handoff = _create_synthetic_handoff(temp_storage)

    tesseract_parser = LocalOCRParserAdapter.with_tesseract(verify=False)
    pipeline_b = ModuleBPipeline(
        storage=temp_storage,
        parser=tesseract_parser,
        tenant_id="tenant-tesseract-test",
    )

    out_payload = pipeline_b.process_handoff(handoff)

    # Must fail closed with status='failed'
    assert out_payload.status == "failed"
    assert out_payload.error is not None
    assert out_payload.error["code"] == "OCR_EXTRACTION_FAILED"
    assert "Tesseract segmentation fault" in out_payload.error["message"]


# ============================================================================
# 7. Live Synthetic Tesseract Execution (Real Local Binary)
# ============================================================================

@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Local Tesseract binary not available")
def test_live_synthetic_tesseract_binary_execution():
    """
    Executes against the real local Tesseract 5.5.3 binary on a synthetic in-memory image.
    Verifies that the entire pipeline functions end-to-end with real OCR output.
    """
    # Create a 200-DPI high-contrast image with crisp printed text
    img = np.full((200, 600, 3), 255, dtype=np.uint8)
    cv2.putText(img, "DOC EXTRACTOR TEST", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 2)
    cv2.putText(img, "PAN NUMBER ABCDE1234F", (30, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2)

    tokens = tesseract_raw_extractor(img)

    assert len(tokens) >= 4
    extracted_texts = [t.text.upper() for t in tokens]

    assert any("DOC" in t for t in extracted_texts)
    assert any("TEST" in t for t in extracted_texts)
    assert any("PAN" in t for t in extracted_texts)

    for t in tokens:
        assert isinstance(t.text, str)
        assert len(t.text.strip()) > 0
        assert len(t.bbox_preprocessed) == 4
        x0, y0, x1, y1 = t.bbox_preprocessed
        assert x1 >= x0 >= 0
        assert y1 >= y0 >= 0
        if t.confidence is not None:
            assert 0.0 <= t.confidence <= 1.0
