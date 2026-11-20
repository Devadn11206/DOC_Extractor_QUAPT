"""
Document Upload Service (Module M5 / Phase 2, API-001).

A thin REST-layer adapter over the EXISTING Module A pipeline (``ModuleAPipeline.process_document``): ingestion
(validation, hashing, idempotency, malware scan, document record), page rendering, preprocessing and the handoff payload
are all done there. Nothing of that is reimplemented here. This service adds only what the HTTP boundary needs:

- tenant binding: the Module A component is the principal's own tenant component, and a mismatch is refused;
- a targeted rollback for a CLIENT rejection that Module A only discovers after it saved the document record (for
  example a PDF with a valid header and a corrupt body, or a password-protected PDF, which the renderer cannot open): the
  rejected document's artifacts and its own idempotency record are removed, so an unsupported file leaves nothing stored
  (the PRD requirement). The removal is VERIFIED: if it cannot be confirmed, the answer is a generic 500, never a 400 that
  would claim nothing is stored;
- translation of Module A's outcome into the public contract. Module A reports ingestion problems INSIDE the returned
  payload (``payload.error``, a dict) rather than by raising, so both that path and a raised ``DocumentExtractorError`` are
  handled, through one mapping. Nothing Module A puts in an error (the filename, a header dump, a threat name, a path, an
  exception text, the idempotency key) is ever forwarded: every public error is built from fixed text.

``DocumentService.process_document`` is deliberately NOT used: it also runs extraction, which API-001 does not do.

PROVISIONAL limits (docs/m5-open-decisions.md, "M2D API-001 Provisional Behavior and Limitations"): rendering and
preprocessing run synchronously inside the request, nothing continues in the background, and the process-wide lock below
serializes Module A runs. Operational failures (an HTTP 500 after the record was saved: preprocessing, storage, an
undecodable image) are NOT rolled back here and can leave a ``processing`` record.

Logging: this module logs exactly one thing, and only when a late-rejection rollback could not be completed or verified:
one constant-message line with a fixed reason code, the request id and the document id (both generated or validated
identifiers). It never logs the client's filename, file content, an exception text or the idempotency key.
"""

import logging
import threading
from typing import Any, BinaryIO, Optional, Tuple

from doc_extractor.api.contracts.documents import validate_document_id
from doc_extractor.api.exceptions import FileTooLargeError
from doc_extractor.exceptions import (
    ConflictError,
    MalwareDetectedError,
    ScannerUnavailableError,
    SecurityCheckFailedError,
    StorageError,
    TenantMismatchError,
    UnsupportedFileError,
)
from doc_extractor.models import compute_request_fingerprint
from doc_extractor.pipeline import ModuleAPipeline

logger = logging.getLogger("doc_extractor.api.upload_service")

# Fixed reason codes of the one log line and of the generic 500 answered when a late-rejection rollback is incomplete.
ROLLBACK_INCOMPLETE_REASON = "late_rejection_rollback_incomplete"
IDEMPOTENCY_CLEANUP_INCOMPLETE_REASON = "late_rejection_idempotency_cleanup_incomplete"

# Module A's own reasons for "too big": the byte limit and the page limit (PRD section 19: 413 FILE_TOO_LARGE).
_TOO_LARGE_REASONS = frozenset({"FILE_SIZE_LIMIT_EXCEEDED", "PAGE_LIMIT_EXCEEDED"})

# Module A error codes that mean "the CLIENT sent an unsupported file" (HTTP 400/413). When one of them is reported for a
# document whose record Module A had already saved, that record is rolled back (see ``_roll_back_late_rejection``).
# DOCUMENT_ENCRYPTED: a password-protected PDF the renderer cannot open. The PRD error table answers an "unsupported or
# unsafe file" with 400 UNSUPPORTED_FILE and FR-ING-002 says nothing is stored; the PRD names no separate code. PROVISIONAL.
_CLIENT_REJECTION_CODES = frozenset({"UNSUPPORTED_FILE", "SECURITY_CHECK_FAILED", "DOCUMENT_ENCRYPTED"})

# PROVISIONAL. One Module A run at a time per process. The PDF rendering library is documented as not thread-safe and the
# route runs in the threadpool, so concurrent uploads must not render at the same time. A side effect is that concurrent
# retries with the same idempotency key are serialized too: the second one waits, then Module A's EXISTING idempotency
# check replays the finished handoff instead of rendering the document again. Single-process scope only (no
# cross-process lock), and throughput is limited until a worker exists.
_MODULE_A_EXECUTION_LOCK = threading.Lock()


def public_upload_error(code: Optional[str], reason: Optional[str], request_id: Optional[str] = None) -> Exception:
    """
    The public exception for a Module A error ``code`` (and its ``details["reason"]``, used only to tell a size limit from
    other unsupported files). Fixed text and empty details only. An unrecognized code is a generic server fault.
    """
    if code == "UNSUPPORTED_FILE":
        if reason in _TOO_LARGE_REASONS:
            return FileTooLargeError(request_id=request_id, details={})
        return UnsupportedFileError(message="This file type isn't supported.", request_id=request_id, details={})
    if code in ("SECURITY_CHECK_FAILED", "DOCUMENT_ENCRYPTED"):
        return UnsupportedFileError(message="This file type isn't supported.", request_id=request_id, details={})
    if code == "MALWARE_DETECTED":
        return MalwareDetectedError(
            message="This file was blocked by the safety scan.", request_id=request_id, details={}
        )
    if code == "SCANNER_UNAVAILABLE":
        return ScannerUnavailableError(
            message="Malware scanning is currently unavailable. Try again later.", request_id=request_id, details={}
        )
    if code == "IDEMPOTENCY_KEY_CONFLICT":
        return ConflictError(message="This request key was already used.", request_id=request_id, details={})
    # Everything else (a storage failure, a rendering or preprocessing failure, an unknown code): generic 500, with a fixed
    # server-side reason. The Module A message and details are dropped: they may carry paths or exception text.
    return StorageError(
        "Upload processing failed.", request_id=request_id, details={"reason": "upload_processing_failed"}
    )


def _error_parts(error: Any) -> Tuple[Optional[str], Optional[str]]:
    """(code, reason) of a Module A error dict, tolerating any shape: nothing else of it is read."""
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    details = error.get("details")
    reason = details.get("reason") if isinstance(details, dict) else None
    return (code if isinstance(code, str) else None), (reason if isinstance(reason, str) else None)


class DocumentUploadService:
    """Tenant-bound upload coordinator over one tenant's ``ModuleAPipeline``."""

    def __init__(self, module_a: ModuleAPipeline, tenant_id: str, storage: Optional[Any] = None) -> None:
        if getattr(module_a, "tenant_id", None) != tenant_id:
            raise TenantMismatchError(message="Tenant mismatch: Module A component belongs to another tenant.")
        self.module_a = module_a
        self.tenant_id = tenant_id
        self.storage = storage if storage is not None else getattr(module_a, "storage", None)

    def upload(
        self,
        stream: BinaryIO,
        claimed_filename: str,
        *,
        idempotency_key: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> str:
        """
        Run Module A on ``stream`` and return the accepted document's id; raise the public error otherwise.

        ``claimed_filename`` and the stream content are untrusted (Module A validates the content by its magic bytes and
        reduces the name to a basename; the client's media type is not even passed in).
        """
        rollback_failure: Optional[str] = None
        try:
            with _MODULE_A_EXECUTION_LOCK:
                payload = self._run(stream, claimed_filename, idempotency_key, request_id)
                error = getattr(payload, "error", None)
                if error:
                    code, _ = _error_parts(error)
                    if code in _CLIENT_REJECTION_CODES:
                        # Same lock as the run: no other upload of this process can interleave with the rollback.
                        rollback_failure = self._roll_back_late_rejection(payload, idempotency_key, request_id)
        except (
            UnsupportedFileError,
            SecurityCheckFailedError,
            MalwareDetectedError,
            ScannerUnavailableError,
            ConflictError,
        ) as exc:
            # The raised path (Module A normally reports these inside the payload, see below): same mapping, same text.
            reason = exc.details.get("reason") if isinstance(getattr(exc, "details", None), dict) else None
            raise public_upload_error(exc.code, reason if isinstance(reason, str) else None, request_id) from None

        if rollback_failure is not None:
            # The file is rejected, but its removal is not confirmed: never answer 400 as if nothing were stored.
            raise StorageError(
                "Upload processing failed.", request_id=request_id, details={"reason": rollback_failure}
            )

        if error or getattr(payload, "status", None) != "processing":
            code, reason = _error_parts(error)
            raise public_upload_error(code, reason, request_id)

        doc_id = validate_document_id(payload.document_id)
        if self.storage is not None:
            record = self.storage.get_document_record(doc_id)
            if record is None or not self._record_belongs(record, doc_id):
                raise StorageError(
                    "Document record not found after upload.",
                    details={"reason": "missing_document_record"},
                )

        return doc_id

    def _record_belongs(self, record: Any, doc_id: str) -> bool:
        """True when ``record`` is this tenant's record of ``doc_id``."""
        rec_tenant = getattr(record, "tenant_id", None) or (record.get("tenant_id") if isinstance(record, dict) else None)
        rec_doc_id = getattr(record, "document_id", None) or (
            record.get("document_id") if isinstance(record, dict) else None
        )
        return rec_tenant == self.tenant_id and rec_doc_id == doc_id

    def _roll_back_late_rejection(
        self, payload: Any, idempotency_key: Optional[str], request_id: Optional[str] = None
    ) -> Optional[str]:
        """
        A client rejection (unsupported or password-protected file) that Module A reported AFTER it had saved the document
        record: remove what that one request stored and VERIFY the removal.

        Returns ``None`` when there was nothing to roll back or the rollback is verified complete, otherwise the fixed
        reason code of the incomplete rollback (it has been logged once). The caller then answers a generic 500.

        - Nothing happens unless this tenant's record of exactly the rejected document id exists. Rejections raised before
          the record is saved (magic bytes, size, page limit, malware, scanner) have no record: nothing to remove.
        - The storage backend's ``cleanup_document`` is called but never trusted (the local backend swallows errors): the
          document is confirmed gone only when its record is absent, it has no page artifacts and, where the backend can
          resolve it, its tenant-relative directory no longer exists. A cleanup that raises is treated the same way.
        - Only after that is the idempotency record removed, under its key lock, and only if it names the same document id
          AND carries the fingerprint of this very request (file hash and filename); another request's record (for example
          a successful upload that used the same key) is never touched. Its removal is verified too.
        - If the document cannot be confirmed removed, the idempotency record is deliberately left in place (the retry then
          re-processes the same document and tries the rollback again). If only the idempotency removal fails, the document
          is gone and the orphaned idempotency record is healed by Module A on the next retry with the same file.
        """
        storage = self.storage
        if storage is None:
            return None
        try:
            doc_id = validate_document_id(getattr(payload, "document_id", None))
        except Exception:
            return None                                   # not an id this request could have stored
        try:
            record = storage.get_document_record(doc_id)
        except Exception:
            return self._rollback_incomplete(ROLLBACK_INCOMPLETE_REASON, request_id, doc_id)
        if record is None or not self._record_belongs(record, doc_id):
            return None

        store = getattr(self.module_a, "idempotency_store", None) if idempotency_key else None
        key_lock = store.get_key_lock(self.tenant_id, idempotency_key) if hasattr(store, "get_key_lock") else None
        if key_lock is not None:
            key_lock.acquire()
        try:
            try:
                storage.cleanup_document(doc_id)
            except Exception:
                pass                                      # whatever happened, only the verification below decides
            if not self._document_is_gone(doc_id):
                return self._rollback_incomplete(ROLLBACK_INCOMPLETE_REASON, request_id, doc_id)
            if store is not None and not self._remove_own_idempotency_record(store, idempotency_key, payload, doc_id):
                return self._rollback_incomplete(IDEMPOTENCY_CLEANUP_INCOMPLETE_REASON, request_id, doc_id)
            return None
        finally:
            if key_lock is not None:
                key_lock.release()

    def _document_is_gone(self, doc_id: str) -> bool:
        """True only when the document's record, page artifacts and directory are all confirmed absent (else: unverified)."""
        storage = self.storage
        try:
            if storage.get_document_record(doc_id) is not None:
                return False
            if storage.list_page_images(doc_id):
                return False
            resolve = getattr(storage, "resolve_uri_to_path", None)
            if resolve is not None and resolve(f"{self.tenant_id}/{doc_id}").exists():
                return False
            return True
        except Exception:
            return False

    def _remove_own_idempotency_record(self, store: Any, idempotency_key: str, payload: Any, doc_id: str) -> bool:
        """
        Delete the idempotency record if, and only if, it is this request's (same document id and fingerprint). True when
        it is confirmed absent afterwards or was never this request's; False when it could not be removed or verified.
        """
        try:
            idem = store.get_record(self.tenant_id, idempotency_key)
            if idem is None:
                return True
            own_fingerprint = compute_request_fingerprint(
                getattr(payload, "file_hash_sha256", "") or "", getattr(payload, "claimed_filename", "") or ""
            )
            if idem.document_id != doc_id or idem.request_fingerprint != own_fingerprint:
                return True                               # another request's record: left untouched
            store.delete_record(self.tenant_id, idempotency_key)
            return store.get_record(self.tenant_id, idempotency_key) is None
        except Exception:
            return False

    @staticmethod
    def _rollback_incomplete(reason: str, request_id: Optional[str], doc_id: str) -> str:
        """The one log line: a constant message, a fixed reason and two safe identifiers. Returns the reason."""
        logger.error(
            "Late rejection rollback incomplete (reason=%s, request_id=%s, document_id=%s)", reason, request_id, doc_id
        )
        return reason

    def _run(
        self,
        stream: BinaryIO,
        claimed_filename: str,
        idempotency_key: Optional[str],
        request_id: Optional[str],
    ) -> Any:
        return self.module_a.process_document(
            file_path_or_stream=stream,
            claimed_filename=claimed_filename,
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
