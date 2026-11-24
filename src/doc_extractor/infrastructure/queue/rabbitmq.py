"""
Durable queue adapter (Team Member 3, Phase 3): ``QueueProducer`` and ``QueueConsumer`` over a broker transport and the lease store.

TWO STORES, ONE RULE. The BROKER carries the messages (durable, replicated, redelivered when a consumer dies). The LEASE STORE
(``api/ports/processing.py``, MongoDB) is the authority on every job: who owns it, which attempt it is on, whether it finished. A
delivery is only a request to look at the job; the claim in the lease store decides whether to work on it. So:
- a duplicate or redelivered message of a COMPLETED or DEAD-LETTERED job is acknowledged and dropped;
- a message of a job another worker holds under a live lease is re-queued with a delay, without using up an attempt;
- a message whose lease has EXPIRED is taken over with the next fencing token, and the previous worker (if it is still running) is
  fenced out of every later write (``LeaseLostError``);
- a job whose attempts are used up is dead-lettered instead of being run again.
This is why the contract can be kept although RabbitMQ has no visibility timeout and no lease extension: ``receive_messages`` claims a
lease for ``visibility_timeout_seconds``, ``extend_lease`` moves that lease (in MongoDB), and expiry plus redelivery plus the token give
the effect of a visibility timeout.

THE CONTRACT (``api/ports/queue.py``) IS UNCHANGED. ``QueueEnvelope.receipt_handle`` is an opaque random token that maps, in THIS
consumer instance, to one broker delivery and one lease. It is only meaningful here (a broker delivery tag is channel-scoped anyway);
correctness never depends on it: ownership is the fencing token in the database. ``approximate_receive_count`` is the job's attempt
count from the lease store and ``visible_until`` is the lease expiry. ``publish`` returns the message id as the tracking id.
``publish_batch`` is sequential and NOT atomic: on a failure ``BrokerPublishError.published`` lists what was already confirmed, and
republishing the whole batch is safe because jobs are de-duplicated by key. Extra methods beyond the contract: ``fencing_token`` and ``verify_lease`` (fencing for a job's side effects), ``defer`` (give a job
back without using an attempt, for a graceful shutdown or lock contention), ``start``, ``close`` and ``health``.

JOB IDENTITY: ``deduplication_id`` when the message has one, otherwise ``message_id``, within the tenant. Two messages of one tenant with
the same key are ONE job; the same key in two tenants is two jobs.

ORDER OF EFFECTS (every step is safe to repeat; a crash between two steps is repaired by the broker's redelivery plus the lease store):
- ack: complete the job in the lease store, THEN acknowledge the delivery. A failed acknowledgement after the durable completion is
  logged (the delivery will return and be dropped as completed), not raised.
- retry: release the job in the lease store with its delay, THEN publish the delayed copy (confirmed), THEN acknowledge the original.
- dead letter: verify the lease, publish to the dead-letter queue (confirmed), THEN mark the job dead-lettered, THEN acknowledge. A
  crash after the publish can leave a dead-letter copy of a job that is later retried: that is the at-least-once side of the choice
  (the alternative loses the operator's only record).

ATTEMPTS. ``max_retries`` of the message means ``max_retries + 1`` attempts in total (GR-018: retry up to 3 times, then fail). An attempt is
counted when a lease is claimed, so a worker that dies mid-job also uses one.

TENANTS. A message must carry the tenant in a header AND in its body, and the two must agree; a message that does not is dead-lettered with
a fixed reason and never processed. All state (leases, idempotency) is tenant-scoped. This adapter is the SHARED-QUEUE baseline: all
tenants share one queue and isolation is enforced by validation and by tenant-scoped state, not by the broker. Whether to use per-tenant
queues or virtual hosts instead is NOT decided here (the register's M5-D01 is a different decision, error-code names; no queue-isolation
decision is recorded in it) and is recorded as an open architectural dependency.

POISON MESSAGES (unparseable, oversized, wrong tenant header, unusable job key) are published to the dead-letter queue with a fixed reason
header and then acknowledged. Nothing logs a message body.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from doc_extractor.api.contracts.records import DocumentProcessingMessage, QueueEnvelope
from doc_extractor.api.ports.processing import ClaimStatus, JobLease, LeaseLostError, ProcessingLeaseStore
from doc_extractor.infrastructure.queue.errors import (
    BrokerPublishError,
    BrokerUnavailableError,
    QueueError,
    QueueUsageError,
    ReceiptHandleError,
)
from doc_extractor.infrastructure.queue.transport import MAX_BODY_BYTES, BrokerTransport, Delivery

logger = logging.getLogger("doc_extractor.queue")

HEADER_TENANT = "tenant_id"
HEADER_NOT_BEFORE = "x-not-before-us"
HEADER_SCHEMA = "x-schema-version"
HEADER_REASON = "x-dead-letter-reason"
SCHEMA_VERSION = 1
MAX_DELAY_SECONDS = 7 * 24 * 3600
MAX_LEASE_SECONDS = 24 * 3600
MAX_JOB_KEY_LENGTH = 256


def _now_us() -> int:
    return time.time_ns() // 1000


@dataclass
class _InFlight:
    delivery: Delivery
    lease: JobLease
    message: DocumentProcessingMessage
    body: bytes


class _Poison(Exception):
    """A delivery that can never be processed. ``reason`` is a fixed code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def job_key_of(message: DocumentProcessingMessage) -> str:
    """The job's identity within its tenant: the deduplication id when present, else the message id."""
    return message.deduplication_id or message.message_id


def _valid_job_key(key: Any) -> bool:
    return isinstance(key, str) and 0 < len(key) <= MAX_JOB_KEY_LENGTH and not any(ord(c) < 32 or ord(c) == 127 for c in key)


def encode_message(message: DocumentProcessingMessage) -> bytes:
    """The wire body: the message as canonical JSON (identifiers and options only; the message model bounds its payload)."""
    body = json.dumps(message.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise QueueUsageError("message_body_is_too_large")
    return body


def _seconds(value: Any, reason: str, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or not 0 <= value <= maximum:
        raise QueueUsageError(reason)
    return float(value)


class RabbitMQQueue:
    """
    ``QueueProducer`` + ``QueueConsumer`` over a ``BrokerTransport`` and a ``ProcessingLeaseStore``. See the module docstring.
    One instance may serve a producer (the API) and a consumer (a worker); a worker uses its own instance and worker id.
    """

    def __init__(
        self,
        transport: BrokerTransport,
        leases: ProcessingLeaseStore,
        *,
        worker_id: Optional[str] = None,
        max_in_flight: int = 10,
        clock_us: Callable[[], int] = _now_us,
    ) -> None:
        if not isinstance(max_in_flight, int) or isinstance(max_in_flight, bool) or not 1 <= max_in_flight <= 100:
            raise ValueError("max_in_flight must be between 1 and 100.")
        self._transport = transport
        self._leases = leases
        self._worker_id = worker_id or f"worker-{uuid.uuid4().hex[:12]}"
        self._max_in_flight = max_in_flight
        self._clock = clock_us
        self._inflight: Dict[str, _InFlight] = {}
        self._receive_lock = asyncio.Lock()
        self.stats: Dict[str, int] = {
            name: 0 for name in (
                "published", "received", "acked", "retried", "dead_lettered", "deferred", "dropped_duplicates",
                "stale_dropped", "poison", "unconfirmed_settles",
            )
        }

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def in_flight(self) -> int:
        return len(self._inflight)

    # -- lifecycle ------------------------------------------------------------------------------------------------------

    async def start(self) -> None:
        await self._transport.start()

    async def close(self) -> None:
        """Close the broker connection. Unsettled deliveries return to the queue (their leases expire on their own)."""
        self._inflight.clear()
        await self._transport.close()

    async def health(self) -> bool:
        return await self._transport.is_healthy()

    # -- producer -------------------------------------------------------------------------------------------------------

    def _route(self, delay_seconds: float) -> Tuple[str, Optional[int]]:
        """The queue to publish to and the ``not before`` instant: the main queue when there is no delay, else a wait tier."""
        topology = self._transport.topology
        if delay_seconds <= 0:
            return topology.main, None
        return topology.wait_queue(topology.tier_for(delay_seconds)), self._clock() + int(delay_seconds * 1_000_000)

    async def _publish_body(self, message: DocumentProcessingMessage, body: bytes, delay_seconds: float) -> None:
        queue, not_before = self._route(delay_seconds)
        headers: Dict[str, Any] = {HEADER_TENANT: message.tenant_id, HEADER_SCHEMA: SCHEMA_VERSION}
        if not_before is not None:
            headers[HEADER_NOT_BEFORE] = not_before
        await self._transport.publish(queue, body, message_id=message.message_id, correlation_id=message.correlation_id, headers=headers)

    async def publish(self, message: DocumentProcessingMessage, delay_seconds: int = 0) -> str:
        if not isinstance(message, DocumentProcessingMessage):
            raise QueueUsageError("message_is_not_a_document_processing_message")
        delay = _seconds(delay_seconds, "delay_seconds_is_not_valid", maximum=MAX_DELAY_SECONDS)
        if not _valid_job_key(job_key_of(message)) or not message.tenant_id.strip():
            raise QueueUsageError("message_identity_is_not_valid")
        await self._publish_body(message, encode_message(message), delay)
        self.stats["published"] += 1
        return message.message_id

    async def publish_batch(self, messages: Sequence[DocumentProcessingMessage]) -> List[str]:
        published: List[str] = []
        for message in messages:
            try:
                published.append(await self.publish(message))
            except BrokerPublishError as exc:
                raise BrokerPublishError(exc.details.get("reason"), published=published) from None
            except BrokerUnavailableError as exc:
                error = BrokerPublishError(exc.details.get("reason"), published=published)
                raise error from None
        return published

    # -- consumer: receive ----------------------------------------------------------------------------------------------

    async def receive_messages(self, max_messages: int = 10, visibility_timeout_seconds: int = 60) -> List[QueueEnvelope]:
        if isinstance(max_messages, bool) or not isinstance(max_messages, int) or not 1 <= max_messages <= 100:
            raise QueueUsageError("max_messages_must_be_between_1_and_100")
        if isinstance(visibility_timeout_seconds, bool) or not isinstance(visibility_timeout_seconds, int) \
                or not 1 <= visibility_timeout_seconds <= MAX_LEASE_SECONDS:
            raise QueueUsageError("visibility_timeout_is_not_valid")
        envelopes: List[QueueEnvelope] = []
        async with self._receive_lock:                    # keeps the in-flight bound exact; it is NOT a correctness mechanism
            wanted = min(max_messages, self._max_in_flight - len(self._inflight))
            for _ in range(max(wanted, 0)):
                try:
                    delivery = await self._transport.get(self._transport.topology.main)
                except BrokerUnavailableError:
                    if envelopes:
                        break                              # hand out what was claimed; the caller sees the failure next time
                    raise
                if delivery is None:
                    break
                envelope = await self._admit(delivery, visibility_timeout_seconds)
                if envelope is not None:
                    envelopes.append(envelope)
        return envelopes

    def _decode(self, delivery: Delivery) -> DocumentProcessingMessage:
        body = delivery.body
        if len(body) > MAX_BODY_BYTES:
            raise _Poison("message_is_too_large")
        try:
            message = DocumentProcessingMessage.model_validate_json(body)
        except ValueError:
            raise _Poison("message_is_not_valid") from None
        if delivery.headers.get(HEADER_TENANT) != message.tenant_id:
            raise _Poison("tenant_header_does_not_match_the_message")
        if not message.tenant_id.strip() or not _valid_job_key(job_key_of(message)):
            raise _Poison("message_identity_is_not_valid")
        return message

    async def _admit(self, delivery: Delivery, lease_seconds: int) -> Optional[QueueEnvelope]:
        """Turn one delivery into an envelope with a claimed lease, or settle it (drop, defer, dead-letter) and return ``None``."""
        try:
            message = self._decode(delivery)
        except _Poison as poison:
            await self._dead_letter_raw(delivery, poison.reason)
            return None
        not_before = delivery.headers.get(HEADER_NOT_BEFORE)
        if isinstance(not_before, int) and not isinstance(not_before, bool) and not_before > self._clock():
            await self._redeliver(delivery, message, (not_before - self._clock()) / 1e6)
            return None
        try:
            result = await self._leases.claim(
                message.tenant_id,
                job_key_of(message),
                message_id=message.message_id,
                document_id=message.document_id,
                max_attempts=message.max_retries + 1,
                worker_id=self._worker_id,
                lease_seconds=lease_seconds,
            )
        except Exception:
            await self._give_back(delivery)               # the lease store is unavailable: fail closed, nothing was started
            raise
        status = result.status
        if status is ClaimStatus.ACQUIRED and result.lease is not None:
            handle = secrets.token_urlsafe(24)
            counted = message.model_copy(update={"attempt_count": result.attempt})
            self._inflight[handle] = _InFlight(delivery, result.lease, counted, delivery.body)
            self.stats["received"] += 1
            return QueueEnvelope(
                receipt_handle=handle, message=counted, approximate_receive_count=result.attempt, visible_until=result.lease.expires_at
            )
        if status in (ClaimStatus.COMPLETED, ClaimStatus.DEAD_LETTERED):
            self.stats["dropped_duplicates"] += 1
            await self._settle_quietly(delivery)
        elif status in (ClaimStatus.HELD, ClaimStatus.NOT_YET_VISIBLE):
            await self._redeliver(delivery, message, max(result.retry_after_seconds or 1.0, 1.0))
        else:                                              # EXHAUSTED: the claim itself dead-lettered the job; keep the operator's copy
            await self._publish_dead(delivery.body, message.tenant_id, "attempts_exhausted")
            self.stats["dead_lettered"] += 1
            await self._settle_quietly(delivery)
        return None

    # -- consumer: settle -----------------------------------------------------------------------------------------------

    def _flight(self, receipt_handle: Any) -> _InFlight:
        flight = self._inflight.get(receipt_handle) if isinstance(receipt_handle, str) else None
        if flight is None:
            raise ReceiptHandleError()
        return flight

    async def _fenced(self, receipt_handle: str, operation: Any) -> Any:
        """Run a lease-store call; when the lease is lost, drop the redundant delivery and report the loss."""
        try:
            return await operation
        except LeaseLostError:
            flight = self._inflight.pop(receipt_handle, None)
            if flight is not None:
                self.stats["stale_dropped"] += 1
                await self._settle_quietly(flight.delivery)
            raise

    async def _finish(self, receipt_handle: str) -> None:
        flight = self._inflight.pop(receipt_handle, None)
        if flight is not None:
            await self._settle_quietly(flight.delivery)

    async def _settle_quietly(self, delivery: Delivery) -> None:
        """Acknowledge a delivery after the durable decision was made. A failure is logged: the broker redelivers and the job is settled."""
        try:
            await self._transport.ack(delivery)
        except QueueError as exc:
            self.stats["unconfirmed_settles"] += 1
            logger.warning("Delivery acknowledgement was not confirmed (reason=%s); the job state is already durable", exc.details.get("reason"))

    async def _give_back(self, delivery: Delivery) -> None:
        try:
            await self._transport.reject(delivery, requeue=True)
        except QueueError:
            pass                                           # the channel is gone: the broker requeues it anyway

    async def ack(self, receipt_handle: str) -> None:
        flight = self._flight(receipt_handle)
        await self._fenced(receipt_handle, self._leases.mark_completed(flight.lease))
        self.stats["acked"] += 1
        await self._finish(receipt_handle)

    async def nack(self, receipt_handle: str, requeue: bool = True, backoff_seconds: int = 0) -> None:
        backoff = _seconds(backoff_seconds, "backoff_seconds_is_not_valid", maximum=MAX_DELAY_SECONDS)
        flight = self._flight(receipt_handle)
        lease = flight.lease
        if requeue and lease.attempt < lease.max_attempts:
            await self._fenced(
                receipt_handle, self._leases.release_for_retry(lease, delay_seconds=backoff, error_code="processing_failed")
            )
            retry = flight.message.model_copy(update={"attempt_count": lease.attempt + 1})
            await self._publish_body(retry, encode_message(retry), backoff)
            self.stats["retried"] += 1
        else:
            reason = "retries_exhausted" if requeue else "rejected_by_worker"
            await self._fenced(receipt_handle, self._leases.verify(lease))
            await self._publish_dead(flight.body, lease.tenant_id, reason)
            await self._fenced(receipt_handle, self._leases.dead_letter(lease, reason=reason))
            self.stats["dead_lettered"] += 1
        await self._finish(receipt_handle)

    async def extend_lease(self, receipt_handle: str, extension_seconds: int = 60) -> None:
        if isinstance(extension_seconds, bool) or not isinstance(extension_seconds, int) or not 1 <= extension_seconds <= MAX_LEASE_SECONDS:
            raise QueueUsageError("extension_seconds_is_not_valid")
        flight = self._flight(receipt_handle)
        flight.lease = await self._fenced(receipt_handle, self._leases.extend(flight.lease, extension_seconds))

    def fencing_token(self, receipt_handle: str) -> int:
        """The fencing token of the lease behind ``receipt_handle``: pass it to side effects of the job so a stale worker's writes can be refused."""
        return self._flight(receipt_handle).lease.fencing_token

    async def verify_lease(self, receipt_handle: str) -> None:
        """Raise ``LeaseLostError`` unless this consumer still owns the job (a fencing check before an irreversible side effect)."""
        flight = self._flight(receipt_handle)
        await self._fenced(receipt_handle, self._leases.verify(flight.lease))

    async def defer(self, receipt_handle: str, delay_seconds: float = 0) -> None:
        """Give the job back WITHOUT using an attempt (graceful shutdown, a busy dependency): it is redelivered after ``delay_seconds``."""
        delay = _seconds(delay_seconds, "delay_seconds_is_not_valid", maximum=MAX_DELAY_SECONDS)
        flight = self._flight(receipt_handle)
        await self._fenced(
            receipt_handle,
            self._leases.release_for_retry(flight.lease, delay_seconds=delay, error_code="deferred", consume_attempt=False),
        )
        back = flight.message.model_copy(update={"attempt_count": max(flight.lease.attempt, 1)})
        await self._publish_body(back, encode_message(back), delay)
        self.stats["deferred"] += 1
        await self._finish(receipt_handle)

    # -- internals ------------------------------------------------------------------------------------------------------

    async def _redeliver(self, delivery: Delivery, message: DocumentProcessingMessage, delay_seconds: float) -> None:
        """Put the very same message back after a delay (a job that is held or not yet due), then acknowledge this copy."""
        try:
            await self._publish_body(message, delivery.body, delay_seconds)
        except QueueError:
            await self._give_back(delivery)               # could not be re-queued with a delay: return it as it is
            raise
        self.stats["deferred"] += 1
        await self._settle_quietly(delivery)

    async def _publish_dead(self, body: bytes, tenant_id: Optional[str], reason: str) -> None:
        headers: Dict[str, Any] = {HEADER_REASON: reason, HEADER_SCHEMA: SCHEMA_VERSION}
        if isinstance(tenant_id, str):
            headers[HEADER_TENANT] = tenant_id
        await self._transport.publish(
            self._transport.topology.dead, body, message_id=uuid.uuid4().hex, correlation_id=None, headers=headers
        )

    async def _dead_letter_raw(self, delivery: Delivery, reason: str) -> None:
        tenant = delivery.headers.get(HEADER_TENANT)
        try:
            await self._publish_dead(delivery.body, tenant if isinstance(tenant, str) else None, reason)
        except QueueError:
            await self._give_back(delivery)               # the dead-letter queue did not confirm: never drop the only copy
            raise
        self.stats["poison"] += 1
        logger.error("A message was dead-lettered as unprocessable (reason=%s)", reason)
        await self._settle_quietly(delivery)
