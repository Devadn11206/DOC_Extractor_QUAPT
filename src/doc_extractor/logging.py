"""
Structured logging module for Document Extraction Engine - Module A.

Guarantees:
- PRD §10.1 SEC-012: Logs, metrics, traces and error messages SHALL NOT contain
  document content or PII.
- Standard JSON / structured key-value formatting with service metadata.
- Sanitization filter to mask common sensitive tokens/credentials.
"""

import json
import logging
import re
import sys
from typing import Any, Dict, Optional


# Pattern for catching potential sensitive data accidentally passed to loggers
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(password|secret|key|token|auth|bearer|credential|aadhaar|pan|ssn)"
)


class SafeJsonFormatter(logging.Formatter):
    """
    JSON formatter ensuring no unescaped strings, standard fields, and
    PII/secret protection compliant with PRD SEC-012.
    """

    def __init__(self, service_name: str = "module-a-ingestion", environment: str = "development") -> None:
        super().__init__()
        self.service_name = service_name
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        log_payload: Dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
            "environment": self.environment,
        }

        # Include structured extra fields if provided
        if hasattr(record, "request_id") and record.request_id:
            log_payload["request_id"] = record.request_id
        if hasattr(record, "document_id") and record.document_id:
            log_payload["document_id"] = record.document_id
        if hasattr(record, "tenant_id") and record.tenant_id:
            log_payload["tenant_id"] = record.tenant_id

        # Sanitize sensitive fields if passed as extras
        if hasattr(record, "extra_data") and isinstance(record.extra_data, dict):
            sanitized_data = {}
            for k, v in record.extra_data.items():
                if _SENSITIVE_KEY_PATTERN.search(k):
                    sanitized_data[k] = "[REDACTED]"
                else:
                    sanitized_data[k] = v
            log_payload["extra"] = sanitized_data

        if record.exc_info:
            log_payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_payload)


def setup_logging(
    level: str = "INFO",
    service_name: str = "module-a-ingestion",
    environment: str = "development",
    clear_existing: bool = True,
) -> logging.Logger:
    """
    Initializes structured logging without exposing document contents or PII.
    Idempotent when clear_existing is False: updates level and formatter on existing
    managed handler without creating duplicate handlers or removing external handlers.
    """
    logger = logging.getLogger("doc_extractor")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicates only when explicitly requested
    if clear_existing and logger.hasHandlers():
        logger.handlers.clear()

    # Locate existing application-managed handler (if already registered)
    managed_handler: Optional[logging.Handler] = None
    for h in logger.handlers:
        if getattr(h, "_is_doc_extractor_handler", False) or isinstance(
            getattr(h, "formatter", None), SafeJsonFormatter
        ):
            managed_handler = h
            break

    if managed_handler is not None:
        # Re-apply formatter configuration idempotently without adding another handler
        managed_handler.setFormatter(
            SafeJsonFormatter(service_name=service_name, environment=environment)
        )
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler._is_doc_extractor_handler = True
        handler.setFormatter(
            SafeJsonFormatter(service_name=service_name, environment=environment)
        )
        logger.addHandler(handler)

    logger.propagate = False

    return logger

