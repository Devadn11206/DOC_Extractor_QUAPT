"""
REAL MongoDB integration tests (Team Member 3, Phase 2A). OPT-IN: they need a running replica set.

They run only when ``DOC_TEST_MONGO_URI`` names a MongoDB replica set that the test may create databases in, for example the
single-node set from ``deploy/compose/mongo-replica.yml``:

    docker compose -f deploy/compose/mongo-replica.yml up -d --wait
    DOC_TEST_MONGO_URI='mongodb://localhost:27017/?replicaSet=rs0' python -m pytest tests/test_mongo_integration.py

``DOC_TEST_MONGO_REPLICA_SET`` (default ``rs0``) names the expected set. ``DOC_TEST_MONGO_STANDALONE_URI`` (optional) names a
STANDALONE server for the check that a standalone is refused where a replica set is required.

Each test uses its own database name (``doc_it_<random>``) and drops it afterwards. Without the variable every test skips
with its reason; a skipped test proves nothing. The CI job ``mongo-integration`` runs them against a real replica set.
"""

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

URI = os.environ.get("DOC_TEST_MONGO_URI", "").strip()
REPLICA_SET = os.environ.get("DOC_TEST_MONGO_REPLICA_SET", "rs0").strip()
STANDALONE_URI = os.environ.get("DOC_TEST_MONGO_STANDALONE_URI", "").strip()

requires_mongo = pytest.mark.skipif(not URI, reason="set DOC_TEST_MONGO_URI to run against a real MongoDB replica set")
requires_standalone = pytest.mark.skipif(not STANDALONE_URI, reason="set DOC_TEST_MONGO_STANDALONE_URI to run the standalone check")

if URI:                                                   # the driver and the foundation are needed only when the tests run
    from doc_extractor.api.audit_chain import GENESIS_HASH, compute_event_hash
    from doc_extractor.api.contracts.records import ApiKeyRecord, AuditEventRecord, ReviewTaskRecord, ReviewTaskStatus
    from doc_extractor.api.ports.repositories import ReviewTaskAssignee, ReviewTaskListQuery, ReviewTaskTransition
    from doc_extractor.infrastructure.mongo import (
        DuplicateKeyPersistenceError,
        MongoConnection,
        MongoSettings,
        PersistenceConfigurationError,
        PersistenceUnavailableError,
        TenantScopedRepository,
        TransactionUnavailableError,
        ensure_indexes,
        from_document,
        to_document,
    )
    from doc_extractor.infrastructure.mongo.indexes import AUDIT_EVENTS, DEFAULT_REGISTRY, REVIEW_TASKS
    from doc_extractor.infrastructure.mongo.api_keys import DuplicateApiKeyError, MongoApiKeyRepository
    from doc_extractor.infrastructure.mongo.audit import MongoAuditRepository
    from doc_extractor.infrastructure.mongo.review_tasks import DuplicateReviewTaskError, MongoReviewTaskRepository


def run_with_database(body, uri=None, replica_set=REPLICA_SET, **settings_overrides):
    """Run ``body(connection)`` against a fresh database, then drop it. One event loop per call (the client is loop-bound)."""

    async def go():
        settings = MongoSettings(uri=uri or URI, database=f"doc_it_{uuid.uuid4().hex[:12]}", app_env="development",
                                 replica_set=replica_set, **settings_overrides)
        connection = MongoConnection(settings)
        await connection.connect()
        try:
            return await body(connection)
        finally:
            await connection.client.drop_database(settings.database)
            await connection.aclose()

    return asyncio.run(go())


@requires_mongo
def test_connect_verifies_a_real_replica_set_and_health_is_ok():
    async def body(connection):
        assert connection.is_connected and connection.replica_set_name == REPLICA_SET and connection.supports_transactions
        health = await connection.health()
        assert health.ok and health.replica_set == REPLICA_SET and health.latency_ms is not None

    run_with_database(body)


@requires_mongo
def test_the_index_bootstrap_creates_every_declared_index_and_is_idempotent_on_a_real_server():
    async def body(connection):
        first = await ensure_indexes(connection.database)
        second = await ensure_indexes(connection.database)
        third = await ensure_indexes(connection.database)
        assert len(first.created) == len(DEFAULT_REGISTRY.all()) and second.created == () and third.created == ()
        names = {entry["name"] async for entry in await connection.collection(REVIEW_TASKS).list_indexes()}
        assert {"rt_tenant_task_uq", "rt_tenant_document_created", "rt_tenant_status_created_task"} <= names
        info = await connection.collection(REVIEW_TASKS).index_information()
        assert info["rt_tenant_task_uq"]["unique"] is True and "unique" not in info["rt_tenant_document_created"]

    run_with_database(body)


@requires_mongo
def test_a_unique_index_refuses_a_duplicate_with_a_typed_error_and_tenants_stay_separate():
    async def body(connection):
        await ensure_indexes(connection.database)
        repo = TenantScopedRepository(connection, REVIEW_TASKS)
        await repo.insert_one("tenant_a", {"task_id": "t1", "document_id": "d1"})
        await repo.insert_one("tenant_b", {"task_id": "t1", "document_id": "d1"})        # same ids, other tenant: allowed
        with pytest.raises(DuplicateKeyPersistenceError) as caught:
            await repo.insert_one("tenant_a", {"task_id": "t1", "document_id": "d2"})
        assert "t1" not in str(caught.value) and "tenant_a" not in str(caught.value)

    run_with_database(body)


@requires_mongo
def test_two_tasks_may_exist_for_one_document_until_the_product_rule_is_approved():
    async def body(connection):
        await ensure_indexes(connection.database)
        repo = TenantScopedRepository(connection, REVIEW_TASKS)
        await repo.insert_one("tenant_a", {"task_id": "t1", "document_id": "d1"})
        await repo.insert_one("tenant_a", {"task_id": "t2", "document_id": "d1"})        # non-unique lookup: no refusal
        assert await repo.count("tenant_a", {"document_id": "d1"}) == 2

    run_with_database(body)


@requires_mongo
def test_tenant_isolation_holds_on_a_real_server():
    async def body(connection):
        repo = TenantScopedRepository(connection, "isolation")
        for tenant, key in (("tenant_a", "k1"), ("tenant_a", "k2"), ("tenant_b", "k1"), ("tenant_b", "k3")):
            await repo.insert_one(tenant, {"key": key})
        assert {d["key"] for d in await repo.find_many("tenant_a")} == {"k1", "k2"}
        assert await repo.find_one("tenant_a", {"key": "k3"}) is None
        assert await repo.update_one("tenant_a", {"key": "k3"}, {"$set": {"x": 1}}) == 0
        assert await repo.delete_one("tenant_a", {"key": "k3"}) == 0
        assert await repo.count("tenant_b") == 2
        assert "_id" not in await repo.find_one("tenant_b", {"key": "k3"})

    run_with_database(body)


@requires_mongo
def test_one_conditional_update_has_exactly_one_winner_under_concurrency():
    async def body(connection):
        repo = TenantScopedRepository(connection, "claims")
        await repo.insert_one("tenant_a", {"task_id": "t1", "status": "unassigned", "owner": None})

        async def claim(reviewer):
            return await repo.find_one_and_update("tenant_a", {"task_id": "t1", "status": "unassigned"},
                                                  {"$set": {"status": "claimed", "owner": reviewer}})

        results = await asyncio.gather(*(claim(f"r{n}") for n in range(25)))
        winners = [doc for doc in results if doc is not None]
        assert len(winners) == 1
        final = await repo.find_one("tenant_a", {"task_id": "t1"})
        assert final["status"] == "claimed" and final["owner"] == winners[0]["owner"]

    run_with_database(body)


@requires_mongo
def test_a_transaction_commits_together_and_aborts_together():
    async def body(connection):
        repo = TenantScopedRepository(connection, "txn")

        async def both(session):
            await repo.insert_one("tenant_a", {"key": "a"}, session=session)
            await repo.insert_one("tenant_a", {"key": "b"}, session=session)

        await connection.run_in_transaction(both)
        assert await repo.count("tenant_a") == 2

        async def fails(session):
            await repo.insert_one("tenant_a", {"key": "c"}, session=session)
            raise RuntimeError("abort after the first write")

        with pytest.raises(RuntimeError, match="abort after the first write"):    # the application's own exception type survives
            await connection.run_in_transaction(fails)
        assert await repo.count("tenant_a", {"key": "c"}) == 0           # rolled back

    run_with_database(body)


@requires_mongo
def test_reads_inside_a_transaction_use_its_session_and_stay_tenant_scoped():
    async def body(connection):
        repo = TenantScopedRepository(connection, "txn_reads")
        await repo.insert_one("tenant_b", {"key": "other"})

        async def work(session):
            await repo.insert_one("tenant_a", {"key": "mine"}, session=session)
            assert (await repo.find_one("tenant_a", {"key": "mine"}, session=session))["key"] == "mine"
            assert await repo.count("tenant_a", session=session) == 1
            assert [d["key"] for d in await repo.find_many("tenant_a", session=session)] == ["mine"]
            assert await repo.count("tenant_b", {"key": "mine"}, session=session) == 0
            assert await repo.find_one("tenant_a", {"key": "other"}, session=session) is None
            assert await repo.count("tenant_a") == 0                     # without the session the uncommitted write is invisible

        await connection.run_in_transaction(work)
        assert await repo.count("tenant_a") == 1

    run_with_database(body)


@requires_mongo
def test_dotted_updates_push_each_and_the_id_rule_work_on_a_real_server():
    async def body(connection):
        repo = TenantScopedRepository(connection, "updates")
        await repo.insert_one("tenant_a", {"key": "k", "metadata": {"count": 1}})
        await repo.insert_one("tenant_b", {"key": "k"})
        assert await repo.update_one("tenant_a", {"key": "k"}, {"$set": {"metadata.count": 2, "metadata.note": "x"}}) == 1
        assert await repo.update_one("tenant_a", {"key": "k"}, {"$push": {"flags": {"$each": ["a", "b"]}}}) == 1
        stored = await repo.find_one("tenant_a", {"key": "k"})
        assert stored["metadata"] == {"count": 2, "note": "x"} and stored["flags"] == ["a", "b"]
        assert "metadata" not in await repo.find_one("tenant_b", {"key": "k"})
        with pytest.raises(Exception) as refused:                        # a caller-chosen _id never reaches the server
            await repo.insert_one("tenant_a", {"_id": "chosen", "key": "other"})
        assert refused.value.details["reason"] == "document_must_not_set_the_id"
        assert await repo.count("tenant_a") == 1

    run_with_database(body)


@requires_mongo
def test_datetimes_round_trip_as_utc_with_millisecond_precision_on_a_real_server():
    async def body(connection):
        repo = TenantScopedRepository(connection, "times")
        task = ReviewTaskRecord(task_id="t1", document_id="d1", tenant_id="tenant_a", status=ReviewTaskStatus.CLAIMED,
                                assigned_reviewer_id="r1",
                                created_at=datetime(2026, 3, 1, 12, 0, 0, 123_456, tzinfo=timezone(timedelta(hours=5, minutes=30))),
                                metadata={"flags": ["A"]})
        await repo.insert_one("tenant_a", to_document(task))
        restored = from_document(ReviewTaskRecord, await repo.find_one("tenant_a", {"task_id": "t1"}))
        assert restored.created_at == datetime(2026, 3, 1, 6, 30, 0, 123_000, tzinfo=timezone.utc)
        assert restored.created_at.utcoffset() == timedelta(0) and restored.status is ReviewTaskStatus.CLAIMED

    run_with_database(body)


@requires_mongo
def test_an_unreachable_server_is_a_bounded_typed_failure():
    async def go():
        settings = MongoSettings(uri="mongodb://127.0.0.1:1/", database="doc_unreachable", app_env="development",
                                 server_selection_timeout_ms=200, connect_timeout_ms=200)
        connection = MongoConnection(settings)
        with pytest.raises(PersistenceUnavailableError):
            await asyncio.wait_for(connection.connect(), timeout=10)
        assert connection.is_connected is False

    asyncio.run(go())


@requires_mongo
@requires_standalone
def test_a_standalone_server_is_refused_when_a_replica_set_is_required():
    async def go():
        connection = MongoConnection(MongoSettings(uri=STANDALONE_URI, database="doc_standalone", app_env="development", replica_set="rs0"))
        with pytest.raises(PersistenceConfigurationError):
            await connection.connect()

    asyncio.run(go())


@requires_mongo
def test_a_transaction_on_a_non_replica_set_is_a_typed_error_when_not_connected():
    async def go():
        connection = MongoConnection(MongoSettings(uri=URI, database="doc_x", app_env="development"))
        with pytest.raises(TransactionUnavailableError):
            await connection.run_in_transaction(lambda session: asyncio.sleep(0))

    asyncio.run(go())


# ---- MongoReviewTaskRepository on a real server (Phase 2A-3) -------------------------------------------------------------

def _rt(task_id, *, tenant="tenant_a", document=None, status=None, owner=None, minutes=0):
    """A review task with whole-millisecond timestamps (stored and read back unchanged)."""
    status = status or ReviewTaskStatus.UNASSIGNED
    created = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    completed = created + timedelta(minutes=1) if status is ReviewTaskStatus.COMPLETED else None
    return ReviewTaskRecord(task_id=task_id, document_id=document or f"doc-{task_id}", tenant_id=tenant, status=status,
                            assigned_reviewer_id=owner, created_at=created, updated_at=created, completed_at=completed)


async def _review_repo(connection, tasks=()):
    await ensure_indexes(connection.database, DEFAULT_REGISTRY)           # the unique (tenant_id, task_id) index
    repo = MongoReviewTaskRepository(connection)
    for task in tasks:
        await repo.create(task.tenant_id, task)
    return repo


@requires_mongo
def test_review_tasks_persist_read_back_list_and_page_on_a_real_server():
    async def body(connection):
        micro = datetime(2026, 1, 1, 0, 0, 0, 123456, tzinfo=timezone.utc)
        repo = await _review_repo(connection, [_rt("a", minutes=5, document="doc-1"), _rt("b", minutes=1, document="doc-1"),
                                               _rt("c", minutes=9, document="doc-2", status=ReviewTaskStatus.CLAIMED, owner="rev_1"),
                                               _rt("x", tenant="tenant_b", minutes=0, document="doc-1")])
        stamped = await repo.create("tenant_a", ReviewTaskRecord(task_id="m", document_id="doc-m", tenant_id="tenant_a",
                                                                created_at=micro, updated_at=micro))
        assert stamped == await repo.get_by_id("tenant_a", "m")
        assert stamped.created_at == datetime(2026, 1, 1, 0, 0, 0, 123000, tzinfo=timezone.utc)        # milliseconds, UTC
        assert (await repo.get_by_document_id("tenant_a", "doc-1")).task_id == "a"                      # the most recent of two
        assert await repo.get_by_document_id("tenant_a", "nope") is None
        page = await repo.list_page("tenant_a", ReviewTaskListQuery(page=1, size=3))
        assert [t.task_id for t in page.items] == ["m", "b", "a"] and page.total_items == 4             # oldest first
        assert [t.task_id for t in (await repo.list_page("tenant_a", ReviewTaskListQuery(page=2, size=3))).items] == ["c"]
        assert (await repo.list_page("tenant_a", ReviewTaskListQuery(page=9, size=3))).items == ()
        free = await repo.list_page("tenant_a", ReviewTaskListQuery(assignee=ReviewTaskAssignee.UNASSIGNED))
        assert sorted(t.task_id for t in free.items) == ["a", "b", "m"]                                  # null owner matches
        mine = await repo.list_page("tenant_a", ReviewTaskListQuery(assignee=ReviewTaskAssignee.MINE, reviewer_id="rev_1"))
        assert [t.task_id for t in mine.items] == ["c"]
        assert [t.task_id for t in await repo.list_tasks("tenant_a", status="claimed")] == ["c"]
        assert [t.task_id for t in await repo.list_tasks("tenant_b")] == ["x"]

    run_with_database(body)


@requires_mongo
def test_task_ids_are_unique_per_tenant_but_a_document_may_have_several_tasks_on_a_real_server():
    async def body(connection):
        repo = await _review_repo(connection, [_rt("t1", document="doc-1")])
        await repo.create("tenant_a", _rt("t2", document="doc-1"))                         # same document: allowed
        await repo.create("tenant_b", _rt("t1", tenant="tenant_b", document="doc-1"))      # same task id, other tenant: allowed
        with pytest.raises(DuplicateReviewTaskError) as refused:
            await repo.create("tenant_a", _rt("t1", document="doc-other"))
        assert isinstance(refused.value, (DuplicateKeyPersistenceError, ValueError))
        assert sorted(t.task_id for t in await repo.list_tasks("tenant_a")) == ["t1", "t2"]

    run_with_database(body)


@requires_mongo
def test_of_many_concurrent_claims_exactly_one_wins_per_task_on_a_real_server():
    async def body(connection):
        repo = await _review_repo(connection, [_rt(f"t{n}") for n in range(6)])
        now = datetime(2026, 2, 1, tzinfo=timezone.utc)
        calls = [repo.claim("tenant_a", f"t{n}", f"rev_{r:02d}", now + timedelta(seconds=r)) for n in range(6) for r in range(8)]
        results = await asyncio.gather(*calls)
        for n in range(6):
            mine = [r for r in results[n * 8:(n + 1) * 8]]
            winners = [r for r in mine if r.outcome is ReviewTaskTransition.CLAIMED]
            assert len(winners) == 1 and [r.outcome for r in mine].count(ReviewTaskTransition.HELD_BY_OTHER) == 7
            final = await repo.get_by_id("tenant_a", f"t{n}")
            assert final.status is ReviewTaskStatus.CLAIMED and final.assigned_reviewer_id == winners[0].task.assigned_reviewer_id
            assert {r.task.assigned_reviewer_id for r in mine} == {final.assigned_reviewer_id}

        same = await _review_repo(connection, [_rt("solo")])
        again = await asyncio.gather(*(same.claim("tenant_a", "solo", "rev_1", now) for _ in range(20)))
        assert [r.outcome for r in again].count(ReviewTaskTransition.CLAIMED) == 1
        assert [r.outcome for r in again].count(ReviewTaskTransition.ALREADY_OWNED) == 19

    run_with_database(body)


@requires_mongo
def test_claim_release_and_complete_follow_the_contract_and_race_correctly_on_a_real_server():
    async def body(connection):
        repo = await _review_repo(connection, [_rt("t1"), _rt("done", status=ReviewTaskStatus.COMPLETED, owner="rev_9")])
        T = ReviewTaskTransition
        now = datetime(2026, 2, 1, tzinfo=timezone.utc)
        assert (await repo.claim("tenant_a", "t1", "rev_1", now)).outcome is T.CLAIMED
        assert (await repo.claim("tenant_a", "t1", "rev_1", now)).outcome is T.ALREADY_OWNED
        assert (await repo.release("tenant_a", "t1", "rev_2", now)).outcome is T.HELD_BY_OTHER
        assert (await repo.complete("tenant_a", "t1", "rev_2", now)).outcome is T.HELD_BY_OTHER
        raw = await connection.collection(REVIEW_TASKS).find_one({"tenant_id": "tenant_a", "task_id": "t1"})
        assert raw["status"] == "claimed" and raw["assigned_reviewer_id"] == "rev_1" and raw.get("completed_at") is None
        released = await repo.release("tenant_a", "t1", "rev_1", now)
        assert released.outcome is T.RELEASED and released.task.assigned_reviewer_id is None
        raw = await connection.collection(REVIEW_TASKS).find_one({"tenant_id": "tenant_a", "task_id": "t1"})
        assert raw["status"] == "unassigned" and raw["assigned_reviewer_id"] is None
        assert (await repo.release("tenant_a", "t1", "rev_1", now)).outcome is T.ALREADY_UNASSIGNED
        assert (await repo.complete("tenant_a", "t1", "rev_1", now)).outcome is T.NOT_CLAIMED
        assert (await repo.claim("tenant_a", "done", "rev_1", now)).outcome is T.COMPLETED
        assert (await repo.claim("tenant_a", "missing", "rev_1", now)).outcome is T.NOT_FOUND

        await repo.claim("tenant_a", "t1", "rev_1", now)
        results = await asyncio.gather(*(repo.complete("tenant_a", "t1", "rev_1", now + timedelta(seconds=n)) for n in range(20)))
        assert [r.outcome for r in results].count(T.COMPLETION_RECORDED) == 1
        assert [r.outcome for r in results].count(T.ALREADY_COMPLETED) == 19
        final = await repo.get_by_id("tenant_a", "t1")
        assert final.status is ReviewTaskStatus.COMPLETED and final.assigned_reviewer_id == "rev_1" and final.completed_at is not None

        for round_ in range(12):                                            # a completion racing the owner's release
            task_id = f"race{round_}"
            await repo.create("tenant_a", _rt(task_id))
            await repo.claim("tenant_a", task_id, "rev_1", now)
            pair = [repo.complete("tenant_a", task_id, "rev_1", now), repo.release("tenant_a", task_id, "rev_1", now)]
            if round_ % 2:
                pair.reverse()
            outcomes = {r.outcome for r in await asyncio.gather(*pair)}
            settled = await repo.get_by_id("tenant_a", task_id)
            if settled.status is ReviewTaskStatus.COMPLETED:
                assert outcomes == {T.COMPLETION_RECORDED, T.COMPLETED}
            else:
                assert settled.status is ReviewTaskStatus.UNASSIGNED and outcomes == {T.RELEASED, T.NOT_CLAIMED}

    run_with_database(body)


@requires_mongo
def test_review_task_operations_are_tenant_isolated_on_a_real_server():
    async def body(connection):
        repo = await _review_repo(connection, [_rt("same", tenant="tenant_a"), _rt("same", tenant="tenant_b")])
        T = ReviewTaskTransition
        now = datetime(2026, 2, 1, tzinfo=timezone.utc)
        assert (await repo.claim("tenant_a", "same", "rev_1", now)).outcome is T.CLAIMED
        assert (await repo.get_by_id("tenant_b", "same")).status is ReviewTaskStatus.UNASSIGNED       # independent tasks
        assert (await repo.release("tenant_c", "same", "rev_1", now)).outcome is T.NOT_FOUND
        assert (await repo.complete("tenant_c", "same", "rev_1", now)).outcome is T.NOT_FOUND
        assert await repo.get_by_id("tenant_c", "same") is None and await repo.get_by_document_id("tenant_c", "doc-same") is None
        assert await repo.update_status("tenant_c", "same", "claimed", "rev_1") is None
        assert (await repo.list_page("tenant_c", ReviewTaskListQuery())).total_items == 0
        assert (await repo.get_by_id("tenant_a", "same")).assigned_reviewer_id == "rev_1"

    run_with_database(body)


@requires_mongo
def test_update_status_is_unconditional_and_validated_on_a_real_server():
    async def body(connection):
        repo = await _review_repo(connection, [_rt("t1")])
        claimed = await repo.update_status("tenant_a", "t1", "claimed", "rev_7", "taking it")
        assert claimed.status is ReviewTaskStatus.CLAIMED and claimed.assigned_reviewer_id == "rev_7"
        assert (await repo.get_by_id("tenant_a", "t1")) == claimed
        for status, reviewer in (("claimed", None), ("unassigned", "rev_7"), ("completed", "rev_7"), ("nonsense", None)):
            with pytest.raises(ValueError):
                await repo.update_status("tenant_a", "t1", status, reviewer)
        assert (await repo.get_by_id("tenant_a", "t1")) == claimed                      # nothing invalid was written
        assert await repo.update_status("tenant_a", "missing", "claimed", "rev_7") is None

    run_with_database(body)


# ---- MongoApiKeyRepository on a real server (Phase 2A-4) --------------------------------------------------------------------

def _ak(key_id, *, tenant="tenant_a", seed=None, minutes=0, **extra):
    """An API-key record whose hash is a real SHA-256 digest and whose timestamps are whole milliseconds."""
    digest = hashlib.sha256((seed or f"{tenant}/{key_id}").encode()).hexdigest()
    return ApiKeyRecord(key_id=key_id, tenant_id=tenant, name="a key", key_hash=digest, key_prefix="dex_",
                        scopes=["documents:read"], roles=["integrator"], created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes),
                        created_by="creator", **extra)


async def _key_repo(connection, keys=()):
    await ensure_indexes(connection.database, DEFAULT_REGISTRY)           # both unique identity indexes
    repo = MongoApiKeyRepository(connection)
    for record in keys:
        await repo.create(record.tenant_id, record)
    return repo


@requires_mongo
def test_api_keys_persist_and_read_back_revoke_and_record_use_on_a_real_server():
    async def body(connection):
        record = _ak("k1", expires_at=datetime(2026, 3, 1, 0, 0, 0, 123000, tzinfo=timezone.utc))
        repo = await _key_repo(connection)
        created = await repo.create("tenant_a", record)
        assert created == record == await repo.get_by_id("tenant_a", "k1") == await repo.get_by_key_hash(record.key_hash)
        assert created.expires_at.tzinfo is not None
        raw = await connection.collection("api_keys").find_one({"tenant_id": "tenant_a", "key_id": "k1"})
        assert raw["key_hash"] == record.key_hash and set(raw) - {"_id"} == set(record.model_dump())      # a digest and the record's fields: no other secret field
        assert await repo.record_last_used("tenant_a", "k1", datetime(2026, 2, 1, 0, 0, 0, 999999, tzinfo=timezone.utc)) is True
        assert (await repo.get_by_id("tenant_a", "k1")).last_used_at == datetime(2026, 2, 1, 0, 0, 0, 999000, tzinfo=timezone.utc)
        assert await repo.record_last_used("tenant_a", "k1", datetime(2026, 1, 15, tzinfo=timezone.utc)) is True       # never backwards
        assert (await repo.get_by_id("tenant_a", "k1")).last_used_at == datetime(2026, 2, 1, 0, 0, 0, 999000, tzinfo=timezone.utc)
        assert await repo.revoke("tenant_a", "k1") is True
        first = (await repo.get_by_id("tenant_a", "k1")).revoked_at
        assert first is not None and (await repo.get_by_key_hash(record.key_hash)).is_active is False
        assert await repo.revoke("tenant_a", "k1") is True and (await repo.get_by_id("tenant_a", "k1")).revoked_at == first
        assert await repo.record_last_used("tenant_a", "k1", datetime(2026, 3, 1, tzinfo=timezone.utc)) is False      # revoked: not active
        assert await repo.revoke("tenant_a", "missing") is False and await repo.get_by_id("tenant_a", "missing") is None

    run_with_database(body)


@requires_mongo
def test_the_unique_identity_indexes_are_enforced_on_a_real_server():
    async def body(connection):
        repo = await _key_repo(connection, [_ak("k1", seed="shared")])
        with pytest.raises(DuplicateApiKeyError) as same_id:
            await repo.create("tenant_a", _ak("k1", seed="another"))
        with pytest.raises(DuplicateApiKeyError) as same_hash:
            await repo.create("tenant_b", _ak("other", tenant="tenant_b", seed="shared"))
        assert same_id.value.details == same_hash.value.details and isinstance(same_id.value, (DuplicateKeyPersistenceError, ValueError))
        await repo.create("tenant_b", _ak("k1", tenant="tenant_b"))                  # the same key_id in another tenant is allowed

        results = await asyncio.gather(*(repo.create("tenant_a", _ak("race", seed=f"s{n}")) for n in range(12)),
                                       *(repo.create(f"tenant_{n}", _ak(f"i{n}", tenant=f"tenant_{n}", seed="one-hash")) for n in range(12)),
                                       return_exceptions=True)
        assert sum(isinstance(r, ApiKeyRecord) for r in results[:12]) == 1 and sum(isinstance(r, ApiKeyRecord) for r in results[12:]) == 1
        assert all(isinstance(r, DuplicateApiKeyError) for r in results if not isinstance(r, ApiKeyRecord))

    run_with_database(body)


@requires_mongo
def test_the_hash_lookup_is_global_and_every_other_operation_is_tenant_isolated_on_a_real_server():
    async def body(connection):
        a, b = _ak("k1", tenant="tenant_a"), _ak("k1", tenant="tenant_b")
        repo = await _key_repo(connection, [a, b, _ak("k2", tenant="tenant_b", minutes=1)])
        assert (await repo.get_by_key_hash(a.key_hash)).tenant_id == "tenant_a"
        assert (await repo.get_by_key_hash(b.key_hash)).tenant_id == "tenant_b"           # the tenant comes from the record
        assert await repo.get_by_key_hash(hashlib.sha256(b"nobody").hexdigest()) is None
        assert await repo.get_by_id("tenant_c", "k1") is None and await repo.list_keys("tenant_c") == []
        assert await repo.revoke("tenant_c", "k1") is False and await repo.record_last_used("tenant_c", "k1", datetime(2026, 2, 1, tzinfo=timezone.utc)) is False
        await repo.revoke("tenant_a", "k1")
        assert (await repo.get_by_id("tenant_b", "k1")).is_active is True                 # same key_id, untouched
        assert [k.key_id for k in await repo.list_keys("tenant_b")] == ["k1", "k2"]
        assert [k.key_id for k in await repo.list_keys("tenant_b", limit=1, offset=1)] == ["k2"]

    run_with_database(body)


@requires_mongo
def test_concurrent_revocation_and_use_never_leave_a_key_active_and_use_ends_at_the_latest_time_on_a_real_server():
    async def body(connection):
        repo = await _key_repo(connection, [_ak("k1"), _ak("k2")])
        late = datetime(2026, 6, 1, tzinfo=timezone.utc)
        calls = [repo.revoke("tenant_a", "k1") for _ in range(8)]
        calls += [repo.record_last_used("tenant_a", "k1", late + timedelta(seconds=n)) for n in range(8)]
        calls += [repo.record_last_used("tenant_a", "k2", late + timedelta(seconds=n)) for n in (5, 1, 9, 3, 7, 0, 8, 2)]
        await asyncio.gather(*calls)
        k1, k2 = await repo.get_by_id("tenant_a", "k1"), await repo.get_by_id("tenant_a", "k2")
        assert k1.is_active is False and k1.revoked_at is not None
        assert k2.last_used_at == late + timedelta(seconds=9) and k2.is_active is True

    run_with_database(body)


@requires_mongo
def test_the_real_service_and_authentication_provider_work_on_the_mongo_adapter_on_a_real_server():
    from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
    from doc_extractor.api.providers.api_key_auth import ApiKeyAuthenticationProvider
    from doc_extractor.api.services.api_key_service import ApiKeyService
    from doc_extractor.api.exceptions import InvalidTokenError

    class Audit:
        async def log_action(self, **event):
            return None

    async def body(connection):
        repo = await _key_repo(connection)
        service = ApiKeyService(repository=repo, audit_logger=Audit())
        provider = ApiKeyAuthenticationProvider(repository=repo, audit_logger=Audit())
        admin = ApiPrincipal(principal_id="admin", tenant_id="tenant_alpha", principal_type=PrincipalType.USER, roles={"admin"},
                             scopes={"api_keys:read", "api_keys:write", "documents:read"})
        created = await service.create_api_key(admin, "ci", ["documents:read"], ["integrator"], expires_in_days=30)
        principal = await provider.authenticate(f"Bearer {created.raw_secret}")
        assert principal.tenant_id == "tenant_alpha" and principal.is_active
        assert (await repo.get_by_id("tenant_alpha", created.key_id)).last_used_at is not None
        raw = await connection.collection("api_keys").find_one({"key_id": created.key_id})
        assert created.raw_secret not in repr(raw)
        assert await service.revoke_api_key(admin, created.key_id) is True
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(f"Bearer {created.raw_secret}")

    run_with_database(body)


# ---- MongoAuditRepository on a real server (Phase 2B) -------------------------------------------------------------------------

def _audit_event(n, *, tenant="tenant_a", resource="doc_1", action="document.upload", minutes=None, micros=0):
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return AuditEventRecord(event_id=f"evt_{tenant}_{n}", actor_id="actor_1", tenant_id=tenant, action=action, resource_id=resource,
                            resource_type="document", timestamp=base + timedelta(minutes=n if minutes is None else minutes, microseconds=micros))


async def _audit_repo(connection, **options):
    await ensure_indexes(connection.database, DEFAULT_REGISTRY)           # the unique (tenant_id, seq) index
    return MongoAuditRepository(connection, **options)


def connect_again(connection):
    """A second, independent client of the same deployment: a 'new process' (a seam the fake-driver check of these tests replaces)."""
    return MongoConnection(connection.settings)


async def _chain(connection, tenant="tenant_a"):
    cursor = connection.collection(AUDIT_EVENTS).find({"tenant_id": tenant}).sort([("seq", 1)])
    return await cursor.to_list(length=None)


@requires_mongo
def test_concurrent_audit_appends_get_distinct_consecutive_sequences_and_a_valid_chain_on_a_real_server():
    async def body(connection):
        repo = await _audit_repo(connection)
        other = MongoAuditRepository(connection)                          # a second repository object over the same database
        calls = [(repo if n % 2 else other).append_event(_audit_event(n)) for n in range(1, 41)]
        calls += [repo.append_event(_audit_event(n, tenant=f"tenant_{t}")) for t in ("b", "c") for n in range(1, 16)]
        await asyncio.gather(*calls)
        chain = await _chain(connection)
        assert [d["seq"] for d in chain] == list(range(1, 41))              # no duplicate, no gap, whoever won each round
        assert all(chain[i]["prev_hash"] == chain[i - 1]["hash"] for i in range(1, 40)) and chain[0]["prev_hash"] == GENESIS_HASH
        assert all(compute_event_hash(d["prev_hash"], d["seq"], json.loads(d["event_json"])) == d["hash"] for d in chain)
        verification = await repo.verify_chain("tenant_a")
        assert verification.valid and verification.checked_events == 40
        for tenant in ("tenant_b", "tenant_c"):
            assert [d["seq"] for d in await _chain(connection, tenant)] == list(range(1, 16))
            assert (await repo.verify_chain(tenant)).valid
        assert sorted(e.event_id for e in await repo.query_events("tenant_a", limit=1000)) == sorted(f"evt_tenant_a_{n}" for n in range(1, 41))

    run_with_database(body)


@requires_mongo
def test_the_server_enforces_one_record_per_tenant_and_sequence_and_the_repository_requires_that_index():
    from pymongo.errors import DuplicateKeyError

    async def body(connection):
        with pytest.raises(PersistenceConfigurationError) as missing:      # a fresh database: nobody created the index yet
            await MongoAuditRepository(connection).require_unique_sequence_index()
        assert missing.value.details["reason"] == "audit_sequence_index_is_missing"
        with pytest.raises(Exception) as refused:                          # and an append refuses to run without it
            await MongoAuditRepository(connection).append_event(_audit_event(1))
        assert refused.value.details["reason"] == "audit_sequence_index_is_missing"
        assert await connection.collection(AUDIT_EVENTS).count_documents({}) == 0

        repo = await _audit_repo(connection)
        await repo.require_unique_sequence_index()
        collection = connection.collection(AUDIT_EVENTS)
        await collection.insert_one({"tenant_id": "tenant_a", "seq": 1, "hash": "x"})
        with pytest.raises(DuplicateKeyError):                             # the server itself refuses a second record at (tenant_a, 1)
            await collection.insert_one({"tenant_id": "tenant_a", "seq": 1, "hash": "y"})
        await collection.insert_one({"tenant_id": "tenant_b", "seq": 1, "hash": "z"})       # another tenant's sequence 1 is independent

    run_with_database(body)


@requires_mongo
def test_the_audit_chain_persists_across_repository_instances_and_connections_on_a_real_server():
    async def body(connection):
        first = await _audit_repo(connection)
        for n in range(1, 6):
            await first.append_event(_audit_event(n))
        head_before = await first.chain_head("tenant_a")

        second_connection = connect_again(connection)                      # a new client: nothing shared with the first
        await second_connection.connect()
        try:
            second = MongoAuditRepository(second_connection)
            assert await second.chain_head("tenant_a") == head_before and head_before[0] == 5
            for n in range(6, 11):
                await second.append_event(_audit_event(n))
            events = await second.query_events("tenant_a")
            assert [e.event_id for e in events] == [f"evt_tenant_a_{n}" for n in range(1, 11)]
            assert (await second.verify_chain("tenant_a")).valid and (await first.verify_chain("tenant_a", batch_size=3)).checked_events == 10
            chain = await _chain(connection)
            assert chain[5]["prev_hash"] == head_before[1]                    # the second client continued the first one's chain
        finally:
            await second_connection.aclose()

    run_with_database(body)


@requires_mongo
def test_a_tampered_audit_chain_is_detected_on_a_real_server():
    async def body(connection):
        repo = await _audit_repo(connection)
        for n in range(1, 8):
            await repo.append_event(_audit_event(n))
        collection = connection.collection(AUDIT_EVENTS)
        assert (await repo.verify_chain("tenant_a")).valid

        record = await collection.find_one({"tenant_id": "tenant_a", "seq": 4})
        await collection.update_one({"tenant_id": "tenant_a", "seq": 4}, {"$set": {"event_json": record["event_json"].replace("doc_1", "doc_2")}})
        edited = await repo.verify_chain("tenant_a", batch_size=3)
        assert (edited.valid, edited.first_bad_seq, edited.reason) == (False, 4, "hash mismatch")
        await collection.update_one({"tenant_id": "tenant_a", "seq": 4}, {"$set": {"event_json": record["event_json"]}})
        assert (await repo.verify_chain("tenant_a")).valid

        await collection.update_one({"tenant_id": "tenant_a", "seq": 5}, {"$set": {"resource_id": "doc_other"}})     # an edited query copy
        copy_edit = await repo.verify_chain("tenant_a")
        assert (copy_edit.first_bad_seq, copy_edit.reason) == (5, "query fields differ from the event")
        await collection.update_one({"tenant_id": "tenant_a", "seq": 5}, {"$set": {"resource_id": "doc_1"}})

        await collection.delete_one({"tenant_id": "tenant_a", "seq": 3})
        removed = await repo.verify_chain("tenant_a")
        assert (removed.valid, removed.first_bad_seq, removed.reason) == (False, 3, "sequence or link broken")

    run_with_database(body)


@requires_mongo
def test_audit_queries_filter_order_and_page_exactly_on_a_real_server():
    async def body(connection):
        repo = await _audit_repo(connection)
        events = [_audit_event(n, resource=f"doc_{n % 3}", action=("a.x" if n % 2 else "b.y"), minutes=n // 2, micros=n * 13) for n in range(1, 31)]
        for event in events:
            await repo.append_event(event)
        await repo.append_event(_audit_event(1, tenant="tenant_b"))
        assert await repo.query_events("tenant_a", limit=1000) == events                       # chain order, round-tripped exactly
        assert [e.event_id for e in await repo.query_events("tenant_a", resource_id="doc_1", action="a.x", limit=1000)] == [
            e.event_id for e in events if e.resource_id == "doc_1" and e.action == "a.x"]
        low, high = events[9].timestamp, events[19].timestamp
        window = await repo.query_events("tenant_a", start_time=low, end_time=high, limit=1000)         # inclusive, exact to the microsecond
        assert [e.event_id for e in window] == [e.event_id for e in events if low <= e.timestamp <= high]
        assert await repo.query_events("tenant_a", end_time=low - timedelta(microseconds=1), start_time=high) == []
        assert [e.event_id for e in await repo.query_events("tenant_a", limit=5, offset=10)] == [e.event_id for e in events[10:15]]
        assert [e.tenant_id for e in await repo.query_events("tenant_b")] == ["tenant_b"] and await repo.query_events("tenant_c") == []

    run_with_database(body)


@requires_mongo
def test_audit_writes_fail_closed_and_leave_a_valid_chain_when_contention_exceeds_the_retry_budget_on_a_real_server():
    async def body(connection):
        repo = await _audit_repo(connection, max_attempts=1)               # one attempt each: only the round's winner can succeed
        results = await asyncio.gather(*(repo.append_event(_audit_event(n)) for n in range(1, 13)), return_exceptions=True)
        succeeded = [r for r in results if r is None]
        failed = [r for r in results if isinstance(r, Exception)]
        assert len(succeeded) + len(failed) == 12 and len(succeeded) >= 1
        assert all(isinstance(r, PersistenceUnavailableError) and r.details["reason"] == "audit_append_contended" for r in failed)
        chain = await _chain(connection)
        assert len(chain) == len(succeeded) and [d["seq"] for d in chain] == list(range(1, len(chain) + 1))     # a failure leaves no record
        assert (await repo.verify_chain("tenant_a")).valid

    run_with_database(body)
