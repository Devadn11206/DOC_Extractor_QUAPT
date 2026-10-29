"""
Automated unit, boundary, and acceptance tests for PdfRenderer and FR-ING-004.

Tests:
1. Valid single-page PDF.
2. 10-page PDF producing exactly 10 page images (PRD Acceptance Criteria).
3. Correct page ordering and 1-based page-number metadata.
4. Render DPI metadata set to 200.
5. Expected page dimensions based on PDF page dimensions and 200 DPI.
6. Correct document ID association.
7. Consistent image format (PNG) and valid output images readable by PIL.
8. Corrupt and truncated PDFs.
9. Encrypted/password-protected PDFs.
10. Empty or unsupported inputs.
11. Atomic rollback and cleanup after simulated mid-document rendering failure.
12. Verification that ingestion and malware gates cannot be bypassed.
"""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch
from PIL import Image
import pytest

from doc_extractor.config import Settings
from doc_extractor.exceptions import (
    DocumentProcessingError,
    MalwareDetectedError,
    UnsupportedFileError,
)
from doc_extractor.ingestion import IngestionService, ProcessingStatus
from doc_extractor.malware import MockMalwareScanner
from doc_extractor.renderer import PdfRenderer
from doc_extractor.storage import LocalDiskStorage
from tests.conftest import create_minimal_pdf


@pytest.fixture
def storage(tmp_path):
    return LocalDiskStorage(base_dir=tmp_path / "storage", tenant_id="tenant_test")


@pytest.fixture
def renderer(storage):
    settings = Settings(target_render_dpi=200)
    return PdfRenderer(settings=settings, storage=storage)


# --- Tests 1-7: Valid Rendering, Page Ordering, 200 DPI Dimensions ---

@pytest.mark.unit
def test_render_single_page_pdf(renderer):
    """Test 1: Valid single-page PDF rendering at 200 DPI."""
    pdf_bytes = create_minimal_pdf(page_count=1)
    result = renderer.render_pdf(pdf_bytes, document_id="doc_single_001")

    assert result.is_complete is True
    assert result.total_pages == 1
    assert result.dpi == 200
    assert len(result.pages) == 1

    page = result.pages[0]
    assert page.page_number == 1
    assert page.document_id == "doc_single_001"
    assert page.dpi == 200
    assert page.image_format == "PNG"
    assert page.image_path.exists()

    # Default PDF letter page size: 612 x 792 points. At 200 DPI:
    # 612 * (200 / 72) = 1700 px; 792 * (200 / 72) = 2200 px
    assert page.width_px == 1700
    assert page.height_px == 2200

    # Verify output image is a valid PNG readable by PIL
    with Image.open(page.image_path) as img:
        assert img.format == "PNG"
        assert img.size == (1700, 2200)


@pytest.mark.unit
def test_acceptance_criteria_10_page_pdf(renderer):
    """
    Test 2-7: PRD Acceptance Criteria for FR-ING-004:
    Given a 10-page PDF, Then 10 page images at 200 DPI exist, in order, linked to the document.
    """
    pdf_10_bytes = create_minimal_pdf(page_count=10)
    doc_id = "doc_prd_acceptance_10"
    result = renderer.render_pdf(pdf_10_bytes, document_id=doc_id)

    assert result.is_complete is True
    assert result.total_pages == 10
    assert result.dpi == 200
    assert len(result.pages) == 10

    for idx, page in enumerate(result.pages):
        expected_page_num = idx + 1
        assert page.page_number == expected_page_num  # Correct 1-based order
        assert page.document_id == doc_id            # Associated with parent document
        assert page.dpi == 200                       # Exact 200 DPI target
        assert page.width_px == 1700                 # Letter width at 200 DPI
        assert page.height_px == 2200                # Letter height at 200 DPI
        assert page.image_path.exists()              # Stored image file exists
        assert f"page_{expected_page_num:04d}.png" in page.image_path.name


# --- Tests 8-10: Corrupt, Truncated, Empty, and Encrypted PDFs ---

@pytest.mark.unit
def test_render_corrupt_or_truncated_pdf(renderer):
    """Test 8: Rejects corrupt and truncated PDFs with sanitized error."""
    corrupt_data = b"%PDF-1.4\n broken data without catalog or pages"
    with pytest.raises(UnsupportedFileError) as exc_info:
        renderer.render_pdf(corrupt_data, document_id="doc_corrupt")
    assert exc_info.value.code == "UNSUPPORTED_FILE"


@pytest.mark.unit
def test_render_empty_pdf(renderer):
    """Test 10: Empty bytes reject with clear error."""
    with pytest.raises(UnsupportedFileError) as exc_info:
        renderer.render_pdf(b"", document_id="doc_empty")
    assert exc_info.value.code == "UNSUPPORTED_FILE"


@pytest.mark.unit
def test_encrypted_pdf_error_handling(renderer):
    """Test 9: Encrypted PDF without password produces DOCUMENT_ENCRYPTED error."""
    # Simulate Pdfium raising password error
    with patch("pypdfium2.PdfDocument") as mock_pdf:
        from pypdfium2._helpers.misc import PdfiumError
        mock_pdf.side_effect = PdfiumError("Password required or document encrypted")
        with pytest.raises(DocumentProcessingError) as exc_info:
            renderer.render_pdf(b"%PDF-mock", document_id="doc_enc")
        assert exc_info.value.code == "DOCUMENT_ENCRYPTED"


# --- Test 11: Cleanup & Rollback on Mid-Document Failure ---

@pytest.mark.unit
def test_cleanup_on_rendering_failure(renderer, storage):
    """
    Test 11: When rendering fails midway through a multi-page PDF,
    partial page images must be cleaned up and not left in storage.
    """
    pdf_bytes = create_minimal_pdf(page_count=5)
    doc_id = "doc_fail_midway"

    # Inject failure on page 3
    orig_store = storage.store_page_image
    call_count = 0

    def fail_on_third_page(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            raise RuntimeError("Simulated render crash on page 3")
        return orig_store(*args, **kwargs)

    storage.store_page_image = fail_on_third_page

    with pytest.raises(DocumentProcessingError) as exc_info:
        renderer.render_pdf(pdf_bytes, document_id=doc_id)

    assert "Rendering failed on page 3" in str(exc_info.value)
    # Storage for this document must be cleaned up (no lingering partial pages)
    stored_pages = storage.list_page_images(doc_id)
    assert len(stored_pages) == 0


# --- Test 12: Pipeline Security Integration & Gating ---

@pytest.mark.security
def test_renderer_cannot_bypass_ingestion_and_malware_gates(storage, sample_eicar_bytes):
    """
    Test 12: Verification that unvalidated or infected inputs cannot be rendered
    when coordinated via the ingestion pipeline.
    """
    renderer = PdfRenderer(storage=storage)
    render_mock = MagicMock()

    # Define downstream callback that would trigger rendering
    def downstream_render(result, stream):
        render_mock(result.document_id)
        renderer.render_pdf(stream, document_id=result.document_id)

    # 1. Test infected file: downstream callback must NOT be reached
    scanner = MockMalwareScanner(is_ready=True)
    service = IngestionService(scanner=scanner, downstream_callback=downstream_render)

    res_infected = service.ingest_document(
        file_path_or_stream=io.BytesIO(sample_eicar_bytes),
        claimed_filename="eicar_contract.pdf",
    )
    assert res_infected.status == ProcessingStatus.REJECTED_MALWARE
    render_mock.assert_not_called()

    # 2. Test invalid / spoofed file
    res_fake = service.ingest_document(
        file_path_or_stream=io.BytesIO(b"MZ\x90\x00NotAPdf"),
        claimed_filename="spoofed.pdf",
    )
    assert res_fake.status == ProcessingStatus.REJECTED
    render_mock.assert_not_called()

    # 3. Test clean, valid PDF: downstream callback IS called and pages render
    clean_pdf = create_minimal_pdf(page_count=2)
    res_clean = service.ingest_document(
        file_path_or_stream=io.BytesIO(clean_pdf),
        claimed_filename="clean_statement.pdf",
    )
    assert res_clean.status == ProcessingStatus.QUEUED
    render_mock.assert_called_once_with(res_clean.document_id)
    stored_pages = storage.list_page_images(res_clean.document_id)
    assert len(stored_pages) == 2

