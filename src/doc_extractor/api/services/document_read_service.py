"""
Document Read Service (Module M5 / Phase 2 Milestone 2D).

Provides tenant-bound read primitives for document retrieval (API-003), document detail (API-002) and the
document listing (API-015).
Strictly isolated by the authenticated principal's tenant identity.
Does not instantiate extraction pipelines, parsers, or OCR models on read paths.

Filesystem access is descriptor-relative: the storage root is opened once, then every component below it (tenant,
document, ``pages``, and the leaf files) is opened relative to the descriptor of its parent with ``O_NOFOLLOW``
(directories additionally with ``O_DIRECTORY``, leaf files additionally with ``O_NONBLOCK``). A path component that is
swapped for a symlink after its parent descriptor was obtained therefore cannot redirect the read, and bytes are read
only from the verified descriptor, never from a path that is opened a second time.

Descriptor ownership: a raw descriptor that has been handed to ``os.fdopen`` belongs to the resulting file object and
the raw variable is set to ``None`` at once; cleanup closes only descriptors the service still owns.

Logging (F6, scope: THIS module): every log statement uses a fixed reason code, an exception CLASS name or an integer
``errno``. Untrusted handoff content (URIs, tenant or document identifiers, exception text) is never interpolated
into a log record. Logging elsewhere on the request path is outside this module and is tracked in
``docs/m5-open-decisions.md`` (M2D Remediation Follow-ups).
"""

import errno
import io
import json
import logging
import os
import stat
import zlib
from typing import Iterable, List, Optional, Tuple

from PIL import Image

from doc_extractor.api.contracts.documents import document_record_from_stored, validate_document_id
from doc_extractor.api.exceptions import (
    ApiValidationError,
    TenantResourceNotFoundError,
)
from doc_extractor.exceptions import (
    ComponentConfigurationError,
    CrossTenantStorageAccessError,
    StorageError,
    TenantStorageIsolationError,
)
from doc_extractor.handoff import ModuleAHandoffPayload
from doc_extractor.models import DocumentRecord
from doc_extractor.storage import LocalDiskStorage
from doc_extractor.tenancy import IDENTITY_MARKER

logger = logging.getLogger("doc_extractor.api.read_service")

# Canonical PNG signature (8 bytes)
PNG_MAGIC_BYTES = b"\x89PNG\r\n\x1a\n"

# Smallest structurally valid PNG: signature + IHDR (12 + 13) + IEND (12)
_PNG_MIN_SIZE = len(PNG_MAGIC_BYTES) + 25 + 12
_PNG_MAX_CHUNK_LENGTH = 0x7FFFFFFF

# Files Module A writes inside a document directory and that this service reads.
_HANDOFF_FILE = "module_a_handoff.json"
_RECORD_FILE = "document_record.json"

# API-015 PROVISIONAL scan ceilings. Every request re-reads every record (there is no index), so these bound the work of
# one request until a persistence index exists (M5-D18, M2D-D01). Above either one the listing is refused with a typed
# error; it is NEVER truncated, and no partial list or statistics are produced.
#
# Two separate limits, because they bound two different costs:
# - ``_MAX_LISTED_ENTRIES``: CANDIDATE document directories (names that are valid document ids, excluding the
#   non-document entries below). Each candidate costs a directory open, a file read and a JSON parse.
# - ``_MAX_EXAMINED_DIRECTORY_ENTRIES``: ALL directory entries the enumeration looks at, whatever they are (documents,
#   the identity marker, ``idempotency``, stray files, junk). It bounds the cost of enumerating the directory itself and
#   is checked while the directory is streamed, so enumeration stops as soon as the limit is exceeded. It must be at
#   least as large as the candidate limit plus the handful of non-document entries a real tenant directory holds.
_MAX_LISTED_ENTRIES = 10_000
_MAX_EXAMINED_DIRECTORY_ENTRIES = 20_000

_LIMIT_REASON_CANDIDATES = "document_listing_limit_exceeded"
_LIMIT_REASON_ENTRIES = "directory_entry_limit_exceeded"

# Entries of a tenant directory that are not documents: the identity marker and the idempotency store.
_NON_DOCUMENT_ENTRIES = frozenset({IDENTITY_MARKER, "idempotency"})


class DocumentListingLimitExceededError(StorageError):
    """
    The tenant directory exceeds a provisional scan ceiling (generic 500). ``details["reason"]`` is the fixed
    ``document_listing_limit_exceeded`` (too many candidate document directories, the default) or
    ``directory_entry_limit_exceeded`` (too many directory entries of any kind).
    """

    def __init__(self, reason: str = _LIMIT_REASON_CANDIDATES) -> None:
        super().__init__(
            message="Document listing exceeds the provisional scan ceiling.",
            code="DOCUMENT_LISTING_LIMIT_EXCEEDED",
            details={"reason": reason},
        )

# Open flags the descriptor-relative walk cannot do without. There is deliberately no silent ``getattr(..., 0)``
# fallback: a platform that lacks one of them refuses to serve files at all.
_REQUIRED_OPEN_FLAGS = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")


def _require_platform_support() -> None:
    """
    Fail closed, BEFORE any stored file is touched, when the platform cannot provide race-safe access:
    ``O_NOFOLLOW``, ``O_DIRECTORY`` and ``O_NONBLOCK`` must exist and ``os.open`` must accept ``dir_fd``.
    """
    missing = [name for name in _REQUIRED_OPEN_FLAGS if not hasattr(os, name)]
    dir_fd_operations = getattr(os, "supports_dir_fd", None)
    dir_fd_open_supported = dir_fd_operations is not None and os.open in dir_fd_operations
    if missing or not dir_fd_open_supported:
        logger.error(
            "Platform cannot provide descriptor-relative access (reason=platform_unsupported, missing_flags=%s, "
            "dir_fd_open=%s)",
            ",".join(missing) or "none",
            dir_fd_open_supported,
        )
        raise ComponentConfigurationError(
            message="Platform does not support race-safe descriptor-relative storage access.",
            details={"reason": "platform_unsupported"},
        )


def _open_directory_nofollow(name: str, parent_fd: int, symlink_reason: str) -> int:
    """Open directory ``name`` relative to ``parent_fd`` without following symlinks; the caller owns the result."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise TenantResourceNotFoundError() from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            logger.warning("Storage directory refused (reason=%s)", symlink_reason)
            raise TenantStorageIsolationError(symlink_reason) from exc
        logger.error("Storage directory open failed (reason=directory_open_failed, errno=%s)", exc.errno)
        raise StorageError(
            "Failed to open storage directory.", details={"reason": "directory_open_failed"}
        ) from exc


def _open_leaf_nofollow(name: str, parent_fd: int, symlink_reason: str) -> int:
    """
    Open file ``name`` relative to ``parent_fd`` with ``O_NOFOLLOW | O_NONBLOCK`` (a FIFO or device can neither be
    followed nor block the open); the caller owns the result and must still verify it with ``fstat``.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise TenantResourceNotFoundError() from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            logger.warning("Storage file refused (reason=%s)", symlink_reason)
            raise TenantStorageIsolationError(symlink_reason) from exc
        if exc.errno == errno.ENXIO:
            logger.warning("Storage file refused (reason=not_a_regular_file)")
            raise TenantStorageIsolationError("not_a_regular_file") from exc
        logger.error("Storage file open failed (reason=file_open_failed, errno=%s)", exc.errno)
        raise StorageError("Failed to open storage file.", details={"reason": "file_open_failed"}) from exc


def _require_regular_file(fd: int) -> None:
    """The opened descriptor itself (not a path) must be a regular file."""
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        logger.warning("Storage file refused (reason=not_a_regular_file)")
        raise TenantStorageIsolationError("not_a_regular_file")


def _read_regular_text_file(name: str, dir_fd: int, symlink_reason: str, unreadable_reason: str) -> str:
    """
    Read the UTF-8 text of regular file ``name`` relative to the verified directory descriptor ``dir_fd``.

    The file is opened ``O_NOFOLLOW | O_NONBLOCK``, the OPENED descriptor must be a regular file (a FIFO, socket,
    device or directory is refused), and the bytes are read from that descriptor only. Ownership of the descriptor moves
    to the file object (``os.fdopen``) and the raw variable is cleared at once, so it can never be closed twice. Read or
    decode failures become a typed ``StorageError`` carrying the fixed ``unreadable_reason``; nothing stored is logged.
    """
    fd: Optional[int] = _open_leaf_nofollow(name, dir_fd, symlink_reason)
    try:
        _require_regular_file(fd)
        text_file = os.fdopen(fd, "r", encoding="utf-8")
    except BaseException:
        _close_owned_descriptors((fd,), strict=False)
        raise
    fd = None                          # the file object is now the sole owner of this descriptor
    try:
        with text_file as f:
            return f.read()
    except (OSError, ValueError) as exc:        # ValueError covers UnicodeDecodeError
        logger.error("Stored file read failed (reason=%s, error_type=%s)", unreadable_reason, type(exc).__name__)
        raise StorageError("Failed to read stored file.", details={"reason": unreadable_reason}) from exc


def _decode_record_object(record_text: str) -> dict:
    """Parse the text of a ``document_record.json`` into a ``dict``; anything else is a typed fault with a fixed reason."""
    try:
        stored = json.loads(record_text)
    except Exception as exc:
        logger.error("Record parse failed (reason=record_json_decode_error, error_type=%s)", type(exc).__name__)
        raise StorageError(
            "Failed to read document record.", details={"reason": "record_json_decode_error"}
        ) from exc
    if not isinstance(stored, dict):
        logger.error("Record parse failed (reason=record_not_an_object)")
        raise StorageError("Failed to read document record.", details={"reason": "record_not_an_object"})
    return stored


def _close_owned_descriptors(fds: Iterable[Optional[int]], *, strict: bool) -> None:
    """
    Close every descriptor the service STILL owns (``None`` means it was never opened or its ownership moved to a
    file object, and must not be touched: that number may already belong to another request).

    A close failure is never swallowed silently: on the success path (``strict=True``) it raises; while another error
    is already propagating (``strict=False``) it is logged with a fixed reason and does not mask that error.
    """
    first_error: Optional[OSError] = None
    for fd in fds:
        if fd is None:
            continue
        try:
            os.close(fd)
        except OSError as exc:
            logger.error("Descriptor close failed (reason=fd_close_failed, errno=%s)", exc.errno)
            if first_error is None:
                first_error = exc
    if strict and first_error is not None:
        raise StorageError("Failed to release storage descriptor.", details={"reason": "fd_close_failed"}) from first_error


def _reject_png(reason: str) -> StorageError:
    """Log a fixed reason and build the typed (generic-500) error for a structurally invalid PNG."""
    logger.error("Page image rejected (reason=png_structure_%s)", reason)
    return StorageError("Corrupted or invalid page image data.", details={"reason": f"png_structure_{reason}"})


def validate_png_structure(data: bytes) -> None:
    """
    Strict PNG chunk-structure validation. Raises a typed ``StorageError`` unless ``data`` is exactly:

    signature, then chunks (length, type, data, CRC-32) where the first is a 13-byte ``IHDR`` (and no other ``IHDR``
    follows), every chunk lies inside the buffer with a correct CRC, and the sequence ends with a zero-length ``IEND``
    whose CRC is the LAST byte of the buffer. Bytes after ``IEND`` (appended payloads, a second ``IEND``) are refused.
    """
    size = len(data)
    if size < _PNG_MIN_SIZE:
        raise _reject_png("too_short")
    if not data.startswith(PNG_MAGIC_BYTES):
        raise _reject_png("bad_signature")

    view = memoryview(data)
    position = len(PNG_MAGIC_BYTES)
    first_chunk = True
    while True:
        if position + 12 > size:                       # no room for length + type + CRC: truncated
            raise _reject_png("truncated")
        length = int.from_bytes(view[position:position + 4], "big")
        chunk_type = bytes(view[position + 4:position + 8])
        if length > _PNG_MAX_CHUNK_LENGTH:
            raise _reject_png("chunk_length_invalid")
        if not all(65 <= b <= 90 or 97 <= b <= 122 for b in chunk_type):
            raise _reject_png("chunk_type_invalid")
        end = position + 12 + length
        if end > size:                                 # the chunk would extend beyond the buffer
            raise _reject_png("chunk_overruns_buffer")
        stored_crc = int.from_bytes(view[end - 4:end], "big")
        if (zlib.crc32(view[position + 4:end - 4]) & 0xFFFFFFFF) != stored_crc:
            raise _reject_png("crc_mismatch")
        if first_chunk:
            if chunk_type != b"IHDR" or length != 13:
                raise _reject_png("ihdr_missing")
            first_chunk = False
        elif chunk_type == b"IHDR":
            raise _reject_png("ihdr_duplicate")
        if chunk_type == b"IEND":
            if length != 0:
                raise _reject_png("iend_not_empty")
            if end != size:
                raise _reject_png("trailing_data")
            return
        position = end


class DocumentReadService:
    """
    Tenant-bound read service for document artifacts.
    Interacts with LocalDiskStorage using descriptor-relative directory operations (openat)
    without initializing heavy ML pipelines.
    """

    def __init__(self, storage: LocalDiskStorage, tenant_id: str) -> None:
        self.storage = storage
        self.tenant_id = tenant_id

    def _open_tenant_directory(self) -> Tuple[int, int]:
        """
        Descriptor-relative walk to the principal's tenant directory: the configured storage root, then the tenant
        directory opened relative to it with ``O_NOFOLLOW | O_DIRECTORY``. Nothing is ever created: a missing storage
        root or tenant directory raises ``TenantResourceNotFoundError``.

        Returns ``(base_dir_fd, tenant_fd)``; the caller owns both. If the walk fails part-way, whatever it had opened
        is closed here before the error propagates, so nothing leaks.
        """
        base_dir_fd: Optional[int] = None
        tenant_fd: Optional[int] = None
        try:
            try:
                base_dir_fd = os.open(str(self.storage.base_dir), os.O_RDONLY | os.O_DIRECTORY)
            except FileNotFoundError:
                raise TenantResourceNotFoundError() from None
            except OSError as exc:
                logger.error("Storage root open failed (reason=storage_root_unreadable, errno=%s)", exc.errno)
                raise StorageError(
                    "Tenant storage is unavailable.", details={"reason": "storage_root_unreadable"}
                ) from exc

            tenant_name = self.storage._tenant_dir(create=False).name
            tenant_fd = _open_directory_nofollow(tenant_name, base_dir_fd, "tenant_directory_is_a_symlink")
        except BaseException:
            _close_owned_descriptors((tenant_fd, base_dir_fd), strict=False)
            raise
        return base_dir_fd, tenant_fd

    def _open_document_directory(self, safe_doc_id: str) -> Tuple[int, int, int]:
        """
        Shared descriptor-relative walk for every single-document read: the tenant directory (see
        ``_open_tenant_directory``), then the document directory opened relative to it with ``O_NOFOLLOW`` (and
        ``O_DIRECTORY``). A tenant or document that does not exist is the concealed 404.

        Returns ``(base_dir_fd, tenant_fd, doc_fd)``; the caller owns all three and must close them. If the walk fails
        part-way, whatever it had opened is closed here before the error propagates, so nothing leaks.
        """
        base_dir_fd, tenant_fd = self._open_tenant_directory()
        doc_fd: Optional[int] = None
        try:
            doc_fd = _open_directory_nofollow(safe_doc_id, tenant_fd, "document_directory_is_a_symlink")
        except BaseException:
            _close_owned_descriptors((doc_fd, tenant_fd, base_dir_fd), strict=False)
            raise
        return base_dir_fd, tenant_fd, doc_fd

    def get_document(self, document_id: str) -> DocumentRecord:
        """
        Retrieves the validated persisted ``DocumentRecord`` of one document of this service's tenant (API-002).

        - ``document_record.json`` is read through the same descriptor-relative walk as the page image (``O_NOFOLLOW``
          per component, ``O_NONBLOCK`` leaf, ``fstat`` regular-file check, ownership transfer through ``os.fdopen``),
          never through the path-based ``LocalDiskStorage.get_document_record``.
        - Unknown, malformed, wrong-case and foreign ids, a document directory without a record, and a record whose
          tenant or document id differs from the principal's tenant or the requested id are ALL the concealed 404.
        - A record that cannot be trusted (unreadable, not JSON, not an object, missing or mistyped fields, unknown
          ``status`` or ``internal_state``) is a typed storage fault with a fixed reason: a generic 500 for the caller.
        - Reads never create a directory.
        """
        safe_doc_id = validate_document_id(document_id)
        _require_platform_support()

        base_dir_fd: Optional[int] = None
        tenant_fd: Optional[int] = None
        doc_fd: Optional[int] = None
        try:
            base_dir_fd, tenant_fd, doc_fd = self._open_document_directory(safe_doc_id)
            record_text = _read_regular_text_file(_RECORD_FILE, doc_fd, "record_file_is_symlink", "record_unreadable")
        except BaseException:
            _close_owned_descriptors((doc_fd, tenant_fd, base_dir_fd), strict=False)
            raise
        _close_owned_descriptors((doc_fd, tenant_fd, base_dir_fd), strict=True)

        stored = _decode_record_object(record_text)

        # Identity first, on the raw values: another tenant's or another document's record is simply "not found".
        if stored.get("tenant_id") != self.tenant_id:
            logger.warning("Cross-tenant document access rejected (reason=record_tenant_mismatch)")
            raise CrossTenantStorageAccessError(details={"reason": "record_tenant_mismatch"})
        if stored.get("document_id") != safe_doc_id:
            logger.warning("Document ID mismatch rejected (reason=record_doc_id_mismatch)")
            raise TenantResourceNotFoundError()

        return document_record_from_stored(stored)

    def list_documents(self) -> List[DocumentRecord]:
        """
        Every valid persisted ``DocumentRecord`` of this service's tenant, in ascending directory-name order (API-015).
        The caller filters, sorts and paginates (``document_listing``); nothing is truncated here.

        - Only the principal's own tenant directory is enumerated, through the same descriptor-relative walk as the
          single-document reads (``O_NOFOLLOW`` per component, ``O_NONBLOCK`` leaf, ``fstat`` regular-file check,
          ownership transfer through ``os.fdopen``). Nothing is created: a missing storage root or tenant directory is
          an empty list.
        - Skipped silently, not counted: the identity marker, the ``idempotency`` directory, names that are not valid
          document ids, and document directories without a ``document_record.json`` (the single read answers 404 for
          those too).
        - Fail closed, as a generic 500 with a fixed reason: a symlinked, non-directory or special entry that looks like
          a document, an unreadable, non-JSON, non-object or invalid record, a record that claims another tenant or
          another document id, a tenant with more candidate entries than ``_MAX_LISTED_ENTRIES`` and a directory with
          more entries of any kind than ``_MAX_EXAMINED_DIRECTORY_ENTRIES``. A record is never skipped quietly, because
          that would silently shrink every total.
        - So the listed ids are exactly the ids for which ``get_document`` succeeds.
        """
        _require_platform_support()
        try:
            base_dir_fd, tenant_fd = self._open_tenant_directory()
        except TenantResourceNotFoundError:
            return []

        records: List[DocumentRecord] = []
        try:
            for name in self._candidate_document_names(tenant_fd):
                record = self._read_listed_record(name, tenant_fd)
                if record is not None:
                    records.append(record)
        except BaseException:
            _close_owned_descriptors((tenant_fd, base_dir_fd), strict=False)
            raise
        _close_owned_descriptors((tenant_fd, base_dir_fd), strict=True)
        return records

    @staticmethod
    def _candidate_document_names(tenant_fd: int) -> List[str]:
        """
        Sorted names of the tenant directory entries that could be documents.

        The directory is STREAMED (``os.scandir`` is an iterator; the listing is never materialized) and two ceilings are
        enforced while it is read: no more than ``_MAX_EXAMINED_DIRECTORY_ENTRIES`` entries of any kind, and no more than
        ``_MAX_LISTED_ENTRIES`` candidates. Exceeding either stops the enumeration at once with the typed error, so a
        directory with a huge number of unrelated entries cannot cost unbounded time or memory, and nothing partial is
        ever returned. Only candidate names are kept.
        """
        names: List[str] = []
        examined = 0
        try:
            with os.scandir(tenant_fd) as entries:
                for entry in entries:
                    examined += 1
                    if examined > _MAX_EXAMINED_DIRECTORY_ENTRIES:
                        logger.error("Document listing refused (reason=directory_entry_limit_exceeded)")
                        raise DocumentListingLimitExceededError(_LIMIT_REASON_ENTRIES)
                    name = entry.name
                    if name in _NON_DOCUMENT_ENTRIES:
                        continue
                    try:
                        validate_document_id(name)
                    except TenantResourceNotFoundError:
                        continue
                    names.append(name)
                    if len(names) > _MAX_LISTED_ENTRIES:
                        logger.error("Document listing refused (reason=document_listing_limit_exceeded)")
                        raise DocumentListingLimitExceededError()
        except OSError as exc:
            logger.error("Tenant directory listing failed (reason=directory_listing_failed, errno=%s)", exc.errno)
            raise StorageError(
                "Failed to list tenant storage.", details={"reason": "directory_listing_failed"}
            ) from exc
        names.sort()
        return names

    def _read_listed_record(self, name: str, tenant_fd: int) -> Optional[DocumentRecord]:
        """The validated record of candidate directory ``name``, or ``None`` when it has no record (not a document)."""
        doc_fd: Optional[int] = None
        try:
            doc_fd = _open_directory_nofollow(name, tenant_fd, "document_directory_is_a_symlink")
            record_text = _read_regular_text_file(_RECORD_FILE, doc_fd, "record_file_is_symlink", "record_unreadable")
        except TenantResourceNotFoundError:          # the directory vanished, or it holds no record
            _close_owned_descriptors((doc_fd,), strict=False)
            return None
        except BaseException:
            _close_owned_descriptors((doc_fd,), strict=False)
            raise
        _close_owned_descriptors((doc_fd,), strict=True)

        stored = _decode_record_object(record_text)
        if stored.get("tenant_id") != self.tenant_id:
            logger.warning("Listed record refused (reason=record_tenant_mismatch)")
            raise TenantStorageIsolationError("record_tenant_mismatch")
        if stored.get("document_id") != name:
            logger.warning("Listed record refused (reason=record_doc_id_mismatch)")
            raise TenantStorageIsolationError("record_doc_id_mismatch")
        return document_record_from_stored(stored)

    def get_page_image(self, document_id: str, page_number: int) -> Tuple[bytes, str]:
        """
        Retrieves raw image bytes and media type for the specified document page.

        Enforces:
        - Path-safe document ID validation (concealing non-existent/malformed IDs as 404).
        - 1-based page number validation.
        - Required platform capabilities (O_NOFOLLOW, O_DIRECTORY, O_NONBLOCK, dir_fd) before any file access (R5).
        - Descriptor-relative traversal from the storage root preventing parent-symlink races (F2).
        - O_NOFOLLOW on every path component; O_NONBLOCK plus ``fstat`` regular-file check on files (F1).
        - Descriptor ownership transfer through ``os.fdopen``; no descriptor is closed twice (R1).
        - Exact canonical page URI (F3); the file read is derived from the page number, never from the URI.
        - Strict PNG chunk structure ending exactly at IEND, then full Pillow verification (F3, R2).
        - Fixed reason codes in this module's log statements; see the module docstring for the F6 scope (R4).
        - Identical 404 concealment across malformed, unknown, foreign, and unrendered pages.

        Returns:
            Tuple of (image_bytes, media_type). Default media_type is "image/png".
        """
        # 1. Validate document_id format
        safe_doc_id = validate_document_id(document_id)

        # 2. Validate page_number
        if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
            raise ApiValidationError(
                message=f"Invalid page number: {page_number}. Page numbers must be 1-based positive integers."
            )

        # 3. Refuse to run without the capabilities the race-safe walk depends on (before any file access)
        _require_platform_support()

        base_dir_fd: Optional[int] = None
        tenant_fd: Optional[int] = None
        doc_fd: Optional[int] = None
        pages_fd: Optional[int] = None
        file_fd: Optional[int] = None

        try:
            # Steps A-B: storage root, tenant, then document directory (shared descriptor-relative walk)
            base_dir_fd, tenant_fd, doc_fd = self._open_document_directory(safe_doc_id)

            # Step C: read module_a_handoff.json from the verified document descriptor
            handoff_raw = _read_regular_text_file(_HANDOFF_FILE, doc_fd, "handoff_file_is_symlink", "handoff_unreadable")

            try:
                handoff_data = json.loads(handoff_raw)
            except Exception as exc:
                logger.error("Handoff parse failed (reason=json_decode_error, error_type=%s)", type(exc).__name__)
                raise StorageError(
                    "Failed to read handoff payload.", details={"reason": "json_decode_error"}
                ) from exc

            if not isinstance(handoff_data, dict):
                logger.error("Handoff parse failed (reason=handoff_not_an_object)")
                raise StorageError("Failed to read handoff payload.", details={"reason": "handoff_not_an_object"})

            # Step D: handoff ownership and identity
            if handoff_data.get("tenant_id") != self.tenant_id:
                logger.warning("Cross-tenant document access rejected (reason=tenant_mismatch)")
                raise CrossTenantStorageAccessError(details={"reason": "tenant_mismatch"})

            if handoff_data.get("document_id") != safe_doc_id:
                logger.warning("Document ID mismatch rejected (reason=doc_id_mismatch)")
                raise TenantResourceNotFoundError()

            try:
                handoff = ModuleAHandoffPayload.from_dict(handoff_data)
            except Exception as exc:
                logger.error("Handoff parse failed (reason=schema_invalid, error_type=%s)", type(exc).__name__)
                raise StorageError(
                    "Failed to read handoff payload.", details={"reason": "schema_invalid"}
                ) from exc

            page_record = next((p for p in handoff.pages if p.page_number == page_number), None)
            if page_record is None:
                raise TenantResourceNotFoundError()

            # Step E: the URI must equal the canonical page artifact exactly (M2D-D03 provisional: original image)
            storage_uri = page_record.original_storage_uri
            if not storage_uri or not isinstance(storage_uri, str):
                raise TenantResourceNotFoundError()

            canonical_name = f"page_{page_number:04d}.png"
            canonical_uri = f"{self.tenant_id}/{safe_doc_id}/pages/{canonical_name}"
            if storage_uri != canonical_uri:
                if "\\" in storage_uri or storage_uri.startswith("/") or ".." in storage_uri.split("/"):
                    logger.warning("Storage URI rejected (reason=traversal_detected)")
                    raise StorageError("Unsafe storage URI detected.", details={"reason": "traversal_detected"})
                if not storage_uri.startswith(f"{self.tenant_id}/"):
                    logger.warning("Storage URI rejected (reason=cross_tenant_uri)")
                    raise CrossTenantStorageAccessError(details={"reason": "cross_tenant_uri"})
                if not storage_uri.startswith(f"{self.tenant_id}/{safe_doc_id}/"):
                    logger.warning("Storage URI rejected (reason=cross_document_uri)")
                    raise TenantResourceNotFoundError()
                logger.warning("Storage URI rejected (reason=non_canonical_artifact)")
                raise StorageError(
                    "Non-canonical storage URI detected.", details={"reason": "non_canonical_artifact"}
                )

            # Step F: pages directory, then the page file, relative to the verified descriptors
            pages_fd = _open_directory_nofollow("pages", doc_fd, "pages_directory_is_a_symlink")
            file_fd = _open_leaf_nofollow(canonical_name, pages_fd, "page_file_is_symlink")
            _require_regular_file(file_fd)
            page_file = os.fdopen(file_fd, "rb")
            file_fd = None                     # the file object is now the sole owner of this descriptor
            try:
                with page_file as f:
                    image_bytes = f.read()
            except OSError as exc:
                logger.error("Page image read failed (reason=page_unreadable, errno=%s)", exc.errno)
                raise StorageError(
                    "Failed to read page image file.", details={"reason": "page_unreadable"}
                ) from exc
        except BaseException:
            # Another error is propagating: release what is still owned, never mask the original error.
            _close_owned_descriptors((file_fd, pages_fd, doc_fd, tenant_fd, base_dir_fd), strict=False)
            raise

        # Success: the directory descriptors are the only ones still owned; a failure to close them is not ignored.
        _close_owned_descriptors((file_fd, pages_fd, doc_fd, tenant_fd, base_dir_fd), strict=True)

        # Step G: strict structure (ends exactly at IEND), then full Pillow verification
        validate_png_structure(image_bytes)

        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.verify()
                if img.format != "PNG":
                    raise ValueError("not_png")
        except Exception as exc:
            logger.error("Page image rejected (reason=png_decode_failure, error_type=%s)", type(exc).__name__)
            raise StorageError(
                "Corrupted or invalid page image data.", details={"reason": "png_decode_failure"}
            ) from exc

        return image_bytes, "image/png"
