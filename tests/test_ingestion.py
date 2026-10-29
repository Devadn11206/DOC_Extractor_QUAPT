"""
Automated unit and integration tests for IngestionService coordination (FR-ING-001, FR-ING-002, FR-ING-003).

Tests:
I. Verification that infected, invalid and unscannable files NEVER reach downstream callback.
J. Sanitized error responses and status mappings (queued, rejected, rejected_malware, failed).
"""

import io
from unittest.mock import MagicMock
import pytest

from doc_extractor.config import Settings
from doc_extractor.ingestion import IngestionService, ProcessingStatus
from doc_extractor.malware import MockMalwareScanner, UnconfiguredScanner
from tests.conftest import create_minimal_pdf


@pytest.fixture
def mock_scanner():
    return MockMalwareScanner(is_ready=True)


@pytest.fixture
def service(mock_scanner):
    settings = Settings(max_file_size_bytes=500000, max_page_count=5)
    return IngestionService(settings=settings, scanner=mock_scanner)


@pytest.mark.unit
def test_successful_ingestion_and_queued_status(service, sample_pdf_bytes):
    """PRD FR-ING-001: Valid clean file is accepted with status 'queued'."""
    downstream_mock = MagicMock()
    service.downstream_callback = downstream_mock

    result = service.ingest_document(
        file_path_or_stream=io.BytesIO(sample_pdf_bytes),
        claimed_filename="valid_document.pdf",
    )

    assert result.is_accepted is True
    assert result.status == ProcessingStatus.QUEUED
    assert result.document_id.startswith("doc_")
    assert result.page_count == 2
    assert result.mime_type == "application/pdf"
    assert result.error is None
    # Downstream callback must be called
    downstream_mock.assert_called_once()


@pytest.mark.security
def test_infected_file_blocks_downstream_callback(service, sample_eicar_bytes):
    """PRD FR-ING-003: EICAR file yields rejected_malware and NEVER reaches downstream."""
    downstream_mock = MagicMock()
    service.downstream_callback = downstream_mock

    result = service.ingest_document(
        file_path_or_stream=io.BytesIO(sample_eicar_bytes),
        claimed_filename="eicar_contract.pdf",
    )

    assert result.is_accepted is False
    assert result.status == ProcessingStatus.REJECTED_MALWARE
    assert result.error is not None
    assert result.error["code"] == "MALWARE_DETECTED"
    # Downstream callback must NEVER be called
    downstream_mock.assert_not_called()


@pytest.mark.security
def test_unsupported_file_blocks_downstream_callback(service):
    """PRD FR-ING-002: Invalid file yields rejected and NEVER reaches downstream."""
    downstream_mock = MagicMock()
    service.downstream_callback = downstream_mock

    exe_bytes = b"MZ\x90\x00executable"
    result = service.ingest_document(
        file_path_or_stream=io.BytesIO(exe_bytes),
        claimed_filename="fake.pdf",
    )

    assert result.is_accepted is False
    assert result.status == ProcessingStatus.REJECTED
    assert result.error["code"] == "UNSUPPORTED_FILE"
    downstream_mock.assert_not_called()


@pytest.mark.security
def test_scanner_error_fails_closed(sample_pdf_bytes):
    """When scanner is offline, ingestion fails closed with status failed and blocks downstream."""
    downstream_mock = MagicMock()
    scanner = MockMalwareScanner(is_ready=False)  # Offline scanner
    service = IngestionService(scanner=scanner, downstream_callback=downstream_mock)

    result = service.ingest_document(
        file_path_or_stream=io.BytesIO(sample_pdf_bytes),
        claimed_filename="clean.pdf",
    )

    assert result.is_accepted is False
    assert result.status == ProcessingStatus.FAILED
    assert result.error["code"] == "SCANNER_UNAVAILABLE"
    downstream_mock.assert_not_called()


@pytest.mark.security
def test_unconfigured_scanner_blocks_all_uploads(sample_pdf_bytes):
    """Fails closed if production scanner is unconfigured."""
    downstream_mock = MagicMock()
    service = IngestionService(scanner=UnconfiguredScanner(), downstream_callback=downstream_mock)

    result = service.ingest_document(
        file_path_or_stream=io.BytesIO(sample_pdf_bytes),
        claimed_filename="clean.pdf",
    )

    assert result.is_accepted is False
    assert result.status == ProcessingStatus.FAILED
    assert result.error["code"] == "SCANNER_UNAVAILABLE"
    downstream_mock.assert_not_called()

