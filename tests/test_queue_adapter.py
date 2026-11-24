"""
Durable queue adapter tests (Team Member 3, Phase 3).

MOCKED: the broker is the in-memory double of ``tests/queue_fakes.py`` (FIFO queues, confirmed publishing, redelivery when a connection
drops, wait queues on a virtual clock, a dead-letter queue) and the lease store is the real ``MongoProcessingLeaseStore`` over the
in-memory MongoDB fake of ``tests/mongo_fakes.py``. Both use ONE virtual clock. These tests prove the adapter's logic. They are NOT
RabbitMQ or MongoDB verification: ``tests/test_queue_integration.py`` runs the adapter against a real broker and a real replica set
and skips without them.
"""

import asyncio
import json
import logging

import pytest

pytest.importorskip("pymongo", reason="the 'mongo' extra provides the driver whose error types these tests map")
from pymongo import errors as driver  # noqa: E402

from doc_extractor.api.contracts.records import DocumentProcessingMessage, QueueEnvelope  # noqa: E402
from doc_extractor.api.ports.processing import LeaseLostError  # noqa: E402
from doc_extractor.api.ports.queue import QueueConsumer, QueueProducer  # noqa: E402
from doc_extractor.infrastructure.mongo import (  # noqa: E402
    DEFAULT_REGISTRY,
    MongoConnection,
    MongoSettings,
    PersistenceUnavailableError,
    ensure_indexes,
)
from doc_extractor.infrastructure.mongo.indexes import PROCESSING_JOBS  # noqa: E402
from doc_extractor.infrastructure.mongo.processing import MongoProcessingLeaseStore  # noqa: E402
from doc_extractor.infrastructure.queue import (  # noqa: E402
    BrokerPublishError,
    BrokerUnavailableError,
    QueueUsageError,
    RabbitMQQueue,
    ReceiptHandleError,
    encode_message,
)
from tests.mongo_fakes import FakeClient, LogCapture, factory_for  # noqa: E402
from tests.queue_fakes import FakeBroker  # noqa: E402

MARKER = "Body-Content-Marker-7731"


class Env:
    def __init__(self, broker, client, leases, queue):
        self.broker, self.client, self.leases, self.queue = broker, client, leases, queue

    def worker(self, name, **options):
        """Another consumer process: its own adapter instance over the same broker and the same lease store."""
        return RabbitMQQueue(self.broker, self.leases, worker_id=name, clock_us=self.broker, **options)

    @property
    def jobs(self):
        return self.client["doc"][PROCESSING_JOBS]


async def build(*, tiers=(1, 5, 15, 60, 300, 900, 3600), max_in_flight=10, indexes=True):
    broker = FakeBroker(tiers=tiers)
    client = FakeClient()
    connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(client))
    await connection.connect()
    if indexes:
        await ensure_indexes(client["doc"], DEFAULT_REGISTRY)
    leases = MongoProcessingLeaseStore(connection, clock_us=broker)
    queue = RabbitMQQueue(broker, leases, worker_id="w1", max_in_flight=max_in_flight, clock_us=broker)
    await queue.start()
    return Env(broker, client, leases, queue)


def scenario(body, **options):
    async def go():
        return await body(await build(**options))

    return asyncio.run(go())


def msg(n=1, *, tenant="tenant_a", dedup=None, max_retries=3, payload=None, correlation=None):
    return DocumentProcessingMessage(
        message_id=f"msg_{n}", document_id=f"doc_{n}", tenant_id=tenant, event_id=f"evt_{n}", deduplication_id=dedup,
        max_retries=max_retries, payload=payload or {}, correlation_id=correlation,
    )


async def one(queue, *, lease=60):
    envelopes = await queue.receive_messages(1, lease)
    assert len(envelopes) == 1, envelopes
    return envelopes[0]


# ---- contract ----------------------------------------------------------------------------------------------------------------

def test_the_adapter_satisfies_both_queue_contracts():
    async def body(env):
        assert isinstance(env.queue, QueueProducer) and isinstance(env.queue, QueueConsumer)

    scenario(body)


# ---- publish and consume -----------------------------------------------------------------------------------------------------

def test_a_published_message_is_received_with_a_lease_and_an_opaque_handle():
    async def body(env):
        sent = msg(1, payload={"mode": "full"}, correlation="corr_1")
        assert await env.queue.publish(sent) == "msg_1"
        envelope = await one(env.queue, lease=90)
        assert isinstance(envelope, QueueEnvelope)
        assert envelope.message.model_dump() == sent.model_dump()
        assert envelope.approximate_receive_count == 1 and envelope.message.attempt_count == 1
        assert envelope.visible_until.timestamp() == pytest.approx(env.broker.now_us / 1e6 + 90, abs=0.001)
        assert len(envelope.receipt_handle) >= 24 and "msg_1" not in envelope.receipt_handle and "tenant_a" not in envelope.receipt_handle
        assert env.queue.in_flight == 1
        snap = await env.leases.snapshot("tenant_a", "msg_1")
        assert snap["state"] == "leased" and snap["fencing_token"] == 1 and snap["lease_owner"] == "w1"

    scenario(body)


def test_the_wire_message_is_persistent_json_with_the_tenant_in_a_header_and_no_extra_fields():
    async def body(env):
        await env.queue.publish(msg(1))
        queue, wire, headers, message_id = env.broker.published[0]
        assert queue == env.broker.topology.main and message_id == "msg_1"
        assert headers["tenant_id"] == "tenant_a" and headers["x-schema-version"] == 1 and "x-not-before-us" not in headers
        assert set(json.loads(wire)) == set(msg(1).model_dump(mode="json"))

    scenario(body)


def test_an_empty_queue_yields_an_empty_batch():
    async def body(env):
        assert await env.queue.receive_messages() == []

    scenario(body)


def test_messages_are_received_in_publish_order_up_to_the_requested_batch():
    async def body(env):
        await env.queue.publish_batch([msg(n) for n in range(1, 6)])
        first = await env.queue.receive_messages(3)
        assert [e.message.message_id for e in first] == ["msg_1", "msg_2", "msg_3"]
        assert [e.message.message_id for e in await env.queue.receive_messages(10)] == ["msg_4", "msg_5"]

    scenario(body)


def test_publish_batch_returns_ids_in_order_and_a_failure_reports_what_was_confirmed():
    async def body(env):
        assert await env.queue.publish_batch([msg(1), msg(2)]) == ["msg_1", "msg_2"]
        original = env.broker.publish
        calls = {"n": 0}

        async def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:
                raise BrokerUnavailableError("broker_publish_failed")
            return await original(*args, **kwargs)

        env.broker.publish = flaky
        with pytest.raises(BrokerPublishError) as raised:
            await env.queue.publish_batch([msg(3), msg(4), msg(5)])
        assert raised.value.published == ["msg_3", "msg_4"] and env.broker.main_depth == 4
        # republishing the whole batch is safe: the jobs are de-duplicated by key when they are claimed
        env.broker.publish = original
        await env.queue.publish_batch([msg(3), msg(4), msg(5)])
        got = await env.queue.receive_messages(10)
        assert sorted(e.message.message_id for e in got) == ["msg_1", "msg_2", "msg_3", "msg_4", "msg_5"]   # the repeats are one job each
        assert env.queue.stats["deferred"] == 2                                                              # ... delayed, not processed twice
        await asyncio.gather(*(env.queue.ack(e.receipt_handle) for e in got))

    scenario(body)


@pytest.mark.parametrize("bad", [0, 101, -1, True, "5", None, 2.5])
def test_receive_refuses_an_invalid_batch_size(bad):
    async def body(env):
        with pytest.raises(QueueUsageError):
            await env.queue.receive_messages(bad)

    scenario(body)


@pytest.mark.parametrize("bad", [0, -1, 86401, True, "60", 1.5])
def test_receive_refuses_an_invalid_visibility_timeout(bad):
    async def body(env):
        with pytest.raises(QueueUsageError):
            await env.queue.receive_messages(1, bad)

    scenario(body)


@pytest.mark.parametrize("delay", [-1, 7 * 24 * 3600 + 1, True, "5", float("nan"), None])
def test_publish_refuses_an_invalid_delay(delay):
    async def body(env):
        with pytest.raises(QueueUsageError):
            await env.queue.publish(msg(1), delay_seconds=delay)
        assert env.broker.published == []

    scenario(body)


def test_publish_refuses_a_non_message_and_an_unusable_job_key():
    async def body(env):
        with pytest.raises(QueueUsageError):
            await env.queue.publish({"message_id": "x"})
        with pytest.raises(QueueUsageError):
            await env.queue.publish(msg(1, dedup="k" * 300))
        assert env.broker.published == []

    scenario(body)


def test_the_number_of_unsettled_messages_is_bounded():
    async def body(env):
        await env.queue.publish_batch([msg(n) for n in range(1, 6)])
        first = await env.queue.receive_messages(10)
        assert len(first) == 2 and env.queue.in_flight == 2
        assert await env.queue.receive_messages(10) == []                 # at the bound: nothing more is handed out
        await env.queue.ack(first[0].receipt_handle)
        assert len(await env.queue.receive_messages(10)) == 1

    scenario(body, max_in_flight=2)


# ---- ack ---------------------------------------------------------------------------------------------------------------------

def test_ack_completes_the_job_acknowledges_the_delivery_and_removes_the_handle():
    async def body(env):
        await env.queue.publish(msg(1))
        envelope = await one(env.queue)
        await env.queue.ack(envelope.receipt_handle)
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "completed"
        assert env.broker.unacked == {} and env.queue.in_flight == 0 and env.broker.main_depth == 0
        with pytest.raises(ReceiptHandleError):
            await env.queue.ack(envelope.receipt_handle)                    # the handle is spent
        with pytest.raises(ReceiptHandleError):
            await env.queue.ack("never-issued")

    scenario(body)


def test_a_duplicate_of_a_completed_job_is_acknowledged_and_never_processed_again():
    async def body(env):
        await env.queue.publish(msg(1))
        await env.queue.ack((await one(env.queue)).receipt_handle)
        await env.queue.publish(msg(1))                                      # the same message published again
        assert await env.queue.receive_messages() == []
        assert env.broker.main_depth == 0 and env.broker.unacked == {} and env.queue.stats["dropped_duplicates"] == 1

    scenario(body)


def test_two_messages_with_one_deduplication_id_are_one_job():
    async def body(env):
        await env.queue.publish(msg(1, dedup="upload-77"))
        await env.queue.publish(msg(2, dedup="upload-77"))
        first = await one(env.queue)
        assert first.message.message_id == "msg_1"
        second_pass = await env.queue.receive_messages(5)                   # the second is for a job someone holds: deferred
        assert second_pass == [] and env.queue.stats["deferred"] == 1
        await env.queue.ack(first.receipt_handle)
        env.broker.advance(61)
        assert await env.queue.receive_messages(5) == [] and env.queue.stats["dropped_duplicates"] == 1
        assert [d["job_key"] for d in env.jobs.docs] == ["upload-77"]

    scenario(body)


# ---- nack, retry and dead letter ---------------------------------------------------------------------------------------------

def test_nack_with_requeue_redelivers_as_the_next_attempt_after_the_backoff():
    async def body(env):
        await env.queue.publish(msg(1))
        envelope = await one(env.queue)
        await env.queue.nack(envelope.receipt_handle, requeue=True, backoff_seconds=30)
        assert env.queue.in_flight == 0 and env.broker.unacked == {}
        queue_name, _, headers, _ = env.broker.published[-1]
        assert queue_name == env.broker.topology.wait_queue(60)              # 30 s rounds UP to the 60 s tier, never early
        assert headers["x-not-before-us"] == env.broker.now_us + 30_000_000
        assert await env.queue.receive_messages() == []
        env.broker.advance(61)
        again = await one(env.queue)
        assert again.approximate_receive_count == 2 and again.message.attempt_count == 2
        snap = await env.leases.snapshot("tenant_a", "msg_1")
        assert snap["fencing_token"] == 2 and snap["last_error_code"] == "processing_failed"

    scenario(body)


def test_nack_without_backoff_requeues_straight_to_the_main_queue():
    async def body(env):
        await env.queue.publish(msg(1))
        await env.queue.nack((await one(env.queue)).receipt_handle)
        assert env.broker.published[-1][0] == env.broker.topology.main
        assert (await one(env.queue)).approximate_receive_count == 2

    scenario(body)


def test_a_job_is_dead_lettered_after_max_retries_plus_one_attempts():
    async def body(env):
        await env.queue.publish(msg(1, max_retries=2))
        for attempt in (1, 2, 3):
            envelope = await one(env.queue)
            assert envelope.approximate_receive_count == attempt
            await env.queue.nack(envelope.receipt_handle, requeue=True)
        assert env.broker.dead_depth == 1 and env.broker.main_depth == 0 and env.broker.unacked == {}
        body_, headers = env.broker.dead_letters()[0]
        assert headers["x-dead-letter-reason"] == "retries_exhausted" and headers["tenant_id"] == "tenant_a"
        assert DocumentProcessingMessage.model_validate_json(body_).message_id == "msg_1"
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "dead_lettered"
        await env.queue.publish(msg(1, max_retries=2))                       # a replayed message does not resurrect it
        assert await env.queue.receive_messages() == []

    scenario(body)


def test_a_job_with_no_retries_is_dead_lettered_by_its_first_failure():
    async def body(env):
        await env.queue.publish(msg(1, max_retries=0))
        await env.queue.nack((await one(env.queue)).receipt_handle, requeue=True)
        assert env.broker.dead_depth == 1 and env.broker.main_depth == 0

    scenario(body)


def test_nack_without_requeue_dead_letters_immediately_with_a_reason():
    async def body(env):
        await env.queue.publish(msg(1))
        await env.queue.nack((await one(env.queue)).receipt_handle, requeue=False)
        assert env.broker.dead_depth == 1 and env.broker.dead_letters()[0][1]["x-dead-letter-reason"] == "rejected_by_worker"
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "dead_lettered"
        assert env.queue.stats["dead_lettered"] == 1

    scenario(body)


@pytest.mark.parametrize("bad", [-1, True, "5", 7 * 24 * 3600 + 1, float("nan")])
def test_nack_refuses_an_invalid_backoff_and_keeps_the_handle(bad):
    async def body(env):
        await env.queue.publish(msg(1))
        envelope = await one(env.queue)
        with pytest.raises(QueueUsageError):
            await env.queue.nack(envelope.receipt_handle, backoff_seconds=bad)
        assert env.queue.in_flight == 1

    scenario(body)


def test_a_failed_dead_letter_publish_keeps_the_delivery_and_the_job_and_can_be_repeated():
    async def body(env):
        await env.queue.publish(msg(1))
        envelope = await one(env.queue)
        env.broker.fail_publish_to[env.broker.topology.dead] = BrokerUnavailableError("broker_publish_failed")
        with pytest.raises(BrokerUnavailableError):
            await env.queue.nack(envelope.receipt_handle, requeue=False)
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "leased"      # not dead-lettered without a record
        assert env.queue.in_flight == 1 and len(env.broker.unacked) == 1
        env.broker.fail_publish_to.clear()
        await env.queue.nack(envelope.receipt_handle, requeue=False)
        assert env.broker.dead_depth == 1 and (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "dead_lettered"

    scenario(body)


def test_a_failed_retry_publish_keeps_the_delivery_and_a_repeat_succeeds():
    async def body(env):
        await env.queue.publish(msg(1))
        envelope = await one(env.queue)
        env.broker.fail["publish"] = BrokerUnavailableError("broker_publish_failed")
        with pytest.raises(BrokerUnavailableError):
            await env.queue.nack(envelope.receipt_handle, backoff_seconds=5)
        assert env.queue.in_flight == 1
        env.broker.fail["publish"] = None
        await env.queue.nack(envelope.receipt_handle, backoff_seconds=5)       # release is idempotent for the same lease
        env.broker.advance(6)
        assert (await one(env.queue)).approximate_receive_count == 2

    scenario(body)


# ---- delay -------------------------------------------------------------------------------------------------------------------

def test_a_delayed_message_is_not_delivered_before_its_time_and_never_early_even_if_the_ttl_fires_first():
    async def body(env):
        await env.queue.publish(msg(1), delay_seconds=30)                   # the largest tier is 5 s: it comes back after 5 s, early
        queue_name, _, headers, _ = env.broker.published[0]
        assert queue_name == env.broker.topology.wait_queue(5) and headers["x-not-before-us"] == env.broker.now_us + 30_000_000
        assert await env.queue.receive_messages() == []
        env.broker.advance(5)
        assert await env.queue.receive_messages() == [] and env.queue.stats["deferred"] == 1     # early: delayed again, not claimed
        assert await env.leases.snapshot("tenant_a", "msg_1") is None
        for _ in range(8):
            env.broker.advance(5)
            envelopes = await env.queue.receive_messages()
            if envelopes:
                break
        assert envelopes and env.broker.now_us >= 1_800_000_000_000_000 + 30_000_000

    scenario(body, tiers=(1, 5))


# ---- live leases, other workers ----------------------------------------------------------------------------------------------

def test_a_job_held_by_a_live_lease_is_delayed_for_the_other_worker_and_does_not_use_an_attempt():
    async def body(env):
        other = env.worker("w2")
        await env.queue.publish(msg(1))
        held = await one(env.queue, lease=120)
        env.broker.drop_connection()                                       # the delivery returns (a network blip) while w1 still works
        assert await other.receive_messages() == []
        assert other.stats["deferred"] == 1 and env.broker.main_depth == 0
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["attempt"] == 1
        assert env.broker.published[-1][0] == env.broker.topology.wait_queue(300)      # ~120 s left, rounded UP to a tier
        await env.queue.extend_lease(held.receipt_handle, 400)             # w1 is alive and keeps renewing its lease
        env.broker.advance(301)
        assert await other.receive_messages() == []                         # the delayed copy is back: still held, delayed again
        assert other.stats["deferred"] == 2 and (await env.leases.snapshot("tenant_a", "msg_1"))["lease_owner"] == "w1"

    scenario(body)


def test_after_a_worker_dies_the_job_is_taken_over_once_its_lease_expired_and_the_old_worker_is_fenced():
    async def body(env):
        w2 = env.worker("w2")
        await env.queue.publish(msg(1))
        dead = await one(env.queue, lease=60)
        env.broker.drop_connection()                                       # w1's connection died: the broker redelivers
        assert await w2.receive_messages(1, 60) == []                       # lease still live: delayed
        for _ in range(3):
            env.broker.advance(61)                                         # the lease has expired by now
            taken = await w2.receive_messages(1, 60)
            if taken:
                break
        assert len(taken) == 1 and taken[0].approximate_receive_count == 2
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["fencing_token"] == 2
        with pytest.raises(LeaseLostError):                                # w1 comes back from a long pause: every write is refused
            await env.queue.ack(dead.receipt_handle)
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "leased"
        await w2.ack(taken[0].receipt_handle)
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "completed"
        assert env.broker.unacked == {} and env.broker.main_depth == 0

    scenario(body)


def test_a_stale_worker_cannot_extend_nack_or_complete_and_its_redundant_delivery_is_dropped():
    async def body(env):
        w2 = env.worker("w2")
        await env.queue.publish(msg(1))
        stale = await one(env.queue, lease=10)
        env.broker.advance(11)
        env.broker.queues[env.broker.topology.main].append((env.broker.published[0][1], dict(env.broker.published[0][2]), "msg_1", True))
        taken = await w2.receive_messages(1, 60)                           # a second delivery of the job: w2 takes the expired lease
        assert len(taken) == 1
        with pytest.raises(LeaseLostError):
            await env.queue.extend_lease(stale.receipt_handle, 60)
        assert env.queue.in_flight == 0 and env.queue.stats["stale_dropped"] == 1
        with pytest.raises(ReceiptHandleError):
            await env.queue.nack(stale.receipt_handle)                     # the handle was dropped with the lost lease
        await w2.ack(taken[0].receipt_handle)
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "completed"

    scenario(body)


def test_a_job_that_keeps_killing_its_workers_is_dead_lettered_not_retried_for_ever():
    async def body(env):
        await env.queue.publish(msg(1, max_retries=1))                       # two attempts in total
        for name in ("a", "b"):
            worker = env.worker(name)
            assert len(await worker.receive_messages(1, 30)) == 1             # starts the attempt ... and dies
            env.broker.drop_connection()
            env.broker.advance(31)
        survivor = env.worker("c")
        assert await survivor.receive_messages(1, 30) == []
        assert env.broker.dead_depth == 1 and env.broker.dead_letters()[0][1]["x-dead-letter-reason"] == "attempts_exhausted"
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "dead_lettered"
        assert env.broker.unacked == {} and env.broker.main_depth == 0

    scenario(body)


def test_extend_lease_moves_the_expiry_and_validates_its_input():
    async def body(env):
        await env.queue.publish(msg(1))
        envelope = await one(env.queue, lease=60)
        before = (await env.leases.snapshot("tenant_a", "msg_1"))["lease_expires_at_us"]
        await env.queue.extend_lease(envelope.receipt_handle, 600)
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["lease_expires_at_us"] == before + 540_000_000
        for bad in (0, -1, 86401, True, 2.5):
            with pytest.raises(QueueUsageError):
                await env.queue.extend_lease(envelope.receipt_handle, bad)
        with pytest.raises(ReceiptHandleError):
            await env.queue.extend_lease("never-issued", 60)

    scenario(body)


def test_defer_gives_the_job_back_without_using_an_attempt():
    async def body(env):
        await env.queue.publish(msg(1))
        envelope = await one(env.queue)
        await env.queue.defer(envelope.receipt_handle, 5)
        assert env.queue.in_flight == 0 and env.broker.unacked == {}
        env.broker.advance(6)
        again = await one(env.queue)
        assert again.approximate_receive_count == 1 and again.message.attempt_count == 1
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["fencing_token"] == 2

    scenario(body)


# ---- poison and tenants -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body_, headers, reason",
    [
        (b"not json", {"tenant_id": "tenant_a"}, "message_is_not_valid"),
        (b'{"message_id": "m"}', {"tenant_id": "tenant_a"}, "message_is_not_valid"),
        (b"x" * (64 * 1024 + 1), {"tenant_id": "tenant_a"}, "message_is_too_large"),
        (encode_message(msg(1)), {}, "tenant_header_does_not_match_the_message"),
        (encode_message(msg(1)), {"tenant_id": "tenant_b"}, "tenant_header_does_not_match_the_message"),
        (encode_message(msg(1)), {"tenant_id": 5}, "tenant_header_does_not_match_the_message"),
    ],
)
def test_an_unprocessable_message_is_dead_lettered_with_a_fixed_reason_and_never_claimed(body_, headers, reason):
    async def body(env):
        env.broker.put_raw(body_, headers)
        assert await env.queue.receive_messages() == []
        assert env.broker.dead_depth == 1 and env.broker.unacked == {} and env.broker.main_depth == 0
        assert env.broker.dead_letters()[0][1]["x-dead-letter-reason"] == reason
        assert env.jobs.docs == [] and env.queue.stats["poison"] == 1

    scenario(body)


def test_a_message_with_an_unsafe_tenant_is_dead_lettered_not_processed():
    async def body(env):
        env.broker.put_raw(b'{"message_id":"m","document_id":"d","tenant_id":"a/../b","event_id":"e"}', {"tenant_id": "a/../b"})
        assert await env.queue.receive_messages() == []
        assert env.broker.dead_depth == 1 and env.jobs.docs == []

    scenario(body)


def test_when_the_dead_letter_queue_does_not_confirm_the_poison_message_is_returned_not_dropped():
    async def body(env):
        env.broker.put_raw(b"not json", {"tenant_id": "tenant_a"})
        env.broker.fail_publish_to[env.broker.topology.dead] = BrokerUnavailableError("broker_publish_failed")
        with pytest.raises(BrokerUnavailableError):
            await env.queue.receive_messages()
        assert env.broker.main_depth == 1 and env.broker.unacked == {}

    scenario(body)


def test_tenants_sharing_ids_are_independent_jobs_and_neither_sees_the_other():
    async def body(env):
        await env.queue.publish(msg(1, tenant="tenant_a", dedup="same-key"))
        await env.queue.publish(msg(1, tenant="tenant_b", dedup="same-key"))
        got = await env.queue.receive_messages(5)
        assert sorted(e.message.tenant_id for e in got) == ["tenant_a", "tenant_b"]
        by_tenant = {e.message.tenant_id: e for e in got}
        await env.queue.ack(by_tenant["tenant_a"].receipt_handle)
        assert (await env.leases.snapshot("tenant_a", "same-key"))["state"] == "completed"
        assert (await env.leases.snapshot("tenant_b", "same-key"))["state"] == "leased"
        await env.queue.nack(by_tenant["tenant_b"].receipt_handle, requeue=False)
        assert env.broker.dead_letters()[0][1]["tenant_id"] == "tenant_b"
        assert (await env.leases.snapshot("tenant_a", "same-key"))["state"] == "completed"
        assert len(env.jobs.docs) == 2 and {d["tenant_id"] for d in env.jobs.docs} == {"tenant_a", "tenant_b"}

    scenario(body)


# ---- failures of the broker and of the lease store ----------------------------------------------------------------------------

def test_an_unavailable_broker_is_a_typed_error_on_publish_and_receive():
    async def body(env):
        env.broker.fail["publish"] = BrokerUnavailableError("broker_publish_failed")
        with pytest.raises(BrokerUnavailableError):
            await env.queue.publish(msg(1))
        env.broker.fail["get"] = BrokerUnavailableError("broker_get_failed")
        with pytest.raises(BrokerUnavailableError):
            await env.queue.receive_messages()
        assert env.queue.stats["published"] == 0

    scenario(body)


def test_a_broker_failure_after_some_claims_hands_out_what_was_claimed():
    async def body(env):
        await env.queue.publish_batch([msg(1), msg(2)])
        original = env.broker.get
        calls = {"n": 0}

        async def flaky(queue):
            calls["n"] += 1
            if calls["n"] == 2:
                raise BrokerUnavailableError("broker_get_failed")
            return await original(queue)

        env.broker.get = flaky
        assert [e.message.message_id for e in await env.queue.receive_messages(5)] == ["msg_1"]

    scenario(body)


def test_a_failed_acknowledgement_after_the_durable_completion_is_logged_not_raised_and_the_redelivery_is_dropped():
    async def body(env):
        await env.queue.publish(msg(1))
        envelope = await one(env.queue)
        env.broker.drop_connection()                                       # the channel died before the acknowledgement
        with LogCapture("doc_extractor.queue") as logs:
            await env.queue.ack(envelope.receipt_handle)
        assert env.queue.stats["unconfirmed_settles"] == 1 and "not confirmed" in logs.text
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "completed"
        assert await env.queue.receive_messages() == [] and env.broker.main_depth == 0       # the returned copy is dropped as completed

    scenario(body)


def test_when_the_lease_store_is_down_the_delivery_goes_back_and_nothing_is_started():
    async def body(env):
        await env.queue.publish(msg(1))
        env.jobs.fail_with = driver.AutoReconnect("down mongodb://u:SECRETPW@h")
        with pytest.raises(PersistenceUnavailableError):
            await env.queue.receive_messages()
        env.jobs.fail_with = None
        assert env.broker.main_depth == 1 and env.broker.unacked == {} and env.queue.in_flight == 0
        assert (await one(env.queue)).approximate_receive_count == 1

    scenario(body)


def test_without_the_unique_job_index_nothing_is_claimed_and_the_message_stays_queued():
    async def body(env):
        await env.queue.publish(msg(1))
        with pytest.raises(Exception) as raised:
            await env.queue.receive_messages()
        assert "processing_job_index_is_missing" in str(raised.value.details)
        assert env.broker.main_depth == 1 and env.queue.in_flight == 0

    scenario(body, indexes=False)


def test_close_closes_the_broker_and_forgets_unsettled_handles():
    async def body(env):
        await env.queue.publish(msg(1))
        await one(env.queue)
        await env.queue.close()
        assert env.broker.closed and env.queue.in_flight == 0 and env.broker.main_depth == 1      # back on the queue for another worker
        assert await env.queue.health() is False

    scenario(body)


# ---- many workers ------------------------------------------------------------------------------------------------------------

def test_many_workers_process_every_job_exactly_once():
    async def body(env):
        total = 40
        await env.queue.publish_batch([msg(n) for n in range(1, total + 1)])
        workers = [env.worker(f"w{i}", max_in_flight=4) for i in range(5)]
        processed = []

        async def run(worker):
            while True:
                batch = await worker.receive_messages(3, 60)
                if not batch:
                    if env.broker.main_depth == 0:
                        return
                    await asyncio.sleep(0)
                    continue
                for envelope in batch:
                    processed.append(envelope.message.message_id)
                    await asyncio.sleep(0)
                    await worker.ack(envelope.receipt_handle)

        await asyncio.gather(*(run(w) for w in workers))
        assert sorted(processed) == sorted(f"msg_{n}" for n in range(1, total + 1))
        assert all(d["state"] == "completed" and d["attempt"] == 1 and d["fencing_token"] == 1 for d in env.jobs.docs)

    scenario(body)


def test_one_job_published_many_times_is_processed_once_across_workers():
    async def body(env):
        for _ in range(8):
            await env.queue.publish(msg(1))
        workers = [env.worker(f"w{i}") for i in range(4)]
        processed = []

        async def run(worker):
            for _ in range(30):
                for envelope in await worker.receive_messages(2, 60):
                    processed.append(worker.worker_id)
                    await asyncio.sleep(0)
                    await worker.ack(envelope.receipt_handle)
                await asyncio.sleep(0)
                env.broker.advance(2)

        await asyncio.gather(*(run(w) for w in workers))
        assert len(processed) == 1
        assert [d["state"] for d in env.jobs.docs] == ["completed"] and env.broker.unacked == {}

    scenario(body)


# ---- hygiene ----------------------------------------------------------------------------------------------------------------

def test_no_log_line_or_error_carries_a_message_body_or_a_credential():
    async def body(env):
        with LogCapture("doc_extractor.queue") as logs:
            env.broker.put_raw(MARKER.encode(), {"tenant_id": "tenant_a"})
            await env.queue.receive_messages()
            await env.queue.publish(msg(1, payload={"note": MARKER}))
            envelope = await one(env.queue)
            env.broker.drop_connection()
            await env.queue.ack(envelope.receipt_handle)
        assert MARKER not in logs.text and "tenant_a" not in logs.text
        for error in (BrokerUnavailableError("broker_connect_failed"), BrokerPublishError(), ReceiptHandleError()):
            assert "amqp" not in repr(error) and "password" not in repr(error).lower()

    scenario(body)
