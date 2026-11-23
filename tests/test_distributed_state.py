"""
Distributed locks and idempotency across replicas (PRD ARC-003, NFR-006, FR-ING-006).

Each "replica" is its own Redis client; they share one fakeredis server, so lock contention between replicas is real.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import fakeredis
import pytest

from doc_extractor.distributed_state import (
    LockTimeoutError,
    RedisIdempotencyStore,
    RedisLockProvider,
    tenant_lock_factory,
)
from doc_extractor.exceptions import StorageError
from doc_extractor.models import IdempotencyRecord
from doc_extractor.tenancy import InvalidTenantIdentifierError


@pytest.fixture
def server():
    return fakeredis.FakeServer()


def _client(server):
    return fakeredis.FakeRedis(server=server)


def _record(tenant="tenant_a", key="key-1", document_id="doc_1", ttl_seconds=86400.0, expired=False):
    now = datetime.now(timezone.utc).timestamp()
    created = now - 10 if not expired else now - 2 * 86400
    return IdempotencyRecord(
        idempotency_key=key,
        tenant_id=tenant,
        request_fingerprint="fp",
        document_id=document_id,
        response_payload={"document_id": document_id},
        created_at_utc=created,
        expires_at_utc=created + ttl_seconds,
    )


def test_a_lock_excludes_another_replica_until_it_is_released(server):
    replica_a = RedisLockProvider(_client(server), acquire_timeout_seconds=0.2, lease_ms=5000)
    replica_b = RedisLockProvider(_client(server), acquire_timeout_seconds=0.2, lease_ms=5000)
    with replica_a.lock("doc:1"):
        with pytest.raises(LockTimeoutError):
            with replica_b.lock("doc:1"):
                pass  # pragma: no cover - must not be reached
    with replica_b.lock("doc:1"):
        pass  # free again after release


def test_different_names_do_not_contend(server):
    provider_a = RedisLockProvider(_client(server), acquire_timeout_seconds=0.1)
    provider_b = RedisLockProvider(_client(server), acquire_timeout_seconds=0.1)
    with provider_a.lock("doc:1"):
        with provider_b.lock("doc:2"):
            pass


def test_a_lock_is_reentrant_within_one_thread(server):
    provider = RedisLockProvider(_client(server), acquire_timeout_seconds=0.1)
    with provider.lock("doc:1"):
        with provider.lock("doc:1"):  # the ingestion path takes the same key lock inside save_record
            pass
    other = RedisLockProvider(_client(server), acquire_timeout_seconds=0.1)
    with other.lock("doc:1"):  # fully released after the outer exit
        pass


def test_release_never_deletes_a_lock_that_another_holder_now_owns(server):
    client = _client(server)
    provider = RedisLockProvider(client, acquire_timeout_seconds=0.1, lease_ms=60000)
    with provider.lock("doc:1"):
        # Simulate lease expiry and takeover by another replica while this holder still runs.
        key = provider._key("doc:1")
        client.set(key, "someone-else", px=60000)
    # Our exit must leave the other holder's lock in place.
    assert client.get(key) == b"someone-else"


def test_an_expired_lease_frees_the_lock_for_the_next_replica(server):
    holder = RedisLockProvider(_client(server), acquire_timeout_seconds=0.1, lease_ms=50)
    waiter = RedisLockProvider(_client(server), acquire_timeout_seconds=2.0, lease_ms=5000)
    context = holder.lock("doc:1")
    context.__enter__()
    try:
        with waiter.lock("doc:1"):  # acquires once the 50 ms lease lapses
            pass
    finally:
        context.__exit__(None, None, None)


def test_lock_store_outage_fails_closed(server):
    class BrokenClient:
        def set(self, *args, **kwargs):
            from redis.exceptions import ConnectionError as RedisConnectionError

            raise RedisConnectionError("refused")

    provider = RedisLockProvider(BrokenClient(), acquire_timeout_seconds=0.1)
    with pytest.raises(StorageError):
        with provider.lock("doc:1"):
            pass  # pragma: no cover


def test_idempotency_record_round_trips_and_is_visible_to_every_replica(server):
    store_a = RedisIdempotencyStore(_client(server))
    store_b = RedisIdempotencyStore(_client(server))
    store_a.save_record(_record(document_id="doc_77"))
    loaded = store_b.get_record("tenant_a", "key-1")
    assert loaded is not None and loaded.document_id == "doc_77"


def test_records_expire_with_the_idempotency_window(server):
    client = _client(server)
    store = RedisIdempotencyStore(client, window_seconds=86400)
    store.save_record(_record())
    key = store._key("tenant_a", "key-1")
    ttl = client.ttl(key)
    assert 0 < ttl <= 86400


def test_an_expired_record_reads_as_absent(server):
    store = RedisIdempotencyStore(_client(server))
    store.save_record(_record(expired=True))
    assert store.get_record("tenant_a", "key-1") is None


def test_tenants_never_share_idempotency_records(server):
    store = RedisIdempotencyStore(_client(server))
    store.save_record(_record(tenant="tenant_a", document_id="doc_a"))
    store.save_record(_record(tenant="tenant_b", document_id="doc_b"))
    assert store.get_record("tenant_a", "key-1").document_id == "doc_a"
    assert store.get_record("tenant_b", "key-1").document_id == "doc_b"


def test_colon_joined_identities_do_not_alias(server):
    store = RedisIdempotencyStore(_client(server))
    store.save_record(_record(tenant="tenant_a", key="b:c", document_id="doc_1"))
    assert store.get_record("tenant_a:b", "c") is None  # would be a hit if keys were joined with ':'


def test_unsafe_tenant_identity_is_refused(server):
    store = RedisIdempotencyStore(_client(server))
    with pytest.raises(InvalidTenantIdentifierError):
        store.get_record("../tenant_b", "key-1")


def test_delete_removes_the_record(server):
    store = RedisIdempotencyStore(_client(server))
    store.save_record(_record())
    store.delete_record("tenant_a", "key-1")
    assert store.get_record("tenant_a", "key-1") is None


def test_concurrent_check_then_create_produces_exactly_one_document(server):
    """Race from NFR-006 / FR-ING-006: many replicas see 'no record' and try to create. Only one may create."""
    created = []
    errors = []
    barrier = threading.Barrier(6)

    def replica(index):
        store = RedisIdempotencyStore(_client(server))
        try:
            barrier.wait()
            with store.get_key_lock("tenant_a", "shared-key"):
                if store.get_record("tenant_a", "shared-key") is None:
                    time.sleep(0.02)  # widen the window where a racing replica could also see "absent"
                    created.append(index)
                    store.save_record(_record(key="shared-key", document_id=f"doc_{index}"))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=replica, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(created) == 1
    assert RedisIdempotencyStore(_client(server)).get_record("tenant_a", "shared-key") is not None


def test_tenant_lock_factory_serializes_audit_appends_per_tenant(server):
    provider = RedisLockProvider(_client(server), acquire_timeout_seconds=0.1, lease_ms=5000)
    factory = tenant_lock_factory(provider)
    other_replica = RedisLockProvider(_client(server), acquire_timeout_seconds=0.1)
    with factory("tenant_a"):
        with pytest.raises(LockTimeoutError):
            with other_replica.lock("audit:tenant_a"):
                pass  # pragma: no cover
        with other_replica.lock("audit:tenant_b"):
            pass  # another tenant's chain is independent
