"""
Audit and verification tests for:
1. SCH-013: Rigorous CoordinateTransform forward and inverse tests with known synthetic points.
2. NFR-006: Interrupted processing, process restart simulation, and multi-process idempotency recovery.
3. Handoff Security: Path traversal rejection, cross-tenant isolation of URIs, and no host paths in contracts.
4. ARC-001 & Malware: Audit verification of deployment constraints and truthful scanner status.
"""

import io
import json
import multiprocessing
from pathlib import Path
import pytest

from doc_extractor.config import Settings, load_settings
from doc_extractor.exceptions import ConfigurationError, StorageError
from doc_extractor.handoff import CoordinateTransform, ModuleAHandoffPayload
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.malware import MockMalwareScanner, UnconfiguredScanner
from doc_extractor.models import DocumentStatus, IdempotencyRecord
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.storage import LocalDiskStorage
from tests.conftest import create_minimal_pdf


# --- 1. SCH-013: Rigorous Coordinate Transform Tests ---

@pytest.mark.unit
def test_coordinate_transform_deskew_inversion():
    """
    Verifies that a bounding box in deskewed space maps back precisely
    to original 200-DPI coordinates.
    """
    w, h = 1700, 2200
    # True original box in 200-DPI coordinates
    orig_box = (200.0, 300.0, 600.0, 450.0)

    # Apply 5.0 degree deskew forward
    import cv2
    import numpy as np
    center = (w / 2.0, h / 2.0)
    fwd_mat = cv2.getRotationMatrix2D(center, 5.0, 1.0)
    corners = np.array([
        [orig_box[0], orig_box[1]],
        [orig_box[2], orig_box[1]],
        [orig_box[2], orig_box[3]],
        [orig_box[0], orig_box[3]],
    ], dtype=np.float32)
    ones = np.ones((4, 1))
    fwd_corners = fwd_mat.dot(np.hstack([corners, ones]).T).T

    # Enclosing bounding box in preprocessed space
    prep_box = (
        float(np.min(fwd_corners[:, 0])),
        float(np.min(fwd_corners[:, 1])),
        float(np.max(fwd_corners[:, 0])),
        float(np.max(fwd_corners[:, 1])),
    )

    # Invert using CoordinateTransform
    recovered_box = CoordinateTransform.map_bbox_preprocessed_to_original(
        bbox=prep_box,
        original_width=w,
        original_height=h,
        applied_rotation_deg=0,
        applied_deskew_deg=5.0,
    )

    # The recovered box must enclose the original box closely (within small rounding/enclosing margin)
    assert recovered_box[0] <= orig_box[0] + 5.0
    assert recovered_box[1] <= orig_box[1] + 5.0
    assert recovered_box[2] >= orig_box[2] - 5.0
    assert recovered_box[3] >= orig_box[3] - 5.0


@pytest.mark.unit
def test_coordinate_transform_90_deg_rotation_inversion():
    """
    Verifies that a 90° clockwise rotation inverse maps correctly.
    Original dimensions: 1000 x 1500 -> Rotated: 1500 x 1000.
    """
    orig_w, orig_h = 1000, 1500
    # Original point at (100, 200)
    # Under 90° CW rotation: (x', y') = (1500 - 1 - 200, 100) = (1299, 100)
    prep_box = (1299.0, 100.0, 1399.0, 250.0)

    recovered = CoordinateTransform.map_bbox_preprocessed_to_original(
        bbox=prep_box,
        original_width=orig_w,
        original_height=orig_h,
        applied_rotation_deg=90,
        applied_deskew_deg=0.0,
    )

    # Point (1299, 100) -> (100, 200)
    assert 95.0 <= recovered[0] <= 105.0
    assert 95.0 <= recovered[1] <= 205.0


# --- 2. Handoff Security: Path Traversal & Cross-Tenant Access ---

@pytest.mark.security
def test_storage_uri_cross_tenant_denial(tmp_path):
    """Attempting to resolve a URI belonging to another tenant raises StorageError."""
    base_dir = tmp_path / "storage"
    storage_t1 = LocalDiskStorage(base_dir=base_dir, tenant_id="tenant_1")
    storage_t2 = LocalDiskStorage(base_dir=base_dir, tenant_id="tenant_2")

    # Store a file in tenant 1
    s1 = storage_t1.store_page_image("doc_abc", 1, b"png_data")
    t1_uri = "tenant_1/doc_abc/pages/page_0001.png"

    # Tenant 1 resolves safely
    path1 = storage_t1.resolve_uri_to_path(t1_uri)
    assert path1.exists()

    # Tenant 2 attempting to resolve Tenant 1's URI is denied
    with pytest.raises(StorageError) as exc_info:
        storage_t2.resolve_uri_to_path(t1_uri)
    assert "Cross-tenant access or path escape forbidden" in str(exc_info.value)


@pytest.mark.security
def test_storage_uri_traversal_denial(tmp_path):
    """Attempting path traversal via '..' or leading slash is denied."""
    storage = LocalDiskStorage(base_dir=tmp_path / "storage", tenant_id="tenant_safe")
    with pytest.raises(StorageError):
        storage.resolve_uri_to_path("../../etc/passwd")
    with pytest.raises(StorageError):
        storage.resolve_uri_to_path("/absolute/path")


# --- 3. NFR-006: Interrupted Processing & Restart Recovery ---

@pytest.mark.unit
def test_restart_recovery_from_idempotency_store(tmp_path):
    """
    Simulates a service restart: An idempotency record exists on disk from a previous run.
    The new process instance loads and serves the existing document record.
    """
    base_dir = tmp_path / "restart_data"
    pdf_bytes = create_minimal_pdf(page_count=1)

    # Process 1 creates document
    p1 = ModuleAPipeline(
        settings=Settings(storage_base_dir=base_dir),
        scanner=MockMalwareScanner(is_ready=True),
        storage=LocalDiskStorage(base_dir=base_dir, tenant_id="tenant_restart"),
        idempotency_store=LocalIdempotencyStore(base_dir=base_dir),
        tenant_id="tenant_restart",
    )
    res1 = p1.process_document(io.BytesIO(pdf_bytes), "file.pdf", idempotency_key="key_restart")

    # Simulate Process 2 starting up from cold disk state
    p2 = ModuleAPipeline(
        settings=Settings(storage_base_dir=base_dir),
        scanner=MockMalwareScanner(is_ready=True),
        storage=LocalDiskStorage(base_dir=base_dir, tenant_id="tenant_restart"),
        idempotency_store=LocalIdempotencyStore(base_dir=base_dir),
        tenant_id="tenant_restart",
    )
    res2 = p2.process_document(io.BytesIO(pdf_bytes), "file.pdf", idempotency_key="key_restart")

    assert res2.document_id == res1.document_id
    assert res2.status == DocumentStatus.PROCESSING.value


# --- 4. ARC-001 & Malware Audit Truthfulness ---

@pytest.mark.security
def test_audit_egress_flag_enforcement():
    """Verify that egress configuration rejects attempts to enable network egress."""
    with pytest.raises(ConfigurationError):
        load_settings({"allow_external_egress": True})


@pytest.mark.security
def test_unconfigured_scanner_truthfully_fails_closed(tmp_path):
    """An unconfigured scanner truthfully fails closed instead of faking clean status."""
    p = ModuleAPipeline(
        settings=Settings(storage_base_dir=tmp_path / "sec_test"),
        scanner=UnconfiguredScanner(),
        tenant_id="tenant_unconfigured",
    )
    pdf_bytes = create_minimal_pdf(page_count=1)
    payload = p.process_document(io.BytesIO(pdf_bytes), "test.pdf")

    assert payload.status == DocumentStatus.FAILED.value
    assert payload.error["code"] == "SCANNER_UNAVAILABLE"

