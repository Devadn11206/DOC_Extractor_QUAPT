"""
Processing worker runtime (Team Member 3, Phase 3): receive a job, keep its lease alive while a handler runs, settle it.

This is the loop around a HANDLER (``async (message, context) -> None``): the extraction pipeline is the handler and is not part of this
module. The worker adds what a long job needs and a bare queue does not give:

- HEARTBEAT. While the handler runs, the lease is extended every ``heartbeat_seconds`` (default one third of the lease). A GPU job can
  therefore outlast its initial lease. If an extension reports that the lease is LOST (another worker took the job over), or the lease
  store has been unreachable for a whole lease, the handler is CANCELLED and its result is discarded: after that point this worker can
  no longer prove it owns the job, and every write it could make is fenced anyway.
- FENCING FOR SIDE EFFECTS. The handler receives a ``LeaseContext``: its ``fencing_token`` for downstream writes, ``lost`` (set when
  ownership is lost) and ``ensure_held()`` (a check against the lease store before an irreversible step).
- SETTLEMENT. Success acknowledges. ``PermanentProcessingError`` dead-letters at once. Any other exception requeues with exponential
  back-off (the queue dead-letters after ``max_retries``). A lost lease settles nothing: the job belongs to someone else.
- OPTIONAL DOCUMENT LOCK. With a Redis lock provider, the worker holds a lock on the document while the handler runs, renewed on the
  heartbeat. Contention defers the job WITHOUT using an attempt. The lock is coordination only: when it cannot be renewed the work stops
  like a lost lease, and correctness is still the lease store's fencing token, never the lock.
- SHUTDOWN. ``run(stop)`` stops receiving when ``stop`` is set, waits up to ``shutdown_grace_seconds`` for running handlers, then cancels
  the rest and gives their jobs back WITHOUT using an attempt (``defer``), so a deployment does not eat into ``max_retries``.
- FAILURE. A failing queue or lease store is logged (fixed reason, error type only) and retried after a pause; a failing settlement is
  retried a few times and then left to the lease expiry and the broker's redelivery. Nothing here logs a message payload.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Set

from doc_extractor.api.contracts.records import DocumentProcessingMessage, QueueEnvelope
from doc_extractor.api.ports.processing import LeaseLostError
from doc_extractor.exceptions import StorageError
from doc_extractor.infrastructure.queue.errors import ReceiptHandleError

logger = logging.getLogger("doc_extractor.queue")

DEFAULT_LEASE_SECONDS = 300
SETTLE_ATTEMPTS = 3


class PermanentProcessingError(Exception):
    """Raised by a handler for a failure that retrying cannot fix: the job is dead-lettered at once."""


@dataclass
class LeaseContext:
    """What a handler gets besides its message. ``lost`` is set when ownership of the job is lost; the handler is cancelled then."""

    tenant_id: str
    document_id: str
    fencing_token: int
    attempt: int
    lost: asyncio.Event
    _verify: Callable[[], Awaitable[None]]

    async def ensure_held(self) -> None:
        """Raise ``LeaseLostError`` unless this worker still owns the job (check before an irreversible side effect)."""
        if self.lost.is_set():
            raise LeaseLostError("lease_is_no_longer_held")
        await self._verify()


Handler = Callable[[DocumentProcessingMessage, LeaseContext], Awaitable[None]]


class ProcessingWorker:
    """See the module docstring. ``consumer`` is a ``RabbitMQQueue`` (or anything with its consumer methods)."""

    def __init__(
        self,
        consumer: Any,
        handler: Handler,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: Optional[float] = None,
        batch_size: int = 1,
        max_concurrency: int = 1,
        poll_interval_seconds: float = 1.0,
        error_pause_seconds: float = 5.0,
        backoff_base_seconds: float = 2.0,
        backoff_cap_seconds: float = 300.0,
        shutdown_grace_seconds: float = 30.0,
        document_locks: Optional[Any] = None,
        lock_timeout_seconds: float = 1.0,
        lock_retry_seconds: float = 15.0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if not 5 <= lease_seconds <= 24 * 3600:
            raise ValueError("lease_seconds must be between 5 seconds and 24 hours.")
        heartbeat = float(heartbeat_seconds) if heartbeat_seconds is not None else lease_seconds / 3
        if not 0 < heartbeat < lease_seconds:
            raise ValueError("heartbeat_seconds must be positive and shorter than the lease.")
        if not 1 <= batch_size <= 100 or not 1 <= max_concurrency <= 100:
            raise ValueError("batch_size and max_concurrency must be between 1 and 100.")
        self._consumer = consumer
        self._handler = handler
        self._lease_seconds = int(lease_seconds)
        self._heartbeat = heartbeat
        self._batch_size = batch_size
        self._max_concurrency = max_concurrency
        self._poll = poll_interval_seconds
        self._error_pause = error_pause_seconds
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._grace = shutdown_grace_seconds
        self._locks = document_locks
        self._lock_timeout = lock_timeout_seconds
        self._lock_retry = lock_retry_seconds
        self._sleep = sleep
        self.stats = {"processed": 0, "failed": 0, "dead_lettered": 0, "lost": 0, "deferred": 0, "settle_failures": 0}

    # -- one batch --------------------------------------------------------------------------------------------------------

    async def run_once(self) -> int:
        """Receive one batch and process it (concurrently, up to ``max_concurrency``). Returns how many jobs were handled."""
        envelopes = await self._consumer.receive_messages(min(self._batch_size, self._max_concurrency), self._lease_seconds)
        if envelopes:
            await asyncio.gather(*(self._process(envelope) for envelope in envelopes))
        return len(envelopes)

    # -- the loop ---------------------------------------------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        active: Set["asyncio.Task[None]"] = set()
        try:
            while not stop.is_set():
                capacity = self._max_concurrency - len(active)
                received = 0
                if capacity > 0:
                    try:
                        envelopes = await self._consumer.receive_messages(min(self._batch_size, capacity), self._lease_seconds)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:                                # noqa: BLE001 - the loop must survive a failing dependency
                        logger.error("Receiving failed (error_type=%s); pausing", type(exc).__name__)
                        await self._pause(stop, self._error_pause)
                        continue
                    for envelope in envelopes:
                        active.add(asyncio.ensure_future(self._process(envelope)))
                    received = len(envelopes)
                if active:
                    done, _ = await asyncio.wait(active, timeout=self._poll if not received else 0.0, return_when=asyncio.FIRST_COMPLETED)
                    active -= done
                    for finished in done:
                        if not finished.cancelled() and finished.exception() is not None:
                            logger.error("A job task failed unexpectedly (error_type=%s)", type(finished.exception()).__name__)
                elif not received:
                    await self._pause(stop, self._poll)
        finally:
            await self._drain(active)

    @staticmethod
    async def _pause(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _drain(self, active: Set["asyncio.Task[None]"]) -> None:
        if not active:
            return
        _, pending = await asyncio.wait(active, timeout=self._grace)
        for task in pending:
            task.cancel()                                                    # _process gives the job back without using an attempt
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    # -- one job ----------------------------------------------------------------------------------------------------------

    def _backoff(self, attempt: int) -> int:
        return int(min(self._backoff_cap, self._backoff_base * (2 ** max(attempt - 1, 0))))

    def _lock_name(self, message: DocumentProcessingMessage) -> str:
        tenant = hashlib.sha256(message.tenant_id.encode("utf-8")).hexdigest()[:32]
        document = hashlib.sha256(message.document_id.encode("utf-8")).hexdigest()[:32]
        return f"processing:{tenant}:{document}"

    async def _process(self, envelope: QueueEnvelope) -> None:
        handle, message = envelope.receipt_handle, envelope.message
        lock = None
        if self._locks is not None:
            try:
                lock = await asyncio.to_thread(
                    self._locks.acquire_lease, self._lock_name(message),
                    lease_ms=self._lease_seconds * 1000, timeout_seconds=self._lock_timeout,
                )
            except Exception as exc:                                         # noqa: BLE001 - contention or an outage: give the job back
                logger.warning("The document lock was not obtained (error_type=%s); deferring the job", type(exc).__name__)
                await self._give_back(handle, self._lock_retry)
                return

        lost = asyncio.Event()
        context = LeaseContext(
            tenant_id=message.tenant_id,
            document_id=message.document_id,
            fencing_token=self._consumer.fencing_token(handle),
            attempt=envelope.approximate_receive_count,
            lost=lost,
            _verify=lambda: self._consumer.verify_lease(handle),
        )
        task = asyncio.ensure_future(self._handler(message, context))
        beat = asyncio.ensure_future(self._heartbeat_loop(handle, lock, lost, task))
        outcome = "ok"
        error: Optional[BaseException] = None
        try:
            try:
                await task
            except asyncio.CancelledError:
                if not lost.is_set():
                    task.cancel()
                    await self._abandon(handle)
                    raise
                outcome = "lost"
            except PermanentProcessingError as exc:
                outcome, error = "permanent", exc
            except Exception as exc:                                         # noqa: BLE001 - any handler failure is a retryable failure
                outcome, error = "error", exc
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            if lock is not None:
                await asyncio.to_thread(lock.release)

        if lost.is_set():
            outcome = "lost"
        if outcome == "lost":
            self.stats["lost"] += 1
            logger.warning("Ownership of a job was lost; its result is discarded")
        elif outcome == "ok":
            if await self._settle(lambda: self._consumer.ack(handle)):
                self.stats["processed"] += 1
        elif outcome == "permanent":
            logger.error("A job failed permanently (error_type=%s)", type(error).__name__)
            if await self._settle(lambda: self._consumer.nack(handle, requeue=False)):
                self.stats["dead_lettered"] += 1
        else:
            logger.warning("A job failed (error_type=%s); it will be retried", type(error).__name__)
            if await self._settle(lambda: self._consumer.nack(handle, requeue=True, backoff_seconds=self._backoff(envelope.approximate_receive_count))):
                self.stats["failed"] += 1

    async def _heartbeat_loop(self, handle: str, lock: Any, lost: asyncio.Event, task: "asyncio.Future[None]") -> None:
        """Extend the lease (and the lock) until cancelled. Loss of ownership, or a lease's worth of failed extensions, stops the handler."""
        silent_for = 0.0
        while True:
            await self._sleep(self._heartbeat)
            try:
                await self._consumer.extend_lease(handle, self._lease_seconds)
                if lock is not None and not await asyncio.to_thread(lock.extend, self._lease_seconds * 1000):
                    raise LeaseLostError("document_lock_was_lost")
                silent_for = 0.0
            except (LeaseLostError, ReceiptHandleError):
                lost.set()
                task.cancel()
                return
            except StorageError as exc:
                silent_for += self._heartbeat
                logger.warning("A lease extension failed (error_type=%s)", type(exc).__name__)
                if silent_for >= self._lease_seconds:                        # nothing proves ownership any more: stop, fail closed
                    lost.set()
                    task.cancel()
                    return

    async def _settle(self, call: Callable[[], Awaitable[None]]) -> bool:
        """Settle a job with a few bounded retries on a failing store. A lost lease or a spent handle is final; so is giving up."""
        for attempt in range(SETTLE_ATTEMPTS):
            try:
                await call()
                return True
            except (LeaseLostError, ReceiptHandleError):
                self.stats["lost"] += 1
                return False
            except StorageError as exc:
                logger.warning("Settling a job failed (error_type=%s, attempt=%d)", type(exc).__name__, attempt + 1)
                await self._sleep(min(self._error_pause, 0.1 * (attempt + 1)) + random.random() * 0.01)
        self.stats["settle_failures"] += 1
        logger.error("A job could not be settled; its lease will expire and the broker will redeliver it")
        return False

    async def _give_back(self, handle: str, delay_seconds: float) -> None:
        try:
            await self._consumer.defer(handle, delay_seconds)
            self.stats["deferred"] += 1
        except (LeaseLostError, ReceiptHandleError):
            pass
        except StorageError as exc:
            logger.warning("A job could not be deferred (error_type=%s); its lease will expire", type(exc).__name__)

    async def _abandon(self, handle: str) -> None:
        """The worker is being cancelled: give the job back without using an attempt (best effort, bounded)."""
        try:
            await asyncio.wait_for(self._give_back(handle, 0), timeout=5.0)
        except (asyncio.TimeoutError, Exception):                            # noqa: BLE001 - shutting down: the lease expiry is the backstop
            pass
