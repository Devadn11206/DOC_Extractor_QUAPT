"""
Unit tests for shared exception hierarchy and PRD API-010 error envelope format.
"""

import pytest
from doc_extractor.exceptions import (
    ConfigurationError,
    DocumentExtractorError,
    DocumentProcessingError,
    SecurityCheckFailedError,
    StorageError,
    UnsupportedFileError,
)


@pytest.mark.unit
def test_unsupported_file_error_envelope():
    """Verify UNSUPPORTED_FILE error produces the PRD API-010 envelope structure."""
    err = UnsupportedFileError(
        message="Magic byte verification failed. Expected PDF/Image header.",
        request_id="req-abc-001",
        details={"detected_magic": "4d5a", "filename": "sample.pdf"},
    )
    envelope = err.to_envelope()

    assert "error" in envelope
    assert envelope["error"]["code"] == "UNSUPPORTED_FILE"
    assert envelope["error"]["request_id"] == "req-abc-001"
    assert envelope["error"]["message"] == "Magic byte verification failed. Expected PDF/Image header."
    assert envelope["error"]["details"]["detected_magic"] == "4d5a"


@pytest.mark.unit
def test_security_check_failed_error():
    """Verify SecurityCheckFailedError defaults."""
    err = SecurityCheckFailedError(request_id="req-sec-99")
    envelope = err.to_envelope()
    assert envelope["error"]["code"] == "SECURITY_CHECK_FAILED"
    assert envelope["error"]["request_id"] == "req-sec-99"


@pytest.mark.unit
def test_exception_inheritance():
    """Verify all custom exceptions inherit from DocumentExtractorError."""
    assert issubclass(ConfigurationError, DocumentExtractorError)
    assert issubclass(UnsupportedFileError, DocumentExtractorError)
    assert issubclass(SecurityCheckFailedError, DocumentExtractorError)
    assert issubclass(DocumentProcessingError, DocumentExtractorError)
    assert issubclass(StorageError, DocumentExtractorError)

