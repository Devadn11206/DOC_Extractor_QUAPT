"""
Tests for Module B Stage 3C: Character-Grid Reconstruction.

Verifies:
- Merging single-row segmented character grids (e.g. '|J|O|H|N|' -> 'JOHN') (FR-PRS-003).
- Multiple rows with explicit row/column hints.
- Geometric ordering without explicit hints.
- Handling varying box widths, spacing, and delimiters.
- Handling overlapping/duplicate character observations.
- Coordinate-transform integration with original 200-DPI space (SCH-013).
- Preserved per-character evidence tracing and confidence provenance.
- Malformed / degenerate input error handling.
- Compatibility with PageLayoutOutput contract.
"""

from pathlib import Path
import numpy as np
import pytest

from doc_extractor.contracts_b import (
    BoundingBox,
    CharacterGridMark,
    ModuleAPageRef,
    PageLayoutOutput,
)
from doc_extractor.loader_b import LoadedPageImage
from doc_extractor.parsing.character_grids import (
    CandidateCharacterGrid,
    CharacterGridError,
    CharacterGridReconstructor,
    RawCharacterObservation,
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

def test_reconstruct_single_row_pan_grid(mock_loaded_page):
    # Simulates PAN card character grid: 10 boxes forming 'ABCDE1234F'
    chars = ["A", "B", "C", "D", "E", "1", "2", "3", "4", "F"]
    observations = []
    x = 100.0
    for ch in chars:
        observations.append(
            RawCharacterObservation(
                char=ch,
                bbox_preprocessed=(x, 200.0, x + 30.0, 240.0),
                confidence=0.96,
            )
        )
        x += 35.0  # 30px width + 5px spacing

    grid = CandidateCharacterGrid(grid_id="pan_01", observations=observations)
    reconstructor = CharacterGridReconstructor()
    result = reconstructor.reconstruct_grid(grid, mock_loaded_page)

    assert isinstance(result, CharacterGridMark)
    assert result.grid_id == "pan_01"
    assert result.reconstructed_text == "ABCDE1234F"
    assert " " not in result.reconstructed_text
    assert len(result.character_boxes) == 10
    assert result.mean_ocr_confidence == pytest.approx(0.96, 0.01)
    assert result.has_ambiguity is False

    # Check evidence traceability
    for idx, box in enumerate(result.character_boxes):
        assert box.char == chars[idx]
        assert box.char_index == idx
        assert box.bbox_original.to_tuple() == observations[idx].bbox_preprocessed


def test_reconstruct_filters_box_delimiters(mock_loaded_page):
    # Simulates '|J|O|H|N|' with explicit vertical bar tokens
    raw = [
        RawCharacterObservation(char="|", bbox_preprocessed=(95, 100, 100, 140)),
        RawCharacterObservation(char="J", bbox_preprocessed=(100, 100, 130, 140), confidence=0.92),
        RawCharacterObservation(char="|", bbox_preprocessed=(130, 100, 135, 140)),
        RawCharacterObservation(char="O", bbox_preprocessed=(135, 100, 165, 140), confidence=0.94),
        RawCharacterObservation(char="|", bbox_preprocessed=(165, 100, 170, 140)),
        RawCharacterObservation(char="H", bbox_preprocessed=(170, 100, 200, 140), confidence=0.91),
        RawCharacterObservation(char="|", bbox_preprocessed=(200, 100, 205, 140)),
        RawCharacterObservation(char="N", bbox_preprocessed=(205, 100, 235, 140), confidence=0.95),
        RawCharacterObservation(char="|", bbox_preprocessed=(235, 100, 240, 140)),
    ]

    grid = CandidateCharacterGrid(grid_id="name_grid", observations=raw)
    reconstructor = CharacterGridReconstructor()
    result = reconstructor.reconstruct_grid(grid, mock_loaded_page)

    assert result.reconstructed_text == "JOHN"
    assert len(result.character_boxes) == 4


def test_reconstruct_multi_row_with_hints(mock_loaded_page):
    # Two-row grid (Row 0: 'CASA', Row 1: 'FORM')
    row0 = [
        RawCharacterObservation(char="C", bbox_preprocessed=(100, 100, 130, 140), row_hint=0, col_hint=0, confidence=0.95),
        RawCharacterObservation(char="A", bbox_preprocessed=(135, 100, 165, 140), row_hint=0, col_hint=1, confidence=0.95),
        RawCharacterObservation(char="S", bbox_preprocessed=(170, 100, 200, 140), row_hint=0, col_hint=2, confidence=0.95),
        RawCharacterObservation(char="A", bbox_preprocessed=(205, 100, 235, 140), row_hint=0, col_hint=3, confidence=0.95),
    ]
    row1 = [
        RawCharacterObservation(char="F", bbox_preprocessed=(100, 150, 130, 190), row_hint=1, col_hint=0, confidence=0.95),
        RawCharacterObservation(char="O", bbox_preprocessed=(135, 150, 165, 190), row_hint=1, col_hint=1, confidence=0.95),
        RawCharacterObservation(char="R", bbox_preprocessed=(170, 150, 200, 190), row_hint=1, col_hint=2, confidence=0.95),
        RawCharacterObservation(char="M", bbox_preprocessed=(205, 150, 235, 190), row_hint=1, col_hint=3, confidence=0.95),
    ]

    grid = CandidateCharacterGrid(grid_id="multirow_grid", observations=row0 + row1)
    reconstructor = CharacterGridReconstructor()
    result = reconstructor.reconstruct_grid(grid, mock_loaded_page)

    assert result.reconstructed_text == "CASAFORM"
    assert len(result.character_boxes) == 8


def test_reconstruct_deduplicates_overlapping_boxes(mock_loaded_page):
    # Two overlapping observations for the same box 'A'
    obs = [
        RawCharacterObservation(char="A", bbox_preprocessed=(100, 100, 130, 140), confidence=0.95),
        RawCharacterObservation(char="A", bbox_preprocessed=(101, 100, 131, 140), confidence=0.90),  # Duplicate overlap
        RawCharacterObservation(char="B", bbox_preprocessed=(140, 100, 170, 140), confidence=0.95),
    ]

    grid = CandidateCharacterGrid(grid_id="dup_grid", observations=obs)
    reconstructor = CharacterGridReconstructor()
    result = reconstructor.reconstruct_grid(grid, mock_loaded_page)

    assert result.reconstructed_text == "AB"
    assert len(result.character_boxes) == 2
    assert result.has_ambiguity is True  # Ambiguity flagged due to overlap deduplication


def test_reconstruct_coordinate_inversion_90_deg(mock_loaded_page):
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

    obs = [
        RawCharacterObservation(char="X", bbox_preprocessed=(1799.0, 100.0, 1899.0, 200.0), confidence=0.92)
    ]
    grid = CandidateCharacterGrid(grid_id="rot_grid", observations=obs)
    reconstructor = CharacterGridReconstructor()
    result = reconstructor.reconstruct_grid(grid, loaded_page)

    # Inverted box to original: (100.0, 100.0, 200.0, 200.0)
    assert result.bbox_original.to_tuple() == (100.0, 100.0, 200.0, 200.0)
    assert result.character_boxes[0].bbox_original.to_tuple() == (100.0, 100.0, 200.0, 200.0)


def test_reconstruct_rejects_empty_and_malformed():
    reconstructor = CharacterGridReconstructor()

    # Empty
    with pytest.raises(CharacterGridError) as exc_info:
        reconstructor.reconstruct_grid(CandidateCharacterGrid("e", []), None)  # type: ignore
    assert "observations list is empty" in str(exc_info.value)

    # Malformed bounding box (x1 < x0)
    bad_obs = [RawCharacterObservation(char="A", bbox_preprocessed=(200, 100, 100, 140))]
    with pytest.raises(CharacterGridError) as exc_info:
        reconstructor.reconstruct_grid(CandidateCharacterGrid("b", bad_obs), None)  # type: ignore
    assert "Malformed character bounding box" in str(exc_info.value)


def test_character_grid_contract_integration():
    bbox = BoundingBox(x0=50, y0=50, x1=200, y1=80)
    grid_mark = CharacterGridMark(
        grid_id="grid_int",
        reconstructed_text="PAN12345",
        mean_ocr_confidence=0.97,
        bbox_preprocessed=bbox,
        bbox_original=bbox,
        character_boxes=[],
    )
    page_output = PageLayoutOutput(
        page_number=1,
        markdown_content="# Form\n\nPAN: PAN12345",
        character_grids=[grid_mark],
    )
    assert len(page_output.character_grids) == 1
    assert page_output.character_grids[0].reconstructed_text == "PAN12345"

