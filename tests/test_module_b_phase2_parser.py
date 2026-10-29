"""
Tests for Module B Phase 2: Visual Layout Parser Engine PoC.

Verifies:
- Pluggable ParserEngine interface conformance (ARC-002).
- Single-page layout parsing yielding valid PageLayoutOutput (FR-PRS-001).
- Per-block OCR confidence normalized to [0, 1] (FR-PRS-005).
- Downstream coordinate inversion adhering to 200-DPI top-left space (SCH-013).
- Reading-order sorting and layout-aware Markdown generation.
- Error handling on un-operational parser, empty images, or malformed OCR responses.
"""

from pathlib import Path
import numpy as np
import pytest

from doc_extractor.contracts_b import (
    BoundingBox,
    LayoutBlock,
    LayoutElementType,
    ModuleAPageRef,
    PageLayoutOutput,
)
from doc_extractor.loader_b import LoadedPageImage
from doc_extractor.parsing.adapter import LocalOCRParserAdapter
from doc_extractor.parsing.base import (
    ParserEngine,
    ParserEngineError,
    RawOCRToken,
)
from doc_extractor.parsing.markdown import generate_page_markdown, sort_blocks_reading_order


# --- Fixtures ---

@pytest.fixture
def dummy_page_ref():
    return ModuleAPageRef(
        page_number=1,
        original_artifact_id="art_orig_1",
        preprocessed_artifact_id="art_prep_1",
        original_storage_uri="t1/doc1/pages/page_0001.png",
        preprocessed_storage_uri="t1/doc1/preprocessed/page_0001_preprocessed.png",
        original_width_px=1000,
        original_height_px=2000,
        preprocessed_width_px=1000,
        preprocessed_height_px=2000,
        dpi=200,
        applied_rotation_deg=0,
        applied_deskew_deg=0.0,
        metadata_storage_uri="t1/doc1/meta.json",
    )


@pytest.fixture
def dummy_loaded_page(dummy_page_ref):
    # Dummy synthetic 1000x2000 image
    img = np.ones((2000, 1000, 3), dtype=np.uint8) * 255
    return LoadedPageImage(
        page_number=1,
        image_bgr=img,
        width_px=1000,
        height_px=2000,
        dpi=200,
        file_path=Path("dummy.png"),
        page_ref=dummy_page_ref,
    )


# --- Unit Tests ---

def test_parser_engine_metadata_and_interface_conformance():
    adapter = LocalOCRParserAdapter(engine_name="TestOCR", engine_version="0.1.0")
    meta = adapter.get_metadata()
    assert meta.engine_name == "TestOCR"
    assert meta.engine_version == "0.1.0"
    assert meta.is_operational is True
    assert meta.supports_optical_confidence is True


def test_parser_unoperational_raises_error(dummy_loaded_page):
    adapter = LocalOCRParserAdapter(engine_name="TestOCR", is_operational=False)
    with pytest.raises(ParserEngineError) as exc_info:
        adapter.parse_page(dummy_loaded_page)
    assert exc_info.value.code == "PARSER_UNAVAILABLE"


def test_parser_empty_page_returns_valid_layout(dummy_loaded_page):
    # Empty token extractor simulating blank page
    adapter = LocalOCRParserAdapter(raw_extractor=lambda img: [])
    result = adapter.parse_page(dummy_loaded_page)

    assert isinstance(result, PageLayoutOutput)
    assert result.page_number == 1
    assert result.markdown_content == ""
    assert len(result.blocks) == 0
    assert result.mean_ocr_confidence == 1.0


def test_parser_single_page_doc01_casa_mock(dummy_loaded_page):
    # Simulate OCR tokens recognized on a DOC-01 CASA form
    def mock_doc01_tokens(img):
        return [
            RawOCRToken(
                text="STATE BANK OF INDIA",
                bbox_preprocessed=(100.0, 50.0, 450.0, 90.0),
                confidence=0.98,
            ),
            RawOCRToken(
                text="CASA ACCOUNT OPENING FORM",
                bbox_preprocessed=(100.0, 110.0, 550.0, 150.0),
                confidence=0.95,
            ),
            RawOCRToken(
                text="Applicant Name: JOHN DOE",
                bbox_preprocessed=(100.0, 200.0, 400.0, 230.0),
                confidence=0.92,
            ),
        ]

    adapter = LocalOCRParserAdapter(raw_extractor=mock_doc01_tokens)
    result = adapter.parse_page(dummy_loaded_page)

    assert isinstance(result, PageLayoutOutput)
    assert len(result.blocks) == 3
    assert "CASA ACCOUNT OPENING FORM" in result.markdown_content
    assert "JOHN DOE" in result.markdown_content

    # Validate optical confidence (FR-PRS-005)
    for b in result.blocks:
        assert 0.0 <= b.ocr_confidence <= 1.0
    assert result.mean_ocr_confidence == pytest.approx(0.95, 0.01)

    # Validate reading order
    assert result.blocks[0].reading_order_index == 0
    assert result.blocks[1].reading_order_index == 1
    assert result.blocks[2].reading_order_index == 2


def test_parser_single_page_doc03_discharge_summary_mock(dummy_loaded_page):
    # Simulate OCR tokens recognized on a DOC-03 Discharge Summary
    def mock_doc03_tokens(img):
        return [
            RawOCRToken(
                text="CITY GENERAL HOSPITAL",
                bbox_preprocessed=(150.0, 40.0, 500.0, 80.0),
                confidence=0.99,
            ),
            RawOCRToken(
                text="DISCHARGE SUMMARY",
                bbox_preprocessed=(150.0, 90.0, 400.0, 120.0),
                confidence=0.97,
            ),
            RawOCRToken(
                text="Diagnosis: Acute Bronchitis",
                bbox_preprocessed=(150.0, 180.0, 480.0, 210.0),
                confidence=0.89,
            ),
        ]

    adapter = LocalOCRParserAdapter(raw_extractor=mock_doc03_tokens)
    result = adapter.parse_page(dummy_loaded_page)

    assert len(result.blocks) == 3
    assert "DISCHARGE SUMMARY" in result.markdown_content
    assert "Acute Bronchitis" in result.markdown_content


def test_coordinate_inversion_integration_with_parser(dummy_page_ref):
    # Page that had 90 deg rotation applied in Module A
    # Original: W=1000, H=2000. Rotated 90 deg: W=2000, H=1000
    rotated_ref = ModuleAPageRef(
        page_number=1,
        original_artifact_id="art_orig_1",
        preprocessed_artifact_id="art_prep_1",
        original_storage_uri="t1/doc1/pages/page_0001.png",
        preprocessed_storage_uri="t1/doc1/preprocessed/page_0001_preprocessed.png",
        original_width_px=1000,
        original_height_px=2000,
        preprocessed_width_px=2000,
        preprocessed_height_px=1000,
        dpi=200,
        applied_rotation_deg=90,
        applied_deskew_deg=0.0,
        metadata_storage_uri="t1/doc1/meta.json",
    )

    img = np.ones((1000, 2000, 3), dtype=np.uint8) * 255
    loaded_page = LoadedPageImage(
        page_number=1,
        image_bgr=img,
        width_px=2000,
        height_px=1000,
        dpi=200,
        file_path=Path("dummy.png"),
        page_ref=rotated_ref,
    )

    # Box in 90 deg rotated preprocessed image: x in [1799, 1899], y in [100, 200]
    def token_extractor(img):
        return [
            RawOCRToken(
                text="SECTION A",
                bbox_preprocessed=(1799.0, 100.0, 1899.0, 200.0),
                confidence=0.91,
            )
        ]

    adapter = LocalOCRParserAdapter(raw_extractor=token_extractor)
    result = adapter.parse_page(loaded_page)

    block = result.blocks[0]
    # Check preprocessed coordinates
    assert block.bbox_preprocessed.to_tuple() == (1799.0, 100.0, 1899.0, 200.0)
    # Check SCH-013 inverted original coordinates: (100.0, 100.0, 200.0, 200.0)
    assert block.bbox_original.to_tuple() == (100.0, 100.0, 200.0, 200.0)
    # Coordinates must be bounded within original page dimensions
    assert block.bbox_original.x1 <= 1000
    assert block.bbox_original.y1 <= 2000


def test_parser_handles_malformed_token_bounding_box(dummy_loaded_page):
    # Inverted bounding box (x1 < x0)
    def bad_extractor(img):
        return [
            RawOCRToken(
                text="Malformed",
                bbox_preprocessed=(500.0, 100.0, 400.0, 200.0),
                confidence=0.8,
            )
        ]

    adapter = LocalOCRParserAdapter(raw_extractor=bad_extractor)
    with pytest.raises(ParserEngineError) as exc_info:
        adapter.parse_page(dummy_loaded_page)
    assert exc_info.value.code == "MALFORMED_BOUNDING_BOX"


def test_reading_order_preservation():
    # Test sorting algorithm on out-of-order blocks
    bbox1 = BoundingBox(x0=50.0, y0=200.0, x1=200.0, y1=240.0)   # Line 2
    bbox2 = BoundingBox(x0=50.0, y0=50.0, x1=200.0, y1=90.0)     # Line 1 left
    bbox3 = BoundingBox(x0=300.0, y0=52.0, x1=450.0, y1=92.0)    # Line 1 right

    b1 = LayoutBlock(block_id="b1", element_type=LayoutElementType.PARAGRAPH, text="Line 2", ocr_confidence=0.9, bbox_preprocessed=bbox1, bbox_original=bbox1, reading_order_index=0)
    b2 = LayoutBlock(block_id="b2", element_type=LayoutElementType.HEADER, text="Title", ocr_confidence=0.9, bbox_preprocessed=bbox2, bbox_original=bbox2, reading_order_index=1)
    b3 = LayoutBlock(block_id="b3", element_type=LayoutElementType.PARAGRAPH, text="Date", ocr_confidence=0.9, bbox_preprocessed=bbox3, bbox_original=bbox3, reading_order_index=2)

    ordered = sort_blocks_reading_order([b1, b2, b3])
    texts = [b.text for b in ordered]
    assert texts == ["Title", "Date", "Line 2"]
    assert ordered[0].reading_order_index == 0
    assert ordered[1].reading_order_index == 1
    assert ordered[2].reading_order_index == 2

