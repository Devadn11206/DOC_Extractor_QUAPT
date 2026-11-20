"""
Documents API contracts and validation rules (Module M5 / Phase 2 Milestone 2D).

Defines:
- Provisional authorization scope constants for document operations (M2D-D04).
- Path-safe document ID validation rules compliant with PRD SEC-006.
- The public response DTO of API-002 (``DocumentDetailResponse``), the validated conversion from the persisted Module A
  record dictionary to ``DocumentRecord``, and the allow-list that decides which error text may be shown to a caller.
- The public response DTOs of API-015 (``DocumentListResponse`` with its statistics and paging models) and the
  validation of the list query parameters.
"""

import re
from dataclasses import replace
from datetime import date, datetime, time, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from doc_extractor.api.contracts.records import DocumentResource
from doc_extractor.api.exceptions import GENERIC_INTERNAL_MESSAGE, ApiValidationError, TenantResourceNotFoundError
from doc_extractor.exceptions import StorageError
from doc_extractor.models import DocumentRecord, DocumentStatus, IngestionState

# Provisional authorization scope for document read operations (M2D-D04)
SCOPE_DOCUMENTS_READ: str = "documents:read"

# Safe document ID pattern: alphanumeric, hyphen, underscore, 1 to 128 characters.
# Strict path segment format prohibiting directory traversal (..), path separators (/ or \\),
# control characters, or whitespace.
SAFE_DOCUMENT_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def validate_document_id(document_id: Any) -> str:
    """
    Validates that document_id is a non-empty, path-safe identifier string.

    Accepts:
    - Standard document identifiers composed of [a-zA-Z0-9_-] up to 128 chars.
    - Existing engine identifier formats (e.g. doc_*, doc_rej_*).

    Rejects:
    - Non-string types, boolean values, empty strings, whitespace.
    - Path traversal attempts (.., /, \\).
    - Unsafe characters, null bytes, control characters.

    Fails closed:
    Raises TenantResourceNotFoundError (mapped to HTTP 404 DOCUMENT_NOT_FOUND)
    to conceal document existence and prevent structural probing per SEC-006.
    """
    if not isinstance(document_id, str) or isinstance(document_id, bool):
        raise TenantResourceNotFoundError()

    if not SAFE_DOCUMENT_ID_REGEX.fullmatch(document_id):
        raise TenantResourceNotFoundError()

    return document_id


# ---------------------------------------------------------------------------------------------------------------------
# Persisted record -> DocumentRecord (API-002)
# ---------------------------------------------------------------------------------------------------------------------

_SHA256_REGEX = re.compile(r"^[0-9a-f]{64}$")

# Timestamps are validated with the SAME validator the public DTO applies to its ``datetime`` fields, so the converter
# can never accept text that the DTO would later reject (which would surface under a different reason code).
_TIMESTAMP_ADAPTER: TypeAdapter = TypeAdapter(datetime)


def _record_fault(reason: str) -> StorageError:
    """
    A persisted record that cannot be trusted. It is a storage fault (generic 500 for the caller); ``reason`` is a
    fixed machine-readable code that is logged server-side and never contains stored values.
    """
    return StorageError("Failed to read document record.", details={"reason": reason})


def _required_text(data: Dict[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value:
        raise _record_fault("record_field_type_invalid")
    return value


def _required_count(data: Dict[str, Any], key: str) -> int:
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _record_fault("record_field_type_invalid")
    return value


def _required_timestamp(data: Dict[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value:
        raise _record_fault("record_timestamp_invalid")
    try:
        _TIMESTAMP_ADAPTER.validate_python(value)          # the DTO's own datetime validation, same input type (str)
    except ValidationError:
        raise _record_fault("record_timestamp_invalid") from None
    return value


def parse_stored_timestamp(value: str) -> datetime:
    """
    The instant of a persisted timestamp, parsed with the SAME validator the converter and the DTO use. A naive value
    (no offset) is read as UTC, so naive and aware timestamps can always be compared. A value that the converter
    already accepted cannot fail here; a failure is the fixed ``record_timestamp_invalid`` fault.
    """
    try:
        parsed = _TIMESTAMP_ADAPTER.validate_python(value)
    except ValidationError:
        raise _record_fault("record_timestamp_invalid") from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _validated_stored_error(error: Any) -> Optional[Dict[str, Any]]:
    """
    The only accepted representations of a stored error:

    - ``None``: no error.
    - ``{}``: no error (the empty-object convention ``DocumentResource.from_record`` already uses).
    - a ``dict`` with a non-empty string ``code`` and, if a ``message`` is present, a string or ``None`` message.
      Other keys (``details``, ``request_id``, ...) are legitimate internal fields; they are neither validated nor
      ever used or exposed.

    Anything else is a corrupt record: the fixed ``record_error_invalid`` fault (generic 500), never a fabricated
    successful answer. Returns the dict, or ``None`` when there is no error.
    """
    if error is None:
        return None
    if not isinstance(error, dict):
        raise _record_fault("record_error_invalid")
    if not error:
        return None
    code = error.get("code")
    if not isinstance(code, str) or not code:
        raise _record_fault("record_error_invalid")
    if "message" in error and error["message"] is not None and not isinstance(error["message"], str):
        raise _record_fault("record_error_invalid")
    return error


_REQUIRED_RECORD_KEYS = (
    "document_id",
    "tenant_id",
    "file_hash_sha256",
    "claimed_filename",
    "file_size_bytes",
    "page_count",
    "mime_type",
    "status",
    "internal_state",
    "created_at",
    "updated_at",
)


def document_record_from_stored(data: Any) -> DocumentRecord:
    """
    Validated conversion of Module A's persisted ``document_record.json`` content (a ``dict``) to the canonical
    ``DocumentRecord``. ``DocumentRecord`` is a plain frozen dataclass with no validation and no ``from_dict``, and
    ``DocumentResource.from_record`` accepts only a ``DocumentRecord`` instance, so every key and type is checked here.

    Any defect (not an object, missing key, wrong type, malformed timestamp or hash, ``status`` or ``internal_state``
    outside the enums) raises a ``StorageError`` with a FIXED reason code and never includes the stored value. Extra
    keys are ignored; only the known fields are carried over. Tenant and document identity are NOT judged here: the
    caller compares them with the principal's tenant and the requested id first.
    """
    if not isinstance(data, dict):
        raise _record_fault("record_not_an_object")
    if any(key not in data for key in _REQUIRED_RECORD_KEYS):
        raise _record_fault("record_field_missing")

    document_id = _required_text(data, "document_id")
    tenant_id = _required_text(data, "tenant_id")
    file_hash = data["file_hash_sha256"]
    if not isinstance(file_hash, str) or not _SHA256_REGEX.fullmatch(file_hash):
        raise _record_fault("record_hash_invalid")
    filename = _required_text(data, "claimed_filename")
    file_size = _required_count(data, "file_size_bytes")
    page_count = _required_count(data, "page_count")
    mime_type = _required_text(data, "mime_type")

    raw_status, raw_state = data["status"], data["internal_state"]
    if not isinstance(raw_status, str):
        raise _record_fault("record_status_invalid")
    if not isinstance(raw_state, str):
        raise _record_fault("record_internal_state_invalid")
    try:
        status = DocumentStatus(raw_status)
    except ValueError:
        raise _record_fault("record_status_invalid") from None
    try:
        internal_state = IngestionState(raw_state)
    except ValueError:
        raise _record_fault("record_internal_state_invalid") from None

    created_at = _required_timestamp(data, "created_at")
    updated_at = _required_timestamp(data, "updated_at")

    idempotency_key = data.get("idempotency_key")
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        raise _record_fault("record_field_type_invalid")
    error = _validated_stored_error(data.get("error"))

    return DocumentRecord(
        document_id=document_id,
        tenant_id=tenant_id,
        file_hash_sha256=file_hash,
        claimed_filename=filename,
        file_size_bytes=file_size,
        page_count=page_count,
        mime_type=mime_type,
        status=status,
        internal_state=internal_state,
        created_at=created_at,
        updated_at=updated_at,
        idempotency_key=idempotency_key,
        error=dict(error) if error is not None else None,
    )


# ---------------------------------------------------------------------------------------------------------------------
# Public error text (API-010, API-011, SEC-012)
# ---------------------------------------------------------------------------------------------------------------------

# The only error codes and texts a caller may see for a stored error. Texts follow PRD section 19. Anything else, and
# every stored message, is replaced: a stored message may carry paths, exception text or document content.
_PUBLIC_ERROR_MESSAGES: Dict[str, str] = {
    "UNSUPPORTED_FILE": "This file type isn't supported.",
    "FILE_TOO_LARGE": "The file is larger than the limit.",
    "MALWARE_DETECTED": "This file was blocked by the safety scan.",
}
_GENERIC_PUBLIC_ERROR: Tuple[str, str] = ("INTERNAL_ERROR", GENERIC_INTERNAL_MESSAGE)


def public_error(error: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    (code, message) that may be shown for a stored error, or (None, None) when there is none (``None`` or ``{}``).

    The structure is validated first (``_validated_stored_error``): a malformed stored error raises the fixed
    ``record_error_invalid`` fault and is never turned into a fabricated answer. For a valid one, only allow-listed
    stable codes are passed through, each with its fixed text; any other non-empty string code becomes the generic
    internal error without exposing that code. The stored ``message``, ``request_id`` and ``details`` are never used.
    """
    validated = _validated_stored_error(error)
    if validated is None:
        return None, None
    code = validated["code"]
    if code in _PUBLIC_ERROR_MESSAGES:
        return code, _PUBLIC_ERROR_MESSAGES[code]
    return _GENERIC_PUBLIC_ERROR


# ---------------------------------------------------------------------------------------------------------------------
# API-002 response DTO
# ---------------------------------------------------------------------------------------------------------------------

def public_document_resource(record: DocumentRecord) -> DocumentResource:
    """
    The public row for one validated ``DocumentRecord``: the sanctioned ``DocumentResource`` fields with the error text
    taken from the allow-list ONLY. Shared by API-002 and API-015, so no endpoint can expose a stored error message.
    """
    # The stored error is replaced BEFORE any DTO sees it: raw stored code, message, request id and details never
    # enter a model, so they can neither be exposed nor make an otherwise valid record fail validation.
    error_code, error_message = public_error(record.error)
    sanitized = replace(
        record, error={"code": error_code, "message": error_message} if error_code is not None else None
    )
    try:
        return DocumentResource.from_record(sanitized)
    except ValidationError as exc:
        raise _record_fault("record_field_type_invalid") from exc


class DocumentDetailResponse(DocumentResource):
    """
    API-002 response: the persisted metadata of one document (the sanctioned ``DocumentResource`` fields) plus the
    reserved ``result`` key.

    PROVISIONAL, with these documented limitations (see docs/m5-open-decisions.md, M2D-D01, M2D-D02, M2D-D04):
    - ``status`` is returned exactly as persisted, and ``created_at`` / ``updated_at`` keep their persisted instants (the
      serialized text may be normalized by the DTO, for example ``+00:00`` becomes ``Z``). Module A writes
      ``status="processing"`` once at ingestion and nothing updates it, so ``status`` is NOT authoritative for the
      extraction outcome.
    - ``result`` is always JSON ``null``: no canonical extraction envelope is persisted anywhere.
    - ``filename`` is the client-supplied name; its exposure stays provisional until the SEC-007 masking decision.
    - ``error_code`` / ``error_message`` come from an allow-list; stored messages and details are never exposed.
    - No document type, schema, classification, confidence, model, run, field, flag or evidence data exists to return.
    """

    result: None = Field(
        default=None,
        description="Reserved for the canonical extraction envelope. Always null until envelope persistence exists.",
    )

    @classmethod
    def from_record(cls, record: DocumentRecord) -> "DocumentDetailResponse":
        """Map a validated ``DocumentRecord`` to the public response (error text from the allow-list only)."""
        try:
            return cls(**public_document_resource(record).model_dump(), result=None)
        except ValidationError as exc:
            raise _record_fault("record_field_type_invalid") from exc


# ---------------------------------------------------------------------------------------------------------------------
# API-015 response DTOs and list query validation (PROVISIONAL, see docs/m5-open-decisions.md M5-D06)
# ---------------------------------------------------------------------------------------------------------------------

class DocumentStats(BaseModel):
    """
    The five PRD API-015 statistics keys, counted over the WHOLE valid tenant record set, ignoring every filter.

    PERSISTED-STATUS statistics only: each key counts records whose stored ``status`` equals it. Module A writes
    ``processing`` once and no writer persists a terminal status (M2D-D01), so ``needs_review``, ``auto_approved`` and
    ``failed`` count only what is stored, which today is zero: a zero means "none persisted", not "none exist". The
    categories need not add up to ``total`` (``reviewed`` and ``rejected`` count only toward ``total``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(..., ge=0, description="All valid records of the tenant")
    processing: int = Field(..., ge=0, description="Records whose persisted status is `processing`")
    needs_review: int = Field(..., ge=0, description="Records whose persisted status is `needs_review`")
    auto_approved: int = Field(..., ge=0, description="Records whose persisted status is `auto_approved`")
    failed: int = Field(..., ge=0, description="Records whose persisted status is `failed`")


class PagingInfo(BaseModel):
    """Pagination metadata of one response. ``total_items`` counts the records that match the filters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int = Field(..., ge=1, description="The requested 1-based page")
    size: int = Field(..., ge=1, description="The requested page size")
    total_items: int = Field(..., ge=0, description="Records matching the filters (all pages)")
    total_pages: int = Field(..., ge=0, description="ceil(total_items / size); 0 when nothing matches")


class DocumentListResponse(BaseModel):
    """
    API-015 response (PRD: ``content[]``, ``stats``, ``paging``). PROVISIONAL (M5-D06 is Open): the field names inside
    ``paging`` and ``stats`` are this implementation's choice.

    Each row is the sanctioned ``DocumentResource``, built through the same error allow-list as API-002, so it carries
    the same persisted-only metadata (``status`` is non-authoritative, ``filename`` is provisionally exposed under
    M2D-D02). There is no ``result``, document type or confidence: none is persisted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: List[DocumentResource]
    stats: DocumentStats
    paging: PagingInfo

    @classmethod
    def from_listing(cls, listing: Any) -> "DocumentListResponse":
        """Map a ``services.document_listing.DocumentListing`` (validated records) to the public response."""
        try:
            return cls(
                content=[public_document_resource(record) for record in listing.records],
                stats=DocumentStats(**listing.stats),
                paging=PagingInfo(
                    page=listing.page,
                    size=listing.size,
                    total_items=listing.total_items,
                    total_pages=listing.total_pages,
                ),
            )
        except ValidationError as exc:
            raise _record_fault("record_field_type_invalid") from exc


# Query values for ``from`` / ``to``: ASCII-only ISO 8601, either a calendar date or a datetime (offset optional). The
# strict shape keeps the accepted set identical on every supported Python and keeps numeric epoch strings, compact and
# week dates out. A literal ``+`` in a URL query must be sent as ``%2B``.
_QUERY_DATE_ONLY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_QUERY_DATETIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})?"
)


def _list_validation_error_at(location: List[str], message: str, error_type: str) -> ApiValidationError:
    """A 422 VALIDATION_ERROR in the same ``details`` shape as the framework's own parameter errors (value never echoed)."""
    return ApiValidationError(
        message="Request validation failed. Check parameter and body specifications.",
        details={"validation_errors": [{"location": location, "message": message, "type": error_type}]},
    )


def _list_validation_error(parameter: str, message: str, error_type: str) -> ApiValidationError:
    return _list_validation_error_at(["query", parameter], message, error_type)


def parse_list_date_param(value: str, parameter: str, *, end_of_range: bool) -> datetime:
    """
    An inclusive bound of the ``from`` / ``to`` filter, as an aware UTC-comparable instant.

    - A calendar date (``2026-11-19``) means the whole UTC day: as ``from`` its first instant (00:00:00), as ``to`` its
      LAST instant (23:59:59.999999), so a date-only ``to`` includes the entire day.
    - A datetime is taken as given and is inclusive; a value without an offset is read as UTC.
    - Anything else (epoch numbers, compact or week dates, impossible dates, an offset beyond range) is a 422.
    """
    invalid = _list_validation_error(parameter, "Must be an ISO 8601 date or datetime.", "invalid_date")
    if _QUERY_DATE_ONLY.fullmatch(value):
        try:
            day = date.fromisoformat(value)
        except ValueError:
            raise invalid from None
        return datetime.combine(day, time.max if end_of_range else time.min, tzinfo=timezone.utc)
    if _QUERY_DATETIME.fullmatch(value):
        try:
            return parse_stored_timestamp(value)
        except Exception:
            raise invalid from None
    raise invalid


def validate_list_date_range(created_from: Optional[datetime], created_to: Optional[datetime]) -> None:
    """``from`` later than ``to`` can never match anything: a 422 instead of a silently empty page."""
    if created_from is not None and created_to is not None and created_from > created_to:
        raise _list_validation_error("from", "Must not be later than `to`.", "invalid_range")


# Every query parameter name the list route recognizes (the route signature declares exactly these, in their wire
# spelling). A name outside this set is never silently ignored.
LIST_QUERY_PARAMETERS = frozenset(
    {"page", "size", "status", "from", "to", "q", "sort", "order", "documentType", "confidenceBelow"}
)
_UNKNOWN_PARAMETER_MESSAGE = (
    "Unknown query parameter. Allowed parameters: page, size, status, from, to, q, sort, order, documentType, "
    "confidenceBelow."
)


def reject_unknown_list_parameters(names: Iterable[str]) -> None:
    """
    A misspelled or invented query parameter (``stauts``, ``tenant_id``, ``limit``, any case variant) is a 422, never a
    silently ignored name that would return an unfiltered list that only looks filtered.

    Nothing the client sent is reflected: the error names no offending parameter and carries no value, only the fixed list
    of allowed names, so attacker-controlled text can reach neither the response nor a log record.
    """
    if any(name not in LIST_QUERY_PARAMETERS for name in names):
        raise _list_validation_error_at(["query"], _UNKNOWN_PARAMETER_MESSAGE, "unknown_parameter")


def reject_unsupported_list_filter(parameter: str, value: Optional[str]) -> None:
    """
    ``documentType`` and ``confidenceBelow`` are named by the PRD, but neither value is persisted anywhere. Using one
    is a 422, never a silently ignored filter that would return an unfiltered list that merely looks filtered.
    """
    if value is not None:
        raise _list_validation_error(
            parameter, "This filter is not supported: the data it needs is not persisted.", "unsupported_filter"
        )
