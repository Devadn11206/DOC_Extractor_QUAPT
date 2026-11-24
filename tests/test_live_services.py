"""
Live-service tests for the security backends (PRD FR-ING-003, SEC-009, ARC-003, NFR-006).

These run against a real clamd and a real Redis, not the fake double. They are opt-in: set
DOC_TEST_CLAMD_ADDRESS (for example 127.0.0.1:3310) and DOC_TEST_REDIS_URL (for example redis://127.0.0.1:16379/0).
Without them each test skips with its reason, so the default run stays green on a developer machine.
"""

import io
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

from doc_extractor.distributed_state import (
    LockTimeoutError,
    RedisIdempotencyStore,
    RedisLockProvider,
    connect_redis,
)
from doc_extractor.malware import EICAR_SIGNATURE, ClamAVSocketScanner, ScanStatus
from doc_extractor.models import IdempotencyRecord

CLAMD = os.environ.get("DOC_TEST_CLAMD_ADDRESS", "").strip()
REDIS_URL = os.environ.get("DOC_TEST_REDIS_URL", "").strip()

needs_clamd = pytest.mark.skipif(not CLAMD, reason="set DOC_TEST_CLAMD_ADDRESS to run against a real clamd")
needs_redis = pytest.mark.skipif(not REDIS_URL, reason="set DOC_TEST_REDIS_URL to run against a real Redis")


@pytest.fixture
def redis_client():
    client = connect_redis(REDIS_URL)
    client.ping()
    yield client
    client.close()


@pytest.fixture
def namespace():
    # A unique namespace per test: real Redis is shared, so tests must never see each other's keys.
    return f"doc_extractor_test_{uuid.uuid4().hex[:12]}"


def _record(tenant="tenant_live", key="key-1", document_id="doc_1"):
    now = datetime.now(timezone.utc).timestamp()
    return IdempotencyRecord(
        idempotency_key=key,
        tenant_id=tenant,
        request_fingerprint="fp",
        document_id=document_id,
        response_payload={"document_id": document_id},
        created_at_utc=now,
        expires_at_utc=now + 86400,
    )


# -- clamd -----------------------------------------------------------------------------------------------------

@needs_clamd
def test_real_clamd_answers_ping():
    assert ClamAVSocketScanner(CLAMD, timeout_seconds=60).is_operational() is True


@needs_clamd
def test_real_clamd_passes_a_clean_file():
    result = ClamAVSocketScanner(CLAMD, timeout_seconds=60).scan_stream(io.BytesIO(b"%PDF-1.7 ordinary content"))
    assert result.status == ScanStatus.CLEAN


@needs_clamd
def test_real_clamd_flags_the_eicar_test_file():
    result = ClamAVSocketScanner(CLAMD, timeout_seconds=60).scan_stream(io.BytesIO(EICAR_SIGNATURE))
    assert result.status == ScanStatus.INFECTED
    assert result.threat_name and "Eicar" in result.threat_name


@needs_clamd
def test_real_clamd_scans_a_multi_chunk_clean_file_as_clean():
    """A file of several 64 KiB chunks must reach clamd intact: a clean one reads as clean, not as an error."""
    payload = b"%PDF-1.7 " * 40000  # about 320 KiB, five chunks
    result = ClamAVSocketScanner(CLAMD, timeout_seconds=60).scan_stream(io.BytesIO(payload))
    assert result.status == ScanStatus.CLEAN


@needs_clamd
def test_real_clamd_does_not_flag_eicar_embedded_in_a_larger_file():
    """Observed behaviour of the real engine: its EICAR entry is a whole-file signature, so the 68-byte test file is
    flagged but the same bytes inside a bigger file are not. The PRD acceptance test uses the exact test file, so
    this documents the engine's contract rather than a defect. The mock scanner (substring search) differs on purpose."""
    result = ClamAVSocketScanner(CLAMD, timeout_seconds=60).scan_stream(io.BytesIO(b"a" * 65536 + EICAR_SIGNATURE))
    assert result.status == ScanStatus.CLEAN


@needs_clamd
def test_an_unreachable_port_on_the_real_host_fails_closed():
    result = ClamAVSocketScanner("127.0.0.1:1", timeout_seconds=2).scan_stream(io.BytesIO(b"%PDF"))
    assert result.status == ScanStatus.ERROR


# -- Redis -----------------------------------------------------------------------------------------------------

@needs_redis
def test_real_redis_lock_excludes_a_second_replica(redis_client, namespace):
    replica_a = RedisLockProvider(connect_redis(REDIS_URL), namespace=namespace, acquire_timeout_seconds=0.3)
    replica_b = RedisLockProvider(connect_redis(REDIS_URL), namespace=namespace, acquire_timeout_seconds=0.3)
    with replica_a.lock("doc:1"):
        with pytest.raises(LockTimeoutError):
            with replica_b.lock("doc:1"):
                pass  # pragma: no cover
    with replica_b.lock("doc:1"):
        pass


@needs_redis
def test_real_redis_lease_expires_when_the_holder_dies(redis_client, namespace):
    holder = RedisLockProvider(connect_redis(REDIS_URL), namespace=namespace, lease_ms=300, acquire_timeout_seconds=0.1)
    waiter = RedisLockProvider(connect_redis(REDIS_URL), namespace=namespace, acquire_timeout_seconds=3.0)
    context = holder.lock("doc:dead")
    context.__enter__()  # the holder never exits: simulates a crashed replica
    started = time.monotonic()
    with waiter.lock("doc:dead"):
        pass
    assert time.monotonic() - started < 3.0
    holder_key = holder._key("doc:dead")
    redis_client.delete(holder_key)


@needs_redis
def test_real_redis_race_creates_exactly_one_document(redis_client, namespace):
    created = []
    errors = []
    barrier = threading.Barrier(6)

    def replica(index):
        store = RedisIdempotencyStore(connect_redis(REDIS_URL), namespace=namespace)
        try:
            barrier.wait()
            with store.get_key_lock("tenant_live", "race-key"):
                if store.get_record("tenant_live", "race-key") is None:
                    time.sleep(0.02)
                    created.append(index)
                    store.save_record(_record(key="race-key", document_id=f"doc_{index}"))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=replica, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(created) == 1


@needs_redis
def test_real_redis_record_carries_the_24_hour_ttl(redis_client, namespace):
    store = RedisIdempotencyStore(connect_redis(REDIS_URL), namespace=namespace)
    store.save_record(_record(key="ttl-key"))
    ttl = redis_client.ttl(store._key("tenant_live", "ttl-key"))
    assert 0 < ttl <= 86400
