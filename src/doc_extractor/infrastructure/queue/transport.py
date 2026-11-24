"""
Broker transport for the durable queue adapter (Team Member 3, Phase 3): RabbitMQ quorum queues over ``aio-pika``.

THE BASELINE IS NOT AN APPROVED DECISION. The Phase 1 assessment named RabbitMQ quorum queues as a CANDIDATE; this is the working
implementation baseline. Nothing here records stakeholder approval of the broker, and the open question of shared queues with
tenant-aware authorisation versus per-tenant queues or virtual hosts is NOT decided (see ``rabbitmq.py``).

WHAT THE BROKER PROVIDES, AND WHAT IT DOES NOT. RabbitMQ has no visibility timeout and no lease extension, and no per-message delay on
a quorum queue. The transport therefore offers only what the broker really does:
- durable, replicated storage: quorum queues, persistent messages, PUBLISHER CONFIRMS (``publish`` returns only when the broker has
  confirmed the message; ``mandatory`` makes an unroutable message an error instead of a silent drop);
- explicit acknowledgement per delivery, and requeue of every unacknowledged delivery when its channel or connection dies;
- delayed redelivery through WAIT QUEUES: one quorum queue per delay tier (default 1 s, 5 s, 15 s, 1 min, 5 min, 15 min, 1 h), each with a
  queue-level ``x-message-ttl`` and a dead-letter route back to the main queue, with at-least-once dead-lettering. All messages of a
  tier share one TTL, so a tier is a FIFO that never blocks on a longer delay. A delay is rounded UP to the next tier (never early); a
  message that comes back before its exact due time (a delay above the last tier) is re-delayed by the adapter using its
  ``x-not-before-us`` header;
- a DEAD-LETTER QUEUE that the adapter publishes to explicitly, and a broker-level ``x-delivery-limit`` as a safety net against a
  message that keeps killing its consumer.
Ownership (who may process a job, attempts, completion) is NOT the broker's job: it is the lease store's (``api/ports/processing.py``).

RECOVERY. ``connect_robust`` reconnects with exponential back-off and restores channels and topology. A delivery received on a lost
channel can no longer be acknowledged; the broker redelivers it and the lease store decides what happens next, so a connection failure
never loses or duplicates a settled job. Every broker call is bounded by a timeout; a failure is a typed error without driver text.

NOT VERIFIED AGAINST A REAL BROKER IN THIS ENVIRONMENT. The queue arguments (quorum type, delivery limit, queue TTL, at-least-once
dead-lettering) are the documented RabbitMQ 3.10+ ones, but no broker was available when this was written: ``tests/test_queue_integration.py``
runs them against a real RabbitMQ and skips without one. Treat a first deployment as the verification.

The client (``aio-pika``, extra ``queue``) is imported only when a connection is made. ``QueueTopology`` and the ``BrokerTransport``
protocol import without it, and tests use a double of the protocol.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Protocol, Tuple, runtime_checkable

from doc_extractor.infrastructure.queue.errors import BrokerConfigurationError, BrokerPublishError, BrokerUnavailableError
from doc_extractor.infrastructure.queue.settings import BrokerSettings

logger = logging.getLogger("doc_extractor.queue")

MAX_BODY_BYTES = 64 * 1024
WAIT_QUEUE_PREFIX = "wait"


@dataclass(frozen=True)
class QueueTopology:
    """The names and delay tiers of one deployment's queues. Pure data: shared by the adapter and every transport."""

    prefix: str
    delay_tiers_seconds: Tuple[int, ...]

    @property
    def main(self) -> str:
        return f"{self.prefix}.jobs"

    @property
    def dead(self) -> str:
        return f"{self.prefix}.jobs.dead"

    def wait_queue(self, tier_seconds: int) -> str:
        return f"{self.prefix}.jobs.{WAIT_QUEUE_PREFIX}.{tier_seconds}s"

    def tier_for(self, delay_seconds: float) -> int:
        """The smallest tier that is at least ``delay_seconds`` (rounded UP, never early); the last tier for anything longer."""
        for tier in self.delay_tiers_seconds:
            if delay_seconds <= tier:
                return tier
        return self.delay_tiers_seconds[-1]

    def all_queues(self) -> Tuple[str, ...]:
        return (self.main, self.dead) + tuple(self.wait_queue(t) for t in self.delay_tiers_seconds)


@runtime_checkable
class Delivery(Protocol):
    """One message handed to the consumer and not yet acknowledged."""

    body: bytes
    headers: Mapping[str, Any]
    redelivered: bool


@runtime_checkable
class BrokerTransport(Protocol):
    """What the queue adapter needs from a broker. Implemented by ``AioPikaTransport`` and by test doubles."""

    topology: QueueTopology

    async def start(self) -> None:
        """Connect and declare the topology. Idempotent. ``BrokerUnavailableError`` / ``BrokerConfigurationError`` on failure."""
        ...

    async def close(self) -> None:
        """Close the connection. Unacknowledged deliveries return to their queue. Idempotent."""
        ...

    async def publish(
        self, queue: str, body: bytes, *, message_id: str, correlation_id: Optional[str], headers: Mapping[str, Any]
    ) -> None:
        """Publish persistently and return only when the broker confirmed it. ``BrokerPublishError`` / ``BrokerUnavailableError``."""
        ...

    async def get(self, queue: str) -> Optional[Delivery]:
        """The next message of ``queue`` (unacknowledged), or ``None`` when it is empty."""
        ...

    async def ack(self, delivery: Delivery) -> None:
        ...

    async def reject(self, delivery: Delivery, *, requeue: bool) -> None:
        ...

    async def is_healthy(self) -> bool:
        ...


def build_topology(settings: BrokerSettings) -> QueueTopology:
    return QueueTopology(prefix=settings.queue_prefix, delay_tiers_seconds=settings.delay_tiers_seconds)


def queue_arguments(topology: QueueTopology, name: str, delivery_limit: int) -> Dict[str, Any]:
    """The declaration arguments of one queue. Quorum everywhere; see the module docstring for the three kinds."""
    arguments: Dict[str, Any] = {"x-queue-type": "quorum"}
    if name == topology.main:
        arguments.update({
            "x-delivery-limit": delivery_limit,
            "x-dead-letter-exchange": "",                      # the default exchange routes by queue name
            "x-dead-letter-routing-key": topology.dead,
        })
    elif name != topology.dead:
        tier = next(t for t in topology.delay_tiers_seconds if topology.wait_queue(t) == name)
        arguments.update({
            "x-message-ttl": tier * 1000,
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": topology.main,
            "x-dead-letter-strategy": "at-least-once",
            "x-overflow": "reject-publish",                    # required by at-least-once dead-lettering
        })
    return arguments


class _AioPikaDelivery:
    __slots__ = ("raw", "body", "headers", "redelivered")

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.body: bytes = bytes(raw.body)
        self.headers: Mapping[str, Any] = dict(raw.headers or {})
        self.redelivered: bool = bool(raw.redelivered)


def _is_broker_failure(exc: BaseException) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    return any((klass.__module__ or "").split(".")[0] in ("aio_pika", "aiormq", "pamqp") for klass in type(exc).__mro__)


def _refused(exc: BaseException) -> bool:
    """True for a broker that ANSWERED and refused (different declaration arguments, access refused), not one that is unreachable."""
    return any(
        klass.__name__ in ("ChannelPreconditionFailed", "ChannelAccessRefused", "ChannelNotFoundEntity", "ProbableAccessDeniedError")
        for klass in type(exc).__mro__
    )


ConnectFunction = Callable[..., Awaitable[Any]]


class AioPikaTransport:
    """``BrokerTransport`` over ``aio-pika``'s robust connection. See the module docstring."""

    def __init__(self, settings: BrokerSettings, *, connect: Optional[ConnectFunction] = None) -> None:
        self._settings = settings
        self.topology = build_topology(settings)
        self._connect = connect
        self._connection: Any = None
        self._channel: Any = None
        self._queues: Dict[str, Any] = {}
        self._module: Any = None
        self._start_lock = asyncio.Lock()

    # -- lifecycle ------------------------------------------------------------------------------------------------------

    def _aio_pika(self) -> Any:
        if self._module is None:
            try:
                import aio_pika
            except ImportError:
                raise BrokerConfigurationError("amqp_client_is_not_installed", setting="queue") from None
            self._module = aio_pika
        return self._module

    async def start(self) -> None:
        async with self._start_lock:
            if self._connection is not None:
                return
            module = self._aio_pika()
            connect = self._connect or module.connect_robust
            connection = None
            try:
                connection = await asyncio.wait_for(
                    connect(self._settings.url.get_secret_value(), timeout=self._settings.connect_timeout_seconds),
                    timeout=self._settings.connect_timeout_seconds + 1.0,
                )
                channel = await connection.channel(publisher_confirms=True)
                await channel.set_qos(prefetch_count=self._settings.max_in_flight)
                queues: Dict[str, Any] = {}
                for name in self.topology.all_queues():
                    queues[name] = await channel.declare_queue(
                        name, durable=True, arguments=queue_arguments(self.topology, name, self._settings.delivery_limit)
                    )
            except BrokerConfigurationError:
                raise
            except Exception as exc:                                         # noqa: BLE001 - mapped to a typed error, text dropped
                await self._close_quietly(connection)
                reason = "broker_topology_was_refused" if _refused(exc) else "broker_connect_failed"
                logger.error("Broker start-up failed (reason=%s error_type=%s)", reason, type(exc).__name__)
                raise BrokerUnavailableError(reason) from None
            self._connection, self._channel, self._queues = connection, channel, queues
            logger.info("Broker connected (queues=%d)", len(queues))

    async def close(self) -> None:
        connection, self._connection, self._channel, self._queues = self._connection, None, None, {}
        await self._close_quietly(connection)

    @staticmethod
    async def _close_quietly(connection: Any) -> None:
        if connection is None:
            return
        try:
            await asyncio.wait_for(connection.close(), timeout=5.0)
        except Exception:                                                    # noqa: BLE001 - closing is best effort
            pass

    def _require_started(self) -> Any:
        if self._channel is None:
            raise BrokerUnavailableError("broker_not_started")
        return self._channel

    # -- operations -----------------------------------------------------------------------------------------------------

    async def publish(
        self, queue: str, body: bytes, *, message_id: str, correlation_id: Optional[str], headers: Mapping[str, Any]
    ) -> None:
        channel = self._require_started()
        if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_BODY_BYTES:
            raise BrokerPublishError("message_body_is_too_large")
        module = self._aio_pika()
        message = module.Message(
            body=bytes(body),
            delivery_mode=module.DeliveryMode.PERSISTENT,
            content_type="application/json",
            message_id=message_id,
            correlation_id=correlation_id,
            headers=dict(headers),
        )
        try:
            await channel.default_exchange.publish(
                message, routing_key=queue, mandatory=True, timeout=self._settings.publish_timeout_seconds
            )
        except Exception as exc:                                             # noqa: BLE001 - mapped to a typed error, text dropped
            if type(exc).__name__ == "DeliveryError":                         # a nack or an unroutable return: the broker answered
                raise BrokerPublishError("publish_refused_by_the_broker") from None
            if _is_broker_failure(exc):
                raise BrokerUnavailableError("broker_publish_failed") from None
            raise

    async def get(self, queue: str) -> Optional[Delivery]:
        self._require_started()
        handle = self._queues.get(queue)
        if handle is None:
            raise BrokerUnavailableError("unknown_queue")
        try:
            raw = await handle.get(no_ack=False, fail=False, timeout=self._settings.publish_timeout_seconds)
        except Exception as exc:                                             # noqa: BLE001
            if _is_broker_failure(exc):
                raise BrokerUnavailableError("broker_get_failed") from None
            raise
        return None if raw is None else _AioPikaDelivery(raw)

    async def ack(self, delivery: Delivery) -> None:
        await self._settle(delivery, lambda raw: raw.ack())

    async def reject(self, delivery: Delivery, *, requeue: bool) -> None:
        await self._settle(delivery, lambda raw: raw.reject(requeue=requeue))

    async def _settle(self, delivery: Delivery, action: Callable[[Any], Awaitable[Any]]) -> None:
        raw = getattr(delivery, "raw", None)
        if raw is None:
            raise BrokerUnavailableError("delivery_is_not_from_this_transport")
        try:
            await asyncio.wait_for(action(raw), timeout=self._settings.publish_timeout_seconds)
        except Exception as exc:                                             # noqa: BLE001
            if _is_broker_failure(exc):
                raise BrokerUnavailableError("broker_settle_failed") from None
            raise

    async def is_healthy(self) -> bool:
        connection, channel = self._connection, self._channel
        if connection is None or channel is None:
            return False
        return not bool(getattr(connection, "is_closed", False)) and not bool(getattr(channel, "is_closed", False))
