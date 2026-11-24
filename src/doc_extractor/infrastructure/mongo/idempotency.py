"""
MongoDB ``IdempotencyStore`` (Team Member 3, Phase 3): tenant-scoped idempotency whose source of truth is a unique index.

THE GAP THIS CLOSES. ``RedisIdempotencyStore`` keeps the only copy of a record in Redis and relies on a Redis lock for its
"check, then create" sequence. A lock lease that expires while its holder is still running, or a Redis that loses its data, lets two
replicas both create a document for one idempotency key. Here the DURABLE record decides:

- one document per (tenant, key hash) in ``idempotency_records``, with a UNIQUE index on ``(tenant_id, key_hash)``;
- ``save_record`` inserts; a second live record for the same key is refused by the index. The same request saving again (same
  fingerprint and document) is accepted as a replay; a DIFFERENT request raises ``ConflictError`` (409 ``IDEMPOTENCY_KEY_CONFLICT``),
  and an EXPIRED record is replaced by one conditional write;
- a record expires after the 24-hour window (API-013): readers check ``expires_at_us`` themselves, and a TTL index removes the
  document later (it is only housekeeping and never decides an answer).

REDIS IS COORDINATION ONLY. ``get_key_lock`` hands the ingestion path a per-(tenant, key) lock so that two replicas do not do the
same work at the same time: a Redis lock when a ``RedisLockProvider`` is supplied, otherwise a striped in-process lock. Neither
is relied on for correctness: if a lock lease expires mid-way, the unique index still admits exactly one live record, and the
loser gets ``ConflictError`` instead of a second document. (Its half-finished document is cleaned up by the upload path's rollback.)

TENANTS. Every query carries the tenant (the repository base ANDs it in), and the key is stored as ``SHA-256(key)`` next to the
tenant, so the same key under two tenants is two records and no key text is needed to look one up. Errors never echo a key.

SYNCHRONOUS INTERFACE OVER AN ASYNCHRONOUS DRIVER. ``IdempotencyStore`` is synchronous (its callers run in worker threads: FastAPI
runs the upload route in its thread pool) and the MongoDB foundation is asynchronous. Each call is submitted to the event loop that
owns the connection (``start()`` records it) and waits for the answer with a bound. A call made FROM that event loop thread would
deadlock it, so it is refused with a typed error instead. Callers on the loop must use the ``a*`` coroutines.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, Optional, TypeVar

from doc_extractor.exceptions import ConflictError
from doc_extractor.idempotency import IdempotencyStore
from doc_extractor.infrastructure.mongo.connection import MongoConnection
from doc_extractor.infrastructure.mongo.errors import (
    DuplicateKeyPersistenceError,
    PersistenceConfigurationError,
    PersistenceDataError,
    PersistenceError,
    PersistenceUnavailableError,
    UnsafeQueryError,
)
from doc_extractor.infrastructure.mongo.indexes import IDEMPOTENCY_RECORDS, unique_index_exists
from doc_extractor.infrastructure.mongo.repository import TenantScopedRepository, validate_tenant
from doc_extractor.models import IdempotencyRecord

T = TypeVar("T")

DEFAULT_CALL_TIMEOUT_SECONDS = 15.0
MAX_KEY_LENGTH = 4_096
LOCK_STRIPES = 256
_UNIQUE_KEYS = [("tenant_id", 1), ("key_hash", 1)]
_SAVE_ROUNDS = 4


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_us() -> int:
    return time.time_ns() // 1000


class _StripedLocks:
    """In-process coordination used when no Redis is configured: a fixed set of re-entrant locks chosen by key hash (bounded memory)."""

    def __init__(self) -> None:
        self._locks = [threading.RLock() for _ in range(LOCK_STRIPES)]

    def lock(self, name: str) -> threading.RLock:
        return self._locks[int(_digest(name)[:8], 16) % LOCK_STRIPES]


class MongoIdempotencyStore(IdempotencyStore):
    """The ``IdempotencyStore`` interface over MongoDB. See the module docstring."""

    def __init__(
        self,
        connection: MongoConnection,
        *,
        locks: Optional[Any] = None,
        window_seconds: float = 86400.0,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        clock_us: Callable[[], int] = _now_us,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("The idempotency window must be positive.")
        if not 0 < call_timeout_seconds <= 120:
            raise ValueError("The call timeout must be between 0 and 120 seconds.")
        self._connection = connection
        self._store = TenantScopedRepository(connection, IDEMPOTENCY_RECORDS)
        self._locks = locks if locks is not None else _StripedLocks()
        self._window = float(window_seconds)
        self._timeout = float(call_timeout_seconds)
        self._clock = clock_us
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._index_verified = False

    # -- start-up -------------------------------------------------------------------------------------------------------------

    async def start(self) -> None:
        """
        Run once inside the event loop that owns the MongoDB connection (the application's start-up): records that loop for the
        synchronous calls and requires the unique index (it is the correctness mechanism, even when index bootstrap is off).
        """
        if not await unique_index_exists(self._connection.collection(IDEMPOTENCY_RECORDS), _UNIQUE_KEYS):
            raise PersistenceConfigurationError("idempotency_index_is_missing", setting="mongo_ensure_indexes")
        self._index_verified = True
        self._loop = asyncio.get_running_loop()

    def stop(self) -> None:
        """Forget the loop (shutdown): later synchronous calls fail closed instead of submitting to a closed loop."""
        self._loop = None

    # -- the synchronous bridge -----------------------------------------------------------------------------------------------

    def _call(self, coroutine: Coroutine[Any, Any, T]) -> T:
        loop = self._loop
        if loop is None or loop.is_closed():
            coroutine.close()
            raise PersistenceError("idempotency_store_is_not_started", collection=IDEMPOTENCY_RECORDS)
        try:
            running: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            coroutine.close()
            raise PersistenceError("blocking_idempotency_call_on_the_event_loop", collection=IDEMPOTENCY_RECORDS)
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        try:
            return future.result(timeout=self._timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise PersistenceUnavailableError("idempotency_operation_timed_out", collection=IDEMPOTENCY_RECORDS) from None

    # -- helpers --------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _key_hash(idempotency_key: Any) -> str:
        if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > MAX_KEY_LENGTH:
            raise UnsafeQueryError("idempotency_key_is_not_valid")
        return _digest(idempotency_key)

    async def _require_index_once(self) -> None:
        if not self._index_verified:
            if not await unique_index_exists(self._connection.collection(IDEMPOTENCY_RECORDS), _UNIQUE_KEYS):
                raise PersistenceError("idempotency_index_is_missing", collection=IDEMPOTENCY_RECORDS)
            self._index_verified = True

    @staticmethod
    def _parse(document: Dict[str, Any]) -> IdempotencyRecord:
        try:
            return IdempotencyRecord(**json.loads(document["record_json"]))
        except (KeyError, TypeError, ValueError):
            raise PersistenceDataError("idempotency_record_is_malformed", collection=IDEMPOTENCY_RECORDS) from None

    def _document_of(self, record: IdempotencyRecord, key_hash: str) -> Dict[str, Any]:
        try:
            record_json = json.dumps(record.to_dict(), sort_keys=True, allow_nan=False)
            expires_at = datetime.fromtimestamp(record.expires_at_utc, tz=timezone.utc)
            expires_us = int(round(record.expires_at_utc * 1_000_000))
        except (TypeError, ValueError, OverflowError, OSError):
            raise PersistenceDataError("idempotency_record_is_not_storable", collection=IDEMPOTENCY_RECORDS) from None
        return {
            "key_hash": key_hash,
            "record_json": record_json,
            "request_fingerprint": record.request_fingerprint,
            "document_id": record.document_id,
            "created_at_us": self._clock(),
            "expires_at_us": expires_us,
            "expires_at": expires_at,
        }

    # -- asynchronous core ----------------------------------------------------------------------------------------------------

    async def aget_record(self, tenant_id: str, idempotency_key: str) -> Optional[IdempotencyRecord]:
        tenant = validate_tenant(tenant_id)
        await self._require_index_once()
        document = await self._store.find_one(tenant, {"key_hash": self._key_hash(idempotency_key)})
        if document is None:
            return None
        if int(document.get("expires_at_us", 0)) <= self._clock():
            return None                                              # the TTL index removes it later; the answer is "no record" now
        return self._parse(document)

    async def asave_record(self, record: IdempotencyRecord) -> None:
        tenant = validate_tenant(record.tenant_id)
        key_hash = self._key_hash(record.idempotency_key)
        await self._require_index_once()
        document = self._document_of(record, key_hash)
        for _ in range(_SAVE_ROUNDS):
            try:
                await self._store.insert_one(tenant, document)
                return
            except DuplicateKeyPersistenceError:
                pass
            existing = await self._store.find_one(tenant, {"key_hash": key_hash})
            if existing is None:
                continue                                             # removed between the insert and the read: insert again
            fields = {k: v for k, v in document.items() if k != "key_hash"}
            if int(existing.get("expires_at_us", 0)) <= self._clock():
                # An expired record never blocks a new request: replace exactly the version that was read.
                matched = await self._store.update_one(
                    tenant, {"key_hash": key_hash, "expires_at_us": existing["expires_at_us"]}, {"$set": fields}
                )
                if matched:
                    return
                continue
            if existing.get("request_fingerprint") == record.request_fingerprint and existing.get("document_id") == record.document_id:
                # The same request again: refresh the stored record (a replay), nothing else may change.
                matched = await self._store.update_one(
                    tenant,
                    {"key_hash": key_hash, "request_fingerprint": record.request_fingerprint, "document_id": record.document_id},
                    {"$set": fields},
                )
                if matched:
                    return
                continue
            raise ConflictError(message="Idempotency key already recorded for a different request.")
        raise PersistenceUnavailableError("idempotency_save_contended", collection=IDEMPOTENCY_RECORDS)

    async def adelete_record(self, tenant_id: str, idempotency_key: str) -> None:
        tenant = validate_tenant(tenant_id)
        await self._require_index_once()
        await self._store.delete_one(tenant, {"key_hash": self._key_hash(idempotency_key)})

    # -- the IdempotencyStore interface (synchronous) ---------------------------------------------------------------------

    def get_record(self, tenant_id: str, idempotency_key: str) -> Optional[IdempotencyRecord]:
        return self._call(self.aget_record(tenant_id, idempotency_key))

    def save_record(self, record: IdempotencyRecord) -> None:
        self._call(self.asave_record(record))

    def delete_record(self, tenant_id: str, idempotency_key: str) -> None:
        self._call(self.adelete_record(tenant_id, idempotency_key))

    def get_key_lock(self, tenant_id: str, idempotency_key: str) -> Any:
        """
        The coordination lock of one (tenant, key): ``acquire()`` / ``release()`` and a context manager. Hashed per part, so no two
        (tenant, key) pairs can alias. It reduces duplicate work; it is NOT what makes the result correct (see the module docstring).
        """
        tenant = validate_tenant(tenant_id)
        name = f"idem:{_digest(tenant)}:{self._key_hash(idempotency_key)}"
        return self._locks.lock(name)
