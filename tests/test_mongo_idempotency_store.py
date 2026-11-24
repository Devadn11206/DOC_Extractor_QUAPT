"""
MongoDB idempotency store and Redis lock-lease tests (Team Member 3, Phase 3).

MOCKED: MongoDB is the in-memory fake of ``tests/mongo_fakes.py`` (unique indexes enforced, atomic single-document operations) and
Redis is ``fakeredis``. Replicas are threads that share one fake MongoDB and one fake Redis server; the synchronous store is
exercised exactly as ingestion uses it, from worker threads, while its connection's event loop runs in a background thread.
These tests do NOT prove behaviour of a real server: ``tests/test_mongo_integration.py`` and ``tests/test_live_services.py``
(real MongoDB, real Redis) do, and skip without them.
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("pymongo", reason="the 'mongo' extra provides the driver whose error types these tests map")
fakeredis = pytest.importorskip("fakeredis", reason="fakeredis stands in for Redis")
from pymongo import errors as driver  # noqa: E402

from doc_extractor.distributed_state import LockTimeoutError, RedisLockProvider  # noqa: E402
from doc_extractor.exceptions import ConflictError, StorageError  # noqa: E402
from doc_extractor.idempotency import IdempotencyStore  # noqa: E402
from doc_extractor.infrastructure.mongo import (  # noqa: E402
    DEFAULT_REGISTRY,
    MongoConnection,
    MongoSettings,
    PersistenceConfigurationError,
    PersistenceError,
    PersistenceUnavailableError,
    TenantScopeError,
    UnsafeQueryError,
    ensure_indexes,
)
from doc_extractor.infrastructure.mongo.idempotency import MongoIdempotencyStore  # noqa: E402
from doc_extractor.infrastructure.mongo.indexes import IDEMPOTENCY_RECORDS  # noqa: E402
from doc_extractor.models import IdempotencyRecord  # noqa: E402
from tests.mongo_fakes import FakeClient, factory_for  # noqa: E402


class LoopThread:
    """The 'application event loop': owns the fake MongoDB connection; sync calls from other threads are submitted to it."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=10)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()


class Env:
    def __init__(self, loop, client, connection):
        self.loop, self.client, self.connection = loop, client, connection

    def store(self, **options):
        store = MongoIdempotencyStore(self.connection, **options)
        self.loop.run(store.start())
        return store

    @property
    def collection(self):
        return self.client["doc"][IDEMPOTENCY_RECORDS]


@pytest.fixture
def env():
    loop = LoopThread()
    client = FakeClient()

    async def prepare():
        connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(client))
        await connection.connect()
        await ensure_indexes(client["doc"], DEFAULT_REGISTRY)
        return connection

    yield Env(loop, client, loop.run(prepare()))
    loop.close()


@pytest.fixture
def server():
    return fakeredis.FakeServer()


def record(tenant="tenant_a", key="key-1", document_id="doc_1", fingerprint="fp", ttl_seconds=86400.0, expired=False):
    now = datetime.now(timezone.utc).timestamp()
    created = now - 10 if not expired else now - 2 * 86400
    return IdempotencyRecord(
        idempotency_key=key, tenant_id=tenant, request_fingerprint=fingerprint, document_id=document_id,
        response_payload={"document_id": document_id, "nested": {"dotted.key": 1}}, created_at_utc=created, expires_at_utc=created + ttl_seconds,
    )


# ---- the interface and the basics ------------------------------------------------------------------------------------------

def test_the_store_is_an_idempotency_store(env):
    assert isinstance(env.store(), IdempotencyStore)


def test_a_saved_record_is_read_back_exactly_including_awkward_payload_keys(env):
    store = env.store()
    saved = record()
    store.save_record(saved)
    assert store.get_record("tenant_a", "key-1") == saved
    assert store.get_record("tenant_a", "other-key") is None


def test_delete_removes_only_that_tenants_record(env):
    store = env.store()
    store.save_record(record(tenant="tenant_a"))
    store.save_record(record(tenant="tenant_b"))
    store.delete_record("tenant_a", "key-1")
    assert store.get_record("tenant_a", "key-1") is None and store.get_record("tenant_b", "key-1") is not None
    store.delete_record("tenant_a", "key-1")                      # deleting nothing is harmless


def test_an_expired_record_is_not_returned_and_a_new_request_replaces_it(env):
    store = env.store()
    store.save_record(record(expired=True, document_id="doc_old"))
    assert store.get_record("tenant_a", "key-1") is None
    store.save_record(record(document_id="doc_new", fingerprint="fp2"))
    assert store.get_record("tenant_a", "key-1").document_id == "doc_new"
    assert len(env.collection.docs) == 1


def test_the_document_stores_a_key_hash_a_ttl_date_and_exact_microsecond_expiry(env):
    store = env.store()
    saved = record(key="plain-text-key")
    store.save_record(saved)
    [doc] = env.collection.docs
    assert doc["tenant_id"] == "tenant_a" and len(doc["key_hash"]) == 64 and "plain-text-key" != doc["key_hash"]
    assert "idempotency_key" not in doc and doc["expires_at_us"] == int(round(saved.expires_at_utc * 1_000_000))
    assert isinstance(doc["expires_at"], datetime)


# ---- tenant isolation ------------------------------------------------------------------------------------------------------

def test_the_same_key_in_two_tenants_is_two_independent_records(env):
    store = env.store()
    store.save_record(record(tenant="tenant_a", document_id="doc_a"))
    store.save_record(record(tenant="tenant_b", document_id="doc_b", fingerprint="other"))
    assert store.get_record("tenant_a", "key-1").document_id == "doc_a"
    assert store.get_record("tenant_b", "key-1").document_id == "doc_b"
    assert store.get_record("tenant_c", "key-1") is None


def test_every_query_names_the_tenant_and_an_invalid_tenant_is_refused_first(env):
    store = env.store()
    env.collection.calls.clear()
    store.save_record(record())
    store.get_record("tenant_a", "key-1")
    store.delete_record("tenant_a", "key-1")
    assert all(c[1].get("tenant_id") == "tenant_a" for c in env.collection.calls if c[0] != "insert_one")
    env.collection.calls.clear()
    for bad in ("", "a/../b", None):
        with pytest.raises(TenantScopeError):
            store.get_record(bad, "key-1")
    assert env.collection.calls == []


@pytest.mark.parametrize("key", ["", None, "x" * 4097, 5])
def test_an_invalid_key_is_refused_with_a_typed_error(env, key):
    store = env.store()
    with pytest.raises(UnsafeQueryError):
        store.get_record("tenant_a", key)


# ---- durable uniqueness ------------------------------------------------------------------------------------------------------

def test_a_different_request_cannot_take_a_live_key(env):
    store = env.store()
    store.save_record(record(document_id="doc_1", fingerprint="fp1"))
    with pytest.raises(ConflictError) as raised:
        store.save_record(record(document_id="doc_2", fingerprint="fp2"))
    assert "key-1" not in str(raised.value) and "key-1" not in str(raised.value.details)
    assert store.get_record("tenant_a", "key-1").document_id == "doc_1" and len(env.collection.docs) == 1


def test_the_same_request_saving_again_is_a_replay_not_a_conflict(env):
    store = env.store()
    store.save_record(record())
    store.save_record(record())
    assert len(env.collection.docs) == 1


def test_many_replicas_racing_on_one_key_leave_exactly_one_record_and_one_winner(env):
    store = env.store()
    outcomes = []

    def replica(n):
        try:
            store.save_record(record(document_id=f"doc_{n}", fingerprint=f"fp_{n}"))
            outcomes.append("saved")
        except ConflictError:
            outcomes.append("conflict")

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(replica, range(16)))
    assert outcomes.count("saved") == 1 and outcomes.count("conflict") == 15
    assert len(env.collection.docs) == 1
    assert store.get_record("tenant_a", "key-1").document_id == env.collection.docs[0]["document_id"]


def test_two_stores_over_the_same_database_agree_as_two_replicas(env):
    one, two = env.store(), env.store()
    one.save_record(record(document_id="doc_1", fingerprint="fp1"))
    assert two.get_record("tenant_a", "key-1").document_id == "doc_1"
    with pytest.raises(ConflictError):
        two.save_record(record(document_id="doc_2", fingerprint="fp2"))


def test_when_a_redis_lock_lease_expires_mid_work_the_unique_record_still_admits_one_request(env, server):
    """The lock is coordination only: replica A's lease expires while it works, replica B enters, and still only one request wins."""
    locks_a = RedisLockProvider(fakeredis.FakeRedis(server=server), lease_ms=60, acquire_timeout_seconds=2.0, retry_interval_seconds=0.01)
    locks_b = RedisLockProvider(fakeredis.FakeRedis(server=server), lease_ms=60, acquire_timeout_seconds=2.0, retry_interval_seconds=0.01)
    replica_a, replica_b = env.store(locks=locks_a), env.store(locks=locks_b)
    entered_b = threading.Event()
    results = {}

    def a():
        with replica_a.get_key_lock("tenant_a", "key-1"):
            assert entered_b.wait(5)                                  # A keeps "working" until B is inside the same section
            try:
                replica_a.save_record(record(document_id="doc_a", fingerprint="fp_a"))
                results["a"] = "saved"
            except ConflictError:
                results["a"] = "conflict"

    def b():
        with replica_b.get_key_lock("tenant_a", "key-1"):            # only possible because A's 60 ms lease expired
            entered_b.set()
            try:
                replica_b.save_record(record(document_id="doc_b", fingerprint="fp_b"))
                results["b"] = "saved"
            except ConflictError:
                results["b"] = "conflict"

    ta = threading.Thread(target=a)
    ta.start()
    time.sleep(0.02)
    tb = threading.Thread(target=b)
    tb.start()
    ta.join(10)
    tb.join(10)
    assert sorted(results.values()) == ["conflict", "saved"], results
    assert len(env.collection.docs) == 1


# ---- the key lock ----------------------------------------------------------------------------------------------------------

def test_the_key_lock_has_the_rlock_interface_ingestion_uses_with_and_without_redis(env, server):
    for store in (env.store(), env.store(locks=RedisLockProvider(fakeredis.FakeRedis(server=server)))):
        lock = store.get_key_lock("tenant_a", "key-1")
        assert lock.acquire() is not False
        lock.release()
        with store.get_key_lock("tenant_a", "key-1"):
            with store.get_key_lock("tenant_a", "key-1"):             # re-entrant in one thread, like the local store's RLock
                pass


def test_a_redis_key_lock_excludes_another_replica_and_does_not_alias_tenant_and_key_pairs(env, server):
    first = env.store(locks=RedisLockProvider(fakeredis.FakeRedis(server=server), acquire_timeout_seconds=0.1, retry_interval_seconds=0.01))
    second = env.store(locks=RedisLockProvider(fakeredis.FakeRedis(server=server), acquire_timeout_seconds=0.1, retry_interval_seconds=0.01))
    with first.get_key_lock("tenant_a", "k"):
        with pytest.raises(LockTimeoutError):
            with second.get_key_lock("tenant_a", "k"):
                pass
        with second.get_key_lock("tenant_a", "k2"):                   # another key: free
            pass
        with second.get_key_lock("tenant_b", "k"):                    # another tenant, same key: free
            pass
    with first.get_key_lock("a:b", "c"):
        with second.get_key_lock("a", "b:c"):                         # a joined-string scheme would alias these two
            pass


def test_a_redis_outage_fails_the_lock_closed(env, server):
    class Down:
        def set(self, *a, **k):
            import redis

            raise redis.exceptions.ConnectionError("down redis://:SECRETPW@host")

    store = env.store(locks=RedisLockProvider(Down()))
    with pytest.raises(StorageError) as raised:
        store.get_key_lock("tenant_a", "k").acquire()
    assert "SECRETPW" not in repr(raised.value)


# ---- the synchronous bridge --------------------------------------------------------------------------------------------------

def test_a_blocking_call_on_the_event_loop_thread_is_refused_instead_of_deadlocking(env):
    store = env.store()

    async def on_the_loop():
        with pytest.raises(PersistenceError) as raised:
            store.get_record("tenant_a", "key-1")
        return raised.value.details["reason"]

    assert env.loop.run(on_the_loop()) == "blocking_idempotency_call_on_the_event_loop"


def test_the_asynchronous_methods_work_on_the_loop(env):
    store = env.store()

    async def body():
        await store.asave_record(record())
        assert (await store.aget_record("tenant_a", "key-1")).document_id == "doc_1"
        await store.adelete_record("tenant_a", "key-1")
        assert await store.aget_record("tenant_a", "key-1") is None

    env.loop.run(body())


def test_a_store_that_was_not_started_or_was_stopped_fails_closed(env):
    store = MongoIdempotencyStore(env.connection)
    with pytest.raises(PersistenceError) as raised:
        store.get_record("tenant_a", "key-1")
    assert raised.value.details["reason"] == "idempotency_store_is_not_started"
    started = env.store()
    started.stop()
    with pytest.raises(PersistenceError):
        started.save_record(record())
    assert not env.collection.docs


def test_a_call_that_takes_too_long_is_a_typed_timeout(env):
    store = env.store(call_timeout_seconds=0.05)

    async def slow(*args, **kwargs):
        await asyncio.sleep(2)

    store._store.find_one = slow
    with pytest.raises(PersistenceUnavailableError) as raised:
        store.get_record("tenant_a", "key-1")
    assert raised.value.details["reason"] == "idempotency_operation_timed_out"


def test_a_driver_failure_is_a_typed_error_and_nothing_is_swallowed(env):
    store = env.store()
    env.collection.fail_with = driver.AutoReconnect("lost connection to mongodb://u:SECRETPW@h")
    for call in (lambda: store.get_record("tenant_a", "k"), lambda: store.save_record(record()), lambda: store.delete_record("tenant_a", "k")):
        with pytest.raises(StorageError) as raised:
            call()
        assert "SECRETPW" not in repr(raised.value) and "SECRETPW" not in str(raised.value.details)


# ---- the unique index ---------------------------------------------------------------------------------------------------------

def test_without_the_unique_index_the_store_refuses_to_start_and_to_write():
    loop = LoopThread()
    try:
        client = FakeClient()

        async def prepare():
            connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(client))
            await connection.connect()
            return connection

        connection = loop.run(prepare())
        store = MongoIdempotencyStore(connection)
        with pytest.raises(PersistenceConfigurationError):
            loop.run(store.start())
        store._loop = loop.loop                                       # forced past the gate: the write path checks the index too
        with pytest.raises(PersistenceError) as raised:
            store.save_record(record())
        assert raised.value.details["reason"] == "idempotency_index_is_missing" and not client["doc"][IDEMPOTENCY_RECORDS].docs
    finally:
        loop.close()


def test_a_record_that_cannot_be_stored_is_a_data_error(env):
    store = env.store()
    bad = record()
    bad.response_payload["x"] = object()
    with pytest.raises(PersistenceError):
        store.save_record(bad)
    assert not env.collection.docs


# ---- Redis lock leases (explicit, long-running form) -----------------------------------------------------------------------

def test_a_lease_has_an_increasing_fencing_token_and_excludes_others(server):
    provider = RedisLockProvider(fakeredis.FakeRedis(server=server), acquire_timeout_seconds=0.05, retry_interval_seconds=0.01)
    first = provider.acquire_lease("job:1")
    with pytest.raises(LockTimeoutError):
        provider.acquire_lease("job:1")
    first.release()
    second = provider.acquire_lease("job:1")
    assert second.fencing_token == first.fencing_token + 1
    assert provider.acquire_lease("job:2").fencing_token == 1          # a separate counter per name
    second.release()


def test_a_lease_can_be_extended_while_owned_and_reports_loss_after_expiry_or_takeover(server):
    provider = RedisLockProvider(fakeredis.FakeRedis(server=server), lease_ms=80, acquire_timeout_seconds=0.01, retry_interval_seconds=0.005)
    lease = provider.acquire_lease("job:1")
    assert lease.still_held() and lease.extend(500)
    time.sleep(0.12)
    assert lease.still_held()                                          # the heartbeat renewed it past its original 80 ms
    short = provider.acquire_lease("job:2", lease_ms=30)
    time.sleep(0.06)
    assert not short.still_held() and not short.extend()               # expired: lost
    taker = provider.acquire_lease("job:2")
    assert not short.still_held() and not short.extend() and taker.still_held()
    short.release()                                                    # the stale holder's release must not free the taker's lock
    assert taker.still_held()
    taker.release()


def test_releasing_a_lease_twice_and_using_it_after_release_is_safe(server):
    provider = RedisLockProvider(fakeredis.FakeRedis(server=server))
    with provider.acquire_lease("job:1") as lease:
        assert lease.still_held()
    lease.release()
    assert not lease.still_held() and not lease.extend()
    provider.acquire_lease("job:1").release()


def test_unconfirmed_ownership_counts_as_lost(server):
    provider = RedisLockProvider(fakeredis.FakeRedis(server=server))
    lease = provider.acquire_lease("job:1")

    class Down:
        def get(self, *a, **k):
            import redis

            raise redis.exceptions.ConnectionError("down")

        def pipeline(self):
            import redis

            raise redis.exceptions.ConnectionError("down")

    provider._client = Down()
    assert lease.still_held() is False and lease.extend() is False


def test_lease_arguments_are_validated(server):
    provider = RedisLockProvider(fakeredis.FakeRedis(server=server))
    for kwargs in ({"lease_ms": 0}, {"timeout_seconds": -1}):
        with pytest.raises(ValueError):
            provider.acquire_lease("job:1", **kwargs)
    with pytest.raises(ValueError):
        provider.acquire_lease("")
