"""
Core Document Ingestion Coordination Service.

Coordinates:
1. Idempotency inspection (API-013, FR-ING-006).
2. Input reception, validation, and content SHA-256 hashing.
3. Pre-processing malware scanning (FR-ING-003, SEC-009).
4. Creation of IngestionResult and fail-closed dispatch to downstream hooks.
5. Canonical document record creation and status alignment (SCH-012).

Guarantees:
- Replays with same key and same content return the existing document result.
- Replays with same key and different content/parameters fail with IDEMPOTENCY_KEY_CONFLICT.
- Multi-tenant boundary isolation (SEC-006).
- Infected, corrupt, or unverified files NEVER reach downstream processing hooks.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import os
from pathlib import Path
import tempfile
import time
from typing import Any, BinaryIO, Callable, Dict, Optional, Union
import uuid

from doc_extractor.config import Settings
from doc_extractor.exceptions import (
    ConflictError,
    DocumentExtractorError,
    MalwareDetectedError,
    ScannerUnavailableError,
    UnsupportedFileError,
)
from doc_extractor.idempotency import IdempotencyStore, LocalIdempotencyStore
from doc_extractor.logging import setup_logging
from doc_extractor.malware import (
    MalwareScanResult,
    MalwareScanner,
    ScanStatus,
    UnconfiguredScanner,
)
from doc_extractor.models import (
    DocumentRecord,
    DocumentStatus,
    IdempotencyRecord,
    IngestionState,
    compute_file_sha256,
    compute_request_fingerprint,
    compute_stream_sha256,
    map_internal_state_to_external_status,
)
from doc_extractor.storage import LocalDiskStorage, StorageBackend
from doc_extractor.validation import DocumentValidator, ValidationResult

class ProcessingStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    AUTO_APPROVED = "auto_approved"
    NEEDS_REVIEW = "needs_review"
    REVIEWED = "reviewed"
    REJECTED = "rejected"
    REJECTED_MALWARE = "rejected_malware"
    FAILED = "failed"


@dataclass
class IngestionResult:
    """Standardized ingestion output returned to caller / API adapter."""
    document_id: str
    status: Union[DocumentStatus, ProcessingStatus]
    is_accepted: bool
    filename: str
    file_size_bytes: int = 0
    page_count: int = 0
    mime_type: Optional[str] = None
    file_hash_sha256: Optional[str] = None
    internal_state: IngestionState = IngestionState.QUEUED
    validation: Optional[ValidationResult] = None
    malware_scan: Optional[MalwareScanResult] = None
    error: Optional[Dict[str, Any]] = None
    ingestion_duration_ms: float = 0.0
    idempotent_replay: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "document_id": self.document_id,
            "status": self.status.value,
            "is_accepted": self.is_accepted,
            "filename": self.filename,
            "file_size_bytes": self.file_size_bytes,
            "page_count": self.page_count,
            "mime_type": self.mime_type,
            "file_hash_sha256": self.file_hash_sha256,
            "internal_state": self.internal_state.value,
            "error": self.error,
            "ingestion_duration_ms": self.ingestion_duration_ms,
            "idempotent_replay": self.idempotent_replay,
        }


class IngestionService:
    """
    Ingestion coordinator enforcing validation, malware scanning, content hashing,
    idempotency protection (API-013), and document record lifecycle (SCH-012).
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        scanner: Optional[MalwareScanner] = None,
        storage: Optional[StorageBackend] = None,
        idempotency_store: Optional[IdempotencyStore] = None,
        downstream_callback: Optional[Callable[[IngestionResult, BinaryIO], None]] = None,
        tenant_id: str = "default_tenant",
    ) -> None:
        self.settings = settings or Settings()
        self.tenant_id = tenant_id
        self.validator = DocumentValidator(self.settings)
        self.scanner = scanner or UnconfiguredScanner()
        self.storage = storage or LocalDiskStorage(self.settings.storage_base_dir, tenant_id=self.tenant_id)
        self.idempotency_store = idempotency_store or LocalIdempotencyStore(self.settings.storage_base_dir)
        self.downstream_callback = downstream_callback
        self.logger = setup_logging(
            level=self.settings.log_level,
            service_name=self.settings.service_name,
            environment=self.settings.app_env,
        )

    def ingest_document(
        self,
        file_path_or_stream: Union[str, Path, BinaryIO],
        claimed_filename: str,
        request_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        extra_request_params: Optional[Dict[str, Any]] = None,
    ) -> IngestionResult:
        """
        Coordinates the complete document ingestion workflow:
        1. Lock by idempotency_key (if provided) to prevent race conditions.
        2. Validate file bytes and calculate streaming SHA-256 content hash.
        3. Check idempotency record / duplicate file within tenant.
        4. Scan for malware (fail-closed).
        5. Persist DocumentRecord and IdempotencyRecord atomically.
        6. Invoke downstream processing hooks for accepted documents.
        """
        start_time = time.perf_counter()
        req_id = request_id or str(uuid.uuid4())
        safe_filename = Path(claimed_filename).name

        self.logger.info(
            "Ingestion request received",
            extra={
                "request_id": req_id,
                "tenant_id": self.tenant_id,
                "extra_data": {"claimed_filename": safe_filename, "has_idempotency_key": bool(idempotency_key)},
            },
        )

        # Fine-grained lock if idempotency key is provided
        key_lock = None
        if idempotency_key and hasattr(self.idempotency_store, "get_key_lock"):
            key_lock = self.idempotency_store.get_key_lock(self.tenant_id, idempotency_key)
            key_lock.acquire()

        try:
            # 1. Validation Step (FR-ING-001, FR-ING-002, GR-001)
            try:
                val_result = self.validator.validate_file(file_path_or_stream, safe_filename)
            except DocumentExtractorError as err:
                duration_ms = (time.perf_counter() - start_time) * 1000
                err.request_id = req_id
                self.logger.warning(
                    "Document rejected during input validation",
                    extra={
                        "request_id": req_id,
                        "tenant_id": self.tenant_id,
                        "extra_data": {"error_code": err.code, "reason": err.details.get("reason")},
                    },
                )
                return IngestionResult(
                    document_id=f"doc_rej_{uuid.uuid4().hex[:8]}",
                    status=ProcessingStatus.REJECTED,
                    internal_state=IngestionState.REJECTED_UNSUPPORTED,
                    is_accepted=False,
                    filename=safe_filename,
                    error=err.to_envelope()["error"],
                    ingestion_duration_ms=duration_ms,
                )

            # 2. Compute Streaming SHA-256 Hash
            if isinstance(file_path_or_stream, (str, Path)):
                file_hash = compute_file_sha256(file_path_or_stream)
                stream = open(file_path_or_stream, "rb")
                should_close = True
            else:
                stream = file_path_or_stream
                should_close = False
                file_hash = compute_stream_sha256(stream)

            request_fingerprint = compute_request_fingerprint(
                file_hash=file_hash,
                claimed_filename=safe_filename,
                extra_params=extra_request_params,
            )

            # 3. Idempotency Check (API-013, FR-ING-006)
            if idempotency_key:
                existing_record = self.idempotency_store.get_record(self.tenant_id, idempotency_key)
                if existing_record:
                    # Check for fingerprint match
                    if existing_record.request_fingerprint != request_fingerprint:
                        raise ConflictError(
                            message="Idempotency key reused with different request payload or file.",
                            request_id=req_id,
                            details={"idempotency_key": idempotency_key},
                        )
                    # Safe replay within 24 hours
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    payload = existing_record.response_payload
                    self.logger.info(
                        "Idempotent replay served",
                        extra={
                            "request_id": req_id,
                            "tenant_id": self.tenant_id,
                            "document_id": existing_record.document_id,
                        },
                    )
                    return IngestionResult(
                        document_id=existing_record.document_id,
                        status=ProcessingStatus(payload.get("status", ProcessingStatus.QUEUED.value)),
                        internal_state=IngestionState(payload.get("internal_state", IngestionState.QUEUED.value)),
                        is_accepted=payload.get("is_accepted", True),
                        filename=payload.get("filename", safe_filename),
                        file_size_bytes=payload.get("file_size_bytes", val_result.file_size_bytes),
                        page_count=payload.get("page_count", val_result.page_count),
                        mime_type=payload.get("mime_type", val_result.detected_mime),
                        file_hash_sha256=file_hash,
                        ingestion_duration_ms=duration_ms,
                        idempotent_replay=True,
                    )

            # 4. Malware Scanning (FR-ING-003, SEC-009)
            doc_id = f"doc_{uuid.uuid4().hex[:12]}"
            try:
                if not self.scanner.is_operational():
                    raise ScannerUnavailableError(
                        message="Malware scanning is required but scanner is not operational.",
                        request_id=req_id,
                        details={"scanner_type": type(self.scanner).__name__},
                    )

                scan_result = self.scanner.scan_stream(stream)
                stream.seek(0)

                if scan_result.status == ScanStatus.INFECTED:
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    err = MalwareDetectedError(
                        message="File rejected: malware signature detected.",
                        request_id=req_id,
                        details={"threat_name": scan_result.threat_name},
                    )
                    return IngestionResult(
                        document_id=doc_id,
                        status=ProcessingStatus.REJECTED_MALWARE,
                        internal_state=IngestionState.REJECTED_MALWARE,
                        is_accepted=False,
                        filename=safe_filename,
                        file_size_bytes=val_result.file_size_bytes,
                        page_count=val_result.page_count,
                        mime_type=val_result.detected_mime,
                        file_hash_sha256=file_hash,
                        validation=val_result,
                        malware_scan=scan_result,
                        error=err.to_envelope()["error"],
                        ingestion_duration_ms=duration_ms,
                    )

                if scan_result.status == ScanStatus.ERROR:
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    err = ScannerUnavailableError(
                        message="Malware scanner encountered an error during scan.",
                        request_id=req_id,
                        details={"error": scan_result.details},
                    )
                    return IngestionResult(
                        document_id=doc_id,
                        status=ProcessingStatus.FAILED,
                        internal_state=IngestionState.FAILED,
                        is_accepted=False,
                        filename=safe_filename,
                        file_size_bytes=val_result.file_size_bytes,
                        page_count=val_result.page_count,
                        mime_type=val_result.detected_mime,
                        file_hash_sha256=file_hash,
                        validation=val_result,
                        malware_scan=scan_result,
                        error=err.to_envelope()["error"],
                        ingestion_duration_ms=duration_ms,
                    )

                # 5. Persist Document Record (SCH-012)
                now_str = datetime.now(timezone.utc).isoformat()
                doc_record = DocumentRecord(
                    document_id=doc_id,
                    tenant_id=self.tenant_id,
                    file_hash_sha256=file_hash,
                    claimed_filename=safe_filename,
                    file_size_bytes=val_result.file_size_bytes,
                    page_count=val_result.page_count,
                    mime_type=val_result.detected_mime,
                    status=DocumentStatus.PROCESSING,
                    internal_state=IngestionState.QUEUED,
                    created_at=now_str,
                    updated_at=now_str,
                    idempotency_key=idempotency_key,
                )
                self.storage.save_document_record(doc_record)

                duration_ms = (time.perf_counter() - start_time) * 1000
                result = IngestionResult(
                    document_id=doc_id,
                    status=ProcessingStatus.QUEUED,
                    internal_state=IngestionState.QUEUED,
                    is_accepted=True,
                    filename=safe_filename,
                    file_size_bytes=val_result.file_size_bytes,
                    page_count=val_result.page_count,
                    mime_type=val_result.detected_mime,
                    file_hash_sha256=file_hash,
                    validation=val_result,
                    malware_scan=scan_result,
                    ingestion_duration_ms=duration_ms,
                )

                # 6. Save Idempotency Record (24h Window)
                if idempotency_key:
                    now_utc = datetime.now(timezone.utc).timestamp()
                    idem_rec = IdempotencyRecord(
                        idempotency_key=idempotency_key,
                        tenant_id=self.tenant_id,
                        request_fingerprint=request_fingerprint,
                        document_id=doc_id,
                        response_payload=result.to_dict(),
                        created_at_utc=now_utc,
                        expires_at_utc=now_utc + 86400.0,
                    )
                    self.idempotency_store.save_record(idem_rec)

                # 7. Invoke Downstream Callback
                if self.downstream_callback is not None:
                    self.downstream_callback(result, stream)

                return result

            except DocumentExtractorError as err:
                duration_ms = (time.perf_counter() - start_time) * 1000
                err.request_id = req_id
                return IngestionResult(
                    document_id=doc_id,
                    status=ProcessingStatus.FAILED,
                    internal_state=IngestionState.FAILED,
                    is_accepted=False,
                    filename=safe_filename,
                    error=err.to_envelope()["error"],
                    ingestion_duration_ms=duration_ms,
                )

            finally:
                if should_close:
                    stream.close()

        finally:
            if key_lock:
                key_lock.release()
