"""
Processing worker tests (Team Member 3, Phase 3).

MOCKED: broker double (``tests/queue_fakes.py``), real ``MongoProcessingLeaseStore`` over the MongoDB fake, ``fakeredis`` for the document
lock, and a VIRTUAL clock shared by broker and lease store. Heartbeats use an injected sleep so they run in a few event-loop turns. These
tests prove the worker's logic (heartbeat, fencing, settlement, shutdown); they are not RabbitMQ, Redis or MongoDB verification.
"""

import asyncio

import pytest

pytest.importorskip("pymongo", reason="the 'mongo' extra provides the driver whose error types these tests map")
fakeredis = pytest.importorskip("fakeredis", reason="fakeredis stands in for Redis")
from pymongo import errors as driver  # noqa: E402

from doc_extractor.api.contracts.records import DocumentProcessingMessage  # noqa: E402
from doc_extractor.api.ports.processing import LeaseLostError  # noqa: E402
from doc_extractor.distributed_state import RedisLockProvider  # noqa: E402
from doc_extractor.infrastructure.mongo import (  # noqa: E402
    DEFAULT_REGISTRY,
    MongoConnection,
    MongoSettings,
    PersistenceUnavailableError,
    ensure_indexes,
)
from doc_extractor.infrastructure.mongo.indexes import PROCESSING_JOBS  # noqa: E402
from doc_extractor.infrastructure.mongo.processing import MongoProcessingLeaseStore  # noqa: E402
from doc_extractor.infrastructure.queue import BrokerUnavailableError, RabbitMQQueue  # noqa: E402
from doc_extractor.infrastructure.queue.worker import PermanentProcessingError, ProcessingWorker  # noqa: E402
from tests.mongo_fakes import FakeClient, LogCapture, factory_for  # noqa: E402
from tests.queue_fakes import FakeBroker  # noqa: E402

MARKER = "Worker-Payload-Marker-4410"


async def turns(n=5):
    for _ in range(n):
        await asyncio.sleep(0)


async def until(predicate, limit=2000):
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition was not reached")


class Env:
    def __init__(self, broker, client, leases):
        self.broker, self.client, self.leases = broker, client, leases

    def queue(self, name="w1", **options):
        return RabbitMQQueue(self.broker, self.leases, worker_id=name, clock_us=self.broker, **options)

    @property
    def jobs(self):
        return self.client["doc"][PROCESSING_JOBS]

    async def snap(self, key="msg_1", tenant="tenant_a"):
        return await self.leases.snapshot(tenant, key)


def scenario(body):
    async def go():
        broker = FakeBroker()
        client = FakeClient()
        connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(client))
        await connection.connect()
        await ensure_indexes(client["doc"], DEFAULT_REGISTRY)
        env = Env(broker, client, MongoProcessingLeaseStore(connection, clock_us=broker))
        await broker.start()
        return await body(env)

    return asyncio.run(go())


def msg(n=1, *, tenant="tenant_a", max_retries=3, document=None, payload=None):
    return DocumentProcessingMessage(
        message_id=f"msg_{n}", document_id=document or f"doc_{n}", tenant_id=tenant, event_id=f"evt_{n}", max_retries=max_retries,
        payload=payload or {},
    )


async def instant(_seconds):
    await asyncio.sleep(0)


def worker(env, handler, name="w1", **options):
    options.setdefault("sleep", instant)
    options.setdefault("lease_seconds", 10)
    options.setdefault("heartbeat_seconds", 1)
    options.setdefault("backoff_base_seconds", 2)
    return ProcessingWorker(env.queue(name), handler, **options)


# ---- settlement --------------------------------------------------------------------------------------------------------------

def test_a_successful_handler_completes_and_acknowledges_the_job():
    async def body(env):
        seen = []

        async def handler(message, context):
            seen.append((message.message_id, context.tenant_id, context.document_id, context.fencing_token, context.attempt))

        w = worker(env, handler)
        await w._consumer.publish(msg(1))
        assert await w.run_once() == 1
        assert seen == [("msg_1", "tenant_a", "doc_1", 1, 1)]
        assert (await env.snap())["state"] == "completed" and env.broker.unacked == {} and w.stats["processed"] == 1

    scenario(body)


def test_a_failing_handler_requeues_with_exponential_backoff_and_the_next_attempt_runs():
    async def body(env):
        attempts = []

        async def handler(message, context):
            attempts.append(context.attempt)
            raise RuntimeError(f"boom {MARKER}")

        w = worker(env, handler)
        await w._consumer.publish(msg(1))
        with LogCapture("doc_extractor.queue") as logs:
            await w.run_once()
            assert env.broker.published[-1][0] == env.broker.topology.wait_queue(5)      # 2 s rounds UP to the 5 s tier
            first_delay = env.broker.published[-1][2]["x-not-before-us"] - env.broker.now_us
            assert first_delay == 2_000_000                                    # base 2 s after the first failure
            env.broker.advance(6)
            await w.run_once()
            second_delay = env.broker.published[-1][2]["x-not-before-us"] - env.broker.now_us
            assert second_delay == 4_000_000                                   # 2 * 2 ** 1
        assert attempts == [1, 2] and w.stats["failed"] == 2
        assert MARKER not in logs.text and "RuntimeError" in logs.text         # the error TYPE is logged, never its text

    scenario(body)


def test_retries_end_in_the_dead_letter_queue_after_max_retries_plus_one_attempts():
    async def body(env):
        calls = []

        async def handler(message, context):
            calls.append(context.attempt)
            raise RuntimeError("always")

        w = worker(env, handler, backoff_base_seconds=1)
        await w._consumer.publish(msg(1, max_retries=2))
        for _ in range(10):
            await w.run_once()
            env.broker.advance(10)
        assert calls == [1, 2, 3] and env.broker.dead_depth == 1 and env.broker.main_depth == 0
        assert (await env.snap())["state"] == "dead_lettered" and env.broker.unacked == {}

    scenario(body)


def test_a_permanent_failure_is_dead_lettered_at_once_without_retries():
    async def body(env):
        async def handler(message, context):
            raise PermanentProcessingError("unsupported document")

        w = worker(env, handler)
        await w._consumer.publish(msg(1))
        await w.run_once()
        assert env.broker.dead_depth == 1 and env.broker.main_depth == 0 and w.stats["dead_lettered"] == 1
        assert env.broker.dead_letters()[0][1]["x-dead-letter-reason"] == "rejected_by_worker"

    scenario(body)


def test_a_settlement_that_fails_twice_is_retried_and_then_succeeds():
    async def body(env):
        async def handler(message, context):
            return None

        w = worker(env, handler)
        await w._consumer.publish(msg(1))
        real = w._consumer._leases.mark_completed
        calls = {"n": 0}

        async def flaky(lease):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PersistenceUnavailableError("down")
            return await real(lease)

        w._consumer._leases.mark_completed = flaky
        await w.run_once()
        assert calls["n"] == 3 and (await env.snap())["state"] == "completed" and w.stats["processed"] == 1

    scenario(body)


def test_a_settlement_that_never_succeeds_is_reported_and_left_to_lease_expiry_and_redelivery():
    async def body(env):
        async def handler(message, context):
            return None

        w = worker(env, handler)
        await w._consumer.publish(msg(1))

        async def down(lease):
            raise PersistenceUnavailableError("down")

        w._consumer._leases.mark_completed = down
        with LogCapture("doc_extractor.queue") as logs:
            await w.run_once()
        assert w.stats["settle_failures"] == 1 and w.stats["processed"] == 0 and "could not be settled" in logs.text
        assert (await env.snap())["state"] == "leased"                              # nothing was falsely recorded as done

    scenario(body)


# ---- heartbeat and fencing ---------------------------------------------------------------------------------------------------

def test_the_lease_is_extended_while_a_long_handler_runs_so_nobody_can_take_the_job_over():
    async def body(env):
        gate = asyncio.Event()
        started = asyncio.Event()

        async def handler(message, context):
            started.set()
            await gate.wait()

        w = worker(env, handler, lease_seconds=10, heartbeat_seconds=1)
        await w._consumer.publish(msg(1))
        run = asyncio.ensure_future(w.run_once())
        await started.wait()
        first = (await env.snap())["lease_expires_at_us"]
        env.broker.advance(8)                                                       # 8 s of the 10 s lease are gone
        await until(lambda: env.jobs.docs[0]["lease_expires_at_us"] > first)       # a heartbeat renewed it to now + 10 s
        env.broker.advance(8)                                                       # 16 s after the claim: past the ORIGINAL lease
        other = env.queue("w2")
        await w._consumer.publish(msg(1))                                          # a duplicate delivery reaches another worker
        assert await other.receive_messages(1, 10) == []                           # ... and finds the job still held
        gate.set()
        await run
        assert (await env.snap())["state"] == "completed"

    scenario(body)


def test_a_worker_whose_lease_was_taken_over_is_stopped_and_discards_its_result():
    async def body(env):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        side_effects = []

        async def handler(message, context):
            started.set()
            try:
                await asyncio.sleep(30)
                side_effects.append("must not happen")
            except asyncio.CancelledError:
                cancelled.set()
                raise

        w = worker(env, handler, lease_seconds=10, heartbeat_seconds=1, sleep=lambda s: asyncio.sleep(0.02))
        await w._consumer.publish(msg(1))
        run = asyncio.ensure_future(w.run_once())
        await started.wait()
        env.broker.advance(11)                                                      # the lease expires with no heartbeat in between
        taken = await env.leases.claim("tenant_a", "msg_1", message_id="msg_1", document_id="doc_1", max_attempts=4, worker_id="w2", lease_seconds=60)
        assert taken.lease.fencing_token == 2
        await run                                                                   # the next heartbeat finds the lease lost
        assert cancelled.is_set() and side_effects == [] and w.stats["lost"] == 1 and w.stats["processed"] == 0
        snap = await env.snap()
        assert snap["state"] == "leased" and snap["lease_owner"] == "w2" and snap["fencing_token"] == 2     # the new owner is untouched

    scenario(body)


def test_ensure_held_fences_a_side_effect_after_a_takeover_and_reports_the_token():
    async def body(env):
        results = {}

        async def handler(message, context):
            await context.ensure_held()
            results["token"] = context.fencing_token
            env.broker.advance(11)
            await env.leases.claim("tenant_a", "msg_1", message_id="msg_1", document_id="doc_1", max_attempts=4, worker_id="w2", lease_seconds=60)
            try:
                await context.ensure_held()
            except LeaseLostError:
                results["fenced"] = True
                raise

        w = worker(env, handler, heartbeat_seconds=9, sleep=lambda s: asyncio.sleep(10))
        await w._consumer.publish(msg(1))
        await w.run_once()
        assert results == {"token": 1, "fenced": True} and (await env.snap())["lease_owner"] == "w2"

    scenario(body)


def test_a_lease_store_that_stays_unreachable_for_a_whole_lease_stops_the_handler_fail_closed():
    async def body(env):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def handler(message, context):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                stopped.set()
                raise

        w = worker(env, handler, lease_seconds=10, heartbeat_seconds=2, sleep=lambda s: asyncio.sleep(0.005))
        await w._consumer.publish(msg(1))
        run = asyncio.ensure_future(w.run_once())
        await started.wait()
        env.jobs.fail_with = driver.AutoReconnect("down")                           # five failed beats of 2 s = one whole lease
        await run
        env.jobs.fail_with = None
        assert stopped.is_set() and w.stats["lost"] == 1

    scenario(body)


# ---- the document lock --------------------------------------------------------------------------------------------------------

def test_the_document_lock_is_held_while_the_handler_runs_and_contention_defers_without_an_attempt():
    async def body(env):
        server = fakeredis.FakeServer()
        locks_a = RedisLockProvider(fakeredis.FakeRedis(server=server), acquire_timeout_seconds=0.05, retry_interval_seconds=0.01)
        locks_b = RedisLockProvider(fakeredis.FakeRedis(server=server), acquire_timeout_seconds=0.05, retry_interval_seconds=0.01)
        gate, started = asyncio.Event(), asyncio.Event()
        order = []

        async def slow(message, context):
            started.set()
            await gate.wait()
            order.append(message.message_id)

        async def quick(message, context):
            order.append(message.message_id)

        first = worker(env, slow, "w1", document_locks=locks_a, lock_retry_seconds=5)
        second = worker(env, quick, "w2", document_locks=locks_b, lock_retry_seconds=5)
        await first._consumer.publish(msg(1, document="doc_shared"))
        await first._consumer.publish(msg(2, document="doc_shared"))               # another job for the SAME document
        run = asyncio.ensure_future(first.run_once())
        await started.wait()
        assert await second.run_once() == 1                                         # received, but the document is busy
        assert second.stats["deferred"] == 1 and order == [] and (await env.snap("msg_2"))["attempt"] == 0
        gate.set()
        await run
        env.broker.advance(6)
        assert await second.run_once() == 1 and order == ["msg_1", "msg_2"]
        assert (await env.snap("msg_2"))["state"] == "completed"

    scenario(body)


def test_losing_the_document_lock_stops_the_handler_like_a_lost_lease():
    async def body(env):
        server = fakeredis.FakeServer()
        redis_client = fakeredis.FakeRedis(server=server)
        locks = RedisLockProvider(redis_client, acquire_timeout_seconds=0.05, retry_interval_seconds=0.01)
        started, stopped = asyncio.Event(), asyncio.Event()

        async def handler(message, context):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                stopped.set()
                raise

        w = worker(env, handler, document_locks=locks, sleep=lambda s: asyncio.sleep(0.01))
        await w._consumer.publish(msg(1))
        run = asyncio.ensure_future(w.run_once())
        await started.wait()
        for key in redis_client.keys("*lock*"):
            if not key.endswith(b":fence"):
                redis_client.delete(key)                                            # the lock lease "expired" under the running job
        await run
        assert stopped.is_set() and w.stats["lost"] == 1 and (await env.snap())["state"] == "leased"

    scenario(body)


def test_a_redis_outage_defers_the_job_instead_of_running_it_unprotected():
    async def body(env):
        import redis

        class Down:
            def set(self, *a, **k):
                raise redis.exceptions.ConnectionError("down")

        ran = []

        async def handler(message, context):
            ran.append(1)

        w = worker(env, handler, document_locks=RedisLockProvider(Down()))
        await w._consumer.publish(msg(1))
        await w.run_once()
        assert ran == [] and w.stats["deferred"] == 1 and (await env.snap())["attempt"] == 0

    scenario(body)


# ---- the loop and shutdown -----------------------------------------------------------------------------------------------------

def test_the_loop_processes_jobs_until_stopped_and_survives_a_failing_broker():
    async def body(env):
        done = []

        async def handler(message, context):
            done.append(message.message_id)

        w = worker(env, handler, poll_interval_seconds=0.005, error_pause_seconds=0.005, max_concurrency=2, batch_size=2)
        await w._consumer.publish_batch([msg(n) for n in range(1, 5)])
        env.broker.fail["get"] = BrokerUnavailableError("broker_get_failed")
        stop = asyncio.Event()
        run = asyncio.ensure_future(w.run(stop))
        await asyncio.sleep(0.03)
        assert done == []                                                           # the outage did not kill the loop
        env.broker.fail["get"] = None
        await until(lambda: len(done) == 4)
        stop.set()
        await run
        assert sorted(done) == ["msg_1", "msg_2", "msg_3", "msg_4"] and env.broker.unacked == {}

    scenario(body)


def test_concurrency_is_bounded():
    async def body(env):
        running, peak, done = 0, 0, []

        async def handler(message, context):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.005)
            running -= 1
            done.append(1)

        w = worker(env, handler, poll_interval_seconds=0.002, max_concurrency=3, batch_size=3)
        await w._consumer.publish_batch([msg(n) for n in range(1, 11)])
        stop = asyncio.Event()
        run = asyncio.ensure_future(w.run(stop))
        await until(lambda: len(done) == 10)
        stop.set()
        await run
        assert peak == 3

    scenario(body)


def test_shutdown_waits_for_the_grace_period_then_gives_running_jobs_back_without_using_an_attempt():
    async def body(env):
        started = asyncio.Event()

        async def handler(message, context):
            started.set()
            await asyncio.sleep(60)

        w = worker(env, handler, poll_interval_seconds=0.005, shutdown_grace_seconds=0.05)
        await w._consumer.publish(msg(1))
        stop = asyncio.Event()
        run = asyncio.ensure_future(w.run(stop))
        await started.wait()
        stop.set()
        await run
        snap = await env.snap()
        assert snap["state"] == "queued" and snap["attempt"] == 0                  # the deployment did not eat into max_retries
        assert env.broker.main_depth == 1 and env.broker.unacked == {} and w.stats["deferred"] == 1
        again = env.queue("w2")
        got = await again.receive_messages(1, 30)
        assert len(got) == 1 and got[0].approximate_receive_count == 1

    scenario(body)


def test_a_job_that_finishes_inside_the_grace_period_is_acknowledged_on_shutdown():
    async def body(env):
        started = asyncio.Event()

        async def handler(message, context):
            started.set()
            await asyncio.sleep(0.02)

        w = worker(env, handler, poll_interval_seconds=0.005, shutdown_grace_seconds=2)
        await w._consumer.publish(msg(1))
        stop = asyncio.Event()
        run = asyncio.ensure_future(w.run(stop))
        await started.wait()
        stop.set()
        await run
        assert (await env.snap())["state"] == "completed" and w.stats["processed"] == 1

    scenario(body)


# ---- tenants and validation ---------------------------------------------------------------------------------------------------

def test_jobs_of_two_tenants_with_the_same_ids_are_processed_independently():
    async def body(env):
        seen = []

        async def handler(message, context):
            seen.append((message.tenant_id, context.fencing_token))

        w = worker(env, handler, batch_size=2, max_concurrency=2)
        await w._consumer.publish(msg(1, tenant="tenant_a"))
        await w._consumer.publish(msg(1, tenant="tenant_b"))
        await w.run_once()
        assert sorted(seen) == [("tenant_a", 1), ("tenant_b", 1)]
        assert (await env.snap(tenant="tenant_a"))["state"] == "completed" and (await env.snap(tenant="tenant_b"))["state"] == "completed"

    scenario(body)


@pytest.mark.parametrize(
    "options",
    [{"lease_seconds": 4}, {"heartbeat_seconds": 0}, {"heartbeat_seconds": 20, "lease_seconds": 10}, {"batch_size": 0},
     {"max_concurrency": 101}],
)
def test_worker_options_are_validated(options):
    async def handler(message, context):
        return None

    with pytest.raises(ValueError):
        ProcessingWorker(object(), handler, **options)
