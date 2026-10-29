"""
Models for Document Metadata, Processing Status Lifecycle (SCH-012),
and Idempotency Replay Tracking (API-013, FR-ING-006).
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Union


class DocumentStatus(str, Enum):
    """
    Externally exposed document status matching PRD SCH-012:
    - processing
    - auto_approved
    - needs_review
    - reviewed
    - rejected
    - failed
    """
    PROCESSING = "processing"
    AUTO_APPROVED = "auto_approved"
    NEEDS_REVIEW = "needs_review"
    REVIEWED = "reviewed"
    REJECTED = "rejected"
    FAILED = "failed"


class IngestionState(str, Enum):
    """
    Internal detailed state within Module A pipeline.
    Maps deterministically to DocumentStatus (SCH-012).
    """
    QUEUED = "queued"                          # Successfully validated, hashed, queued -> PROCESSING
    VALIDATING = "validating"                  # In validation/malware scan -> PROCESSING
    PREPROCESSING = "preprocessing"            # In rendering/preprocessing -> PROCESSING
    READY_FOR_EXTRACTION = "ready_for_extraction"  # Stage 1 completed -> PROCESSING
    REJECTED_UNSUPPORTED = "rejected_unsupported"  # Validation failed -> REJECTED
    REJECTED_MALWARE = "rejected_malware"          # Malware detected -> REJECTED
    FAILED = "failed"                              # System error / scan failure -> FAILED


def map_internal_state_to_external_status(state: IngestionState) -> DocumentStatus:
    """Maps internal pipeline state to public SCH-012 status."""
    if state in (IngestionState.QUEUED, IngestionState.VALIDATING, IngestionState.PREPROCESSING, IngestionState.READY_FOR_EXTRACTION):
        return DocumentStatus.PROCESSING
    elif state in (IngestionState.REJECTED_UNSUPPORTED, IngestionState.REJECTED_MALWARE):
        return DocumentStatus.REJECTED
    elif state == IngestionState.FAILED:
        return DocumentStatus.FAILED
    return DocumentStatus.FAILED


VALID_STATUS_TRANSITIONS: Dict[DocumentStatus, List[DocumentStatus]] = {
    DocumentStatus.PROCESSING: [
        DocumentStatus.AUTO_APPROVED,
        DocumentStatus.NEEDS_REVIEW,
        DocumentStatus.REJECTED,
        DocumentStatus.FAILED,
    ],
    DocumentStatus.NEEDS_REVIEW: [
        DocumentStatus.REVIEWED,
        DocumentStatus.REJECTED,
        DocumentStatus.FAILED,
    ],
    DocumentStatus.AUTO_APPROVED: [],  # Terminal
    DocumentStatus.REVIEWED: [],       # Terminal
    DocumentStatus.REJECTED: [],       # Terminal
    DocumentStatus.FAILED: [],         # Terminal
}


def compute_stream_sha256(stream: BinaryIO, chunk_size: int = 64 * 1024) -> str:
    """
    Computes SHA-256 hash by streaming chunks to avoid loading unbounded files into memory.
    Resets stream position to beginning before and after hashing.
    """
    stream.seek(0)
    hasher = hashlib.sha256()
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        hasher.update(chunk)
    stream.seek(0)
    return hasher.hexdigest()


def compute_file_sha256(file_path: Union[str, Path], chunk_size: int = 64 * 1024) -> str:
    """Streams a file from disk and computes its SHA-256 hash."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_request_fingerprint(
    file_hash: str,
    claimed_filename: str,
    extra_params: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Computes a cryptographic fingerprint of the mutating request parameters.
    Used by API-013 to detect idempotency key conflicts.
    """
    hasher = hashlib.sha256()
    hasher.update(file_hash.encode("utf-8"))
    hasher.update(claimed_filename.encode("utf-8"))
    if extra_params:
        import json
        sorted_params = json.dumps(extra_params, sort_keys=True)
        hasher.update(sorted_params.encode("utf-8"))
    return hasher.hexdigest()


@dataclass(frozen=True)
class DocumentRecord:
    """
    Canonical document record stored per tenant.
    """
    document_id: str
    tenant_id: str
    file_hash_sha256: str
    claimed_filename: str
    file_size_bytes: int
    page_count: int
    mime_type: str
    status: DocumentStatus
    internal_state: IngestionState
    created_at: str
    updated_at: str
    idempotency_key: Optional[str] = None
    error: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["internal_state"] = self.internal_state.value
        return data


@dataclass(frozen=True)
class IdempotencyRecord:
    """
    Record of an idempotency key execution for 24-hour replay protection (API-013).
    """
    idempotency_key: str
    tenant_id: str
    request_fingerprint: str
    document_id: str
    response_payload: Dict[str, Any]
    created_at_utc: float  # Unix timestamp
    expires_at_utc: float  # Unix timestamp (created_at + 24 hours)

    def is_expired(self, current_time_utc: Optional[float] = None) -> bool:
        now = current_time_utc if current_time_utc is not None else datetime.now(timezone.utc).timestamp()
        return now > self.expires_at_utc

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

