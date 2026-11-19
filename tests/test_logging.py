"""
Unit tests for structured logging and PRD SEC-012 compliance.

Tests verify:
- Logs are emitted in valid structured JSON format.
- Context metadata (service, environment, request_id, document_id) are captured.
- PII and credentials passed in extra dictionaries are sanitized/redacted.
"""

import json
import logging
import pytest
from doc_extractor.logging import SafeJsonFormatter, setup_logging


@pytest.mark.unit
def test_safe_json_formatter():
    """Verify standard fields and JSON formatting."""
    formatter = SafeJsonFormatter(service_name="test-service", environment="test")
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=20,
        msg="Document received for ingestion",
        args=(),
        exc_info=None,
    )
    record.request_id = "req-12345"
    record.document_id = "doc-99999"

    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["service"] == "test-service"
    assert parsed["environment"] == "test"
    assert parsed["level"] == "INFO"
    assert parsed["message"] == "Document received for ingestion"
    assert parsed["request_id"] == "req-12345"
    assert parsed["document_id"] == "doc-99999"


@pytest.mark.security
def test_sensitive_data_redaction():
    """Verify that sensitive keys (tokens, passwords, aadhaar) are masked in logs."""
    formatter = SafeJsonFormatter(service_name="test-service", environment="test")
    record = logging.LogRecord(
        name="test_logger",
        level=logging.WARNING,
        pathname=__file__,
        lineno=45,
        msg="Configuration warning",
        args=(),
        exc_info=None,
    )
    record.extra_data = {
        "safe_field": "public_data",
        "api_token": "secret_abc_123",
        "aadhaar_number": "1234-5678-9012",
        "password": "supersecretpassword",
    }

    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["extra"]["safe_field"] == "public_data"
    assert parsed["extra"]["api_token"] == "[REDACTED]"
    assert parsed["extra"]["aadhaar_number"] == "[REDACTED]"
    assert parsed["extra"]["password"] == "[REDACTED]"


@pytest.mark.unit
def test_setup_logging_level():
    """Verify setup_logging configures logger level and handlers properly."""
    logger = setup_logging(level="DEBUG", service_name="ingestion-worker", environment="development")
    assert logger.level == logging.DEBUG
    assert len(logger.handlers) == 1


@pytest.mark.unit
def test_setup_logging_idempotent_no_duplicate_handlers():
    """Verify setup_logging with clear_existing=False does not duplicate handlers and preserves external handlers."""
    logger = setup_logging(level="INFO", service_name="svc-1", environment="test", clear_existing=True)
    external_handler = logging.NullHandler()
    logger.addHandler(external_handler)

    try:
        assert external_handler in logger.handlers
        initial_count = len(logger.handlers)

        # Call setup_logging repeatedly with clear_existing=False
        setup_logging(level="DEBUG", service_name="svc-2", environment="prod", clear_existing=False)
        setup_logging(level="WARNING", service_name="svc-3", environment="staging", clear_existing=False)

        # Count of handlers should remain identical
        assert len(logger.handlers) == initial_count
        assert external_handler in logger.handlers

        # Exactly one handler with SafeJsonFormatter
        json_handlers = [h for h in logger.handlers if isinstance(getattr(h, "formatter", None), SafeJsonFormatter)]
        assert len(json_handlers) == 1
        assert json_handlers[0].formatter.service_name == "svc-3"
        assert json_handlers[0].formatter.environment == "staging"
        assert logger.level == logging.WARNING
    finally:
        logger.removeHandler(external_handler)
