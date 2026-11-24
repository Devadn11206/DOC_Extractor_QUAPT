"""
REAL-SERVICE integration tests for the durable queue, the lease store and distributed idempotency (Team Member 3, Phase 3). OPT-IN.

They run only against real servers named by environment variables; without a variable the tests that need that service SKIP with
their reason, and a skipped test proves nothing. Nothing here uses a fake: a pass means the real broker, database or Redis behaved as
the adapter assumes. The CI job ``queue-integration`` provides all three and fails on any skip.

    DOC_TEST_MONGO_URI      a MongoDB replica set the test may create databases in   (e.g. mongodb://localhost:27017/?replicaSet=rs0)
    DOC_TEST_RABBITMQ_URL   a RabbitMQ the test may declare queues in                (e.g. amqp://guest:guest@localhost:5672/)
    DOC_TEST_REDIS_URL      a Redis the test may use                                 (e.g. redis://localhost:6379/0)

Every test uses its own database name and its own queue-name prefix (``doc_it_<random>``) and removes them afterwards. The queue tests
use short delay tiers and short leases so they take seconds, not minutes. RabbitMQ quorum queues are the WORKING BASELINE, not an
approved architecture decision: these tests verify the baseline, they do not approve it.
"""

import asyncio
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

MONGO_URI = os.environ.get("DOC_TEST_MONGO_URI", "").strip()
RABBITMQ_URL = os.environ.get("DOC_TEST_RABBITMQ_URL", "").strip()
REDIS_URL = os.environ.get("DOC_TEST_REDIS_URL", "").strip()

needs_mongo = pytest.mark.skipif(not MONGO_URI, reason="set DOC_TEST_MONGO_URI to run against a real MongoDB replica set")
needs_broker = pytest.mark.skipif(
    not (MONGO_URI and RABBITMQ_URL), reason="set DOC_TEST_MONGO_URI and DOC_TEST_RABBITMQ_URL to run against a real RabbitMQ and MongoDB"
)
needs_redis = pytest.mark.skipif(not (MONGO_URI and REDIS_URL), reason="set DOC_TEST_MONGO_URI and DOC_TEST_REDIS_URL to run against a real Redis and MongoDB")

if MONGO_URI:
    from pydantic import SecretStr

    from doc_extractor.api.contracts.records import DocumentProcessingMessage
    from doc_extractor.api.ports.processing import ClaimStatus, LeaseLostError
    from doc_extractor.exceptions import ConflictError
    from doc_extractor.infrastructure.mongo import MongoConnection, MongoSettings, ensure_indexes
    from doc_extractor.infrastructure.mongo.idempotency import MongoIdempotencyStore
    from doc_extractor.infrastructure.mongo.indexes import IDEMPOTENCY_RECORDS, PROCESSING_JOBS
    from doc_extractor.infrastructure.mongo.processing import MongoProcessingLeaseStore
    from doc_extractor.models import IdempotencyRecord
if MONGO_URI and RABBITMQ_URL:
    from doc_extractor.infrastructure.queue import AioPikaTransport, BrokerPublishError, BrokerSettings, RabbitMQQueue
if MONGO_URI and REDIS_URL:
    from doc_extractor.distributed_state import LockTimeoutError, RedisLockProvider, connect_redis

TIERS = (1, 2, 5)


def with_database(body):
    """Run ``body(connection)`` against a fresh database with every index, then drop it. One event loop per call."""

    async def go():
        settings = MongoSettings(uri=MONGO_URI, database=f"doc_it_{uuid.uuid4().hex[:12]}", app_env="development")
        connection = MongoConnection(settings)
        await connection.connect()
        try:
            await ensure_indexes(connection.database)
            return await body(connection)
        finally:
            await connection.client.drop_database(settings.database)
            await connection.aclose()

    return asyncio.run(go())


def msg(n, *, tenant="tenant_a", dedup=None, max_retries=3):
    return DocumentProcessingMessage(
        message_id=f"msg_{n}", document_id=f"doc_{n}", tenant_id=tenant, event_id=f"evt_{n}", deduplication_id=dedup, max_retries=max_retries
    )


def transport_for(prefix, **options):
    settings = BrokerSettings(url=SecretStr(RABBITMQ_URL), queue_prefix=prefix, delay_tiers_seconds=TIERS, connect_timeout_seconds=10, **options)
    return AioPikaTransport(settings)


async def clean_queues(transport):
    for queue in list(transport._queues.values()):
        try:
            await queue.delete(if_unused=False, if_empty=False)
        except Exception:                                               # noqa: BLE001 - cleanup only
            pass


async def eventually(call, *, seconds=15.0, interval=0.25):
    deadline = time.monotonic() + seconds
    while True:
        result = await call()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(interval)


def rabbit_scenario(body):
    """A fresh queue prefix and a fresh database; ``body(env)`` gets the connection, the lease store and a queue factory."""

    async def inner(connection):
        prefix = f"doc_it_{uuid.uuid4().hex[:10]}"
        leases = MongoProcessingLeaseStore(connection)
        made = []

        async def make(worker_id):
            transport = transport_for(prefix)
            queue = RabbitMQQueue(transport, leases, worker_id=worker_id)
            await queue.start()
            made.append((queue, transport))
            return queue

        try:
            return await body(type("Env", (), {"connection": connection, "leases": leases, "make": staticmethod(make), "prefix": prefix}))
        finally:
            if made:
                await clean_queues(made[0][1])
            for queue, _ in made:
                await queue.close()

    return with_database(inner)


# ---- MongoDB: leases ---------------------------------------------------------------------------------------------------------

@needs_mongo
def test_real_mongodb_exactly_one_of_many_concurrent_first_claims_wins_and_the_ttl_index_exists():
    async def body(connection):
        leases = MongoProcessingLeaseStore(connection)
        results = await asyncio.gather(*(
            leases.claim("tenant_a", "job_1", message_id="m", document_id="d", max_attempts=3, worker_id=f"w{i}", lease_seconds=30)
            for i in range(25)
        ))
        statuses = [r.status for r in results]
        assert statuses.count(ClaimStatus.ACQUIRED) == 1 and statuses.count(ClaimStatus.HELD) == 24
        assert await connection.collection(PROCESSING_JOBS).count_documents({}) == 1
        info = await connection.collection(PROCESSING_JOBS).index_information()
        assert info["pj_tenant_job_uq"]["unique"] is True and info["pj_expires_ttl"]["expireAfterSeconds"] == 0

    with_database(body)


@needs_mongo
def test_real_mongodb_an_expired_lease_has_one_taker_and_the_stale_worker_is_fenced():
    async def body(connection):
        leases = MongoProcessingLeaseStore(connection)
        stale = (await leases.claim("tenant_a", "job_1", message_id="m", document_id="d", max_attempts=3, worker_id="w0", lease_seconds=1)).lease
        await asyncio.sleep(1.3)
        results = await asyncio.gather(*(
            leases.claim("tenant_a", "job_1", message_id="m", document_id="d", max_attempts=3, worker_id=f"w{i}", lease_seconds=30)
            for i in range(1, 16)
        ))
        winners = [r for r in results if r.status is ClaimStatus.ACQUIRED]
        assert len(winners) == 1 and winners[0].lease.fencing_token == 2 and winners[0].lease.attempt == 2
        for call in (leases.mark_completed(stale), leases.extend(stale, 30), leases.verify(stale)):
            with pytest.raises(LeaseLostError):
                await call
        await leases.mark_completed(winners[0].lease)
        assert (await leases.snapshot("tenant_a", "job_1"))["state"] == "completed"

    with_database(body)


@needs_mongo
def test_real_mongodb_tenants_with_the_same_job_key_are_independent():
    async def body(connection):
        leases = MongoProcessingLeaseStore(connection)
        a = (await leases.claim("tenant_a", "job_1", message_id="m", document_id="d", max_attempts=3, worker_id="w1", lease_seconds=30)).lease
        b = (await leases.claim("tenant_b", "job_1", message_id="m", document_id="d", max_attempts=3, worker_id="w1", lease_seconds=30)).lease
        await leases.mark_completed(a)
        assert (await leases.snapshot("tenant_b", "job_1"))["state"] == "leased"
        await leases.verify(b)

    with_database(body)


# ---- MongoDB: idempotency ----------------------------------------------------------------------------------------------------

def _record(key="key-1", document_id="doc_1", fingerprint="fp", ttl=86400.0, tenant="tenant_a"):
    now = datetime.now(timezone.utc).timestamp()
    return IdempotencyRecord(key, tenant, fingerprint, document_id, {"document_id": document_id}, now, now + ttl)


@needs_mongo
def test_real_mongodb_racing_replicas_leave_one_idempotency_record_and_it_survives_a_new_connection():
    results = {"saved": 0, "conflict": 0}

    async def body(connection):
        loop = asyncio.get_running_loop()
        store = MongoIdempotencyStore(connection)
        await store.start()
        lock = threading.Lock()

        def replica(n):
            try:
                store.save_record(_record(document_id=f"doc_{n}", fingerprint=f"fp_{n}"))
                outcome = "saved"
            except ConflictError:
                outcome = "conflict"
            with lock:
                results[outcome] += 1

        await asyncio.gather(*(loop.run_in_executor(None, replica, n) for n in range(12)))
        assert results == {"saved": 1, "conflict": 11}
        assert await connection.collection(IDEMPOTENCY_RECORDS).count_documents({}) == 1
        again = MongoConnection(connection.settings)
        await again.connect()
        try:
            other = MongoIdempotencyStore(again)
            await other.start()
            winner = await other.aget_record("tenant_a", "key-1")
            assert winner is not None and await other.aget_record("tenant_b", "key-1") is None
        finally:
            await again.aclose()

    with_database(body)


@needs_mongo
def test_real_mongodb_an_expired_idempotency_record_is_replaced_and_the_ttl_index_exists():
    async def body(connection):
        store = MongoIdempotencyStore(connection)
        await store.start()
        await store.asave_record(_record(document_id="doc_old", ttl=1.0))
        await asyncio.sleep(1.2)
        assert await store.aget_record("tenant_a", "key-1") is None
        await store.asave_record(_record(document_id="doc_new", fingerprint="fp2"))
        assert (await store.aget_record("tenant_a", "key-1")).document_id == "doc_new"
        info = await connection.collection(IDEMPOTENCY_RECORDS).index_information()
        assert info["idem_tenant_key_uq"]["unique"] is True and info["idem_expires_ttl"]["expireAfterSeconds"] == 0

    with_database(body)


# ---- Redis ---------------------------------------------------------------------------------------------------------------------

@needs_redis
def test_real_redis_lock_leases_have_increasing_fencing_tokens_and_a_stale_holder_cannot_free_a_taken_lock():
    client = connect_redis(REDIS_URL)
    namespace = f"doc_it_{uuid.uuid4().hex[:10]}"
    provider = RedisLockProvider(client, namespace=namespace, acquire_timeout_seconds=0.1, retry_interval_seconds=0.02)
    try:
        first = provider.acquire_lease("job:1", lease_ms=300)
        with pytest.raises(LockTimeoutError):
            provider.acquire_lease("job:1")
        assert first.still_held() and first.extend(2000)
        first.release()
        short = provider.acquire_lease("job:1", lease_ms=200)
        assert short.fencing_token == first.fencing_token + 1
        time.sleep(0.4)
        assert not short.still_held() and not short.extend()
        taker = provider.acquire_lease("job:1", lease_ms=5000)
        short.release()
        assert taker.still_held() and taker.fencing_token == short.fencing_token + 1
        taker.release()
    finally:
        for key in client.scan_iter(f"{namespace}:*"):
            client.delete(key)
        client.close()


@needs_redis
def test_real_redis_and_mongodb_a_lock_that_expires_mid_work_still_leaves_one_idempotency_record():
    client_a, client_b = connect_redis(REDIS_URL), connect_redis(REDIS_URL)
    namespace = f"doc_it_{uuid.uuid4().hex[:10]}"
    outcomes = {}

    async def body(connection):
        loop = asyncio.get_running_loop()
        locks_a = RedisLockProvider(client_a, namespace=namespace, lease_ms=200, acquire_timeout_seconds=5, retry_interval_seconds=0.02)
        locks_b = RedisLockProvider(client_b, namespace=namespace, lease_ms=200, acquire_timeout_seconds=5, retry_interval_seconds=0.02)
        a, b = MongoIdempotencyStore(connection, locks=locks_a), MongoIdempotencyStore(connection, locks=locks_b)
        await a.start()
        await b.start()
        entered = threading.Event()

        def run(name, store, wait_for_other):
            with store.get_key_lock("tenant_a", "key-1"):
                if wait_for_other:
                    entered.wait(10)                                    # A "works" past its 200 ms lease until B is inside
                else:
                    entered.set()
                try:
                    store.save_record(_record(document_id=f"doc_{name}", fingerprint=f"fp_{name}"))
                    outcomes[name] = "saved"
                except ConflictError:
                    outcomes[name] = "conflict"

        first = loop.run_in_executor(None, run, "a", a, True)
        await asyncio.sleep(0.05)
        second = loop.run_in_executor(None, run, "b", b, False)
        await asyncio.gather(first, second)
        assert sorted(outcomes.values()) == ["conflict", "saved"]
        assert await connection.collection(IDEMPOTENCY_RECORDS).count_documents({}) == 1

    try:
        with_database(body)
    finally:
        for key in client_a.scan_iter(f"{namespace}:*"):
            client_a.delete(key)
        client_a.close()
        client_b.close()


# ---- RabbitMQ ------------------------------------------------------------------------------------------------------------------

@needs_broker
def test_real_rabbitmq_declares_the_topology_idempotently_and_refuses_an_unroutable_publish():
    async def body(env):
        queue = await env.make("w1")
        await queue.start()                                             # a second declaration with identical arguments is accepted
        transport = queue._transport
        with pytest.raises(BrokerPublishError):                          # mandatory publishing: no such queue is an error, not a silent drop
            await transport.publish("no.such.queue", b"{}", message_id="m", correlation_id=None, headers={})
        assert await queue.health() is True

    rabbit_scenario(body)


@needs_broker
def test_real_rabbitmq_a_message_is_published_received_with_a_lease_and_acknowledged():
    async def body(env):
        queue = await env.make("w1")
        assert await queue.publish(msg(1)) == "msg_1"
        envelopes = await eventually(lambda: queue.receive_messages(1, 30))
        assert envelopes[0].message.message_id == "msg_1" and envelopes[0].approximate_receive_count == 1
        await queue.extend_lease(envelopes[0].receipt_handle, 60)
        await queue.ack(envelopes[0].receipt_handle)
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "completed"
        await queue.publish(msg(1))                                      # a duplicate of the completed job is dropped
        await asyncio.sleep(1.0)
        assert await queue.receive_messages(1, 30) == []

    rabbit_scenario(body)


@needs_broker
def test_real_rabbitmq_a_retry_with_backoff_comes_back_through_the_wait_queue_as_the_next_attempt():
    async def body(env):
        queue = await env.make("w1")
        await queue.publish(msg(1))
        first = (await eventually(lambda: queue.receive_messages(1, 30)))[0]
        started = time.monotonic()
        await queue.nack(first.receipt_handle, requeue=True, backoff_seconds=1)
        second = (await eventually(lambda: queue.receive_messages(1, 30), seconds=20))[0]
        assert second.approximate_receive_count == 2 and time.monotonic() - started >= 0.9        # not early
        await queue.ack(second.receipt_handle)

    rabbit_scenario(body)


@needs_broker
def test_real_rabbitmq_a_delayed_publish_is_not_visible_early():
    async def body(env):
        queue = await env.make("w1")
        await queue.publish(msg(1), delay_seconds=2)
        assert await queue.receive_messages(1, 30) == []
        started = time.monotonic()
        envelopes = await eventually(lambda: queue.receive_messages(1, 30), seconds=20)
        assert envelopes and time.monotonic() - started >= 0.5
        await queue.ack(envelopes[0].receipt_handle)

    rabbit_scenario(body)


@needs_broker
def test_real_rabbitmq_exhausted_retries_and_poison_messages_reach_the_dead_letter_queue():
    async def body(env):
        queue = await env.make("w1")
        await queue.publish(msg(1, max_retries=0))
        first = (await eventually(lambda: queue.receive_messages(1, 30)))[0]
        await queue.nack(first.receipt_handle, requeue=True)                 # no retries left: dead-lettered
        await queue._transport.publish(                                      # a foreign publisher puts garbage on the main queue
            queue._transport.topology.main, b"not json", message_id="raw", correlation_id=None, headers={"tenant_id": "tenant_a"})
        assert await eventually(lambda: _no_work(queue))
        dead = []
        for _ in range(2):
            delivery = await eventually(lambda: queue._transport.get(queue._transport.topology.dead))
            dead.append(delivery.headers["x-dead-letter-reason"])
            await queue._transport.ack(delivery)
        assert sorted(dead) == ["message_is_not_valid", "retries_exhausted"]
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "dead_lettered"

    rabbit_scenario(body)


async def _no_work(queue):
    return not await queue.receive_messages(1, 30) and True


@needs_broker
def test_real_rabbitmq_after_a_worker_connection_is_lost_the_job_is_taken_over_once_the_lease_expired():
    async def body(env):
        dying = await env.make("w1")
        await dying.publish(msg(1))
        lost = (await eventually(lambda: dying.receive_messages(1, 2)))[0]    # a 2 s lease
        await dying._transport._connection.close()                           # the connection dies: the broker requeues the delivery
        survivor = await env.make("w2")
        taken = (await eventually(lambda: survivor.receive_messages(1, 30), seconds=30))
        assert taken[0].approximate_receive_count == 2
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["fencing_token"] == 2
        with pytest.raises(Exception):
            await dying.ack(lost.receipt_handle)                             # the dead worker is fenced (and its channel is gone)
        await survivor.ack(taken[0].receipt_handle)
        assert (await env.leases.snapshot("tenant_a", "msg_1"))["state"] == "completed"

    rabbit_scenario(body)


@needs_broker
def test_real_rabbitmq_many_workers_process_every_job_exactly_once_and_tenants_stay_separate():
    async def body(env):
        workers = [await env.make(f"w{i}") for i in range(3)]
        for n in range(1, 16):
            await workers[0].publish(msg(n, tenant="tenant_a" if n % 2 else "tenant_b"))
        seen = []

        async def run(worker):
            idle = 0
            while idle < 8:
                batch = await worker.receive_messages(2, 30)
                idle = 0 if batch else idle + 1
                for envelope in batch:
                    seen.append((envelope.message.tenant_id, envelope.message.message_id))
                    await worker.ack(envelope.receipt_handle)
                if not batch:
                    await asyncio.sleep(0.2)

        await asyncio.gather(*(run(w) for w in workers))
        assert len(seen) == 15 and len(set(seen)) == 15
        for tenant, message_id in seen:
            assert (await env.leases.snapshot(tenant, message_id))["state"] == "completed"

    rabbit_scenario(body)
