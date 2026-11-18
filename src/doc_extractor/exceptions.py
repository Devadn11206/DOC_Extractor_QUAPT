"""
Shared error definitions and hierarchy for Module A and engine integrations.

Adheres to:
- PRD §7.2 API-010: Standardized error code envelope
- PRD §7.2 API-011: Error messages SHALL NOT contain extracted document content or PII
- PRD §9 GR-001: Strict rejection with standard machine-readable codes
"""

from typing import Any, Dict, Optional

# One shared base for Module A and M4 (see engine_errors): a re-export, not a copy.
from engine_errors import DocumentExtractorError  # noqa: F401


class ConfigurationError(DocumentExtractorError):
    """Raised when environment settings or module configurations are invalid."""

    def __init__(
        self,
        message: str,
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code="CONFIGURATION_FAILED",
            request_id=request_id,
            details=details,
        )


class UnsupportedFileError(DocumentExtractorError):
    """
    Raised when an uploaded file is not supported or fails magic-byte verification.
    PRD standard error code: UNSUPPORTED_FILE (API-010, FR-ING-002, GR-001).
    """

    def __init__(
        self,
        message: str = "The uploaded file format is not supported or content is corrupt.",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code="UNSUPPORTED_FILE",
            request_id=request_id,
            details=details,
        )


class SecurityCheckFailedError(DocumentExtractorError):
    """
    Raised when malware scan, decompression-bomb check, or security boundary fails.
    Corresponds to PRD FR-ING-003, SEC-009, GR-001.
    """

    def __init__(
        self,
        message: str = "File rejected due to security policy or malware check.",
        code: str = "SECURITY_CHECK_FAILED",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            request_id=request_id,
            details=details,
        )


class MalwareDetectedError(DocumentExtractorError):
    """
    Raised when malware scanning detects an infected or malicious file (e.g. EICAR).
    Corresponds to PRD FR-ING-003, SEC-009, GR-001.
    """

    def __init__(
        self,
        message: str = "File rejected: malware signature detected.",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code="MALWARE_DETECTED",
            request_id=request_id,
            details=details,
        )


class ScannerUnavailableError(DocumentExtractorError):
    """
    Raised when a required malware scanner is unreachable or fails to complete.
    Enforces fail-closed security boundary.
    """

    def __init__(
        self,
        message: str = "Malware scan failed or scanner is unavailable.",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code="SCANNER_UNAVAILABLE",
            request_id=request_id,
            details=details,
        )


class ConflictError(DocumentExtractorError):
    """
    Raised when an idempotency key is reused with a different request fingerprint or content.
    PRD standard error code: IDEMPOTENCY_KEY_CONFLICT.
    """

    def __init__(
        self,
        message: str = "Idempotency key reused with different request parameters or content.",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code="IDEMPOTENCY_KEY_CONFLICT",
            request_id=request_id,
            details=details,
        )


class DocumentProcessingError(DocumentExtractorError):
    """Raised when an internal stage (rendering, de-skewing, transformation) fails."""

    def __init__(
        self,
        message: str,
        code: str = "PROCESSING_FAILED",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            request_id=request_id,
            details=details,
        )


class StorageError(DocumentExtractorError):
    """Raised when local filesystem or encrypted storage operations fail."""

    def __init__(
        self,
        message: str,
        code: str = "STORAGE_FAILED",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            request_id=request_id,
            details=details,
        )


class CrossTenantStorageAccessError(StorageError):
    """
    A storage reference names another tenant's data or escapes the caller's tenant directory. Distinct from
    ``TenantStorageIsolationError`` (an integrity fault in the tenant's own location): the REST API answers this one
    exactly like a missing resource (404, SEC-006) so another tenant's data cannot be probed.
    """

    def __init__(self, details: Optional[Dict[str, Any]] = None, request_id: Optional[str] = None) -> None:
        super().__init__(
            message="Cross-tenant access or path escape forbidden.",
            code="CROSS_TENANT_ACCESS_FORBIDDEN",
            request_id=request_id,
            details=details,
        )


class InvalidTenantIdentifierError(StorageError, ValueError):
    """
    A tenant identifier is not usable as an isolation boundary (empty, oversized, or able to leave its own
    directory). It is a ``ValueError`` too, so model validators turn it into a typed validation error, and a
    ``StorageError`` so storage callers can catch it. The offending value is never echoed.
    """

    def __init__(self, reason: str, request_id: Optional[str] = None) -> None:
        super().__init__(
            message="Invalid tenant identifier.",
            code="INVALID_TENANT_IDENTIFIER",
            request_id=request_id,
            details={"reason": reason},
        )


class TenantStorageIsolationError(StorageError):
    """
    A tenant's storage location failed an isolation check: it would collide with another identity on this
    filesystem, is a symlink or leaves the tenant boundary, or carries another tenant's identity marker.
    """

    def __init__(self, reason: str, request_id: Optional[str] = None) -> None:
        super().__init__(
            message="Tenant storage isolation check failed.",
            code="TENANT_STORAGE_ISOLATION_FAILED",
            request_id=request_id,
            details={"reason": reason},
        )


class LLMExecutionError(DocumentExtractorError):
    """
    Raised when LLM grammar-constrained extraction fails and fallback is disabled (SCH-007).
    Signals provider network failure, invalid model response, or schema validation failure.
    """

    def __init__(
        self,
        message: str = "LLM extraction execution failed under strict no-fallback policy.",
        model_name: Optional[str] = None,
        request_id: Optional[str] = None,
        code: str = "LLM_EXECUTION_FAILED",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        merged = dict(details or {})
        if model_name:
            merged["model_name"] = model_name
        super().__init__(
            message=message,
            code=code,
            request_id=request_id,
            details=merged,
        )
        self.model_name = model_name
