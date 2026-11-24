"""
Distributed rate limiting (PRD API-012, SEC-017). Two replicas share one bucket; the limit is exact under contention;
an unreachable store fails closed; subjects never appear in Redis key names.
"""

import threading

import fakeredis
import pytest

from doc_extractor.exceptions import StorageError
from doc_extractor.rate_limit import RedisTokenBucket


class Clock:
    def __init__(self, start=1_000.0):
        self.now = start

    def __call__(self):
        return self.now


def _bucket(server, clock, capacity=3, refill=1.0):
    return RedisTokenBucket(fakeredis.FakeRedis(server=server), capacity=capacity, refill_per_second=refill, clock=clock)


def test_the_burst_is_allowed_then_refused_with_a_retry_after():
    server, clock = fakeredis.FakeServer(), Clock()
    bucket = _bucket(server, clock, capacity=3, refill=1.0)
    assert [bucket.take("key-a").allowed for _ in range(3)] == [True, True, True]
    refused = bucket.take("key-a")
    assert refused.allowed is False
    assert refused.retry_after_seconds == 1  # one token refills in one second


def test_tokens_refill_with_time_up_to_the_capacity():
    server, clock = fakeredis.FakeServer(), Clock()
    bucket = _bucket(server, clock, capacity=2, refill=1.0)
    bucket.take("k")
    bucket.take("k")
    assert bucket.take("k").allowed is False
    clock.now += 1.0
    assert bucket.take("k").allowed is True
    assert bucket.take("k").allowed is False
    clock.now += 60.0  # a long idle period refills to the capacity, never beyond it
    assert [bucket.take("k").allowed for _ in range(3)] == [True, True, False]


def test_subjects_have_independent_buckets():
    server, clock = fakeredis.FakeServer(), Clock()
    bucket = _bucket(server, clock, capacity=1, refill=0.1)
    assert bucket.take("alice").allowed is True
    assert bucket.take("alice").allowed is False
    assert bucket.take("bob").allowed is True


def test_two_replicas_share_one_bucket():
    server, clock = fakeredis.FakeServer(), Clock()
    replica_a = _bucket(server, clock, capacity=4, refill=0.001)
    replica_b = _bucket(server, clock, capacity=4, refill=0.001)
    decisions = []
    for _ in range(4):
        decisions.append(replica_a.take("shared").allowed)
        decisions.append(replica_b.take("shared").allowed)
    assert decisions.count(True) == 4  # four tokens in total, not four per replica


def test_the_limit_is_exact_under_concurrent_replicas():
    """Optimistic concurrency: many replicas racing for the last tokens never over-spend the bucket."""
    server, clock = fakeredis.FakeServer(), Clock()
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(12)

    def replica():
        bucket = _bucket(server, clock, capacity=5, refill=0.0001)
        barrier.wait()
        allowed = bucket.take("hot-key").allowed
        with lock:
            results.append(allowed)

    threads = [threading.Thread(target=replica) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 5
    assert results.count(False) == 7


def test_an_unreachable_store_fails_closed():
    class Down:
        def pipeline(self):
            from redis.exceptions import ConnectionError as RedisConnectionError

            raise RedisConnectionError("refused")

    bucket = RedisTokenBucket(Down(), capacity=3, refill_per_second=1.0)
    with pytest.raises(StorageError):
        bucket.take("k")


def test_the_subject_never_appears_in_a_redis_key():
    server, clock = fakeredis.FakeServer(), Clock()
    client = fakeredis.FakeRedis(server=server)
    bucket = RedisTokenBucket(client, capacity=3, refill_per_second=1.0, clock=clock)
    bucket.take("de_live_secretcredential")
    keys = [k.decode() for k in client.keys("*")]
    assert keys and not any("secretcredential" in k or "de_live" in k for k in keys)


def test_empty_subject_and_bad_parameters_are_rejected():
    server = fakeredis.FakeServer()
    bucket = _bucket(server, Clock())
    with pytest.raises(ValueError):
        bucket.take("")
    with pytest.raises(ValueError):
        RedisTokenBucket(fakeredis.FakeRedis(server=server), capacity=0, refill_per_second=1.0)
    with pytest.raises(ValueError):
        RedisTokenBucket(fakeredis.FakeRedis(server=server), capacity=1, refill_per_second=0)


# -- through the application ----------------------------------------------------------------------------------------

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[api]')")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from doc_extractor.api.app import create_app  # noqa: E402
from doc_extractor.api.composition import ApplicationContainer  # noqa: E402
from doc_extractor.api.config import ApiSettings  # noqa: E402
from doc_extractor.api.providers.jwt_auth import issue_access_token  # noqa: E402

ISSUER = "https://identity.example.internal"
AUDIENCE = "doc-extractor-api"


def test_the_api_answers_429_with_retry_after_once_the_bucket_is_empty(tmp_path):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    settings = ApiSettings(
        app_env="development",
        jwt_public_key_path=str(public_path),
        jwt_issuer=ISSUER,
        jwt_audience=AUDIENCE,
        rate_limit_capacity=3,
    )
    server, clock = fakeredis.FakeServer(), Clock()
    limiter = RedisTokenBucket(fakeredis.FakeRedis(server=server), capacity=3, refill_per_second=0.5, clock=clock)
    container = ApplicationContainer(api_settings=settings, rate_limiter=limiter)
    client = TestClient(create_app(settings, container=container))
    token = issue_access_token(
        private_pem, issuer=ISSUER, audience=AUDIENCE, subject="user_1", tenant_id="tenant_a", roles=["analyst"],
    )
    headers = {"Authorization": f"Bearer {token}"}

    statuses = [client.get("/api/v1/documents", headers=headers).status_code for _ in range(4)]
    assert 429 not in statuses[:3]  # the burst is served (auth and routing pass before the bucket is spent)
    refused = client.get("/api/v1/documents", headers=headers)
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "RATE_LIMITED"
    assert int(refused.headers["Retry-After"]) >= 1


def test_unauthenticated_requests_do_not_spend_a_bucket(tmp_path):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    settings = ApiSettings(
        app_env="development", jwt_public_key_path=str(public_path), jwt_issuer=ISSUER, jwt_audience=AUDIENCE,
    )
    server, clock = fakeredis.FakeServer(), Clock()
    limiter = RedisTokenBucket(fakeredis.FakeRedis(server=server), capacity=1, refill_per_second=0.001, clock=clock)
    container = ApplicationContainer(api_settings=settings, rate_limiter=limiter)
    client = TestClient(create_app(settings, container=container))
    for _ in range(5):
        assert client.get("/api/v1/documents").status_code == 401
    assert fakeredis.FakeRedis(server=server).keys("*") == []  # nothing was taken from the bucket
