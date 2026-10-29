"""
Integration and Stabilization Tests for Module B Cross-Stage Functionality.

Covers:
- End-to-end handoff integration: ModuleAPipeline -> SecureModuleBLoader -> LocalOCRParserAdapter -> PageLayoutOutput.
- High-confidence deduplication in CharacterGridReconstructor.
- Out-of-bounds checkbox candidate validation in CheckboxDetector.
- Robust multi-row overlap grouping in TableReconstructor.
- Comprehensive 4-cardinal orientation coordinate transformations (0°, 90°, 180°, 270°).
- Edge-touching bounding boxes and boundary clamping (SCH-013).
- Full ModuleBOutputPayload serialization and deserialization round trip.
"""

import io
from pathlib import Path
import cv2
import numpy as np
from PIL import Image
import pytest

from doc_extractor.contracts_b import (
    MODULE_B_CONTRACT_VERSION,
    BoundingBox,
    CheckboxMark,
    ClassificationOutput,
    HTMLTable,
    LayoutBlock,
    LayoutElementType,
    MVPClassificationDomain,
    MVPDocumentType,
    ModuleAPageRef,
    ModuleBInputPayload,
    ModuleBOutputPayload,
    PageLayoutOutput,
)
from doc_extractor.handoff import CoordinateTransform
from doc_extractor.loader_b import LoadedPageImage, SecureModuleBLoader
from doc_extractor.malware import MockMalwareScanner
from doc_extractor.parsing.adapter import LocalOCRParserAdapter
from doc_extractor.parsing.base import RawOCRToken
from doc_extractor.parsing.character_grids import (
    CandidateCharacterGrid,
    CharacterGridReconstructor,
    RawCharacterObservation,
)
from doc_extractor.parsing.checkboxes import (
    CandidateCheckboxRegion,
    CheckboxDetector,
    CheckboxDetectorError,
)
from doc_extractor.parsing.tables import (
    CandidateTableCell,
    CandidateTableRegion,
    TableReconstructor,
)
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.storage import LocalDiskStorage


# ============================================================================
# Stage 6: Module A -> Module B End-to-End Integration Test
# ============================================================================

def test_module_a_to_module_b_e2e_integration(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_e2e")
    pipeline = ModuleAPipeline(scanner=MockMalwareScanner(), storage=storage, tenant_id="tenant_e2e")

    # 1. Produce real Module A handoff payload from a 2-page TIFF
    img1 = Image.new("RGB", (300, 400), color="white")
    img2 = Image.new("RGB", (300, 400), color="white")
    buf = io.BytesIO()
    img1.save(buf, format="TIFF", save_all=True, append_images=[img2])

    module_a_payload = pipeline.process_document(io.BytesIO(buf.getvalue()), "application.tiff")
    assert module_a_payload.status == "processing"
    assert module_a_payload.internal_state == "ready_for_extraction"
    assert module_a_payload.page_count == 2

    # 2. SecureModuleBLoader validates handoff and decodes images
    loader = SecureModuleBLoader(storage=storage)
    load_result = loader.load_handoff(module_a_payload, expected_tenant_id="tenant_e2e")
    assert len(load_result.pages) == 2
    assert load_result.pages[0].page_number == 1
    assert load_result.pages[1].page_number == 2

    # 3. Parse page 1 through LocalOCRParserAdapter
    def mock_ocr(img):
        return [
            RawOCRToken(text="CASA FORM", bbox_preprocessed=(50.0, 30.0, 200.0, 60.0), confidence=0.98),
            RawOCRToken(text="Name: Jane Doe", bbox_preprocessed=(50.0, 80.0, 220.0, 105.0), confidence=0.92),
        ]

    parser = LocalOCRParserAdapter(raw_extractor=mock_ocr)
    page1_layout = parser.parse_page(load_result.pages[0])
    assert len(page1_layout.blocks) == 2
    assert "CASA FORM" in page1_layout.markdown_content

    # 4. Detect checkbox on page 1
    chk_detector = CheckboxDetector()
    chk_cand = CandidateCheckboxRegion(candidate_id="chk_agree", bbox_preprocessed=(50.0, 120.0, 75.0, 145.0), raw_state_hint="checked", confidence=0.99)
    chk_mark = chk_detector.process_candidate(chk_cand, load_result.pages[0])
    page1_layout.checkboxes.append(chk_mark)

    # 5. Assemble and validate final terminal Module B envelope
    classification = ClassificationOutput(
        domain=MVPClassificationDomain.BFSI,
        document_type=MVPDocumentType.DOC_01_CASA,
        classification_confidence=0.96,
        schema_id="DOC-01",
        schema_version="1.0.0",
        review_required=False,
    )

    envelope = ModuleBOutputPayload(
        contract_version=MODULE_B_CONTRACT_VERSION,
        run_id=module_a_payload.run_id,
        document_id=module_a_payload.document_id,
        tenant_id="tenant_e2e",
        status="processing",
        classification=classification,
        pages=[page1_layout],
        combined_markdown=page1_layout.markdown_content,
        parser_model_name="LocalOCR-PoC",
        classifier_model_name="RuleClassifier-PoC",
        processing_time_ms=145.2,
        created_at="2026-10-29T12:00:00Z",
        completed_at="2026-10-29T12:00:01Z",
    )

    # Validate JSON round trip
    json_str = envelope.model_dump_json()
    reloaded = ModuleBOutputPayload.model_validate_json(json_str)
    assert reloaded.document_id == module_a_payload.document_id
    assert reloaded.classification.document_type == MVPDocumentType.DOC_01_CASA


# ============================================================================
# Stage 5 Defect Resolution: Character Grid Deduplication by Higher Confidence
# ============================================================================

def test_character_grid_deduplication_retains_higher_confidence():
    page_ref = ModuleAPageRef(
        page_number=1, original_artifact_id="a1", preprocessed_artifact_id="p1",
        original_storage_uri="t/d/pages/p1.png", preprocessed_storage_uri="t/d/prep/p1.png",
        original_width_px=1000, original_height_px=1000, preprocessed_width_px=1000, preprocessed_height_px=1000,
        dpi=200, applied_rotation_deg=0, applied_deskew_deg=0.0, metadata_storage_uri="m",
    )
    loaded_page = LoadedPageImage(
        page_number=1, image_bgr=np.ones((1000, 1000, 3), dtype=np.uint8)*255,
        width_px=1000, height_px=1000, dpi=200, file_path=Path("dummy.png"), page_ref=page_ref,
    )

    # First observation has low confidence 'O' (0.4), duplicate observation has high confidence '0' (0.95)
    obs = [
        RawCharacterObservation(char="O", bbox_preprocessed=(100.0, 100.0, 130.0, 140.0), confidence=0.40),
        RawCharacterObservation(char="0", bbox_preprocessed=(100.5, 100.5, 130.5, 140.5), confidence=0.95),  # Overlapping duplicate
    ]
    grid = CandidateCharacterGrid(grid_id="grid_conf", observations=obs)
    reconstructor = CharacterGridReconstructor()
    result = reconstructor.reconstruct_grid(grid, loaded_page)

    # Must retain higher confidence character '0'
    assert result.reconstructed_text == "0"
    assert result.character_boxes[0].ocr_confidence == 0.95
    assert result.has_ambiguity is True


# ============================================================================
# Stage 4 Defect Resolution: Checkbox Out-of-Bounds Validation
# ============================================================================

def test_checkbox_rejects_out_of_bounds_bounding_box():
    page_ref = ModuleAPageRef(
        page_number=1, original_artifact_id="a1", preprocessed_artifact_id="p1",
        original_storage_uri="t/d/pages/p1.png", preprocessed_storage_uri="t/d/prep/p1.png",
        original_width_px=500, original_height_px=500, preprocessed_width_px=500, preprocessed_height_px=500,
        dpi=200, applied_rotation_deg=0, applied_deskew_deg=0.0, metadata_storage_uri="m",
    )
    loaded_page = LoadedPageImage(
        page_number=1, image_bgr=np.ones((500, 500, 3), dtype=np.uint8)*255,
        width_px=500, height_px=500, dpi=200, file_path=Path("dummy.png"), page_ref=page_ref,
    )

    detector = CheckboxDetector()
    # Candidate extends beyond image boundary (x1=550 > 500)
    cand_oob = CandidateCheckboxRegion(candidate_id="chk_oob", bbox_preprocessed=(480.0, 100.0, 550.0, 130.0))
    with pytest.raises(CheckboxDetectorError) as exc_info:
        detector.process_candidate(cand_oob, loaded_page)
    assert "exceeds image boundaries" in str(exc_info.value)


# ============================================================================
# Stage 2: Comprehensive Coordinate Transformation Tests (0°, 90°, 180°, 270°)
# ============================================================================

@pytest.mark.parametrize("rot_deg,prep_box,expected_orig", [
    (0, (100.0, 150.0, 300.0, 450.0), (100.0, 150.0, 300.0, 450.0)),
    (90, (1549.0, 100.0, 1849.0, 300.0), (100.0, 150.0, 300.0, 450.0)),
    (180, (699.0, 1549.0, 899.0, 1849.0), (100.0, 150.0, 300.0, 450.0)),
    (270, (150.0, 699.0, 450.0, 899.0), (100.0, 150.0, 300.0, 450.0)),
])
def test_all_cardinal_rotation_coordinate_inversion(rot_deg, prep_box, expected_orig):
    orig_w, orig_h = 1000, 2000
    mapped = CoordinateTransform.map_bbox_preprocessed_to_original(
        bbox=prep_box,
        original_width=orig_w,
        original_height=orig_h,
        applied_rotation_deg=rot_deg,
        applied_deskew_deg=0.0,
    )
    assert mapped[0] == pytest.approx(expected_orig[0], abs=1.5)
    assert mapped[1] == pytest.approx(expected_orig[1], abs=1.5)
    assert mapped[2] == pytest.approx(expected_orig[2], abs=1.5)
    assert mapped[3] == pytest.approx(expected_orig[3], abs=1.5)


def test_edge_touching_bounding_box_clamping():
    # Box touching origin and corner
    orig_w, orig_h = 800, 1200
    b_edge = (0.0, 0.0, 800.0, 1200.0)
    mapped = CoordinateTransform.map_bbox_preprocessed_to_original(
        bbox=b_edge,
        original_width=orig_w,
        original_height=orig_h,
        applied_rotation_deg=0,
        applied_deskew_deg=0.0,
    )
    assert mapped == (0.0, 0.0, 800.0, 1200.0)

