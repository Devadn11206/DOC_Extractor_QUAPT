"""
Regression test suite for Module A hardening defects:
- Issue 1: File extension mismatch (JPEG, PNG, TIFF actual path vs URI).
- Issue 2: Multi-page TIFF frame rendering and sequential page records.
- Issue 3: Unsafe storage path validation and tenant prefix containment.
- Issue 4: Raster DPI metadata handling and 200 DPI coordinate consistency.
- Issue 5: Duplicate document idempotency replay avoiding duplicate rendering.
- Issue 6: Malware scanner configuration and fail-safe wiring.
"""

import io
from pathlib import Path
import tempfile
from PIL import Image
import pytest

from doc_extractor.config import Settings
from doc_extractor.exceptions import ScannerUnavailableError, StorageError
from doc_extractor.handoff import CoordinateTransform
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.malware import (
    DisabledMalwareScanner,
    MockMalwareScanner,
    UnconfiguredScanner,
    build_malware_scanner,
)
from doc_extractor.models import DocumentStatus
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.storage import LocalDiskStorage


# ============================================================================
# Issue 1: File extension mismatch
# ============================================================================

def test_regression_issue_1_jpeg_storage_extension_matches_uri(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_ext")
    pipeline = ModuleAPipeline(scanner=MockMalwareScanner(), storage=storage, tenant_id="tenant_ext")

    buf = io.BytesIO()
    Image.new("RGB", (120, 140), color="blue").save(buf, format="JPEG")
    payload = pipeline.process_document(io.BytesIO(buf.getvalue()), "photo.jpg")

    assert len(payload.pages) == 1
    page = payload.pages[0]

    # Verify URI points to valid artifact on disk
    resolved_path = storage.resolve_uri_to_path(page.original_storage_uri)
    assert resolved_path.exists(), f"Stored original artifact not found at {resolved_path}"
    assert page.original_artifact_id == "page_0001.png"
    assert resolved_path.name == "page_0001.png"

    # Verify preprocessed URI also resolves
    resolved_prep = storage.resolve_uri_to_path(page.preprocessed_storage_uri)
    assert resolved_prep.exists(), f"Stored preprocessed artifact not found at {resolved_prep}"


# ============================================================================
# Issue 2: Multi-page TIFF handling
# ============================================================================

def test_regression_issue_2_multipage_tiff_produces_all_pages(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_tiff")
    pipeline = ModuleAPipeline(scanner=MockMalwareScanner(), storage=storage, tenant_id="tenant_tiff")

    img1 = Image.new("RGB", (100, 150), color="red")
    img2 = Image.new("RGB", (200, 250), color="green")
    img3 = Image.new("RGB", (300, 350), color="blue")
    buf = io.BytesIO()
    img1.save(buf, format="TIFF", save_all=True, append_images=[img2, img3])

    payload = pipeline.process_document(io.BytesIO(buf.getvalue()), "multipage.tiff")

    assert payload.page_count == 3
    assert len(payload.pages) == 3

    for idx, page in enumerate(payload.pages):
        expected_page_num = idx + 1
        assert page.page_number == expected_page_num
        assert page.original_artifact_id == f"page_{expected_page_num:04d}.png"
        resolved_orig = storage.resolve_uri_to_path(page.original_storage_uri)
        assert resolved_orig.exists()
        resolved_prep = storage.resolve_uri_to_path(page.preprocessed_storage_uri)
        assert resolved_prep.exists()

    assert payload.pages[0].original_width_px == 100
    assert payload.pages[1].original_width_px == 200
    assert payload.pages[2].original_width_px == 300


# ============================================================================
# Issue 3: Unsafe storage path validation
# ============================================================================

def test_regression_issue_3_storage_blocks_sibling_tenant_prefix(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_alpha")

    # Legitimate tenant URI must resolve safely
    legit_uri = "tenant_alpha/doc1/pages/page_0001.png"
    resolved = storage.resolve_uri_to_path(legit_uri)
    expected_parent = (tmp_path / "tenant_alpha").resolve()
    assert resolved.is_relative_to(expected_parent)

    # Sibling tenant sharing string prefix (e.g. tenant_alpha_evil) must fail
    sibling_evil_uri = "tenant_alpha_evil/doc1/pages/page_0001.png"
    with pytest.raises(StorageError) as exc_info:
        storage.resolve_uri_to_path(sibling_evil_uri)
    assert "Cross-tenant access or path escape forbidden" in exc_info.value.message

    # Directory traversal must fail
    traversal_uri = "tenant_alpha/../tenant_beta/doc1/pages/page_0001.png"
    with pytest.raises(StorageError):
        storage.resolve_uri_to_path(traversal_uri)


# ============================================================================
# Issue 4: Raster DPI and coordinate consistency
# ============================================================================

def test_regression_issue_4_raster_dpi_normalization(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_dpi")
    pipeline = ModuleAPipeline(scanner=MockMalwareScanner(), storage=storage, tenant_id="tenant_dpi")

    # 1. 100 DPI image (400x400 px at 100 DPI -> 4x4 inches -> 800x800 at 200 DPI)
    buf_100 = io.BytesIO()
    Image.new("RGB", (400, 400), color="white").save(buf_100, format="PNG", dpi=(100, 100))
    payload_100 = pipeline.process_document(io.BytesIO(buf_100.getvalue()), "img_100dpi.png")
    page_100 = payload_100.pages[0]
    assert page_100.dpi == 200
    assert page_100.original_width_px == 800
    assert page_100.original_height_px == 800

    # 2. 300 DPI image (600x900 px at 300 DPI -> 2x3 inches -> 400x600 at 200 DPI)
    buf_300 = io.BytesIO()
    Image.new("RGB", (600, 900), color="white").save(buf_300, format="JPEG", dpi=(300, 300))
    payload_300 = pipeline.process_document(io.BytesIO(buf_300.getvalue()), "img_300dpi.jpg")
    page_300 = payload_300.pages[0]
    assert page_300.dpi == 200
    assert page_300.original_width_px == 400
    assert page_300.original_height_px == 600

    # 3. No DPI metadata image preserves pixel dimensions while declaring target 200 DPI
    buf_nodpi = io.BytesIO()
    Image.new("RGB", (500, 700), color="white").save(buf_nodpi, format="PNG")
    payload_nodpi = pipeline.process_document(io.BytesIO(buf_nodpi.getvalue()), "img_nodpi.png")
    page_nodpi = payload_nodpi.pages[0]
    assert page_nodpi.dpi == 200
    assert page_nodpi.original_width_px == 500
    assert page_nodpi.original_height_px == 700


# ============================================================================
# Issue 5: Duplicate document processing
# ============================================================================

def test_regression_issue_5_idempotent_replay_returns_persisted_handoff(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_idem")
    idempotency_store = LocalIdempotencyStore(tmp_path)
    pipeline = ModuleAPipeline(
        scanner=MockMalwareScanner(),
        storage=storage,
        idempotency_store=idempotency_store,
        tenant_id="tenant_idem",
    )

    buf = io.BytesIO()
    Image.new("RGB", (200, 200), color="yellow").save(buf, format="PNG")
    doc_bytes = buf.getvalue()

    # First run
    p1 = pipeline.process_document(io.BytesIO(doc_bytes), "invoice.png", idempotency_key="key_abc_123")
    assert p1.status == DocumentStatus.PROCESSING.value
    assert len(p1.pages) == 1

    # Second run with same idempotency key must replay identical payload without re-running preprocessing
    p2 = pipeline.process_document(io.BytesIO(doc_bytes), "invoice.png", idempotency_key="key_abc_123")
    assert p2.run_id == p1.run_id
    assert p2.document_id == p1.document_id
    assert p2.page_count == p1.page_count
    assert len(p2.pages) == 1
    assert p2.pages[0].original_storage_uri == p1.pages[0].original_storage_uri


# ============================================================================
# Issue 6: Malware scanner configuration
# ============================================================================

def test_regression_issue_6_scanner_factory_and_pipeline_wiring(tmp_path):
    # 1. Default settings with malware_scan_enabled=True and malware_scanner_type='mock'
    s_mock = Settings(storage_base_dir=tmp_path, malware_scan_enabled=True, malware_scanner_type="mock")
    p_mock = ModuleAPipeline(settings=s_mock, tenant_id="t_mock")
    assert isinstance(p_mock.scanner, MockMalwareScanner)
    assert p_mock.scanner.is_operational() is True

    # 2. Scanning enabled with unknown/unconfigured scanner must fail safely (closed)
    s_unconf = Settings(storage_base_dir=tmp_path, malware_scan_enabled=True, malware_scanner_type="unknown_scanner")
    p_unconf = ModuleAPipeline(settings=s_unconf, tenant_id="t_unconf")
    assert isinstance(p_unconf.scanner, UnconfiguredScanner)
    assert p_unconf.scanner.is_operational() is False

    buf = io.BytesIO()
    Image.new("RGB", (100, 100), color="white").save(buf, format="PNG")
    payload = p_unconf.process_document(io.BytesIO(buf.getvalue()), "sample.png")
    assert payload.status == DocumentStatus.FAILED.value
    assert payload.error is not None
    assert payload.error["code"] == "SCANNER_UNAVAILABLE"

    # 3. Scanning disabled in dev/staging environment permitted
    s_disabled = Settings(
        storage_base_dir=tmp_path,
        app_env="development",
        malware_scan_enabled=False,
    )
    p_disabled = ModuleAPipeline(settings=s_disabled, tenant_id="t_disabled")
    assert isinstance(p_disabled.scanner, DisabledMalwareScanner)
    assert p_disabled.scanner.is_operational() is True

    # 4. Scanning disabled in production must raise error
    with pytest.raises(ScannerUnavailableError) as exc_info:
        build_malware_scanner(enabled=False, app_env="production")
    assert "cannot be disabled in production" in exc_info.value.message

