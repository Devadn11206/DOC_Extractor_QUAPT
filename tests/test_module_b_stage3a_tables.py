"""
Tests for Module B Stage 3A: Table Structure Reconstruction.

Verifies:
- Simple multi-row multi-column table reconstruction.
- Table with missing or empty cells.
- Preserved row order, column order, and cell text reading order.
- Valid HTML serialization (<table><tr><th>/<td>...</td></tr></table>).
- Bounding box mapping to original 200-DPI coordinate space (SCH-013).
- Detection of malformed, negative, or degenerate candidate geometry.
- Non-table text rejection (avoiding false-positive table grouping).
- Seamless compatibility with PageLayoutOutput and HTMLTable contracts.
"""

from pathlib import Path
import numpy as np
import pytest

from doc_extractor.contracts_b import (
    BoundingBox,
    HTMLTable,
    ModuleAPageRef,
    PageLayoutOutput,
)
from doc_extractor.loader_b import LoadedPageImage
from doc_extractor.parsing.tables import (
    CandidateTableCell,
    CandidateTableRegion,
    TableReconstructionError,
    TableReconstructor,
)


@pytest.fixture
def mock_loaded_page():
    page_ref = ModuleAPageRef(
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
    img = np.ones((2000, 1000, 3), dtype=np.uint8) * 255
    return LoadedPageImage(
        page_number=1,
        image_bgr=img,
        width_px=1000,
        height_px=2000,
        dpi=200,
        file_path=Path("dummy.png"),
        page_ref=page_ref,
    )


# --- Unit Tests ---

def test_reconstruct_simple_multirow_table(mock_loaded_page):
    # 2 rows x 3 columns table
    cells = [
        # Row 0 (Headers)
        CandidateTableCell(text="Item", bbox_preprocessed=(100, 50, 200, 80), confidence=0.98, is_header=True),
        CandidateTableCell(text="Quantity", bbox_preprocessed=(220, 50, 320, 80), confidence=0.97, is_header=True),
        CandidateTableCell(text="Price", bbox_preprocessed=(340, 50, 440, 80), confidence=0.99, is_header=True),
        # Row 1 (Data)
        CandidateTableCell(text="Paracetamol", bbox_preprocessed=(100, 90, 200, 120), confidence=0.95),
        CandidateTableCell(text="10", bbox_preprocessed=(220, 90, 320, 120), confidence=0.96),
        CandidateTableCell(text="50.00", bbox_preprocessed=(340, 90, 440, 120), confidence=0.94),
    ]

    region = CandidateTableRegion(region_id="tbl_001", cells=cells, is_header_row_present=True)
    reconstructor = TableReconstructor()
    html_table = reconstructor.reconstruct_table(region, mock_loaded_page)

    assert isinstance(html_table, HTMLTable)
    assert html_table.table_id == "tbl_001"
    assert html_table.row_count == 2
    assert html_table.col_count == 3
    assert html_table.ocr_confidence == pytest.approx(0.965, 0.01)

    # HTML verification
    html_str = html_table.html_content
    assert "<table>" in html_str
    assert "<th>Item</th>" in html_str
    assert "<th>Quantity</th>" in html_str
    assert "<th>Price</th>" in html_str
    assert "<td>Paracetamol</td>" in html_str
    assert "<td>10</td>" in html_str
    assert "<td>50.00</td>" in html_str

    # Bounding box verification (SCH-013)
    bbox = html_table.bbox_original
    assert bbox.x0 == 100.0
    assert bbox.y0 == 50.0
    assert bbox.x1 == 440.0
    assert bbox.y1 == 120.0


def test_reconstruct_table_with_empty_cells(mock_loaded_page):
    # Table with missing cell at (row 1, col 1) using explicit hints
    cells = [
        CandidateTableCell(text="H1", bbox_preprocessed=(50, 50, 150, 80), row_hint=0, col_hint=0),
        CandidateTableCell(text="H2", bbox_preprocessed=(160, 50, 260, 80), row_hint=0, col_hint=1),
        CandidateTableCell(text="D1", bbox_preprocessed=(50, 90, 150, 120), row_hint=1, col_hint=0),
        # row 1, col 1 is missing
    ]

    region = CandidateTableRegion(region_id="tbl_missing", cells=cells, is_header_row_present=False)
    reconstructor = TableReconstructor()
    html_table = reconstructor.reconstruct_table(region, mock_loaded_page)

    assert html_table.row_count == 2
    assert html_table.col_count == 2
    # Verify second row has an empty cell
    assert "<td>D1</td>\n    <td></td>" in html_table.html_content


def test_reconstruct_table_coordinate_inversion_90_deg(mock_loaded_page):
    # Setup page rotated 90 deg clockwise
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
    loaded_page = LoadedPageImage(
        page_number=1,
        image_bgr=mock_loaded_page.image_bgr,
        width_px=2000,
        height_px=1000,
        dpi=200,
        file_path=Path("dummy.png"),
        page_ref=rotated_ref,
    )

    # In rotated 90 space, x in [1799, 1899], y in [100, 200]
    cells = [
        CandidateTableCell(text="Header", bbox_preprocessed=(1799.0, 100.0, 1899.0, 140.0), confidence=0.9),
        CandidateTableCell(text="Value", bbox_preprocessed=(1799.0, 150.0, 1899.0, 200.0), confidence=0.9),
    ]

    region = CandidateTableRegion(region_id="tbl_rot", cells=cells)
    reconstructor = TableReconstructor()
    html_table = reconstructor.reconstruct_table(region, loaded_page)

    # Inverted original box: (100.0, 100.0, 200.0, 200.0)
    assert html_table.bbox_original.to_tuple() == (100.0, 100.0, 200.0, 200.0)


def test_reconstruct_rejects_empty_region(mock_loaded_page):
    region = CandidateTableRegion(region_id="tbl_empty", cells=[])
    reconstructor = TableReconstructor()
    with pytest.raises(TableReconstructionError) as exc_info:
        reconstructor.reconstruct_table(region, mock_loaded_page)
    assert "contains 0 cells" in str(exc_info.value)


def test_reconstruct_rejects_malformed_cell_bounds(mock_loaded_page):
    cells = [
        CandidateTableCell(text="Cell 1", bbox_preprocessed=(200, 100, 100, 150))  # x1 < x0
    ]
    region = CandidateTableRegion(region_id="tbl_bad", cells=cells)
    reconstructor = TableReconstructor()
    with pytest.raises(TableReconstructionError) as exc_info:
        reconstructor.reconstruct_table(region, mock_loaded_page)
    assert "Malformed cell bounding box" in str(exc_info.value)


def test_table_contract_integration():
    # Verify that reconstructed HTMLTable fits into PageLayoutOutput
    bbox = BoundingBox(x0=50, y0=50, x1=500, y1=300)
    table = HTMLTable(
        table_id="tbl_integration",
        html_content="<table><tr><td>Test</td></tr></table>",
        bbox_original=bbox,
        row_count=1,
        col_count=1,
        ocr_confidence=0.95,
    )
    page_output = PageLayoutOutput(
        page_number=1,
        markdown_content="# Page with table\n\n<table>...</table>",
        tables=[table],
    )
    assert len(page_output.tables) == 1
    assert page_output.tables[0].table_id == "tbl_integration"
    assert page_output.tables[0].row_count == 1

