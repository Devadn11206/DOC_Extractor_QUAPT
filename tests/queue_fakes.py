"""
In-memory test double of a message broker (Team Member 3, Phase 3). NOT a RabbitMQ: it implements the ``BrokerTransport`` protocol with the
semantics the queue adapter depends on, so adapter logic can be tested deterministically. Results obtained with it are MOCKED results and
are never reported as RabbitMQ verification (``tests/test_queue_integration.py`` runs the adapter against a real broker).

What it models: FIFO queues; confirmed publishing (a publish to an undeclared queue is refused, like ``mandatory``); an unacknowledged
delivery that returns to the head of its queue, marked redelivered, when the connection is dropped (``drop_connection``) -- and a stale
delivery that can no longer be acknowledged; wait queues whose messages move to the main queue when their TTL has passed on a VIRTUAL
clock (``advance``); a dead-letter queue; a rejected main-queue message that is dead-lettered. The virtual clock is the callable the
adapter and the lease store should both use, so time moves coherently.
"""

from __future__ import annotations

import collections
from typing import Any, Deque, Dict, List, Mapping, Optional, Tuple

from doc_extractor.infrastructure.queue.errors import BrokerPublishError, BrokerUnavailableError
from doc_extractor.infrastructure.queue.transport import QueueTopology

START_US = 1_800_000_000_000_000


class FakeDelivery:
    def __init__(self, queue: str, body: bytes, headers: Mapping[str, Any], redelivered: bool, session: int, message_id: str) -> None:
        self.queue, self.body, self.headers, self.redelivered = queue, body, dict(headers), redelivered
        self.session, self.message_id = session, message_id


class FakeBroker:
    """``BrokerTransport`` double with a virtual clock (``broker()`` returns microseconds since the epoch)."""

    def __init__(self, prefix: str = "doc_extractor", tiers: Tuple[int, ...] = (1, 5, 15, 60, 300, 900, 3600)) -> None:
        self.topology = QueueTopology(prefix=prefix, delay_tiers_seconds=tiers)
        self.now_us = START_US
        self.queues: Dict[str, Deque[Tuple[bytes, Dict[str, Any], str, bool]]] = {}
        self.waiting: List[Tuple[int, bytes, Dict[str, Any], str]] = []        # (due_us, body, headers, message_id) in wait queues
        self.unacked: Dict[int, FakeDelivery] = {}
        self.session = 1
        self.started = False
        self.closed = False
        self.fail: Dict[str, Optional[BaseException]] = {"publish": None, "get": None, "ack": None, "reject": None, "start": None}
        self.fail_publish_to: Dict[str, BaseException] = {}
        self.published: List[Tuple[str, bytes, Dict[str, Any], str]] = []
        self.acked: List[FakeDelivery] = []
        self._tag = 0

    # -- the clock ------------------------------------------------------------------------------------------------------------

    def __call__(self) -> int:
        return self.now_us

    def advance(self, seconds: float) -> None:
        self.now_us += int(seconds * 1_000_000)
        due = [w for w in self.waiting if w[0] <= self.now_us]
        self.waiting = [w for w in self.waiting if w[0] > self.now_us]
        for _, body, headers, message_id in sorted(due, key=lambda w: w[0]):
            self.queues[self.topology.main].append((body, headers, message_id, False))

    # -- test controls --------------------------------------------------------------------------------------------------------

    def drop_connection(self) -> None:
        """The channel dies: every unacknowledged delivery returns to the head of its queue as redelivered; old deliveries go stale."""
        for delivery in reversed(list(self.unacked.values())):                 # the first delivered ends up first in line again
            self.queues[delivery.queue].appendleft((delivery.body, dict(delivery.headers), delivery.message_id, True))
        self.unacked.clear()
        self.session += 1

    def depth(self, queue: str) -> int:
        return len(self.queues.get(queue, ()))

    @property
    def main_depth(self) -> int:
        return self.depth(self.topology.main)

    @property
    def dead_depth(self) -> int:
        return self.depth(self.topology.dead)

    def dead_letters(self) -> List[Tuple[bytes, Dict[str, Any]]]:
        return [(b, h) for b, h, _, _ in self.queues.get(self.topology.dead, ())]

    def put_raw(self, body: bytes, headers: Optional[Mapping[str, Any]] = None, queue: Optional[str] = None) -> None:
        """Place a message on a queue exactly as a (possibly hostile or buggy) foreign publisher could."""
        self.queues[queue or self.topology.main].append((body, dict(headers or {}), "raw", False))

    # -- BrokerTransport ------------------------------------------------------------------------------------------------------

    async def start(self) -> None:
        if self.fail["start"] is not None:
            raise self.fail["start"]
        self.started, self.closed = True, False
        for name in self.topology.all_queues():
            self.queues.setdefault(name, collections.deque())

    async def close(self) -> None:
        self.closed = True
        self.drop_connection()

    async def publish(self, queue: str, body: bytes, *, message_id: str, correlation_id: Optional[str], headers: Mapping[str, Any]) -> None:
        if self.fail["publish"] is not None:
            raise self.fail["publish"]
        if queue in self.fail_publish_to:
            raise self.fail_publish_to[queue]
        if queue not in self.queues:
            raise BrokerPublishError("publish_refused_by_the_broker")          # unroutable with ``mandatory``
        record = (queue, body, dict(headers), message_id)
        self.published.append(record)
        wait_tiers = {self.topology.wait_queue(t): t for t in self.topology.delay_tiers_seconds}
        if queue in wait_tiers:
            self.waiting.append((self.now_us + wait_tiers[queue] * 1_000_000, body, dict(headers), message_id))
        else:
            self.queues[queue].append((body, dict(headers), message_id, False))

    async def get(self, queue: str) -> Optional[FakeDelivery]:
        if self.fail["get"] is not None:
            raise self.fail["get"]
        if not self.queues.get(queue):
            return None
        body, headers, message_id, redelivered = self.queues[queue].popleft()
        self._tag += 1
        delivery = FakeDelivery(queue, body, headers, redelivered, self.session, message_id)
        self.unacked[self._tag] = delivery
        delivery.tag = self._tag                                               # type: ignore[attr-defined]
        return delivery

    def _live(self, delivery: FakeDelivery) -> int:
        tag = getattr(delivery, "tag", None)
        if tag not in self.unacked or delivery.session != self.session:
            raise BrokerUnavailableError("broker_settle_failed")
        return tag

    async def ack(self, delivery: FakeDelivery) -> None:
        if self.fail["ack"] is not None:
            raise self.fail["ack"]
        tag = self._live(delivery)
        del self.unacked[tag]
        self.acked.append(delivery)

    async def reject(self, delivery: FakeDelivery, *, requeue: bool) -> None:
        if self.fail["reject"] is not None:
            raise self.fail["reject"]
        tag = self._live(delivery)
        del self.unacked[tag]
        if requeue:
            self.queues[delivery.queue].appendleft((delivery.body, dict(delivery.headers), delivery.message_id, True))
        elif delivery.queue == self.topology.main:
            self.queues[self.topology.dead].append((delivery.body, dict(delivery.headers), delivery.message_id, False))

    async def is_healthy(self) -> bool:
        return self.started and not self.closed
