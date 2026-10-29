"""
Idempotency Store Interface and File-backed / In-Memory Implementations.

Adheres to:
- PRD API-013: Mutating calls accept Idempotency-Key; replays within 24 hours return original result.
- PRD FR-ING-006: Same tenant + same idempotency key + same hash -> return existing document ID.
- PRD SEC-006: Tenant isolation.
- Concurrent lock protection: Prevents race conditions during identical simultaneous uploads.
"""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from typing import Dict, Optional, Tuple, Union

from doc_extractor.exceptions import ConflictError, StorageError
from doc_extractor.models import IdempotencyRecord


class IdempotencyStore(ABC):
    """Abstract interface for storing and retrieving idempotency records."""

    @abstractmethod
    def get_record(self, tenant_id: str, idempotency_key: str) -> Optional[IdempotencyRecord]:
        """Fetch idempotency record for tenant and key if not expired."""
        pass

    @abstractmethod
    def save_record(self, record: IdempotencyRecord) -> None:
        """Persist or update an idempotency record."""
        pass

    @abstractmethod
    def delete_record(self, tenant_id: str, idempotency_key: str) -> None:
        """Removes an idempotency record (e.g. upon fatal failure rollback)."""
        pass


class LocalIdempotencyStore(IdempotencyStore):
    """
    Thread-safe, filesystem-backed idempotency store.
    Stores records under: {base_dir}/{tenant_id}/idempotency/{key_hash}.json
    
    Guarantees:
    - Scoped strictly to tenant (SEC-006).
    - 24-hour expiration check (API-013).
    - Atomic writes via temporary files.
    - Fine-grained per-key thread lock to handle concurrency safely (NFR-006).
    """

    def __init__(self, base_dir: Union[str, Path], window_seconds: float = 86400.0) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._key_locks: Dict[str, threading.Lock] = {}

    def get_key_lock(self, tenant_id: str, idempotency_key: str) -> threading.Lock:
        """Returns a thread lock specific to (tenant_id, idempotency_key)."""
        composite = f"{tenant_id}:{idempotency_key}"
        with self._lock:
            if composite not in self._key_locks:
                self._key_locks[composite] = threading.Lock()
            return self._key_locks[composite]

    def _get_record_path(self, tenant_id: str, idempotency_key: str) -> Path:
        import hashlib
        safe_tenant = Path(tenant_id).name
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        tenant_dir = (self.base_dir / safe_tenant / "idempotency").resolve()
        tenant_dir.mkdir(parents=True, exist_ok=True)
        return tenant_dir / f"{key_hash}.json"

    def get_record(self, tenant_id: str, idempotency_key: str) -> Optional[IdempotencyRecord]:
        file_path = self._get_record_path(tenant_id, idempotency_key)
        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rec = IdempotencyRecord(**data)
            # Check 24h expiration
            if rec.is_expired():
                file_path.unlink(missing_ok=True)
                return None
            return rec
        except Exception as exc:
            raise StorageError(
                message=f"Failed to load idempotency record: {str(exc)}",
                details={"tenant_id": tenant_id, "idempotency_key": idempotency_key},
            ) from exc

    def save_record(self, record: IdempotencyRecord) -> None:
        file_path = self._get_record_path(record.tenant_id, record.idempotency_key)
        temp_file = file_path.parent / f".tmp_{file_path.name}"

        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(record.to_dict(), f, indent=2)
            temp_file.replace(file_path)
        except Exception as exc:
            if temp_file.exists():
                temp_file.unlink(missing_ok=True)
            raise StorageError(
                message=f"Failed to persist idempotency record: {str(exc)}",
                details={"tenant_id": record.tenant_id, "idempotency_key": record.idempotency_key},
            ) from exc

    def delete_record(self, tenant_id: str, idempotency_key: str) -> None:
        file_path = self._get_record_path(tenant_id, idempotency_key)
        if file_path.exists():
            file_path.unlink(missing_ok=True)

