"""
Durable processing ownership: the lease port (Team Member 3, Phase 3).

A broker can deliver a job message more than once and cannot, on its own, say which consumer still owns the job. The
``ProcessingLeaseStore`` is the AUTHORITATIVE, durable record of that ownership. It is the only place that decides who may
process a job, how many attempts it has used and whether it is finished, whatever the transport (RabbitMQ, a test double,
a later broker) delivers.

THE MODEL (one record per tenant and job key)
- ``claim`` is one atomic transition across all processes. It either starts a new lease (and returns a ``JobLease``) or says
  why not (already completed, dead-lettered, held by a live lease, not yet visible, attempts exhausted).
- Every lease carries a FENCING TOKEN: an integer that grows by one on every successful claim of the job. ``extend``,
  ``mark_completed``, ``release_for_retry``, ``dead_letter`` and ``verify`` succeed only while the record still carries the caller's
  token in the ``leased`` state. A worker whose lease expired and was taken over (stale worker) therefore cannot change the
  job any more: it gets ``LeaseLostError``. Time only decides WHEN a lease may be taken over; the token decides who may write.
  Downstream side effects of a job should carry the token (``JobLease.fencing_token``) so that they can be fenced too.
- Attempts are counted at claim time, so a job that keeps killing its worker cannot be taken over for ever: once the attempts
  are used up the next takeover dead-letters it (``ClaimStatus.EXHAUSTED``) instead of running it again.

TENANTS. Every operation takes the tenant and an adapter must keep the tenants apart (SEC-006); one tenant's job key never
meets another tenant's.

Nothing here names a broker or a database. Implementations: ``infrastructure.mongo.processing.MongoProcessingLeaseStore``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from doc_extractor.exceptions import StorageError


class LeaseLostError(StorageError):
    """
    The caller no longer owns the job: its lease was taken over, the job was already settled differently, or the record is
    gone. The caller must stop working on the job and discard its result. The message is fixed; no identifier is echoed.
    """

    def __init__(self, reason: str = "lease_is_no_longer_held") -> None:
        super().__init__(
            message="The processing lease is no longer held.",
            code="LEASE_LOST",
            details={"reason": reason},
        )


class ClaimStatus(str, Enum):
    ACQUIRED = "acquired"                  # the caller owns the job now (``ClaimResult.lease`` is set)
    COMPLETED = "completed"                # already processed successfully: drop the delivery
    DEAD_LETTERED = "dead_lettered"        # already given up on: drop the delivery
    HELD = "held"                          # another live lease owns it: retry after ``retry_after_seconds``
    NOT_YET_VISIBLE = "not_yet_visible"    # a scheduled retry is not due yet: retry after ``retry_after_seconds``
    EXHAUSTED = "exhausted"                # the attempts were used up; the job has just been dead-lettered by this claim


@dataclass(frozen=True)
class JobLease:
    """One worker's ownership of one job. Immutable; ``extend`` returns a new one."""

    tenant_id: str
    job_key: str
    fencing_token: int
    attempt: int                           # 1 for the first claim, 2 for the second, ...
    max_attempts: int
    worker_id: str
    expires_at: datetime                   # UTC; the lease may be taken over after this instant


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    lease: Optional[JobLease] = None
    retry_after_seconds: Optional[float] = None
    attempt: int = 0                       # the job's attempt count at the time of the answer


@runtime_checkable
class ProcessingLeaseStore(Protocol):
    """Contract of the durable processing-ownership record. See the module docstring."""

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
        """Atomically start a lease on the job, or say why it cannot be started. Creates the record on the first claim."""
        ...

    async def extend(self, lease: JobLease, extension_seconds: int) -> JobLease:
        """Move the lease expiry to ``extension_seconds`` from now (never earlier). ``LeaseLostError`` unless still owned."""
        ...

    async def verify(self, lease: JobLease) -> None:
        """Raise ``LeaseLostError`` unless ``lease`` still owns the job. A fencing check for side effects."""
        ...

    async def mark_completed(self, lease: JobLease) -> None:
        """Mark the job completed. Repeating it for the same lease is harmless. ``LeaseLostError`` unless owned."""
        ...

    async def release_for_retry(
        self,
        lease: JobLease,
        *,
        delay_seconds: float = 0,
        error_code: Optional[str] = None,
        consume_attempt: bool = True,
    ) -> None:
        """Return the job to the queue state, claimable again after ``delay_seconds``. ``LeaseLostError`` unless owned."""
        ...

    async def dead_letter(self, lease: JobLease, *, reason: str) -> None:
        """Give up on the job for good. Repeating it for the same lease is harmless. ``LeaseLostError`` unless owned."""
        ...

    async def snapshot(self, tenant_id: str, job_key: str) -> Optional[Dict[str, Any]]:
        """Operational view of the record (state, attempt, token, expiry), or ``None``. Never a message payload."""
        ...
