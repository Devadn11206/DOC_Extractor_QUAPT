"""
MongoDB audit repository tests (Team Member 3, Phase 2B).

MOCKED: the MongoDB client is the in-memory fake of ``tests/mongo_fakes.py`` (every call suspends its caller once and then runs
atomically, like a single-document server operation; unique indexes are enforced). These tests prove the repository's logic: the
chain format, sequence allocation under contention, tenant scoping, append-only surface, verification and fail-closed behaviour.
They do NOT prove behaviour of a real server: that is ``tests/test_mongo_integration.py`` against a real replica set, which skips
when none is available.

The chain format is the EXISTING one (``api/audit_chain.py``): one test appends the same events to the file-based repository and to
this one and requires identical sequence numbers, previous hashes and hashes.
"""

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("pymongo", reason="the 'mongo' extra provides the driver whose error types these tests map")
from pymongo import errors as driver  # noqa: E402

import doc_extractor.infrastructure.mongo.audit as audit_module  # noqa: E402
from doc_extractor.api.audit_chain import GENESIS_HASH, DurableAuditLogger, HashChainedAuditRepository, compute_event_hash  # noqa: E402
from doc_extractor.api.contracts.records import AuditEventRecord  # noqa: E402
from doc_extractor.api.exceptions import AuditUnavailableError  # noqa: E402
from doc_extractor.api.ports.repositories import AuditRepository  # noqa: E402
from doc_extractor.infrastructure.mongo import (  # noqa: E402
    DEFAULT_REGISTRY,
    DuplicateKeyPersistenceError,
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
from doc_extractor.infrastructure.mongo.audit import MongoAuditRepository, microseconds  # noqa: E402
from doc_extractor.infrastructure.mongo.indexes import AUDIT_EVENTS  # noqa: E402
from tests.mongo_fakes import FakeClient, LogCapture, factory_for  # noqa: E402

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
SECRET = "Audit-Content-Marker-5521"


def run(coro):
    return asyncio.run(coro)


def at(minutes, micros=0):
    return BASE + timedelta(minutes=minutes, microseconds=micros)


def ev(n, *, tenant="tenant_a", resource="doc_1", action="document.upload", minutes=None, micros=0, actor="actor_1", outcome="SUCCESS",
       metadata=None):
    return AuditEventRecord(
        event_id=f"evt_{tenant}_{n}", actor_id=actor, tenant_id=tenant, action=action, resource_id=resource, resource_type="document",
        timestamp=at(n if minutes is None else minutes, micros), outcome=outcome, metadata=metadata or {},
    )


async def no_pause(_seconds):
    await asyncio.sleep(0)


class Mongo:
    def __init__(self, repo, client, connection):
        self.repo, self.client, self.connection = repo, client, connection

    @property
    def collection(self):
        return self.client["doc"][AUDIT_EVENTS]

    @property
    def docs(self):
        return sorted(self.collection.docs, key=lambda d: (d["tenant_id"], d["seq"]))


async def build(*, indexes=True, **options):
    client = FakeClient()
    connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(client))
    await connection.connect()
    if indexes:
        await ensure_indexes(client["doc"], DEFAULT_REGISTRY)
    options.setdefault("sleep", no_pause)
    mongo = Mongo(MongoAuditRepository(connection, **options), client, connection)
    mongo.collection.calls.clear()
    return mongo


def with_audit(body, **options):
    async def go():
        return await body(await build(**options))

    return asyncio.run(go())


async def fill(repo, count, *, tenant="tenant_a", **kwargs):
    for n in range(1, count + 1):
        await repo.append_event(ev(n, tenant=tenant, **kwargs))


# ---- persistence, retrieval and the stored document ------------------------------------------------------------------------

def test_the_repository_implements_the_audit_port():
    repo = MongoAuditRepository(MongoConnection(MongoSettings(uri="mongodb://localhost", database="d")))
    assert isinstance(repo, AuditRepository)
    for name in ("append_event", "query_events"):
        assert list(inspect.signature(getattr(AuditRepository, name)).parameters) == list(inspect.signature(getattr(MongoAuditRepository, name)).parameters)
        assert inspect.iscoroutinefunction(getattr(MongoAuditRepository, name))


def test_events_are_persisted_and_read_back_in_chain_order():
    async def body(mongo):
        events = [ev(n) for n in (3, 1, 2)]                      # appended in this order, whatever their timestamps
        for event in events:
            await mongo.repo.append_event(event)
        return events, await mongo.repo.query_events("tenant_a")

    events, stored = with_audit(body)
    assert stored == events                                      # the chain order is the append order


def test_the_stored_document_carries_the_sequence_both_hashes_and_the_hash_version():
    async def body(mongo):
        await fill(mongo.repo, 3)
        return mongo.docs

    docs = with_audit(body)
    assert [d["seq"] for d in docs] == [1, 2, 3] and all(d["hash_version"] == 1 and d["tenant_id"] == "tenant_a" for d in docs)
    assert docs[0]["prev_hash"] == GENESIS_HASH and docs[1]["prev_hash"] == docs[0]["hash"] and docs[2]["prev_hash"] == docs[1]["hash"]
    for doc in docs:
        assert compute_event_hash(doc["prev_hash"], doc["seq"], json.loads(doc["event_json"])) == doc["hash"]
        assert set(doc) == {"tenant_id", "seq", "prev_hash", "hash", "hash_version", "event_json", "event_id", "resource_id", "action",
                            "timestamp_us"}


def test_the_chain_head_reports_the_newest_record_and_an_empty_chain_starts_at_genesis():
    async def body(mongo):
        empty = await mongo.repo.chain_head("tenant_a")
        await fill(mongo.repo, 4)
        return empty, await mongo.repo.chain_head("tenant_a"), mongo.docs[-1]["hash"]

    empty, head, last_hash = with_audit(body)
    assert empty == (0, GENESIS_HASH) and head == (4, last_hash)


# ---- the format is the existing one ----------------------------------------------------------------------------------------

def test_the_chain_is_identical_to_the_file_based_chain_for_the_same_events(tmp_path):
    events = [ev(n, resource=f"doc_{n % 3}", action=("a.x" if n % 2 else "b.y"), micros=n * 7, metadata={"outcome_code": "SUCCESS"}) for n in range(1, 9)]
    file_repo = HashChainedAuditRepository(tmp_path)

    async def body(mongo):
        for event in events:
            await file_repo.append_event(event)
            await mongo.repo.append_event(event)
        return mongo.docs

    docs = with_audit(body)
    path = file_repo._chain_path("tenant_a", create=False)
    file_records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [(d["seq"], d["prev_hash"], d["hash"]) for d in docs] == [(r["seq"], r["prev_hash"], r["hash"]) for r in file_records]
    assert [json.loads(d["event_json"]) for d in docs] == [r["event"] for r in file_records]
    assert asyncio.run(file_repo.query_events("tenant_a")) == events


def test_a_chain_written_here_verifies_with_the_file_repositorys_own_hash_function():
    async def body(mongo):
        await fill(mongo.repo, 5)
        return mongo.docs

    previous = GENESIS_HASH
    for doc in with_audit(body):
        assert doc["prev_hash"] == previous and compute_event_hash(previous, doc["seq"], json.loads(doc["event_json"])) == doc["hash"]
        previous = doc["hash"]


def test_the_stored_text_is_the_canonical_json_that_was_hashed():
    async def body(mongo):
        await mongo.repo.append_event(ev(1, metadata={"outcome_code": "SUCCESS", "duration_ms": 12.5}))
        return mongo.docs[0]

    doc = with_audit(body)
    body_dict = json.loads(doc["event_json"])
    assert doc["event_json"] == json.dumps(body_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---- verification ----------------------------------------------------------------------------------------------------------

def test_an_intact_chain_verifies_and_an_empty_one_is_valid():
    async def body(mongo):
        empty = await mongo.repo.verify_chain("tenant_a")
        await fill(mongo.repo, 12)
        return empty, await mongo.repo.verify_chain("tenant_a"), await mongo.repo.verify_chain("tenant_a", batch_size=5)

    empty, full, small_batches = with_audit(body)
    assert empty.valid and empty.checked_events == 0 and empty.first_bad_seq is None
    assert full.valid and full.checked_events == 12 and small_batches == full


def forge(tenant, seq, previous, **overrides):
    """A record that is internally consistent (its hash is right) but not what the repository wrote."""
    event = ev(seq, tenant=tenant)
    body = event.model_dump(mode="json")
    body.update(overrides.pop("body", {}))
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    document = {"tenant_id": tenant, "seq": seq, "prev_hash": previous, "hash": compute_event_hash(previous, seq, body), "hash_version": 1,
                "event_json": canonical, "event_id": event.event_id, "resource_id": event.resource_id, "action": event.action,
                "timestamp_us": microseconds(event.timestamp)}
    document.update(overrides)
    return document


@pytest.mark.parametrize("name, tamper, reason, bad_seq", [
    ("edited event", lambda docs: docs[2].update(event_json=docs[2]["event_json"].replace("doc_1", "doc_X")), "hash mismatch", 3),
    ("edited hash", lambda docs: docs[2].update(hash="f" * 64), "hash mismatch", 3),
    ("edited previous hash", lambda docs: docs[2].update(prev_hash="e" * 64), "sequence or link broken", 3),
    ("removed middle record", lambda docs: docs.pop(2), "sequence or link broken", 3),
    ("reordered records", lambda docs: (docs[1].update(seq=3), docs[2].update(seq=2)), "sequence or link broken", 2),
    ("inserted duplicate sequence", lambda docs: docs.append({**docs[1], "event_id": "evt_dup"}), "sequence or link broken", 3),
    ("unsupported hash version", lambda docs: docs[3].update(hash_version=2), "unsupported hash version", 4),
    ("missing field", lambda docs: docs[3].pop("prev_hash"), "malformed record", 4),
    # strings sort after numbers (BSON order), so the gap is what the walk meets first; the stray record is caught by the final count
    ("sequence is not an integer", lambda docs: docs[1].update(seq="2"), "sequence or link broken", 2),
    ("event text is not JSON", lambda docs: docs[3].update(event_json="{not json"), "malformed record", 4),
    ("event text is not an object", lambda docs: docs[3].update(event_json="[1,2]"), "malformed record", 4),
    ("edited query copy: resource", lambda docs: docs[4].update(resource_id="doc_other"), "query fields differ from the event", 5),
    ("edited query copy: action", lambda docs: docs[4].update(action="other.action"), "query fields differ from the event", 5),
    ("edited query copy: time", lambda docs: docs[4].update(timestamp_us=docs[4]["timestamp_us"] + 1), "query fields differ from the event", 5),
    ("edited query copy: event id", lambda docs: docs[4].update(event_id="evt_other"), "query fields differ from the event", 5),
])
def test_verification_reports_the_first_defect_with_a_fixed_reason(name, tamper, reason, bad_seq):
    async def body(mongo):
        await fill(mongo.repo, 6)
        stored = mongo.collection.docs
        tamper(stored)
        return await mongo.repo.verify_chain("tenant_a"), await mongo.repo.verify_chain("tenant_a", batch_size=2)

    result, small = with_audit(body)
    assert result.valid is False and result.reason == reason and result.first_bad_seq == bad_seq, name
    assert result.checked_events == bad_seq - 1 and small == result


def test_a_record_with_a_correct_hash_but_a_foreign_tenant_in_its_event_is_refused():
    async def body(mongo):
        await fill(mongo.repo, 2)
        previous = mongo.docs[-1]["hash"]
        mongo.collection.docs.append(forge("tenant_a", 3, previous, body={"tenant_id": "tenant_b"}))
        return await mongo.repo.verify_chain("tenant_a")

    result = with_audit(body)
    assert (result.valid, result.first_bad_seq, result.reason) == (False, 3, "event belongs to another tenant")


def test_a_correctly_hashed_record_that_is_not_an_audit_event_is_refused():
    async def body(mongo):
        await fill(mongo.repo, 2)
        previous = mongo.docs[-1]["hash"]
        forged = forge("tenant_a", 3, previous, body={"action": ""})
        mongo.collection.docs.append(forged)
        return await mongo.repo.verify_chain("tenant_a")

    assert with_audit(body).reason == "event is not a valid audit event"


def test_verification_never_echoes_event_content():
    async def body(mongo):
        await mongo.repo.append_event(ev(1, actor=SECRET, resource=SECRET))
        mongo.collection.docs[0]["event_json"] = mongo.collection.docs[0]["event_json"].replace(SECRET, SECRET + "x")
        return await mongo.repo.verify_chain("tenant_a")

    result = with_audit(body)
    assert result.valid is False and SECRET not in repr(result) and SECRET not in str(result.reason)


def test_removal_of_the_newest_records_is_not_detected_by_the_database_chain_alone():
    """The documented limit: nothing outside the database vouches for the head. Signed checkpoints are future work (SEC-008)."""
    async def body(mongo):
        await fill(mongo.repo, 5)
        mongo.collection.docs[:] = [d for d in mongo.collection.docs if d["seq"] <= 3]
        return await mongo.repo.verify_chain("tenant_a")

    result = with_audit(body)
    assert result.valid is True and result.checked_events == 3          # a truncated tail looks like a shorter, valid chain


def test_one_tenants_damage_does_not_affect_another_tenants_verification():
    async def body(mongo):
        await fill(mongo.repo, 4, tenant="tenant_a")
        await fill(mongo.repo, 4, tenant="tenant_b")
        next(d for d in mongo.collection.docs if d["tenant_id"] == "tenant_a" and d["seq"] == 2)["hash"] = "0" * 64
        return await mongo.repo.verify_chain("tenant_a"), await mongo.repo.verify_chain("tenant_b")

    broken, intact = with_audit(body)
    assert broken.valid is False and intact.valid is True and intact.checked_events == 4


def test_verification_rejects_a_bad_batch_size_and_an_invalid_tenant_before_the_driver():
    async def body(mongo):
        for bad in (0, -1, 1000, 1001, "5", None, True):
            with pytest.raises(UnsafeQueryError):
                await mongo.repo.verify_chain("tenant_a", batch_size=bad)
        for tenant in ("", "   ", None, {"$ne": 1}):
            with pytest.raises((TenantScopeError, UnsafeQueryError)):
                await mongo.repo.verify_chain(tenant)
        return mongo.collection.calls

    assert with_audit(body) == []


# ---- tenant isolation ------------------------------------------------------------------------------------------------------

def test_each_tenant_has_its_own_independent_chain_and_reads_only_its_own_events():
    async def body(mongo):
        for n in range(1, 5):
            await mongo.repo.append_event(ev(n, tenant="tenant_a"))
            await mongo.repo.append_event(ev(n, tenant="tenant_b"))
        await mongo.repo.append_event(ev(9, tenant="tenant_a"))
        return (mongo.docs, await mongo.repo.query_events("tenant_a"), await mongo.repo.query_events("tenant_b"),
                await mongo.repo.query_events("tenant_c"), await mongo.repo.chain_head("tenant_b"))

    docs, a, b, c, head_b = with_audit(body)
    assert [d["seq"] for d in docs if d["tenant_id"] == "tenant_a"] == [1, 2, 3, 4, 5]
    assert [d["seq"] for d in docs if d["tenant_id"] == "tenant_b"] == [1, 2, 3, 4]
    first_a = next(d for d in docs if d["tenant_id"] == "tenant_a" and d["seq"] == 1)
    first_b = next(d for d in docs if d["tenant_id"] == "tenant_b" and d["seq"] == 1)
    assert first_a["prev_hash"] == first_b["prev_hash"] == GENESIS_HASH and first_a["hash"] != first_b["hash"]
    assert {e.tenant_id for e in a} == {"tenant_a"} and len(a) == 5 and {e.tenant_id for e in b} == {"tenant_b"} and len(b) == 4
    assert c == [] and head_b[0] == 4


def test_every_driver_call_carries_the_tenant():
    async def body(mongo):
        await fill(mongo.repo, 3, tenant="tenant_a")
        await mongo.repo.query_events("tenant_a", resource_id="doc_1", action="document.upload", start_time=at(0), end_time=at(9))
        await mongo.repo.verify_chain("tenant_a")
        await mongo.repo.chain_head("tenant_a")
        return mongo.collection.calls

    calls = with_audit(body)
    assert {c[0] for c in calls} == {"find_one", "find", "insert_one", "count_documents"}
    assert all(call[1]["tenant_id"] == "tenant_a" for call in calls)


@pytest.mark.parametrize("tenant", ["", "   ", None, 5, {"$ne": None}, ["a"], "../x", "a" * 300])
def test_an_invalid_tenant_is_refused_before_the_driver(tenant):
    async def body(mongo):
        with pytest.raises((TenantScopeError, UnsafeQueryError)):
            await mongo.repo.query_events(tenant)
        with pytest.raises((TenantScopeError, UnsafeQueryError)):
            await mongo.repo.chain_head(tenant)
        return mongo.collection.calls

    assert with_audit(body) == []


def test_an_event_is_appended_only_to_the_chain_of_its_own_tenant():
    async def body(mongo):
        await mongo.repo.append_event(ev(1, tenant="tenant_a"))
        await mongo.repo.append_event(ev(1, tenant="tenant_b"))
        return {(d["tenant_id"], json.loads(d["event_json"])["tenant_id"]) for d in mongo.docs}

    assert with_audit(body) == {("tenant_a", "tenant_a"), ("tenant_b", "tenant_b")}


# ---- sequence allocation and concurrency -----------------------------------------------------------------------------------

def test_many_concurrent_appends_to_one_tenant_get_distinct_consecutive_sequence_numbers_and_a_valid_chain():
    async def body(mongo):
        await asyncio.gather(*(mongo.repo.append_event(ev(n)) for n in range(1, 41)))
        return mongo.docs, await mongo.repo.verify_chain("tenant_a"), await mongo.repo.query_events("tenant_a")

    docs, verification, events = with_audit(body)
    assert [d["seq"] for d in docs] == list(range(1, 41))                              # no duplicate, no gap
    assert verification.valid and verification.checked_events == 40
    assert sorted(e.event_id for e in events) == sorted(f"evt_tenant_a_{n}" for n in range(1, 41))     # every event once
    assert all(docs[i]["prev_hash"] == docs[i - 1]["hash"] for i in range(1, 40))      # the link survives the races


def test_two_repository_instances_racing_on_one_database_keep_one_valid_chain():
    """Two 'processes': separate repository objects (no shared Python state) over the same database."""
    async def body(mongo):
        other = MongoAuditRepository(mongo.connection, sleep=no_pause)
        calls = []
        for n in range(1, 31):
            calls.append((mongo.repo if n % 2 else other).append_event(ev(n)))
        await asyncio.gather(*calls)
        return mongo.docs, await mongo.repo.verify_chain("tenant_a")

    docs, verification = with_audit(body)
    assert [d["seq"] for d in docs] == list(range(1, 31)) and verification.valid


def test_concurrent_appends_to_different_tenants_do_not_contend_or_interfere():
    async def body(mongo):
        await asyncio.gather(*(mongo.repo.append_event(ev(n, tenant=f"tenant_{t}")) for t in range(4) for n in range(1, 11)))
        return [await mongo.repo.verify_chain(f"tenant_{t}") for t in range(4)], mongo.docs

    results, docs = with_audit(body)
    assert all(r.valid and r.checked_events == 10 for r in results) and len(docs) == 40


def test_the_unique_index_is_what_prevents_a_fork_and_the_repository_refuses_to_run_without_it():
    async def refused(mongo):
        with pytest.raises(PersistenceError) as caught:
            await mongo.repo.append_event(ev(1))
        return caught.value, mongo.collection.docs

    error, docs = with_audit(refused, indexes=False)
    assert error.details["reason"] == "audit_sequence_index_is_missing" and docs == []

    async def forked(mongo):
        mongo.repo._index_verified = True                                   # force the unsafe configuration to show why it is refused
        await asyncio.gather(*(mongo.repo.append_event(ev(n)) for n in range(1, 9)))
        return [d["seq"] for d in mongo.collection.docs], await mongo.repo.verify_chain("tenant_a")

    seqs, verification = with_audit(forked, indexes=False)
    assert len(seqs) != len(set(seqs)) and verification.valid is False       # without the index the chain forks


def test_a_writer_that_loses_a_race_retries_on_the_new_head_and_keeps_the_previous_hash_relationship():
    async def body(mongo):
        other = MongoAuditRepository(mongo.connection, sleep=no_pause)
        original = mongo.collection.find_one
        state = {"raced": False}

        async def racing(flt, sort=None, session=None):
            result = await original(flt, sort=sort, session=session)
            if not state["raced"] and sort == [("seq", -1)]:
                state["raced"] = True
                await other.append_event(ev(100))                            # another writer takes sequence 1 right after our read
            return result

        mongo.collection.find_one = racing
        await mongo.repo.append_event(ev(1))
        mongo.collection.find_one = original
        return mongo.docs, await mongo.repo.verify_chain("tenant_a")

    docs, verification = with_audit(body)
    assert [json.loads(d["event_json"])["event_id"] for d in docs] == ["evt_tenant_a_100", "evt_tenant_a_1"]
    assert docs[1]["seq"] == 2 and docs[1]["prev_hash"] == docs[0]["hash"] and verification.valid


def test_retries_are_bounded_and_an_exhausted_budget_fails_the_append_visibly():
    pauses = []

    async def count_pause(seconds):
        pauses.append(seconds)

    async def body(mongo):
        attempts = []

        async def always_taken(doc, session=None):
            attempts.append(doc["seq"])
            raise driver.DuplicateKeyError(f"E11000 duplicate key {SECRET}", 11000)

        mongo.collection.insert_one = always_taken
        with pytest.raises(PersistenceUnavailableError) as caught:
            await mongo.repo.append_event(ev(1))
        return caught.value, attempts, mongo.collection.docs

    error, attempts, docs = with_audit(body, max_attempts=5, sleep=count_pause, jitter=lambda: 1.0)
    assert error.details["reason"] == "audit_append_contended" and SECRET not in str(error) + repr(error.details)
    assert len(attempts) == 5 and len(pauses) == 5 and docs == []              # bounded, paused between attempts, nothing recorded
    assert all(0 < p <= 0.05 for p in pauses) and pauses == sorted(pauses)


def test_a_duplicate_that_is_this_very_record_counts_as_recorded_and_is_not_written_twice():
    async def body(mongo):
        original = mongo.collection.insert_one

        async def recorded_then_reported_as_duplicate(doc, session=None):
            await original(doc, session=session)
            raise driver.DuplicateKeyError("E11000", 11000)

        mongo.collection.insert_one = recorded_then_reported_as_duplicate
        await mongo.repo.append_event(ev(1))
        mongo.collection.insert_one = original
        return mongo.docs, await mongo.repo.verify_chain("tenant_a")

    docs, verification = with_audit(body)
    assert len(docs) == 1 and verification.valid


def test_a_non_duplicate_driver_failure_is_not_retried():
    """After an ambiguous failure a blind retry could record the event twice, so the append fails at once instead."""
    async def body(mongo):
        attempts = []

        async def broken(doc, session=None):
            attempts.append(doc["seq"])
            raise driver.NetworkTimeout(f"lost mongo-0 {SECRET}")

        mongo.collection.insert_one = broken
        with pytest.raises(PersistenceUnavailableError) as caught:
            await mongo.repo.append_event(ev(1))
        return caught.value, attempts, mongo.collection.docs

    error, attempts, docs = with_audit(body)
    assert attempts == [1] and docs == []
    assert SECRET not in str(error) + repr(error.details) and "mongo-0" not in str(error) + repr(error.details) and error.__cause__ is None


@pytest.mark.parametrize("attempts", [0, -1, 1001, "5", None, 1.5, True])
def test_an_unreasonable_retry_budget_is_refused(attempts):
    with pytest.raises(ValueError):
        MongoAuditRepository(MongoConnection(MongoSettings(uri="mongodb://localhost", database="d")), max_attempts=attempts)


# ---- append-only -----------------------------------------------------------------------------------------------------------

def test_the_repository_offers_no_way_to_update_delete_or_insert_a_raw_record():
    public = {name for name, member in inspect.getmembers(MongoAuditRepository) if not name.startswith("_") and callable(member)}
    assert public == {"append_event", "query_events", "verify_chain", "chain_head", "require_unique_sequence_index"}
    for forbidden in ("update_one", "delete_one", "find_one_and_update", "insert_one", "update_status", "delete", "update", "remove", "purge", "truncate"):
        assert not hasattr(MongoAuditRepository, forbidden), forbidden


def test_only_reads_and_inserts_ever_reach_the_driver():
    async def body(mongo):
        await asyncio.gather(*(mongo.repo.append_event(ev(n)) for n in range(1, 11)))
        await mongo.repo.query_events("tenant_a")
        await mongo.repo.verify_chain("tenant_a")
        return {call[0] for call in mongo.collection.calls}

    assert with_audit(body) <= {"insert_one", "find_one", "find", "count_documents"}          # inserts and READS only


def test_history_is_untouched_by_later_appends():
    async def body(mongo):
        await fill(mongo.repo, 3)
        before = [dict(d) for d in mongo.docs]
        await fill(mongo.repo, 3, tenant="tenant_b")
        await mongo.repo.append_event(ev(4))
        return before, mongo.docs

    before, after = with_audit(body)
    assert [d for d in after if d["tenant_id"] == "tenant_a"][:3] == before


# ---- corrupt records -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("damage", [
    {"event_json": "{broken"}, {"event_json": "[]"}, {"event_json": json.dumps({"event_id": "x"})}, {"event_json": None},
    {"event_json": json.dumps({"tenant_id": "tenant_b", "event_id": "e", "actor_id": "a", "action": "x", "resource_id": "r", "resource_type": "t"})},
])
def test_querying_a_corrupt_record_is_a_typed_error_that_leaks_nothing(damage):
    async def body(mongo):
        await fill(mongo.repo, 2)
        mongo.collection.docs[1].update(damage)
        with pytest.raises(PersistenceDataError) as caught:
            await mongo.repo.query_events("tenant_a")
        return caught.value

    error = with_audit(body)
    assert error.details["reason"] == "stored_audit_event_is_not_valid" and SECRET not in str(error) + repr(error.details)


@pytest.mark.parametrize("damage", [{"seq": "2"}, {"hash": "short"}, {"hash": "G" * 64}, {"hash": None}, {"hash": "A" * 64}])
def test_an_append_refuses_to_continue_a_chain_whose_head_cannot_be_read(damage):
    async def body(mongo):
        await fill(mongo.repo, 2)
        head = max(mongo.collection.docs, key=lambda d: d["seq"] if isinstance(d["seq"], int) else 0)
        head.update(damage)
        before = len(mongo.collection.docs)
        with pytest.raises(PersistenceDataError) as caught:
            await mongo.repo.append_event(ev(3))
        return caught.value, before, len(mongo.collection.docs)

    error, before, after = with_audit(body)
    assert error.details["reason"] == "audit_chain_head_is_not_valid" and before == after


# ---- failures and fail-closed ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("failure, expected, reason", [
    (driver.NetworkTimeout("lost mongo-0 SECRET"), PersistenceUnavailableError, "persistence_unavailable"),
    (driver.AutoReconnect("down SECRET"), PersistenceUnavailableError, "persistence_unavailable"),
    (driver.OperationFailure("not authorized SECRET", code=13), PersistenceError, "authorization_failed"),
    (driver.WriteConcernError("wc SECRET") if hasattr(driver, "WriteConcernError") else driver.PyMongoError("x"), PersistenceError, None),
])
def test_a_driver_failure_fails_the_append_with_a_typed_sanitised_error_and_records_nothing(failure, expected, reason):
    async def body(mongo):
        mongo.collection.fail_with = failure
        with LogCapture("doc_extractor") as logs:
            with pytest.raises(expected) as caught:
                await mongo.repo.append_event(ev(1))
        mongo.collection.fail_with = None
        return caught.value, mongo.collection.docs, logs.text

    error, docs, logs = with_audit(body)
    assert isinstance(error, PersistenceError) and docs == []
    if reason:
        assert error.details["reason"] == reason
    assert "SECRET" not in str(error) + repr(error.details) + logs and "mongo-0" not in str(error) + repr(error.details) + logs


def test_a_failed_append_changes_nothing_and_the_next_one_continues_the_same_chain():
    async def body(mongo):
        await fill(mongo.repo, 2)
        mongo.collection.fail_with = driver.AutoReconnect("down")
        with pytest.raises(PersistenceUnavailableError):
            await mongo.repo.append_event(ev(3))
        mongo.collection.fail_with = None
        await mongo.repo.append_event(ev(3))
        return mongo.docs, await mongo.repo.verify_chain("tenant_a")

    docs, verification = with_audit(body)
    assert [d["seq"] for d in docs] == [1, 2, 3] and verification.valid


def test_a_disconnected_repository_raises_a_typed_error():
    async def body(mongo):
        await mongo.connection.aclose()
        for call in (mongo.repo.append_event(ev(1)), mongo.repo.query_events("tenant_a"), mongo.repo.verify_chain("tenant_a")):
            with pytest.raises(PersistenceUnavailableError) as caught:
                await call
            assert caught.value.details["reason"] == "not_connected"

    with_audit(body)


def test_a_failing_index_check_is_a_typed_error_and_nothing_is_written():
    async def body(mongo):
        async def broken():
            raise driver.NetworkTimeout("lost mongo-0 SECRET")

        mongo.collection.list_indexes = broken
        with pytest.raises(PersistenceUnavailableError) as caught:
            await mongo.repo.append_event(ev(1))
        return caught.value, mongo.collection.docs

    error, docs = with_audit(body)
    assert docs == [] and "SECRET" not in str(error) + repr(error.details)


def test_the_index_is_checked_once_and_not_on_every_append():
    async def body(mongo):
        calls = []
        original = mongo.collection.list_indexes

        async def counting():
            calls.append(1)
            return await original()

        mongo.collection.list_indexes = counting
        await fill(mongo.repo, 5)
        return calls

    assert len(with_audit(body)) == 1


def test_only_an_audit_event_record_can_be_appended_and_an_oversized_one_is_refused():
    async def body(mongo):
        for bad in (None, {"event_id": "x"}, "text", ev(1).model_dump()):
            with pytest.raises(UnsafeQueryError):
                await mongo.repo.append_event(bad)
        huge = ev(1, metadata={"outcome_code": "SUCCESS"}).model_copy(update={"actor_id": "a" * (300 * 1024)})
        with pytest.raises(PersistenceDataError) as caught:
            await mongo.repo.append_event(huge)
        return caught.value, mongo.collection.docs

    error, docs = with_audit(body)
    assert error.details["reason"] == "audit_event_is_too_large" and docs == []


# ---- queries: the port's filters, equal to the file repository ---------------------------------------------------------------

def _population():
    events = []
    for n in range(1, 41):
        events.append(ev(n, resource=f"doc_{n % 4}", action=("document.upload" if n % 3 else "api_key.revoked"), minutes=n // 2, micros=n * 11))
    return events


def test_query_filters_and_paging_match_the_file_repository_exactly(tmp_path):
    events = _population()
    file_repo = HashChainedAuditRepository(tmp_path)

    async def body(mongo):
        for event in events:
            await file_repo.append_event(event)
            await mongo.repo.append_event(event)
        mismatches, checked = [], 0
        stamps = [events[0].timestamp, events[9].timestamp, events[19].timestamp, events[-1].timestamp, events[5].timestamp - timedelta(microseconds=1)]
        for resource in (None, "doc_1", "doc_9"):
            for action in (None, "document.upload", "api_key.revoked"):
                for start in (None, stamps[1], stamps[2]):
                    for end in (None, stamps[3], stamps[4]):
                        for limit, offset in ((100, 0), (5, 0), (5, 3), (3, 100), (1000, 2)):
                            expected = await file_repo.query_events("tenant_a", resource, action, start, end, limit, offset)
                            actual = await mongo.repo.query_events("tenant_a", resource, action, start, end, limit, offset)
                            checked += 1
                            if expected != actual:
                                mismatches.append((resource, action, start, end, limit, offset))
        return checked, mismatches

    checked, mismatches = with_audit(body)
    assert checked == 3 * 3 * 3 * 3 * 5 and mismatches == []


def test_time_bounds_are_inclusive_and_exact_to_the_microsecond():
    async def body(mongo):
        base = datetime(2026, 5, 5, 12, 0, 0, 123456, tzinfo=timezone.utc)
        for n, micros in enumerate((0, 1, 2, 3), start=1):
            await mongo.repo.append_event(ev(n, minutes=0).model_copy(update={"timestamp": base + timedelta(microseconds=micros)}))
        window = await mongo.repo.query_events("tenant_a", start_time=base + timedelta(microseconds=1), end_time=base + timedelta(microseconds=2))
        below = await mongo.repo.query_events("tenant_a", end_time=base - timedelta(microseconds=1))
        return [e.event_id for e in window], below

    window, below = with_audit(body)
    assert window == ["evt_tenant_a_2", "evt_tenant_a_3"] and below == []


def test_a_naive_time_bound_is_read_as_utc_and_a_non_datetime_is_refused():
    async def body(mongo):
        await fill(mongo.repo, 3)
        naive = await mongo.repo.query_events("tenant_a", start_time=datetime(2026, 1, 1, 0, 2))      # = at(2) in UTC
        for bad in ("2026-01-01", 5, {"$gt": 1}):
            with pytest.raises(UnsafeQueryError):
                await mongo.repo.query_events("tenant_a", start_time=bad)
        return [e.event_id for e in naive]

    assert with_audit(body) == ["evt_tenant_a_2", "evt_tenant_a_3"]


def test_the_exact_microsecond_helper():
    assert microseconds(datetime(1970, 1, 1, tzinfo=timezone.utc)) == 0
    assert microseconds(datetime(2026, 1, 1, 0, 0, 0, 999999, tzinfo=timezone.utc)) % 1_000_000 == 999999
    assert microseconds(datetime(2026, 1, 1)) == microseconds(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert microseconds(datetime(2026, 1, 1, 5, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))) == microseconds(datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_paging_bounds_chunks_and_order():
    async def body(mongo):
        await fill(mongo.repo, 1250)
        everything = await mongo.repo.query_events("tenant_a", limit=5000)
        page = await mongo.repo.query_events("tenant_a", limit=10, offset=1240)
        zero = await mongo.repo.query_events("tenant_a", limit=0)
        for kwargs in ({"limit": -1}, {"offset": -1}, {"limit": "5"}, {"offset": 1.5}):
            with pytest.raises(UnsafeQueryError):
                await mongo.repo.query_events("tenant_a", **kwargs)
        return everything, page, zero

    everything, page, zero = with_audit(body)
    assert [e.event_id for e in everything] == [f"evt_tenant_a_{n}" for n in range(1, 1251)] and len(page) == 10 and zero == []


# ---- the logger on top: fail closed, sanitised ------------------------------------------------------------------------------

def test_the_durable_logger_persists_sanitised_events_through_the_repository():
    async def body(mongo):
        logger = DurableAuditLogger(mongo.repo)
        await logger.log_action("actor_1", "tenant_a", "api_key.created", "key_1", "api_key", request_id="req_1",
                                metadata={"outcome_code": "SUCCESS", "password": SECRET, "note": SECRET, "token": SECRET})
        return await mongo.repo.query_events("tenant_a"), mongo.collection.docs

    events, docs = with_audit(body)
    assert len(events) == 1 and events[0].action == "api_key.created" and events[0].metadata == {"outcome_code": "SUCCESS"}
    assert SECRET not in repr(docs)                                        # the existing sanitisation applies before anything is stored


def test_a_failed_audit_write_fails_the_operation_closed_through_the_logger():
    async def body(mongo):
        logger = DurableAuditLogger(mongo.repo)
        assert logger.propagates_failures is True
        mongo.collection.fail_with = driver.NetworkTimeout(f"lost mongo-0 {SECRET}")
        with pytest.raises(AuditUnavailableError) as caught:
            await logger.log_action("actor_1", "tenant_a", "api_key.created", "key_1", "api_key")
        mongo.collection.fail_with = None
        return caught.value, mongo.collection.docs

    error, docs = with_audit(body)
    assert docs == [] and SECRET not in str(error) + repr(getattr(error, "details", ""))


def test_contention_beyond_the_budget_fails_the_logged_operation_closed():
    async def body(mongo):
        async def taken(doc, session=None):
            raise driver.DuplicateKeyError("E11000", 11000)

        mongo.collection.insert_one = taken
        with pytest.raises(AuditUnavailableError):
            await DurableAuditLogger(mongo.repo).log_action("actor_1", "tenant_a", "x.y", "r", "t")

    with_audit(body, max_attempts=3)


def test_key_creation_fails_closed_and_is_rolled_back_when_the_mongo_audit_write_fails():
    from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
    from doc_extractor.api.services.api_key_service import ApiKeyService
    from doc_extractor.infrastructure.mongo.api_keys import MongoApiKeyRepository

    async def body(mongo):
        keys = MongoApiKeyRepository(mongo.connection)
        service = ApiKeyService(repository=keys, audit_logger=DurableAuditLogger(mongo.repo))
        admin = ApiPrincipal(principal_id="admin", tenant_id="tenant_a", principal_type=PrincipalType.USER, roles={"admin"},
                             scopes={"api_keys:read", "api_keys:write", "documents:read"})
        ok = await service.create_api_key(admin, "first", ["documents:read"], ["integrator"], expires_in_days=10)
        mongo.collection.fail_with = driver.NetworkTimeout("down")
        with pytest.raises(AuditUnavailableError):
            await service.create_api_key(admin, "second", ["documents:read"], ["integrator"], expires_in_days=10)
        mongo.collection.fail_with = None
        listed = await keys.list_keys("tenant_a")
        return ok, listed, await mongo.repo.query_events("tenant_a")

    async def go():
        mongo = await build()
        await ensure_indexes(mongo.client["doc"], DEFAULT_REGISTRY)
        return await body(mongo)

    ok, listed, events = asyncio.run(go())
    by_name = {k.name: k for k in listed}                                   # created within one millisecond: order is by key id
    assert set(by_name) == {"first", "second"} and by_name["second"].is_active is False and by_name["first"].is_active is True
    assert [e.action for e in events] == ["api_key.created"]               # the failed creation left no audit record and no usable key


# ---- the repository's own hygiene -------------------------------------------------------------------------------------------

def test_the_repository_logs_nothing_and_names_no_substitute():
    source = audit_module.__doc__ + inspect.getsource(audit_module)
    for forbidden in ("logging", "logger", "InMemory", "mongomock", "fallback_to", "review_task_double", "LocalMemory", "print("):
        assert forbidden not in source, forbidden


def test_appending_and_verifying_log_no_event_content():
    async def body(mongo):
        with LogCapture("doc_extractor") as logs:
            await mongo.repo.append_event(ev(1, actor=SECRET, resource=SECRET))
            await mongo.repo.query_events("tenant_a")
            await mongo.repo.verify_chain("tenant_a")
        return logs.text

    assert SECRET not in with_audit(body)


# ---- the unique-index start-up gate ----------------------------------------------------------------------------------------

def test_the_start_up_gate_accepts_only_the_unique_tenant_and_sequence_index():
    async def body(mongo):
        with pytest.raises(PersistenceConfigurationError) as missing:
            await mongo.repo.require_unique_sequence_index()
        collection = mongo.collection
        await collection.create_index([("tenant_id", 1), ("seq", 1)], name="not_unique")
        with pytest.raises(PersistenceConfigurationError):
            await mongo.repo.require_unique_sequence_index()
        await collection.create_index([("seq", 1), ("tenant_id", 1)], name="wrong_order", unique=True)
        with pytest.raises(PersistenceConfigurationError):
            await mongo.repo.require_unique_sequence_index()
        await collection.create_index([("tenant_id", 1), ("seq", 1), ("action", 1)], name="longer", unique=True)
        with pytest.raises(PersistenceConfigurationError):
            await mongo.repo.require_unique_sequence_index()
        await collection.create_index([("tenant_id", 1), ("seq", 1)], name="audit_tenant_seq_uq", unique=True)
        await mongo.repo.require_unique_sequence_index()
        return missing.value

    error = with_audit(body, indexes=False)
    assert error.details == {"reason": "audit_sequence_index_is_missing", "setting": "mongo_ensure_indexes"}


def test_the_declared_audit_indexes_are_tenant_first_and_only_the_sequence_index_is_unique():
    specs = {spec.name: spec for spec in DEFAULT_REGISTRY.for_collection(AUDIT_EVENTS)}
    assert set(specs) == {"audit_tenant_seq_uq", "audit_tenant_resource_seq", "audit_tenant_action_seq", "audit_tenant_time_seq"}
    assert specs["audit_tenant_seq_uq"].unique is True and specs["audit_tenant_seq_uq"].keys == (("tenant_id", 1), ("seq", 1))
    assert [name for name, spec in specs.items() if spec.unique] == ["audit_tenant_seq_uq"]
    assert all(spec.keys[0][0] == "tenant_id" and spec.keys[-1][0] == "seq" for spec in specs.values()) and not any(s.tenant_exempt for s in specs.values())


# ---- verification finds a fork wherever it falls, and records outside the sequence ---------------------------------------------

@pytest.mark.parametrize("batch_size", [1, 2, 3, 4, 5, 7, 500])
@pytest.mark.parametrize("duplicated", [1, 2, 3, 4, 5, 6])
def test_a_second_record_at_one_sequence_number_is_found_whatever_the_batch_boundary(batch_size, duplicated):
    async def body(mongo):
        await fill(mongo.repo, 6)
        original = next(d for d in mongo.collection.docs if d["seq"] == duplicated)
        mongo.collection.docs.append({**original, "event_id": "evt_fork"})          # a fork: two records, one sequence number
        return await mongo.repo.verify_chain("tenant_a", batch_size=batch_size)

    result = with_audit(body)
    assert result.valid is False and result.reason == "sequence or link broken", (batch_size, duplicated)
    assert result.first_bad_seq == duplicated + 1 and result.checked_events == duplicated      # exactly where the chain stops making sense


def test_a_stray_record_that_sorts_outside_the_numeric_sequence_cannot_hide_at_the_end():
    async def body(mongo):
        await fill(mongo.repo, 4)
        stray = forge("tenant_a", 5, mongo.docs[-1]["hash"])
        stray["seq"] = "5"                                                              # a string where the number belongs
        mongo.collection.docs.append(stray)
        return await mongo.repo.verify_chain("tenant_a"), await mongo.repo.verify_chain("tenant_a", batch_size=2)

    result, small = with_audit(body)
    assert result.valid is False and result.reason == "sequence or link broken" and result.first_bad_seq == 5 and small == result


def test_a_chain_longer_than_one_batch_verifies_and_reports_a_late_defect_at_its_true_position():
    async def body(mongo):
        await fill(mongo.repo, 25)
        intact = await mongo.repo.verify_chain("tenant_a", batch_size=4)
        next(d for d in mongo.collection.docs if d["seq"] == 19)["hash"] = "a" * 64
        return intact, await mongo.repo.verify_chain("tenant_a", batch_size=4)

    intact, broken = with_audit(body)
    assert intact.valid and intact.checked_events == 25
    assert (broken.valid, broken.first_bad_seq, broken.checked_events, broken.reason) == (False, 19, 18, "hash mismatch")


@pytest.mark.parametrize("kwargs", [{"resource_id": {"$ne": ""}}, {"action": {"$regex": ".*"}}, {"resource_id": ["doc_1"]}, {"action": 5},
                                    {"resource_id": b"doc_1"}])
def test_a_filter_value_must_be_an_exact_string_and_never_an_operator_expression(kwargs):
    async def body(mongo):
        await fill(mongo.repo, 3)
        before = len(mongo.collection.calls)
        with pytest.raises(UnsafeQueryError) as caught:
            await mongo.repo.query_events("tenant_a", **kwargs)
        return caught.value, len(mongo.collection.calls) - before

    error, new_calls = with_audit(body)
    assert error.details["reason"] == "filter_value_is_not_a_string" and new_calls == 0


# ---- composition ---------------------------------------------------------------------------------------------------------------

from doc_extractor.api.app import create_app  # noqa: E402
from doc_extractor.api.composition import ApplicationContainer  # noqa: E402
from doc_extractor.api.config import ApiSettings  # noqa: E402
from doc_extractor.malware import MockMalwareScanner  # noqa: E402

URI = "mongodb://app:Audit-Secret-Marker-7@mongo-0:27017/?replicaSet=rs0"


def container_with(client=None, env="development", **kwargs):
    client = client or FakeClient()
    values = {"app_env": env, "mongo_uri": URI if env == "development" else URI + "&tls=true", "mongo_database": "doc"}
    values.update(kwargs.pop("settings", {}))
    settings = ApiSettings(**values)
    connection = MongoConnection(MongoSettings.from_api_settings(settings), client_factory=factory_for(client))
    kwargs.setdefault("malware_scanner", MockMalwareScanner())
    return ApplicationContainer(api_settings=settings, mongo_connection=connection, **kwargs), client


class _Sink:
    propagates_failures = True

    async def log_action(self, **event):
        return None

    async def log_event(self, event):
        return None


def test_start_up_wires_the_durable_audit_repository_and_a_fail_closed_logger():
    from doc_extractor.api.ports.audit import SafeStructuredAuditLogger

    container, client = container_with()
    assert container.audit_repo is None and container.audit_logger is None          # construction opens and wires nothing
    run(container.startup())
    assert isinstance(container.audit_repo, MongoAuditRepository) and isinstance(container.audit_repo, AuditRepository)
    assert isinstance(container.audit_logger, DurableAuditLogger) and container.audit_logger.propagates_failures is True
    assert not isinstance(container.audit_logger, SafeStructuredAuditLogger)        # never a best-effort stand-in
    repository, logger = container.audit_repo, container.audit_logger
    run(container.startup())
    assert container.audit_repo is repository and container.audit_logger is logger  # idempotent


@pytest.mark.parametrize("env", ["development", "staging", "production"])
def test_start_up_wires_audit_in_every_environment_that_has_a_database(env):
    container, _ = container_with(env=env)
    run(container.startup())
    assert isinstance(container.audit_repo, MongoAuditRepository) and isinstance(container.audit_logger, DurableAuditLogger)


def test_an_injected_audit_logger_is_never_replaced():
    sink = _Sink()
    container, _ = container_with(audit_logger=sink)
    run(container.startup())
    assert container.audit_logger is sink and container.audit_repo is None


def test_an_injected_audit_repository_is_never_replaced():
    injected = object()
    container, _ = container_with(audit_repo=injected)
    run(container.startup())
    assert container.audit_repo is injected and container.audit_logger is None       # exactly as before this phase


def test_an_explicitly_configured_file_audit_store_is_never_replaced(tmp_path):
    container, _ = container_with(settings={"audit_store_dir": str(tmp_path)})
    configured = container.audit_repo
    assert isinstance(configured, HashChainedAuditRepository)
    run(container.startup())
    assert container.audit_repo is configured and isinstance(container.audit_logger, DurableAuditLogger)
    assert not isinstance(container.audit_repo, MongoAuditRepository)


def test_without_mongodb_no_audit_sink_is_created():
    container = ApplicationContainer(api_settings=ApiSettings(app_env="development"), malware_scanner=MockMalwareScanner())
    run(container.startup())
    assert container.audit_repo is None and container.audit_logger is None


def _nothing_configured(container):
    return (container.audit_repo is None and container.audit_logger is None and container.review_task_repo is None
            and container.api_key_repo is None)


def test_a_failed_start_up_leaves_no_audit_sink_and_no_other_adapter_configured():
    unreachable = FakeClient(fail_connect=driver.NetworkTimeout("down"))
    container, _ = container_with(unreachable)
    with pytest.raises(PersistenceUnavailableError):
        run(container.startup())
    assert _nothing_configured(container) and unreachable.closed is True

    client = FakeClient()
    container, _ = container_with(client)
    client["doc"][AUDIT_EVENTS].fail_with = driver.OperationFailure("boom", code=85)       # the index bootstrap fails
    with pytest.raises(PersistenceError):
        run(container.startup())
    assert _nothing_configured(container) and client.closed is True

    client = FakeClient()
    container, _ = container_with(client, settings={"mongo_ensure_indexes_on_startup": False})   # bootstrap off, nobody migrated
    with pytest.raises(PersistenceConfigurationError) as caught:
        run(container.startup())
    assert caught.value.details["reason"] == "audit_sequence_index_is_missing" and _nothing_configured(container) and client.closed is True


def test_with_bootstrap_off_the_chain_index_must_already_exist_and_then_start_up_succeeds():
    container, client = container_with(settings={"mongo_ensure_indexes_on_startup": False})
    run(ensure_indexes(client["doc"], DEFAULT_REGISTRY))                             # what a migration job does
    run(container.startup())
    assert isinstance(container.audit_repo, MongoAuditRepository)


def test_a_failed_start_up_can_be_retried_and_then_configures_audit():
    client = FakeClient(fail_connect=driver.AutoReconnect("down"))
    container, _ = container_with(client)
    with pytest.raises(PersistenceUnavailableError):
        run(container.startup())
    client.fail_connect, client.closed = None, False
    run(container.startup())
    assert isinstance(container.audit_repo, MongoAuditRepository) and isinstance(container.audit_logger, DurableAuditLogger)


def test_the_chain_survives_a_restart_and_a_second_repository_instance_continues_it():
    client = FakeClient()
    first, _ = container_with(client)
    run(first.startup())
    run(fill(first.audit_repo, 3))
    run(first.ashutdown())
    client.closed = False
    second, _ = container_with(client)                                               # a new process over the same database
    run(second.startup())
    run(second.audit_repo.append_event(ev(4)))
    events = run(second.audit_repo.query_events("tenant_a"))
    assert [e.event_id for e in events] == [f"evt_tenant_a_{n}" for n in (1, 2, 3, 4)]
    assert run(second.audit_repo.verify_chain("tenant_a")).valid and run(second.audit_repo.chain_head("tenant_a"))[0] == 4


def test_the_lifespan_publishes_the_audit_sink_and_never_replaces_an_explicit_override():
    from fastapi.testclient import TestClient

    container, _ = container_with()
    app = create_app(container.api_settings, container)
    assert app.state.audit_logger is None and app.state.audit_repo is None            # copied before start-up configured them
    with TestClient(app):
        assert app.state.audit_logger is container.audit_logger and app.state.audit_repo is container.audit_repo
        assert isinstance(app.state.audit_logger, DurableAuditLogger)

    explicit = _Sink()
    container, _ = container_with()
    app = create_app(container.api_settings, container)
    app.state.audit_logger = explicit
    with TestClient(app):
        assert app.state.audit_logger is explicit and isinstance(app.state.audit_repo, MongoAuditRepository)


def test_a_failed_start_up_publishes_no_audit_sink_to_the_application_state():
    from fastapi.testclient import TestClient

    container, _ = container_with(FakeClient(fail_connect=driver.NetworkTimeout("down")))
    app = create_app(container.api_settings, container)
    with pytest.raises(PersistenceUnavailableError):
        with TestClient(app):
            pytest.fail("the application must not start")
    assert app.state.audit_logger is None and app.state.audit_repo is None


def test_a_failed_api_key_login_is_recorded_durably_without_the_credential_or_the_hash():
    """What wiring the durable logger now does for the existing provider: failures are audited (identifiers and codes only)."""
    from fastapi.testclient import TestClient

    container, _ = container_with()
    app = create_app(container.api_settings, container)
    token = "dex_live_" + "Z" * 60
    with LogCapture("doc_extractor") as logs, TestClient(app, raise_server_exceptions=False) as http:
        assert http.get("/api/v1/review-tasks", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    events = run(container.audit_repo.query_events("unknown"))
    assert [(e.action, e.outcome) for e in events] == [("api_key.authentication_failed", "FAILED")]
    stored = " ".join(str(d) for d in container.mongo.client["doc"][AUDIT_EVENTS].docs)
    assert token not in stored and token not in logs.text and "ZZZZZZZZZZ" not in stored
    assert run(container.audit_repo.verify_chain("unknown")).valid
