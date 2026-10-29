"""
Tests for Module B Stage 3B: Checkbox Detection and State Representation.

Verifies:
- Clearly checked boxes rendered as '[x]' (FR-PRS-002).
- Clearly unchecked boxes rendered as '[ ]'.
- Ambiguous marks classified as '[?]' without silent fabrication.
- Upstream candidate hints vs. pixel-level interior fill analysis.
- SCH-013 original 200-DPI coordinate inversion.
- Degenerate / malformed bounding box error handling.
- Seamless integration with PageLayoutOutput.checkboxes contract.
"""

from pathlib import Path
import cv2
import numpy as np
import pytest

from doc_extractor.contracts_b import (
    BoundingBox,
    CheckboxMark,
    ModuleAPageRef,
    PageLayoutOutput,
)
from doc_extractor.loader_b import LoadedPageImage
from doc_extractor.parsing.checkboxes import (
    CandidateCheckboxRegion,
    CheckboxDetector,
    CheckboxDetectorError,
    CheckboxState,
)


@pytest.fixture
def mock_page_with_boxes():
    # Synthetic 1000x2000 page
    img = np.ones((2000, 1000, 3), dtype=np.uint8) * 255

    # Draw box 1: Checked (at x=[100, 140], y=[200, 240])
    cv2.rectangle(img, (100, 200), (140, 240), (0, 0, 0), 2)
    # Draw dark tick/cross mark inside box 1
    cv2.line(img, (108, 208), (132, 232), (0, 0, 0), 4)
    cv2.line(img, (132, 208), (108, 232), (0, 0, 0), 4)

    # Draw box 2: Unchecked (at x=[100, 140], y=[300, 340])
    cv2.rectangle(img, (100, 300), (140, 340), (0, 0, 0), 2)

    # Draw box 3: Ambiguous smudge/noise (at x=[100, 140], y=[400, 440])
    cv2.rectangle(img, (100, 400), (140, 440), (0, 0, 0), 2)
    # Circle of radius 6 creates ~113 dark pixels inside 576 interior (~0.196 fill ratio, between 0.08 and 0.22)
    cv2.circle(img, (120, 420), 6, (0, 0, 0), -1)

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

def test_detect_checked_box_pixel_analysis(mock_page_with_boxes):
    detector = CheckboxDetector()
    cand = CandidateCheckboxRegion(candidate_id="chk_01", bbox_preprocessed=(100.0, 200.0, 140.0, 240.0))
    mark = detector.process_candidate(cand, mock_page_with_boxes)

    assert isinstance(mark, CheckboxMark)
    assert mark.mark_id == "chk_01"
    assert mark.state == "[x]"
    assert mark.is_uncertain is False
    assert 0.0 <= mark.ocr_confidence <= 1.0
    assert mark.bbox_original.to_tuple() == (100.0, 200.0, 140.0, 240.0)


def test_detect_unchecked_box_pixel_analysis(mock_page_with_boxes):
    detector = CheckboxDetector()
    cand = CandidateCheckboxRegion(candidate_id="chk_02", bbox_preprocessed=(100.0, 300.0, 140.0, 340.0))
    mark = detector.process_candidate(cand, mock_page_with_boxes)

    assert mark.mark_id == "chk_02"
    assert mark.state == "[ ]"
    assert mark.is_uncertain is False
    assert 0.0 <= mark.ocr_confidence <= 1.0


def test_detect_ambiguous_box_renders_uncertain(mock_page_with_boxes):
    detector = CheckboxDetector()
    cand = CandidateCheckboxRegion(candidate_id="chk_03", bbox_preprocessed=(100.0, 400.0, 140.0, 440.0))
    mark = detector.process_candidate(cand, mock_page_with_boxes)

    assert mark.mark_id == "chk_03"
    assert mark.state == "[?]"
    assert mark.is_uncertain is True


def test_detect_via_upstream_hint(mock_page_with_boxes):
    detector = CheckboxDetector()
    # Upstream model supplied hint 'selected' with confidence 0.94
    cand = CandidateCheckboxRegion(
        candidate_id="chk_hint",
        bbox_preprocessed=(100.0, 200.0, 140.0, 240.0),
        raw_state_hint="selected",
        confidence=0.94,
    )
    mark = detector.process_candidate(cand, mock_page_with_boxes)

    assert mark.state == "[x]"
    assert mark.ocr_confidence == 0.94
    assert mark.is_uncertain is False


def test_checkbox_coordinate_inversion_90_deg(mock_page_with_boxes):
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
    # A 90-degree rotated preprocessed image has width 2000, height 1000
    rotated_img = np.ones((1000, 2000, 3), dtype=np.uint8) * 255
    loaded_page = LoadedPageImage(
        page_number=1,
        image_bgr=rotated_img,
        width_px=2000,
        height_px=1000,
        dpi=200,
        file_path=Path("dummy.png"),
        page_ref=rotated_ref,
    )

    # Box in 90 deg rotated preprocessed coordinates
    cand = CandidateCheckboxRegion(
        candidate_id="chk_rot",
        bbox_preprocessed=(1799.0, 100.0, 1899.0, 200.0),
        raw_state_hint="checked",
        confidence=0.95,
    )
    detector = CheckboxDetector()
    mark = detector.process_candidate(cand, loaded_page)

    assert mark.bbox_original.to_tuple() == (100.0, 100.0, 200.0, 200.0)


def test_reject_malformed_checkbox_geometry(mock_page_with_boxes):
    detector = CheckboxDetector()
    # Inverted box (x1 < x0)
    cand_inverted = CandidateCheckboxRegion(candidate_id="chk_bad", bbox_preprocessed=(200, 100, 100, 150))
    with pytest.raises(CheckboxDetectorError) as exc_info:
        detector.process_candidate(cand_inverted, mock_page_with_boxes)
    assert "Malformed checkbox bounding box" in str(exc_info.value)

    # Degenerate box (< min_box_dim_px)
    cand_tiny = CandidateCheckboxRegion(candidate_id="chk_tiny", bbox_preprocessed=(100, 100, 102, 102))
    with pytest.raises(CheckboxDetectorError) as exc_info:
        detector.process_candidate(cand_tiny, mock_page_with_boxes)
    assert "Degenerate checkbox dimensions" in str(exc_info.value)


def test_checkbox_contract_integration():
    bbox = BoundingBox(x0=10, y0=20, x1=30, y1=40)
    mark = CheckboxMark(
        mark_id="chk_int",
        state="[x]",
        ocr_confidence=0.98,
        bbox_preprocessed=bbox,
        bbox_original=bbox,
    )
    page_output = PageLayoutOutput(
        page_number=1,
        markdown_content="# Application Form\n\n[x] Agree to terms",
        checkboxes=[mark],
    )
    assert len(page_output.checkboxes) == 1
    assert page_output.checkboxes[0].state == "[x]"
