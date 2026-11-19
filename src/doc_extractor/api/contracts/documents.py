"""
Documents API contracts and validation rules (Module M5 / Phase 2 Milestone 2D).

Defines:
- Provisional authorization scope constants for document operations (M2D-D04).
- Path-safe document ID validation rules compliant with PRD SEC-006.
- The public response DTO of API-002 (``DocumentDetailResponse``), the validated conversion from the persisted Module A
  record dictionary to ``DocumentRecord``, and the allow-list that decides which error text may be shown to a caller.
"""

import re
from dataclasses import replace
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from pydantic import Field, TypeAdapter, ValidationError

from doc_extractor.api.contracts.records import DocumentResource
from doc_extractor.api.exceptions import GENERIC_INTERNAL_MESSAGE, TenantResourceNotFoundError
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
        # The stored error is replaced BEFORE any DTO sees it: raw stored code, message, request id and details never
        # enter a model, so they can neither be exposed nor make an otherwise valid record fail validation.
        error_code, error_message = public_error(record.error)
        sanitized = replace(
            record, error={"code": error_code, "message": error_message} if error_code is not None else None
        )
        try:
            return cls(**DocumentResource.from_record(sanitized).model_dump(), result=None)
        except ValidationError as exc:
            raise _record_fault("record_field_type_invalid") from exc
