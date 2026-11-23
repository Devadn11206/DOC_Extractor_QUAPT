"""
Distributed locks and idempotency state for multi-replica scaling (PRD ARC-003, NFR-006, FR-ING-006).

The local idempotency store and its thread locks only protect one process. With several API or worker replicas,
two replicas can both see "no record yet" and both process the same upload. This module closes that gap with Redis:

- ``RedisLockProvider.lock(name)``: an atomic lease. Acquire is ``SET key token NX PX lease``; release is a
  WATCH-guarded compare-and-delete, so a holder whose lease already expired can never delete a lock now held by
  another replica. A lock is reentrant within one thread, matching the local store's RLock semantics, because the
  ingestion path takes the same key lock again inside ``save_record``.
- ``RedisIdempotencyStore``: the ``IdempotencyStore`` interface on Redis. Records expire with the 24-hour window
  (API-013). Keys hash the tenant and the idempotency key separately, so no two (tenant, key) pairs can alias (SEC-006).

Fail closed: when Redis cannot be reached, or a lock is not acquired within the timeout, the operation raises
(``StorageError`` / ``LockTimeoutError``). It never proceeds without the lock.

Lease caveat: a critical section that runs longer than ``lease_ms`` loses its lock. Choose the lease above the
longest guarded operation; the guarded operations here are short (one record read or write).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from redis.exceptions import RedisError, WatchError

from doc_extractor.exceptions import StorageError
from doc_extractor.idempotency import IdempotencyStore
from doc_extractor.models import IdempotencyRecord
from doc_extractor.tenancy import require_path_safe_tenant_id

logger = logging.getLogger("doc_extractor.distributed_state")

DEFAULT_NAMESPACE = "doc_extractor"
DEFAULT_LEASE_MS = 30_000
DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 10.0
DEFAULT_RETRY_INTERVAL_SECONDS = 0.05


class LockTimeoutError(StorageError):
    """A distributed lock could not be acquired within the timeout. The guarded operation did not run."""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


class _HeldLock:
    __slots__ = ("key", "token", "depth")

    def __init__(self, key: str, token: str) -> None:
        self.key = key
        self.token = token
        self.depth = 1


class _LockContext:
    """Context manager returned by ``RedisLockProvider.lock``; reentrant within the current thread."""

    def __init__(self, provider: "RedisLockProvider", name: str) -> None:
        self._provider = provider
        self._name = name

    def __enter__(self) -> "_LockContext":
        self._provider._enter(self._name)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._provider._exit(self._name)


class RedisLockProvider:
    """Atomic, leased, reentrant-per-thread locks over one Redis deployment."""

    def __init__(
        self,
        client: Any,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        lease_ms: int = DEFAULT_LEASE_MS,
        acquire_timeout_seconds: float = DEFAULT_ACQUIRE_TIMEOUT_SECONDS,
        retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
    ) -> None:
        if lease_ms <= 0:
            raise ValueError("The lock lease must be positive.")
        if acquire_timeout_seconds < 0 or retry_interval_seconds <= 0:
            raise ValueError("Lock timing values must be non-negative (retry interval positive).")
        self._client = client
        self._namespace = namespace
        self._lease_ms = int(lease_ms)
        self._timeout = float(acquire_timeout_seconds)
        self._retry = float(retry_interval_seconds)
        self._local = threading.local()

    def lock(self, name: str) -> _LockContext:
        """A lock for ``name`` (for example ``audit:<tenant>`` or an idempotency key). Use with ``with``."""
        if not name:
            raise ValueError("A lock name is required.")
        return _LockContext(self, name)

    def _held(self) -> Dict[str, _HeldLock]:
        held = getattr(self._local, "held", None)
        if held is None:
            held = {}
            self._local.held = held
        return held

    def _key(self, name: str) -> str:
        return f"{self._namespace}:lock:{_digest(name)}"

    def _enter(self, name: str) -> None:
        held = self._held()
        if name in held:
            held[name].depth += 1
            return
        key = self._key(name)
        token = uuid.uuid4().hex
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                acquired = self._client.set(key, token, nx=True, px=self._lease_ms)
            except RedisError as exc:
                raise StorageError(message="Lock store is unavailable.", details={"error_type": type(exc).__name__}) from exc
            if acquired:
                held[name] = _HeldLock(key, token)
                return
            if time.monotonic() >= deadline:
                raise LockTimeoutError(message="Could not acquire the lock in time.", details={"lock": "held elsewhere"})
            time.sleep(self._retry)

    def _exit(self, name: str) -> None:
        held = self._held()
        entry = held.get(name)
        if entry is None:
            return
        entry.depth -= 1
        if entry.depth > 0:
            return
        del held[name]
        self._release(entry)

    def _release(self, entry: _HeldLock) -> None:
        try:
            with self._client.pipeline() as pipe:
                pipe.watch(entry.key)
                if _as_text(pipe.get(entry.key)) != entry.token:
                    # The lease expired and another holder owns the key now. Deleting it would break their exclusion.
                    pipe.unwatch()
                    logger.warning("Lock lease expired before release; the lock was not deleted.")
                    return
                pipe.multi()
                pipe.delete(entry.key)
                pipe.execute()
        except WatchError:
            logger.warning("Lock changed hands during release; the lock was not deleted.")
        except RedisError as exc:
            # The lease will expire on its own; the next holder waits at most one lease.
            logger.warning("Lock release failed (%s); the lease will expire.", type(exc).__name__)


class RedisIdempotencyStore(IdempotencyStore):
    """
    Idempotency records in Redis, safe across replicas (FR-ING-006, API-013, NFR-006).

    ``get_key_lock`` returns the distributed lock for one (tenant, key) pair, so the ingestion path's
    check-then-create runs under one lock on every replica.
    """

    def __init__(
        self,
        client: Any,
        *,
        locks: Optional[RedisLockProvider] = None,
        namespace: str = DEFAULT_NAMESPACE,
        window_seconds: float = 86400.0,
    ) -> None:
        if window_seconds <= 0:
            raise ValueError("The idempotency window must be positive.")
        self._client = client
        self._namespace = namespace
        self._window = float(window_seconds)
        self._locks = locks or RedisLockProvider(client, namespace=namespace)

    def _key(self, tenant_id: str, idempotency_key: str) -> str:
        require_path_safe_tenant_id(tenant_id)  # refuses unsafe identities with a typed error (SEC-006)
        return f"{self._namespace}:idem:{_digest(tenant_id)}:{_digest(idempotency_key)}"

    def get_key_lock(self, tenant_id: str, idempotency_key: str) -> _LockContext:
        return self._locks.lock(self._key(tenant_id, idempotency_key))

    def get_record(self, tenant_id: str, idempotency_key: str) -> Optional[IdempotencyRecord]:
        key = self._key(tenant_id, idempotency_key)
        try:
            raw = self._client.get(key)
        except RedisError as exc:
            raise StorageError(message="Idempotency store is unavailable.", details={"error_type": type(exc).__name__}) from exc
        if raw is None:
            return None
        try:
            record = IdempotencyRecord(**json.loads(_as_text(raw) or ""))
        except (ValueError, TypeError) as exc:
            raise StorageError(message="Failed to load idempotency record.", details={"tenant_id": tenant_id}) from exc
        if record.is_expired():
            return None
        return record

    def save_record(self, record: IdempotencyRecord) -> None:
        key = self._key(record.tenant_id, record.idempotency_key)
        ttl_seconds = max(1, int(math.ceil(self._window)))
        payload = json.dumps(record.to_dict(), sort_keys=True)
        with self.get_key_lock(record.tenant_id, record.idempotency_key):
            try:
                self._client.set(key, payload, ex=ttl_seconds)
            except RedisError as exc:
                raise StorageError(
                    message="Failed to persist idempotency record.",
                    details={"tenant_id": record.tenant_id, "error_type": type(exc).__name__},
                ) from exc

    def delete_record(self, tenant_id: str, idempotency_key: str) -> None:
        key = self._key(tenant_id, idempotency_key)
        with self.get_key_lock(tenant_id, idempotency_key):
            try:
                self._client.delete(key)
            except RedisError as exc:
                raise StorageError(
                    message="Failed to delete idempotency record.",
                    details={"tenant_id": tenant_id, "error_type": type(exc).__name__},
                ) from exc


def connect_redis(url: str) -> Any:
    """Client for a Redis URL. Timeouts are bounded, so an unreachable store fails closed instead of hanging a request."""
    import redis as redis_lib

    return redis_lib.Redis.from_url(
        url,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
        health_check_interval=30,
        decode_responses=False,
    )


def tenant_lock_factory(provider: RedisLockProvider) -> Callable[[str], _LockContext]:
    """Adapter for ``HashChainedAuditRepository(lock_factory=...)``: one lock per tenant chain across replicas."""
    return lambda tenant_id: provider.lock(f"audit:{tenant_id}")
