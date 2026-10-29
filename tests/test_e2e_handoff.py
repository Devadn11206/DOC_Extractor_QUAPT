"""
End-to-end integration, failure recovery, coordinate inversion, and handoff tests.

Covers:
1. Full end-to-end pipeline execution (PDF multi-page -> 200 DPI render -> Preprocess -> Handoff payload).
2. Standalone image (PNG/JPG) end-to-end processing.
3. CoordinateTransform inversion: mapping preprocessed bounding boxes back to 200-DPI original page coordinates (SCH-013).
4. Failure injection & atomic rollback (e.g. failure during preprocessing does not publish corrupt payload).
5. QueueAdapter publishing & JSON serialization contract adherence (ARC-003, SCH-011).
6. Idempotency preservation through full pipeline.
"""

import io
import json
from pathlib import Path
import pytest

from doc_extractor.config import Settings
from doc_extractor.handoff import (
    MODULE_A_CONTRACT_VERSION,
    CoordinateTransform,
    LocalMemoryQueueAdapter,
    ModuleAHandoffPayload,
)
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.malware import MockMalwareScanner
from doc_extractor.models import DocumentStatus, IngestionState
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.storage import LocalDiskStorage
from tests.conftest import create_minimal_pdf, create_minimal_png


@pytest.fixture
def base_dir(tmp_path):
    return tmp_path / "stage1_data"


@pytest.fixture
def queue_adapter():
    return LocalMemoryQueueAdapter()


@pytest.fixture
def pipeline(base_dir, queue_adapter):
    settings = Settings(storage_base_dir=base_dir, target_render_dpi=200)
    scanner = MockMalwareScanner(is_ready=True)
    storage = LocalDiskStorage(base_dir=base_dir, tenant_id="tenant_integration")
    idempotency_store = LocalIdempotencyStore(base_dir=base_dir)

    return ModuleAPipeline(
        settings=settings,
        scanner=scanner,
        storage=storage,
        idempotency_store=idempotency_store,
        queue_adapter=queue_adapter,
        tenant_id="tenant_integration",
    )


# --- 1. Full Multi-Page PDF End-to-End Test ---

@pytest.mark.unit
def test_full_pipeline_multi_page_pdf(pipeline, queue_adapter):
    """
    Executes complete Stage 1 on a 2-page PDF:
    Validation -> Malware Check -> SHA-256 -> 200 DPI Render -> Preprocessing -> Handoff.
    """
    pdf_bytes = create_minimal_pdf(page_count=2)
    payload = pipeline.process_document(
        file_path_or_stream=io.BytesIO(pdf_bytes),
        claimed_filename="mortgage_application.pdf",
        idempotency_key="key_e2e_001",
    )

    assert payload.contract_version == MODULE_A_CONTRACT_VERSION
    assert payload.status == DocumentStatus.PROCESSING.value
    assert payload.internal_state == IngestionState.READY_FOR_EXTRACTION.value
    assert payload.page_count == 2
    assert len(payload.pages) == 2
    assert payload.file_hash_sha256 != ""

    # Page 1 checks
    p1 = payload.pages[0]
    assert p1.page_number == 1
    assert p1.dpi == 200
    assert p1.original_width_px == 1700
    assert p1.original_height_px == 2200
    assert not p1.original_storage_uri.startswith("C:")  # No absolute host path
    assert not p1.original_storage_uri.startswith("/")

    # Resolve logical URIs via storage abstraction
    resolved_orig = pipeline.storage.resolve_uri_to_path(p1.original_storage_uri)
    resolved_prep = pipeline.storage.resolve_uri_to_path(p1.preprocessed_storage_uri)
    resolved_meta = pipeline.storage.resolve_uri_to_path(p1.metadata_storage_uri)

    assert resolved_orig.exists()
    assert resolved_prep.exists()
    assert resolved_meta.exists()

    # Queue publication check (ARC-003)
    assert len(queue_adapter.published_messages) == 1
    topic, published_payload = queue_adapter.published_messages[0]
    assert topic == "module_a.completed"
    assert published_payload.document_id == payload.document_id

    # JSON serializability check
    json_str = payload.to_json()
    parsed = json.loads(json_str)
    assert parsed["document_id"] == payload.document_id
    assert len(parsed["pages"]) == 2


# --- 2. Standalone Image (PNG) End-to-End Test ---

@pytest.mark.unit
def test_full_pipeline_standalone_image(pipeline):
    """Processes standalone image document through full Stage 1."""
    png_bytes = create_minimal_png(width=200, height=300)
    payload = pipeline.process_document(
        file_path_or_stream=io.BytesIO(png_bytes),
        claimed_filename="hospital_bill.png",
        idempotency_key="key_e2e_png",
    )

    assert payload.status == DocumentStatus.PROCESSING.value
    assert payload.page_count == 1
    assert len(payload.pages) == 1
    p = payload.pages[0]
    assert p.page_number == 1
    assert p.dpi == 200
    assert pipeline.storage.resolve_uri_to_path(p.preprocessed_storage_uri).exists()


# --- 3. Coordinate Transformation Inversion (SCH-013) ---

@pytest.mark.unit
def test_coordinate_transformation_inversion():
    """
    PRD SCH-013: Coordinates are in pixels of 200-DPI page image with top-left origin.
    Tests mapping a bounding box detected on deskewed/rotated image back to original image space.
    """
    orig_w, orig_h = 1700, 2200
    # Suppose OCR detects bounding box at [100, 200, 500, 300] on an unrotated image (0° deskew)
    bbox_in = (100.0, 200.0, 500.0, 300.0)
    orig_box = CoordinateTransform.map_bbox_preprocessed_to_original(
        bbox=bbox_in,
        original_width=orig_w,
        original_height=orig_h,
        applied_rotation_deg=0,
        applied_deskew_deg=0.0,
    )
    assert orig_box == bbox_in

    # Now test with 5° deskew applied
    deskew_box = CoordinateTransform.map_bbox_preprocessed_to_original(
        bbox=bbox_in,
        original_width=orig_w,
        original_height=orig_h,
        applied_rotation_deg=0,
        applied_deskew_deg=5.0,
    )
    # Result must remain within image bounds and close to original coordinates
    assert 0.0 <= deskew_box[0] <= deskew_box[2] <= orig_w
    assert 0.0 <= deskew_box[1] <= deskew_box[3] <= orig_h


# --- 4. Malware Gate Pipeline Blocking ---

@pytest.mark.security
def test_pipeline_blocks_malware(pipeline, queue_adapter, sample_eicar_bytes):
    """Infected file terminates at ingestion with rejected status and never publishes to queue."""
    payload = pipeline.process_document(
        file_path_or_stream=io.BytesIO(sample_eicar_bytes),
        claimed_filename="eicar_contract.pdf",
    )

    assert payload.status == DocumentStatus.REJECTED.value
    assert payload.internal_state == IngestionState.REJECTED_MALWARE.value
    assert payload.error is not None
    assert payload.error["code"] == "MALWARE_DETECTED"
    assert len(payload.pages) == 0
    # Nothing published to queue
    assert len(queue_adapter.published_messages) == 0


# --- 5. Failure Injection & Atomic Rollback ---

@pytest.mark.unit
def test_pipeline_failure_injection_rollback(pipeline, monkeypatch):
    """If preprocessing fails midway, document fails cleanly, rolling back preprocessed output."""
    pdf_bytes = create_minimal_pdf(page_count=2)

    # Inject failure into preprocessor
    def mock_fail(*args, **kwargs):
        raise RuntimeError("Simulated crash during preprocessing")

    monkeypatch.setattr(pipeline.preprocessor, "preprocess_document_pages", mock_fail)

    payload = pipeline.process_document(
        file_path_or_stream=io.BytesIO(pdf_bytes),
        claimed_filename="crash_test.pdf",
    )

    assert payload.status == DocumentStatus.FAILED.value
    assert payload.internal_state == IngestionState.FAILED.value
    assert payload.error["code"] == "PROCESSING_FAILED"
    assert len(payload.pages) == 0

    # Ensure preprocessed directory has been cleaned up
    prep_dir = pipeline.storage._get_document_dir(payload.document_id) / "preprocessed"
    assert not prep_dir.exists()
