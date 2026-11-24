"""
MongoDB ``ProcessingLeaseStore`` (Team Member 3, Phase 3): durable, tenant-isolated job ownership with fencing tokens.

WHY MONGODB, NOT THE BROKER. A broker (RabbitMQ) has no visibility timeout and no lease extension: it knows only "delivered,
unacknowledged" per channel. Which worker may process a job, how many attempts it has used and whether it finished must survive
a broker redelivery, a worker crash and a partitioned worker that keeps running. That state lives here, in one MongoDB record per
tenant and job key (``processing_jobs``), and every change to it is ONE conditional write, so it is atomic across processes.
There is no process-local lock anywhere in the correctness path.

THE RECORD  ``{tenant_id, job_key, state, attempt, max_attempts, fencing_token, lease_owner, lease_expires_at_us, visible_at_us, ...}``
- ``state``: ``queued`` (claimable once ``visible_at_us`` has passed), ``leased`` (one worker owns it until ``lease_expires_at_us``),
  ``completed`` and ``dead_lettered`` (both final).
- ``fencing_token`` grows by one on every claim. A write that changes a leased job names the token it holds and matches only while
  the record still carries it, so a worker whose lease was taken over cannot complete, release, extend or dead-letter the job.
- ``attempt`` is counted when a claim starts. A job whose attempts are used up is dead-lettered by the next claim instead of being
  run again, so a job that crashes its workers cannot be recovered for ever.

CLAIM RACES. The first claim inserts the record; the unique ``(tenant_id, job_key)`` index lets exactly one of several concurrent
workers do it, and the others read the winner's record and report ``HELD``. A claim of an existing record is one
``find_one_and_update`` whose filter says "claimable now" (queued and due, or leased and expired): exactly one worker matches it.
A bounded number of rounds absorbs the races between those steps; running out fails closed with a typed, retryable error.

TIME decides only WHEN a lease can be taken over (the taker's clock against the stored expiry); it never decides who may WRITE
(the token does). A taker whose clock runs fast can take a healthy lease early: the previous owner is then fenced out, which is
wasteful but safe. Keep hosts on NTP and choose leases far above the clock skew.

RETENTION. A completed record is kept for ``retention_seconds`` (default 14 days) so that a late duplicate delivery is still
recognised and dropped, and then removed by the TTL index on ``expires_at``. A dead-lettered record is kept until an operator
removes it. Readers never rely on the TTL: it runs about once a minute.

NOT HERE: a sweeper that finds expired leases of ALL tenants and republishes them. The repository base is tenant-scoped on purpose
and this store does not add a second cross-tenant path. Recovery of a dead worker is driven by the broker's redelivery of its
unacknowledged message, which the claim then arbitrates (see ``infrastructure/queue/rabbitmq.py``).
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from doc_extractor.api.ports.processing import ClaimResult, ClaimStatus, JobLease, LeaseLostError
from doc_extractor.infrastructure.mongo.connection import MongoConnection
from doc_extractor.infrastructure.mongo.errors import (
    DuplicateKeyPersistenceError,
    PersistenceConfigurationError,
    PersistenceDataError,
    PersistenceError,
    PersistenceUnavailableError,
    UnsafeQueryError,
)
from doc_extractor.infrastructure.mongo.indexes import PROCESSING_JOBS, unique_index_exists
from doc_extractor.infrastructure.mongo.repository import TenantScopedRepository, validate_tenant

STATE_QUEUED = "queued"
STATE_LEASED = "leased"
STATE_COMPLETED = "completed"
STATE_DEAD_LETTERED = "dead_lettered"

DEFAULT_RETENTION_SECONDS = 14 * 24 * 3600
MAX_LEASE_SECONDS = 24 * 3600
MAX_ATTEMPTS_LIMIT = 100
DEFAULT_CLAIM_ROUNDS = 8
MAX_IDENTIFIER_LENGTH = 256

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_UNIQUE_KEYS = [("tenant_id", 1), ("job_key", 1)]
_CODE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_REQUIRED = ("job_key", "state", "attempt", "max_attempts", "fencing_token")


def _now_us() -> int:
    return time.time_ns() // 1000


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _identifier(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_IDENTIFIER_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise UnsafeQueryError(reason)
    return value


def _to_datetime(microseconds: int) -> datetime:
    return _EPOCH + timedelta(microseconds=microseconds)


class MongoProcessingLeaseStore:
    """The ``ProcessingLeaseStore`` port over MongoDB. See the module docstring."""

    def __init__(
        self,
        connection: MongoConnection,
        *,
        clock_us: Callable[[], int] = _now_us,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        claim_rounds: int = DEFAULT_CLAIM_ROUNDS,
    ) -> None:
        if not _is_int(retention_seconds) or not 60 <= retention_seconds <= 366 * 24 * 3600:
            raise ValueError("retention_seconds must be between one minute and one year.")
        if not _is_int(claim_rounds) or not 1 <= claim_rounds <= 100:
            raise ValueError("claim_rounds must be between 1 and 100.")
        self._connection = connection
        self._store = TenantScopedRepository(connection, PROCESSING_JOBS)
        self._clock = clock_us
        self._retention_us = retention_seconds * 1_000_000
        self._rounds = claim_rounds
        self._index_verified = False

    @property
    def collection_name(self) -> str:
        return PROCESSING_JOBS

    # -- the unique index is what makes the first claim race-free: it must exist ---------------------------------------------

    async def require_unique_job_index(self) -> None:
        """Start-up gate: ``PersistenceConfigurationError`` unless the unique ``(tenant_id, job_key)`` index exists."""
        if not await unique_index_exists(self._connection.collection(PROCESSING_JOBS), _UNIQUE_KEYS):
            raise PersistenceConfigurationError("processing_job_index_is_missing", setting="mongo_ensure_indexes")
        self._index_verified = True

    async def _require_index_once(self) -> None:
        if not self._index_verified:
            if not await unique_index_exists(self._connection.collection(PROCESSING_JOBS), _UNIQUE_KEYS):
                raise PersistenceError("processing_job_index_is_missing", collection=PROCESSING_JOBS)
            self._index_verified = True

    # -- helpers --------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _lease_of(tenant_id: str, document: Dict[str, Any]) -> JobLease:
        try:
            return JobLease(
                tenant_id=tenant_id,
                job_key=document["job_key"],
                fencing_token=int(document["fencing_token"]),
                attempt=int(document["attempt"]),
                max_attempts=int(document["max_attempts"]),
                worker_id=str(document["lease_owner"]),
                expires_at=_to_datetime(int(document["lease_expires_at_us"])),
            )
        except (KeyError, TypeError, ValueError):
            raise PersistenceDataError("processing_record_is_malformed", collection=PROCESSING_JOBS) from None

    @staticmethod
    def _check(document: Dict[str, Any]) -> None:
        if any(field not in document for field in _REQUIRED) or not all(
            _is_int(document[field]) for field in ("attempt", "max_attempts", "fencing_token")
        ):
            raise PersistenceDataError("processing_record_is_malformed", collection=PROCESSING_JOBS)

    def _owned(self, lease: JobLease) -> Dict[str, Any]:
        """The filter that matches the job only while ``lease`` still owns it."""
        return {"job_key": lease.job_key, "state": STATE_LEASED, "fencing_token": lease.fencing_token, "lease_owner": lease.worker_id}

    async def _settled_by(self, lease: JobLease, state: str) -> None:
        """A repeat of a settling call by the SAME lease succeeds; anything else means ownership was lost."""
        document = await self._store.find_one(lease.tenant_id, {"job_key": lease.job_key})
        if document is not None and document.get("state") == state and document.get("fencing_token") == lease.fencing_token:
            return
        raise LeaseLostError("lease_is_no_longer_held")

    @staticmethod
    def _code(value: Optional[str]) -> str:
        return value if isinstance(value, str) and _CODE.fullmatch(value) else "unspecified"

    # -- the port -------------------------------------------------------------------------------------------------------------

    async def claim(
        self,
        tenant_id: str,
        job_key: str,
        *,
        message_id: str,
        document_id: str,
        max_attempts: int,
        worker_id: str,
        lease_seconds: int,
    ) -> ClaimResult:
        tenant = validate_tenant(tenant_id)
        key = _identifier(job_key, "job_key_is_not_valid")
        message = _identifier(message_id, "message_id_is_not_valid")
        document = _identifier(document_id, "document_id_is_not_valid")
        worker = _identifier(worker_id, "worker_id_is_not_valid")
        if not _is_int(max_attempts) or not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
            raise UnsafeQueryError("max_attempts_is_not_valid")
        if not _is_int(lease_seconds) or not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
            raise UnsafeQueryError("lease_seconds_is_not_valid")
        await self._require_index_once()

        for _ in range(self._rounds):
            now = self._clock()
            expires = now + lease_seconds * 1_000_000
            claimable = {
                "job_key": key,
                "attempt": {"$lt": max_attempts},
                "$or": [
                    {"state": STATE_QUEUED, "visible_at_us": {"$lte": now}},
                    {"state": STATE_LEASED, "lease_expires_at_us": {"$lte": now}},
                ],
            }
            taken = await self._store.find_one_and_update(
                tenant,
                claimable,
                {
                    "$set": {"state": STATE_LEASED, "lease_owner": worker, "lease_expires_at_us": expires, "updated_at_us": now},
                    "$inc": {"fencing_token": 1, "attempt": 1},
                },
                return_after=True,
            )
            if taken is not None:
                self._check(taken)
                return ClaimResult(ClaimStatus.ACQUIRED, self._lease_of(tenant, taken), attempt=int(taken["attempt"]))

            current = await self._store.find_one(tenant, {"job_key": key})
            if current is None:
                try:
                    await self._store.insert_one(
                        tenant,
                        {
                            "job_key": key,
                            "message_id": message,
                            "document_id": document,
                            "state": STATE_LEASED,
                            "attempt": 1,
                            "max_attempts": max_attempts,
                            "fencing_token": 1,
                            "lease_owner": worker,
                            "lease_expires_at_us": expires,
                            "visible_at_us": now,
                            "created_at_us": now,
                            "updated_at_us": now,
                        },
                    )
                except DuplicateKeyPersistenceError:
                    continue                                     # another worker created it first: read its record next round
                lease = JobLease(tenant, key, 1, 1, max_attempts, worker, _to_datetime(expires))
                return ClaimResult(ClaimStatus.ACQUIRED, lease, attempt=1)

            self._check(current)
            state, attempt = current["state"], int(current["attempt"])
            if state == STATE_COMPLETED:
                return ClaimResult(ClaimStatus.COMPLETED, attempt=attempt)
            if state == STATE_DEAD_LETTERED:
                return ClaimResult(ClaimStatus.DEAD_LETTERED, attempt=attempt)
            if state == STATE_LEASED and int(current.get("lease_expires_at_us", 0)) > now:
                return ClaimResult(ClaimStatus.HELD, retry_after_seconds=(int(current["lease_expires_at_us"]) - now) / 1e6, attempt=attempt)
            if state == STATE_QUEUED and int(current.get("visible_at_us", 0)) > now and attempt < max_attempts:
                return ClaimResult(ClaimStatus.NOT_YET_VISIBLE, retry_after_seconds=(int(current["visible_at_us"]) - now) / 1e6, attempt=attempt)
            if state in (STATE_QUEUED, STATE_LEASED) and attempt >= max_attempts:
                # The attempts are used up and nobody holds a live lease: give up on the job, once, atomically.
                matched = await self._store.update_one(
                    tenant,
                    {"job_key": key, "state": state, "fencing_token": current["fencing_token"], "attempt": {"$gte": max_attempts}},
                    {
                        "$set": {"state": STATE_DEAD_LETTERED, "dead_letter_reason": "attempts_exhausted", "updated_at_us": now},
                        "$unset": {"lease_owner": "", "lease_expires_at_us": ""},
                    },
                )
                if matched:
                    return ClaimResult(ClaimStatus.EXHAUSTED, attempt=attempt)
            # claimable by now (another worker changed the record between our two reads): run the round again
        raise PersistenceUnavailableError("processing_claim_contended", collection=PROCESSING_JOBS)

    async def extend(self, lease: JobLease, extension_seconds: int) -> JobLease:
        if not _is_int(extension_seconds) or not 1 <= extension_seconds <= MAX_LEASE_SECONDS:
            raise UnsafeQueryError("extension_seconds_is_not_valid")
        now = self._clock()
        expires = max(now + extension_seconds * 1_000_000, (lease.expires_at - _EPOCH) // timedelta(microseconds=1))
        matched = await self._store.update_one(
            lease.tenant_id, self._owned(lease), {"$set": {"lease_expires_at_us": expires, "updated_at_us": now}}
        )
        if not matched:
            raise LeaseLostError("lease_is_no_longer_held")
        return replace(lease, expires_at=_to_datetime(expires))

    async def verify(self, lease: JobLease) -> None:
        if await self._store.find_one(lease.tenant_id, self._owned(lease)) is None:
            raise LeaseLostError("lease_is_no_longer_held")

    async def mark_completed(self, lease: JobLease) -> None:
        now = self._clock()
        matched = await self._store.update_one(
            lease.tenant_id,
            self._owned(lease),
            {
                "$set": {
                    "state": STATE_COMPLETED,
                    "completed_at_us": now,
                    "updated_at_us": now,
                    "expires_at": _to_datetime(now + self._retention_us),
                },
                "$unset": {"lease_owner": "", "lease_expires_at_us": ""},
            },
        )
        if not matched:
            await self._settled_by(lease, STATE_COMPLETED)

    async def release_for_retry(
        self,
        lease: JobLease,
        *,
        delay_seconds: float = 0,
        error_code: Optional[str] = None,
        consume_attempt: bool = True,
    ) -> None:
        if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, (int, float)) or not 0 <= delay_seconds <= 7 * 24 * 3600:
            raise UnsafeQueryError("delay_seconds_is_not_valid")
        now = self._clock()
        update: Dict[str, Any] = {
            "$set": {
                "state": STATE_QUEUED,
                "visible_at_us": now + int(delay_seconds * 1_000_000),
                "last_error_code": self._code(error_code),
                "updated_at_us": now,
            },
            "$unset": {"lease_owner": "", "lease_expires_at_us": ""},
        }
        if not consume_attempt:
            update["$inc"] = {"attempt": -1}
        matched = await self._store.update_one(lease.tenant_id, self._owned(lease), update)
        if not matched:
            await self._settled_by(lease, STATE_QUEUED)

    async def dead_letter(self, lease: JobLease, *, reason: str) -> None:
        now = self._clock()
        matched = await self._store.update_one(
            lease.tenant_id,
            self._owned(lease),
            {
                "$set": {"state": STATE_DEAD_LETTERED, "dead_letter_reason": self._code(reason), "updated_at_us": now},
                "$unset": {"lease_owner": "", "lease_expires_at_us": ""},
            },
        )
        if not matched:
            await self._settled_by(lease, STATE_DEAD_LETTERED)

    async def snapshot(self, tenant_id: str, job_key: str) -> Optional[Dict[str, Any]]:
        tenant = validate_tenant(tenant_id)
        document = await self._store.find_one(tenant, {"job_key": _identifier(job_key, "job_key_is_not_valid")})
        if document is None:
            return None
        keep = (
            "job_key", "state", "attempt", "max_attempts", "fencing_token", "lease_owner", "lease_expires_at_us",
            "visible_at_us", "last_error_code", "dead_letter_reason", "completed_at_us",
        )
        return {field: document[field] for field in keep if field in document}
