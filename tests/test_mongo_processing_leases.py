"""
MongoDB processing-lease store tests (Team Member 3, Phase 3).

MOCKED: the MongoDB client is the in-memory fake of ``tests/mongo_fakes.py`` (every call suspends its caller once and then runs
atomically, like a single-document server operation; unique indexes are enforced) and time is a controllable clock. These tests
prove the store's logic: atomic claim, fencing of stale workers, bounded attempts, expiry takeover, tenant scoping and fail-closed
errors. They do NOT prove behaviour of a real server: that is ``tests/test_mongo_integration.py``, which skips without a replica set.
"""

import asyncio

import pytest

pytest.importorskip("pymongo", reason="the 'mongo' extra provides the driver whose error types these tests map")
from pymongo import errors as driver  # noqa: E402

from doc_extractor.api.ports.processing import ClaimStatus, JobLease, LeaseLostError, ProcessingLeaseStore  # noqa: E402
from doc_extractor.infrastructure.mongo import (  # noqa: E402
    DEFAULT_REGISTRY,
    MongoConnection,
    MongoSettings,
    PersistenceConfigurationError,
    PersistenceDataError,
    PersistenceError,
    PersistenceUnavailableError,
    TenantScopeError,
    UnsafeQueryError,
    ensure_indexes,
)
from doc_extractor.infrastructure.mongo.indexes import PROCESSING_JOBS  # noqa: E402
from doc_extractor.infrastructure.mongo.processing import MongoProcessingLeaseStore  # noqa: E402
from tests.mongo_fakes import FakeClient, factory_for  # noqa: E402

START_US = 1_800_000_000_000_000


class Clock:
    def __init__(self):
        self.now = START_US

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += int(seconds * 1_000_000)


class Env:
    def __init__(self, store, client, clock):
        self.store, self.client, self.clock = store, client, clock

    @property
    def collection(self):
        return self.client["doc"][PROCESSING_JOBS]


async def build(*, indexes=True, **options):
    client = FakeClient()
    connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(client))
    await connection.connect()
    if indexes:
        await ensure_indexes(client["doc"], DEFAULT_REGISTRY)
    clock = Clock()
    env = Env(MongoProcessingLeaseStore(connection, clock_us=clock, **options), client, clock)
    env.collection.calls.clear()
    return env


def scenario(body, **options):
    async def go():
        return await body(await build(**options))

    return asyncio.run(go())


async def claim(env, *, tenant="tenant_a", key="job_1", worker="w1", lease=60, max_attempts=3, document="doc_1"):
    return await env.store.claim(
        tenant, key, message_id=f"msg_{key}", document_id=document, max_attempts=max_attempts, worker_id=worker, lease_seconds=lease
    )


# ---- the port ---------------------------------------------------------------------------------------------------------------

def test_the_store_implements_the_lease_port():
    assert isinstance(MongoProcessingLeaseStore(MongoConnection(MongoSettings(uri="mongodb://localhost", database="d"))), ProcessingLeaseStore)


# ---- claim ------------------------------------------------------------------------------------------------------------------

def test_the_first_claim_creates_the_record_and_owns_it():
    async def body(env):
        result = await claim(env)
        assert result.status is ClaimStatus.ACQUIRED and result.attempt == 1
        lease = result.lease
        assert (lease.tenant_id, lease.job_key, lease.fencing_token, lease.attempt, lease.max_attempts, lease.worker_id) == (
            "tenant_a", "job_1", 1, 1, 3, "w1")
        assert int((lease.expires_at.timestamp()) * 1_000_000) == START_US + 60_000_000
        snap = await env.store.snapshot("tenant_a", "job_1")
        assert snap["state"] == "leased" and snap["fencing_token"] == 1 and snap["lease_owner"] == "w1"

    scenario(body)


def test_a_live_lease_blocks_every_other_claim_and_says_when_to_come_back():
    async def body(env):
        await claim(env, worker="w1", lease=60)
        env.clock.advance(20)
        other = await claim(env, worker="w2")
        assert other.status is ClaimStatus.HELD and other.lease is None
        assert other.retry_after_seconds == pytest.approx(40.0, abs=0.001)

    scenario(body)


def test_an_expired_lease_is_taken_over_with_the_next_token_and_the_attempt_is_counted():
    async def body(env):
        first = (await claim(env, worker="w1", lease=60)).lease
        env.clock.advance(61)
        second = await claim(env, worker="w2")
        assert second.status is ClaimStatus.ACQUIRED
        assert second.lease.fencing_token == first.fencing_token + 1 and second.lease.attempt == 2 and second.lease.worker_id == "w2"

    scenario(body)


def test_exactly_the_boundary_instant_counts_as_expired():
    async def body(env):
        await claim(env, lease=60)
        env.clock.advance(60)
        assert (await claim(env, worker="w2")).status is ClaimStatus.ACQUIRED

    scenario(body)


# ---- fencing ---------------------------------------------------------------------------------------------------------------

def test_a_stale_worker_is_fenced_out_of_every_write_after_a_takeover():
    async def body(env):
        stale = (await claim(env, worker="w1", lease=60)).lease
        env.clock.advance(61)
        fresh = (await claim(env, worker="w2")).lease
        for call in (
            lambda: env.store.extend(stale, 60),
            lambda: env.store.verify(stale),
            lambda: env.store.mark_completed(stale),
            lambda: env.store.release_for_retry(stale),
            lambda: env.store.dead_letter(stale, reason="x"),
        ):
            with pytest.raises(LeaseLostError):
                await call()
        snap = await env.store.snapshot("tenant_a", "job_1")
        assert snap["state"] == "leased" and snap["fencing_token"] == fresh.fencing_token and snap["lease_owner"] == "w2"
        await env.store.verify(fresh)                      # the new owner is unaffected by the stale attempts
        await env.store.mark_completed(fresh)

    scenario(body)


def test_the_token_alone_fences_a_previous_lease_of_the_same_worker_id():
    """A restarted worker keeps its id: only the token tells its old lease from the new one."""
    async def body(env):
        old = (await claim(env, worker="w1", lease=60)).lease
        env.clock.advance(61)
        new = (await claim(env, worker="w1")).lease
        assert new.fencing_token == old.fencing_token + 1
        for call in (lambda: env.store.mark_completed(old), lambda: env.store.extend(old, 60), lambda: env.store.verify(old),
                     lambda: env.store.release_for_retry(old), lambda: env.store.dead_letter(old, reason="x")):
            with pytest.raises(LeaseLostError):
                await call()
        await env.store.mark_completed(new)

    scenario(body)


def test_an_expired_lease_that_nobody_took_over_may_still_be_extended_and_completed():
    async def body(env):
        lease = (await claim(env, lease=60)).lease
        env.clock.advance(120)                                   # expired, but still the record's token: nobody took it
        renewed = await env.store.extend(lease, 60)
        assert renewed.fencing_token == lease.fencing_token
        await env.store.mark_completed(renewed)
        assert (await env.store.snapshot("tenant_a", "job_1"))["state"] == "completed"

    scenario(body)


def test_extend_moves_the_expiry_to_now_plus_the_extension_and_never_earlier():
    async def body(env):
        lease = (await claim(env, lease=600)).lease
        env.clock.advance(10)
        same = await env.store.extend(lease, 5)                  # 10 + 5 s is earlier than the current 600 s: unchanged
        assert same.expires_at == lease.expires_at
        later = await env.store.extend(lease, 900)
        assert later.expires_at > lease.expires_at
        snap = await env.store.snapshot("tenant_a", "job_1")
        assert snap["lease_expires_at_us"] == START_US + 10_000_000 + 900_000_000

    scenario(body)


def test_extension_keeps_a_lease_from_being_taken_over():
    async def body(env):
        lease = (await claim(env, lease=60)).lease
        env.clock.advance(50)
        await env.store.extend(lease, 60)
        env.clock.advance(50)                                    # 100 s after the claim, 50 s after the heartbeat
        assert (await claim(env, worker="w2")).status is ClaimStatus.HELD

    scenario(body)


# ---- completion, release, dead letter ----------------------------------------------------------------------------------------

def test_completing_is_final_idempotent_for_the_same_lease_and_drops_later_claims():
    async def body(env):
        lease = (await claim(env)).lease
        await env.store.mark_completed(lease)
        await env.store.mark_completed(lease)                          # a repeat after a lost reply is harmless
        result = await claim(env, worker="w2")
        assert result.status is ClaimStatus.COMPLETED and result.lease is None
        with pytest.raises(LeaseLostError):
            await env.store.release_for_retry(lease)             # a settled job cannot be reopened by its old lease
        with pytest.raises(LeaseLostError):
            await env.store.dead_letter(lease, reason="late")
        assert (await env.store.snapshot("tenant_a", "job_1"))["state"] == "completed"

    scenario(body)


def test_a_completed_record_carries_a_retention_deadline_and_a_dead_lettered_one_does_not():
    async def body(env):
        done = (await claim(env, key="done")).lease
        dead = (await claim(env, key="dead")).lease
        await env.store.mark_completed(done)
        await env.store.dead_letter(dead, reason="poison")
        docs = {d["job_key"]: d for d in env.collection.docs}
        assert docs["done"]["expires_at"].year >= 2026 and "lease_owner" not in docs["done"]
        assert "expires_at" not in docs["dead"] and docs["dead"]["dead_letter_reason"] == "poison"

    scenario(body, retention_seconds=3600)


def test_release_for_retry_makes_the_job_claimable_only_after_the_delay():
    async def body(env):
        lease = (await claim(env)).lease
        await env.store.release_for_retry(lease, delay_seconds=30, error_code="processing_failed")
        early = await claim(env, worker="w2")
        assert early.status is ClaimStatus.NOT_YET_VISIBLE and early.retry_after_seconds == pytest.approx(30.0, abs=0.001)
        env.clock.advance(30)
        again = await claim(env, worker="w2")
        assert again.status is ClaimStatus.ACQUIRED and again.lease.attempt == 2 and again.lease.fencing_token == 2
        assert (await env.store.snapshot("tenant_a", "job_1"))["last_error_code"] == "processing_failed"

    scenario(body)


def test_release_repeated_by_the_same_lease_is_harmless_but_not_after_another_worker_claimed():
    async def body(env):
        lease = (await claim(env)).lease
        await env.store.release_for_retry(lease)
        await env.store.release_for_retry(lease)
        assert (await claim(env, worker="w2")).status is ClaimStatus.ACQUIRED
        with pytest.raises(LeaseLostError):
            await env.store.release_for_retry(lease)

    scenario(body)


def test_a_release_that_does_not_consume_the_attempt_leaves_the_count_unchanged():
    async def body(env):
        lease = (await claim(env)).lease
        await env.store.release_for_retry(lease, consume_attempt=False)
        assert (await env.store.snapshot("tenant_a", "job_1"))["attempt"] == 0
        assert (await claim(env, worker="w2")).lease.attempt == 1

    scenario(body)


def test_an_unusable_error_code_is_replaced_by_a_fixed_one():
    async def body(env):
        lease = (await claim(env)).lease
        await env.store.release_for_retry(lease, error_code="Bad Code with spaces / secret")
        assert (await env.store.snapshot("tenant_a", "job_1"))["last_error_code"] == "unspecified"

    scenario(body)


def test_a_dead_lettered_job_is_never_claimed_again():
    async def body(env):
        lease = (await claim(env)).lease
        await env.store.dead_letter(lease, reason="poison")
        await env.store.dead_letter(lease, reason="poison")      # repeat: harmless
        result = await claim(env, worker="w2")
        assert result.status is ClaimStatus.DEAD_LETTERED and result.lease is None

    scenario(body)


# ---- bounded attempts -------------------------------------------------------------------------------------------------------

def test_a_job_that_keeps_losing_its_worker_is_dead_lettered_when_the_attempts_are_used_up():
    async def body(env):
        for worker in ("w1", "w2"):
            assert (await claim(env, worker=worker, lease=60, max_attempts=2)).status is ClaimStatus.ACQUIRED
            env.clock.advance(61)                                # the worker died: its lease expires
        result = await claim(env, worker="w3", max_attempts=2)
        assert result.status is ClaimStatus.EXHAUSTED and result.lease is None
        assert (await env.store.snapshot("tenant_a", "job_1"))["state"] == "dead_lettered"
        assert (await claim(env, worker="w4", max_attempts=2)).status is ClaimStatus.DEAD_LETTERED

    scenario(body)


def test_the_final_attempt_is_still_protected_by_its_live_lease():
    async def body(env):
        await claim(env, worker="w1", max_attempts=1)
        assert (await claim(env, worker="w2", max_attempts=1)).status is ClaimStatus.HELD

    scenario(body)


def test_a_retry_that_is_not_due_does_not_dead_letter_even_when_the_count_is_high():
    async def body(env):
        lease = (await claim(env, max_attempts=1)).lease
        await env.store.release_for_retry(lease, delay_seconds=30)
        assert (await claim(env, worker="w2", max_attempts=1)).status is ClaimStatus.EXHAUSTED

    scenario(body)


# ---- concurrency across workers ---------------------------------------------------------------------------------------------

def test_many_concurrent_first_claims_have_exactly_one_winner():
    async def body(env):
        results = await asyncio.gather(*(claim(env, worker=f"w{i}") for i in range(25)))
        statuses = [r.status for r in results]
        assert statuses.count(ClaimStatus.ACQUIRED) == 1 and statuses.count(ClaimStatus.HELD) == 24
        assert env.collection.docs and len(env.collection.docs) == 1

    scenario(body)


def test_many_concurrent_takeovers_of_an_expired_lease_have_exactly_one_winner_and_one_token_step():
    async def body(env):
        await claim(env, worker="w0", lease=60)
        env.clock.advance(61)
        results = await asyncio.gather(*(claim(env, worker=f"w{i}") for i in range(1, 26)))
        winners = [r for r in results if r.status is ClaimStatus.ACQUIRED]
        assert len(winners) == 1 and winners[0].lease.fencing_token == 2 and winners[0].lease.attempt == 2
        assert (await env.store.snapshot("tenant_a", "job_1"))["fencing_token"] == 2

    scenario(body)


def test_concurrent_claims_of_different_jobs_do_not_interfere():
    async def body(env):
        results = await asyncio.gather(*(claim(env, key=f"job_{i}", worker=f"w{i}") for i in range(20)))
        assert all(r.status is ClaimStatus.ACQUIRED and r.lease.fencing_token == 1 for r in results)

    scenario(body)


# ---- tenant isolation -----------------------------------------------------------------------------------------------------

def test_the_same_job_key_in_two_tenants_is_two_independent_jobs():
    async def body(env):
        a = (await claim(env, tenant="tenant_a")).lease
        b = (await claim(env, tenant="tenant_b")).lease
        assert (a.fencing_token, b.fencing_token) == (1, 1)
        await env.store.mark_completed(a)
        assert (await claim(env, tenant="tenant_a", worker="w2")).status is ClaimStatus.COMPLETED
        assert (await claim(env, tenant="tenant_b", worker="w2")).status is ClaimStatus.HELD
        assert await env.store.snapshot("tenant_c", "job_1") is None

    scenario(body)


def test_a_lease_can_never_change_another_tenants_job_even_with_the_same_key_owner_and_token():
    async def body(env):
        a = (await claim(env, tenant="tenant_a")).lease
        b = (await claim(env, tenant="tenant_b")).lease
        forged = JobLease("tenant_b", a.job_key, a.fencing_token, a.attempt, a.max_attempts, a.worker_id, a.expires_at)
        await env.store.mark_completed(forged)                         # B's own record has token 1 and owner w1: that IS B's lease
        assert (await env.store.snapshot("tenant_a", "job_1"))["state"] == "leased"
        assert (await env.store.snapshot("tenant_b", "job_1"))["state"] == "completed"
        await env.store.verify(a)
        with pytest.raises(LeaseLostError):
            await env.store.verify(JobLease("tenant_c", a.job_key, 1, 1, 3, "w1", a.expires_at))
        assert b.tenant_id == "tenant_b"

    scenario(body)


def test_every_driver_call_names_the_tenant():
    async def body(env):
        lease = (await claim(env)).lease
        await env.store.extend(lease, 60)
        await env.store.verify(lease)
        await env.store.mark_completed(lease)
        await env.store.snapshot("tenant_a", "job_1")
        filters = [c[1] for c in env.collection.calls if c[0] in ("find_one", "find_one_and_update", "update_one")]
        assert filters and all(f.get("tenant_id") == "tenant_a" for f in filters)
        assert not any(c[0] == "insert_one" and c[1].get("tenant_id") != "tenant_a" for c in env.collection.calls)

    scenario(body)


def test_an_invalid_tenant_is_refused_before_any_driver_call():
    async def body(env):
        for bad in ("", "  ", "a/../b", None):
            with pytest.raises(TenantScopeError):
                await claim(env, tenant=bad)
        assert env.collection.calls == []

    scenario(body)


# ---- validation -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        {"key": ""}, {"key": "x" * 257}, {"key": "bad\nkey"}, {"worker": ""}, {"lease": 0}, {"lease": 86401}, {"lease": True},
        {"lease": 1.5}, {"max_attempts": 0}, {"max_attempts": 101}, {"max_attempts": False}, {"document": ""},
    ],
)
def test_invalid_claim_arguments_are_refused_with_a_typed_error(kwargs):
    async def body(env):
        with pytest.raises(UnsafeQueryError):
            await claim(env, **kwargs)
        assert env.collection.calls == []

    scenario(body)


def test_invalid_extension_and_delay_arguments_are_refused():
    async def body(env):
        lease = (await claim(env)).lease
        for bad in (0, -5, 86401, True, 2.5):
            with pytest.raises(UnsafeQueryError):
                await env.store.extend(lease, bad)
        for bad in (-1, 8 * 24 * 3600, True, "5", float("nan")):
            with pytest.raises(UnsafeQueryError):
                await env.store.release_for_retry(lease, delay_seconds=bad)

    scenario(body)


def test_constructor_bounds():
    connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="d"))
    for options in ({"retention_seconds": 1}, {"retention_seconds": True}, {"claim_rounds": 0}, {"claim_rounds": 101}):
        with pytest.raises(ValueError):
            MongoProcessingLeaseStore(connection, **options)


# ---- the unique index and failures ------------------------------------------------------------------------------------------

def test_without_the_unique_job_index_the_store_refuses_to_claim():
    async def body(env):
        with pytest.raises(PersistenceError) as raised:
            await claim(env)
        assert raised.value.details["reason"] == "processing_job_index_is_missing"
        with pytest.raises(PersistenceConfigurationError):
            await env.store.require_unique_job_index()
        assert not env.collection.docs

    scenario(body, indexes=False)


def test_the_startup_gate_passes_when_the_index_exists():
    async def body(env):
        await env.store.require_unique_job_index()

    scenario(body)


def test_a_driver_failure_is_a_typed_unavailable_error_with_no_driver_text():
    async def body(env):
        await claim(env, key="healthy")
        env.collection.fail_with = driver.AutoReconnect("connection lost to mongodb://user:SECRETPW@host")
        with pytest.raises(PersistenceUnavailableError) as raised:
            await claim(env, key="job_2")
        assert "SECRETPW" not in repr(raised.value) and "SECRETPW" not in str(raised.value.details)

    scenario(body)


def test_a_failed_settling_write_is_an_error_never_a_silent_success():
    async def body(env):
        lease = (await claim(env)).lease
        env.collection.fail_with = driver.AutoReconnect("down")
        for call in (lambda: env.store.mark_completed(lease), lambda: env.store.extend(lease, 5), lambda: env.store.release_for_retry(lease)):
            with pytest.raises(PersistenceUnavailableError):
                await call()
        env.collection.fail_with = None
        assert (await env.store.snapshot("tenant_a", "job_1"))["state"] == "leased"

    scenario(body)


def test_a_malformed_stored_record_is_a_data_error_not_a_claim():
    async def body(env):
        await claim(env)
        env.collection.docs[0]["attempt"] = "many"
        env.clock.advance(61)
        with pytest.raises(PersistenceDataError):
            await claim(env, worker="w2")

    scenario(body)


def test_snapshot_exposes_state_only_never_message_or_document_identifiers():
    async def body(env):
        await claim(env, document="doc_secret_9")
        snap = await env.store.snapshot("tenant_a", "job_1")
        assert "document_id" not in snap and "message_id" not in snap and "tenant_id" not in snap

    scenario(body)
