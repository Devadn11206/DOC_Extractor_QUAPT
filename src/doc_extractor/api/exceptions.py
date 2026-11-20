"""
Canonical error handling and API-010 exception mapping (Module M5).

Adheres to:
- PRD §7.2 API-010: Standardized error code envelope
- PRD §7.2 API-011 & SEC-012: No sensitive info or PII in error messages
- Preserves full compatibility with DocumentExtractorError hierarchy.
"""

import logging
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional, Tuple
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from doc_extractor.exceptions import (
    ComponentConfigurationError,
    ConfigurationError,
    ConflictError as IngestionConflictError,
    DocumentExtractorError,
    CrossTenantStorageAccessError,
    DocumentProcessingError,
    InvalidTenantIdentifierError,
    LLMExecutionError,
    MalwareDetectedError,
    ScannerUnavailableError,
    SecurityCheckFailedError,
    StorageError,
    TenantMismatchError,
    TenantStorageIsolationError,
    TenantStorageUnavailableError,
    UnsupportedFileError,
)

# Import approved M4 schema lifecycle exceptions for canonical HTTP mapping
from schema_lifecycle import exceptions as _m4  # same distribution: a failed import must fail loudly


logger = logging.getLogger("doc_extractor.api.errors")


class ApiHttpError(DocumentExtractorError):
    """Base class for HTTP-aware API exceptions mapped to API-010 envelopes."""

    status_code: int = 500

    def __init__(
        self,
        message: str = "An unexpected error occurred.",
        code: str = "INTERNAL_ERROR",
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, request_id=request_id, details=details)
        if status_code is not None:
            self.status_code = status_code


class AuthenticationRequiredError(ApiHttpError):
    """Raised when authentication credentials are missing or invalid (HTTP 401)."""

    status_code = 401

    def __init__(
        self,
        message: str = "Authentication credentials were not provided or are invalid.",
        code: str = "AUTHENTICATION_REQUIRED",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=401, request_id=request_id, details=details)


class InvalidTokenError(AuthenticationRequiredError):
    """Raised when an API key token is malformed, invalid, or fails verification (HTTP 401)."""

    def __init__(
        self,
        message: str = "Invalid API key token.",
        code: str = "INVALID_CREDENTIALS",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, request_id=request_id, details=details)


class TokenExpiredError(AuthenticationRequiredError):
    """Raised when an API key token has expired per SEC-002 (HTTP 401)."""

    def __init__(
        self,
        message: str = "API key token has expired.",
        code: str = "TOKEN_EXPIRED",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, request_id=request_id, details=details)


class ForbiddenError(ApiHttpError):
    """Raised when caller lacks required authorization scopes or roles (HTTP 403)."""

    status_code = 403

    def __init__(
        self,
        message: str = "Principal lacks required permissions for this action.",
        code: str = "FORBIDDEN",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=403, request_id=request_id, details=details)


class PrivilegedRoleNotAllowedError(ForbiddenError):
    """Raised when attempting to issue an ordinary API key with privileged roles (HTTP 403)."""

    def __init__(
        self,
        message: str = "Privileged roles (admin, schema_approver) cannot be assigned to API keys.",
        code: str = "PRIVILEGED_ROLE_NOT_ALLOWED",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, request_id=request_id, details=details)


class TenantAccessDeniedError(ForbiddenError):
    """Raised when caller attempts to access another tenant's resource (HTTP 403)."""

    def __init__(
        self,
        message: str = "Access to the requested tenant resource is forbidden.",
        code: str = "TENANT_ACCESS_DENIED",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, request_id=request_id, details=details)


class NotFoundError(ApiHttpError):
    """Raised when the requested resource does not exist (HTTP 404)."""

    status_code = 404

    def __init__(
        self,
        message: str = "The requested resource was not found.",
        code: str = "DOCUMENT_NOT_FOUND",
        status_code: Optional[int] = None,
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=status_code or 404, request_id=request_id, details=details)


class TenantResourceNotFoundError(NotFoundError):
    """
    Raised when accessing a document or resource belonging to another tenant,
    concealing resource existence per PRD §19 (HTTP 404 DOCUMENT_NOT_FOUND).
    """

    def __init__(
        self,
        message: str = "Document not found.",
        code: str = "DOCUMENT_NOT_FOUND",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, request_id=request_id, details=details)


class FileTooLargeError(ApiHttpError):
    """Raised when an uploaded document or payload exceeds configured size/page limits per PRD §19 (HTTP 413)."""

    status_code = 413

    def __init__(
        self,
        message: str = "The file is larger than the limit.",
        code: str = "FILE_TOO_LARGE",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=413, request_id=request_id, details=details)


class ExtractionTimeoutError(ApiHttpError):
    """Raised when synchronous extraction exceeds its deadline (PRD API-004: 30 s). HTTP 504; the code is [Proposed]."""

    status_code = 504

    def __init__(
        self,
        message: str = "The extraction did not finish in time.",
        code: str = "EXTRACTION_TIMEOUT",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=504, request_id=request_id, details=details)


class ApiConflictError(ApiHttpError):
    """
    Raised when a request conflicts with the current state of a resource (HTTP 409, code ``CONFLICT``).

    NOT Module A's ``doc_extractor.exceptions.ConflictError`` (an idempotency key reused with a different request, code
    ``IDEMPOTENCY_KEY_CONFLICT``), which is a different condition that also answers 409. The names used to be identical.
    Both codes stay as they are until the owners decide M5-D01 (the PRD names ``IDEMPOTENCY_CONFLICT``).
    """

    status_code = 409

    def __init__(
        self,
        message: str = "The requested operation conflicts with the current resource state.",
        code: str = "CONFLICT",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=409, request_id=request_id, details=details)


class ReviewTaskNotFoundError(NotFoundError):
    """
    A review task that does not exist for the caller's tenant, or whose identifier is malformed, or that belongs to another
    tenant: one answer for all three (SEC-006), HTTP 404 ``REVIEW_TASK_NOT_FOUND``.
    """

    def __init__(
        self,
        message: str = "Review task not found.",
        code: str = "REVIEW_TASK_NOT_FOUND",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, request_id=request_id, details=details)


class ReviewTaskClaimedError(ApiConflictError):
    """
    The task is owned by another principal (PRD section 19: 409 ``TASK_CLAIMED`` [Proposed]). PROVISIONAL (M5-D11, Open):
    ``details.claimed_by`` is the owner's principal ID, because no display name exists and none is invented.
    """

    def __init__(
        self,
        message: str = "This task is claimed by another reviewer.",
        code: str = "TASK_CLAIMED",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, request_id=request_id, details=details)


class ReviewTaskCompletedError(ApiConflictError):
    """The task is completed, so it can be neither claimed nor released (HTTP 409 ``TASK_COMPLETED``, a PROPOSED code: M5-D11)."""

    def __init__(
        self,
        message: str = "This task is already completed.",
        code: str = "TASK_COMPLETED",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, request_id=request_id, details=details)


class RateLimitExceededError(ApiHttpError):
    """Raised when client exceeds designated request rate limits per PRD §7.2 API-012 & §19 (HTTP 429)."""

    status_code = 429

    def __init__(
        self,
        message: str = "Too many requests. Try again shortly.",
        code: str = "RATE_LIMITED",
        retry_after_seconds: Optional[int] = None,
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.retry_after_seconds = max(0, int(retry_after_seconds)) if retry_after_seconds is not None else None
        super().__init__(message=message, code=code, status_code=429, request_id=request_id, details=details)


class ApiValidationError(ApiHttpError):
    """Raised when client input fails parameter or schema validation (HTTP 422)."""

    status_code = 422

    def __init__(
        self,
        message: str = "Request parameter or payload validation failed.",
        code: str = "VALIDATION_ERROR",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=422, request_id=request_id, details=details)


class InvalidKeyExpirationError(ApiValidationError):
    """Raised when requested API key expiration exceeds policy or is invalid (HTTP 422)."""

    def __init__(
        self,
        message: str = "API key expiration exceeds maximum allowed lifetime of 90 days.",
        code: str = "INVALID_KEY_EXPIRATION",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, request_id=request_id, details=details)


class InvalidScopeError(ApiValidationError):
    """Raised when requested API key scope is forbidden or invalid (HTTP 422)."""

    def __init__(
        self,
        message: str = "Requested API key scope is invalid or forbidden.",
        code: str = "INVALID_SCOPE",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, request_id=request_id, details=details)


class InternalServerError(ApiHttpError):
    """Raised for unrecoverable server errors (HTTP 500)."""

    status_code = 500

    def __init__(
        self,
        message: str = "An unexpected internal error occurred.",
        code: str = "INTERNAL_ERROR",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, status_code=500, request_id=request_id, details=details)


class PortNotConfiguredError(ApiHttpError):
    """
    An infrastructure port (repository, queue, ...) has no adapter configured: the service cannot
    serve the request yet (HTTP 503), which is not an internal fault (500). Uses the existing
    ``SERVICE_UNAVAILABLE`` code; ``details`` names the port, never document content (API-011).
    """

    status_code = 503

    def __init__(
        self,
        port: str,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(
            message=f"The {port} adapter is not configured; the service is unavailable.",
            code="SERVICE_UNAVAILABLE",
            status_code=503,
            request_id=request_id,
            details={"port": port},
        )
        self.port = port


class ContainerUnavailableError(PortNotConfiguredError):
    """
    ApplicationContainer is unconfigured or has been shut down (HTTP 503).
    """

    status_code = 503

    def __init__(
        self,
        message: str = "The ApplicationContainer is unavailable or has been shut down.",
        request_id: Optional[str] = None,
    ) -> None:
        ApiHttpError.__init__(
            self,
            message=message,
            code="SERVICE_UNAVAILABLE",
            status_code=503,
            request_id=request_id,
            details={"port": "ApplicationContainer"},
        )
        self.port = "ApplicationContainer"


class AuditUnavailableError(ApiHttpError):
    """
    A MANDATORY audit record could not be written (SEC-008). The operation is never reported as a clean success: the
    service refuses (HTTP 503) instead of continuing unaudited. ``details`` names the operation and whether its effect
    was applied or rolled back; never a secret, an identifier of document content, or the underlying error text.
    """

    status_code = 503

    def __init__(self, operation: str, effect: str = "rolled_back", request_id: Optional[str] = None) -> None:
        super().__init__(
            message="The audit trail is unavailable; the operation could not be recorded.",
            code="AUDIT_UNAVAILABLE",
            status_code=503,
            request_id=request_id,
            details={"operation": operation, "effect": effect},
        )
        self.operation = operation
        self.effect = effect


# Backward-compatible name: importing ``ConflictError`` from this module still works and is the same class.
ConflictError = ApiConflictError


# Status code mapping for existing DocumentExtractorError hierarchy and PRD §19
_STATUS_CODE_MAP: Dict[Any, int] = {
    AuthenticationRequiredError: 401,
    ForbiddenError: 403,
    TenantAccessDeniedError: 404,         # SEC-006: concealed as not found, see concealed_not_found()
    CrossTenantStorageAccessError: 404,   # idem
    TenantMismatchError: 404,             # SEC-006: concealed as not found per F6
    NotFoundError: 404,
    TenantResourceNotFoundError: 404,
    ApiConflictError: 409,
    IngestionConflictError: 409,
    UnsupportedFileError: 400,          # PRD §19: 400 UNSUPPORTED_FILE
    SecurityCheckFailedError: 400,
    MalwareDetectedError: 400,          # PRD §19: 400 MALWARE_DETECTED
    FileTooLargeError: 413,             # PRD §19: 413 FILE_TOO_LARGE
    ScannerUnavailableError: 503,
    RateLimitExceededError: 429,        # PRD §19: 429 RATE_LIMITED
    ApiValidationError: 422,
    ConfigurationError: 500,
    ComponentConfigurationError: 500,
    InvalidTenantIdentifierError: 422,    # typed validation error (subclass of StorageError: listed first by MRO)
    TenantStorageIsolationError: 500,     # integrity fault: generic body for the client (see _SERVER_SIDE_ONLY)
    TenantStorageUnavailableError: 500,   # storage cannot be read or provisioned: generic body for the client
    StorageError: 500,
    LLMExecutionError: 502,
    DocumentProcessingError: 500,
}

# M4 schema-lifecycle exceptions: every class has an explicit HTTP status. Each keeps its OWN domain code
# (UNAUTHENTICATED, FORBIDDEN, SCHEMA_SELF_APPROVAL, ... are already the PRD section 19 names); only the
# status was missing, which made them answer HTTP 500. Keyed by class name so a missing M4 package only
# skips the mapping. tests/test_api_m4_exception_mapping.py fails when M4 gains an unmapped exception.
_M4_STATUS_BY_CLASS_NAME: Dict[str, int] = {
    "SchemaLifecycleError": 400,             # generic lifecycle failure (fallback for the base class)
    "SchemaValidationError": 422,            # SCH-001/002 metamodel violation
    "SchemaSerializationError": 422,         # SCH-003 unparseable schema document
    "SchemaVersionError": 422,               # unparseable or invalid version
    "InvalidStateTransitionError": 409,      # SCH-004 transition not allowed from the current state
    "SchemaPromotionNotAllowedError": 409,   # promotion without the approval service
    "SchemaNotApprovedError": 409,           # PRD section 19: 409 SCHEMA_NOT_APPROVED
    "SchemaNotFoundError": 404,
    "SchemaRollbackError": 422,              # SCH-006 rollback targets an invalid version
    "TenantAccessError": 403,                # M4 raises it only for a missing/blank tenant context (not cross-tenant: see concealed_not_found)
    "UnauthenticatedError": 401,             # PRD section 19: 401 UNAUTHENTICATED
    "ForbiddenActionError": 403,             # PRD section 19: 403 FORBIDDEN
    "SchemaSelfApprovalError": 403,          # PRD section 19: 403 SCHEMA_SELF_APPROVAL
    "BenchmarkVerificationError": 422,       # SCH-005/007 harness result does not qualify
}

for _name, _status in _M4_STATUS_BY_CLASS_NAME.items():
    _STATUS_CODE_MAP[getattr(_m4, _name)] = _status      # AttributeError if M4 renames a class: loud, not silent


# SEC-006: access to ANOTHER tenant's resource must answer exactly like a resource that does not exist (404, never 403),
# so identifiers cannot be probed across tenants. Only genuine cross-tenant signals are concealed; every other
# permission denial keeps its 403 and its code.
_CONCEALED_AS_NOT_FOUND: Dict[type, Tuple[str, str]] = {
    TenantAccessDeniedError: ("DOCUMENT_NOT_FOUND", "Document not found."),
    CrossTenantStorageAccessError: ("DOCUMENT_NOT_FOUND", "Document not found."),
    TenantMismatchError: ("DOCUMENT_NOT_FOUND", "Document not found."),
}

# M4 reports a cross-tenant attempt only as a plain ForbiddenActionError whose message starts like this (it also puts
# both tenant identifiers in that message, which must never reach a client). tests/test_api_cross_tenant_not_found.py
# triggers the real M4 code path, so a wording change in M4 fails there instead of silently reverting to 403.
M4_CROSS_TENANT_MARKER = "Cross-tenant access violation"


def concealed_not_found(exc: BaseException) -> Optional[Tuple[str, str]]:
    """(code, message) of the not-found response that conceals a cross-tenant access, else None."""
    for cls in type(exc).__mro__:
        if cls in _CONCEALED_AS_NOT_FOUND:
            return _CONCEALED_AS_NOT_FOUND[cls]
    if isinstance(exc, _m4.ForbiddenActionError) and M4_CROSS_TENANT_MARKER in str(getattr(exc, "message", "")):
        return ("SCHEMA_NOT_FOUND", "Schema not found.")
    return None


# Storage faults that are the OPERATOR's business, not the caller's: an isolation check failed (alias of another identity,
# symlink, marker mismatch), the storage root cannot be read or written, or a general storage failure occurred.
# Their reason strings or messages describe the server's storage layout or internal paths, so the client gets
# the same generic 500 as any internal fault (API-011). The reason is logged server-side only.
_SERVER_SIDE_ONLY = (
    TenantStorageIsolationError,
    TenantStorageUnavailableError,
    StorageError,
    ComponentConfigurationError,
    ConfigurationError,
)
GENERIC_INTERNAL_MESSAGE = "An unexpected internal server error occurred."


def get_status_code_for_exception(exc: Exception) -> int:
    """
    Derive the HTTP status for an exception: an explicit ``status_code`` first, then the nearest class in
    the exception's MRO that has a mapping (so a subclass such as ``SchemaSelfApprovalError`` is never
    shadowed by its parent), else 500.
    """
    if concealed_not_found(exc) is not None:
        return 404
    if hasattr(exc, "status_code") and isinstance(exc.status_code, int):
        return exc.status_code
    for exc_cls in type(exc).__mro__:
        if exc_cls in _STATUS_CODE_MAP:
            return _STATUS_CODE_MAP[exc_cls]
    return 500


# Protocol headers an exception may carry to the client. Everything else on an exception is dropped, so internal
# headers can never leak into a response; nothing is invented except WWW-Authenticate on a 401 (RFC 9110 requires it).
_PRESERVED_HEADERS = {"retry-after": "Retry-After", "www-authenticate": "WWW-Authenticate", "allow": "Allow"}
_MAX_HEADER_VALUE = 512


def _clean_header_value(value: Any) -> Optional[str]:
    text = str(value).strip()
    if not text or len(text) > _MAX_HEADER_VALUE or any(ch in text for ch in "\r\n\x00"):
        return None
    return text


# The longest wait the API ever asks of a client: 24 hours. A larger (or absurd, e.g. 10**30) value is clamped to it.
MAX_RETRY_AFTER_SECONDS = 86_400


def _bounded_seconds(seconds: int) -> str:
    return str(min(max(0, seconds), MAX_RETRY_AFTER_SECONDS))


def sanitize_retry_after(value: Any, *, now: Optional[datetime] = None) -> Optional[str]:
    """
    A valid ``Retry-After`` (API-012): a number of seconds or an HTTP-date, bounded to ``MAX_RETRY_AFTER_SECONDS``
    (86,400). A negative value is clamped to 0, a larger one to the maximum, as is an HTTP-date further away than the
    maximum (sent as seconds). Anything that is neither (text, NaN, infinity, header injection) is dropped rather than
    sent, because a malformed header is worse than none. ``now`` exists for tests.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return _bounded_seconds(value)
    if isinstance(value, float):
        return _bounded_seconds(int(value)) if math.isfinite(value) else None
    text = _clean_header_value(value)
    if text is None:
        return None
    if re.fullmatch(r"-?\d+", text):
        return _bounded_seconds(int(text))
    try:
        when = parsedate_to_datetime(text)                    # an HTTP-date
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if (when - current).total_seconds() > MAX_RETRY_AFTER_SECONDS:
        return str(MAX_RETRY_AFTER_SECONDS)
    return text


def preserved_headers(raw: Any) -> Dict[str, str]:
    """The Retry-After, WWW-Authenticate and Allow headers of an exception, validated; nothing else."""
    kept: Dict[str, str] = {}
    for name, value in (raw or {}).items():
        canonical = _PRESERVED_HEADERS.get(str(name).lower())
        if canonical is None:
            continue
        cleaned = sanitize_retry_after(value) if canonical == "Retry-After" else _clean_header_value(value)
        if cleaned is not None:
            kept[canonical] = cleaned
    return kept


def build_error_response(
    code: str,
    message: str,
    status_code: int,
    request_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    """Builds a canonical API-010 JSONResponse with X-Request-ID and optional custom response headers."""
    resp_headers = dict(headers or {})
    if request_id:
        resp_headers["X-Request-ID"] = request_id
    if status_code == 401 and not any(name.lower() == "www-authenticate" for name in resp_headers):
        resp_headers["WWW-Authenticate"] = "Bearer"        # a 401 must say how to authenticate (RFC 9110, section 15.5.2)

    payload = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details or {},
        }
    }
    return JSONResponse(status_code=status_code, content=payload, headers=resp_headers)


def extract_request_id(request: Request, exc: Optional[Exception] = None) -> str:
    """
    Helper to extract the authoritative request ID established by RequestIdMiddleware.
    request.state.request_id is strictly authoritative; exc.request_id is never permitted
    to override the middleware's assigned request identifier.
    Falls back to ContextVar or generates a server-controlled ID if unexpectedly absent.
    """
    if hasattr(request, "state") and getattr(request.state, "request_id", None):
        return request.state.request_id
    from doc_extractor.api.middleware import get_request_id_from_context
    ctx_id = get_request_id_from_context()
    if ctx_id:
        return ctx_id
    from uuid import uuid4
    fallback_id = f"req_{uuid4().hex}"
    if hasattr(request, "state"):
        request.state.request_id = fallback_id
    return fallback_id


async def document_extractor_error_handler(request: Request, exc: DocumentExtractorError) -> JSONResponse:
    """Handles all DocumentExtractorError subclasses and formats them per API-010."""
    status_code = get_status_code_for_exception(exc)
    req_id = extract_request_id(request, exc)
    extra_headers = preserved_headers(getattr(exc, "headers", None))
    if getattr(exc, "retry_after_seconds", None) is not None:
        retry_after = sanitize_retry_after(exc.retry_after_seconds)
        if retry_after is not None:
            extra_headers["Retry-After"] = retry_after
    concealed = concealed_not_found(exc)
    if concealed is not None:                        # cross-tenant: same body as a missing resource, no details, no identifiers
        return build_error_response(
            code=concealed[0], message=concealed[1], status_code=404, request_id=req_id, details={}, headers=extra_headers
        )
    if isinstance(exc, _SERVER_SIDE_ONLY) and not isinstance(exc, InvalidTenantIdentifierError):
        # Diagnostics stay on the server: the machine-readable reason and the request id, never the tenant or any path.
        logger.error(
            "Storage fault answered with a generic 500: %s reason=%s request_id=%s",
            getattr(exc, "code", type(exc).__name__), (getattr(exc, "details", None) or {}).get("reason"), req_id,
        )
        return build_error_response(
            code="INTERNAL_ERROR", message=GENERIC_INTERNAL_MESSAGE, status_code=500, request_id=req_id, details={}
        )
    return build_error_response(
        code=exc.code,
        message=exc.message,
        status_code=status_code,
        request_id=req_id,
        details=exc.details,
        headers=extra_headers,
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Handles Starlette/FastAPI HTTPExceptions conforming to API-010."""
    status_code = exc.status_code
    req_id = extract_request_id(request)

    code_lookup = {
        400: "BAD_REQUEST",
        401: "AUTHENTICATION_REQUIRED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "CONFLICT",
        413: "FILE_TOO_LARGE",
        415: "UNSUPPORTED_MEDIA_TYPE",
        422: "VALIDATION_ERROR",
        429: "RATE_LIMITED",
        500: "INTERNAL_ERROR",
        503: "SERVICE_UNAVAILABLE",
    }
    code = code_lookup.get(status_code, f"HTTP_{status_code}")
    message = str(exc.detail) if exc.detail else "HTTP request failed."
    extra_headers = preserved_headers(exc.headers)           # Retry-After (validated), WWW-Authenticate, Allow
    return build_error_response(
        code=code,
        message=message,
        status_code=status_code,
        request_id=req_id,
        details={},
        headers=extra_headers,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Handles FastAPI Pydantic request validation errors without leaking sensitive data."""
    req_id = extract_request_id(request)
    sanitized_errors = []
    for err in exc.errors():
        sanitized_errors.append({
            "location": [str(x) for x in err.get("loc", [])],
            "message": err.get("msg", "Invalid value"),
            "type": err.get("type", "value_error"),
        })

    return build_error_response(
        code="VALIDATION_ERROR",
        message="Request validation failed. Check parameter and body specifications.",
        status_code=422,
        request_id=req_id,
        details={"validation_errors": sanitized_errors},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler for unexpected internal server errors (500)."""
    req_id = extract_request_id(request)
    # Message must never expose internal stack traces or database info (API-011)
    return build_error_response(
        code="INTERNAL_ERROR",
        message="An unexpected internal server error occurred.",
        status_code=500,
        request_id=req_id,
        details={},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Registers canonical API-010 exception handlers on the FastAPI application."""
    app.add_exception_handler(DocumentExtractorError, document_extractor_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
