"""
MongoDB ReviewTaskRepository tests (Team Member 3, Phase 2A-3).

MOCKED: the MongoDB client is the in-memory fake of ``tests/mongo_fakes.py`` (every call suspends its caller once and then runs
atomically, like a single-document server operation). These tests prove the adapter's logic and the calls it sends to the
driver. They do NOT prove behaviour of a real server, in particular the atomicity of ``find_one_and_update`` across processes:
that is ``tests/test_mongo_integration.py`` against a real replica set, which skips when none is available.

Two kinds of test:

1. CONTRACT tests, parametrised over BOTH the reference in-memory double (``tests/review_task_double.py``, the contract's
   executable form) and the MongoDB adapter. Both must give the same answer to the same scenario.
2. MONGO tests: tenant scoping of every driver call, the shape of the conditional updates, concurrent claims (including a
   deliberately racy adapter that the same tests must catch), lost-race retry, failure mapping, stored-data validation,
   index-based behaviour and composition.
"""

import asyncio
import inspect
import random
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("pymongo", reason="the 'mongo' extra provides the driver whose error types these tests map")
from pymongo import errors as driver  # noqa: E402

import doc_extractor.infrastructure.mongo.review_tasks as review_tasks_module  # noqa: E402
from doc_extractor.api.app import create_app  # noqa: E402
from doc_extractor.api.composition import ApplicationContainer  # noqa: E402
from doc_extractor.api.config import ApiSettings  # noqa: E402
from doc_extractor.api.contracts.records import ReviewTaskRecord, ReviewTaskStatus  # noqa: E402
from doc_extractor.api.exceptions import ReviewTaskClaimedError, ReviewTaskCompletedError  # noqa: E402
from doc_extractor.api.ports.repositories import (  # noqa: E402
    ReviewTaskAssignee,
    ReviewTaskListQuery,
    ReviewTaskRepository,
    ReviewTaskTransition as T,
    ReviewTaskTransitionResult,
)
from doc_extractor.api.services.review_task_service import ReviewTaskService  # noqa: E402
from doc_extractor.infrastructure.mongo import (  # noqa: E402
    DEFAULT_REGISTRY,
    DuplicateKeyPersistenceError,
    MongoConnection,
    MongoSettings,
    PersistenceDataError,
    PersistenceError,
    PersistenceUnavailableError,
    TenantScopeError,
    UnsafeQueryError,
    ensure_indexes,
)
from doc_extractor.infrastructure.mongo.review_tasks import (  # noqa: E402
    DuplicateReviewTaskError,
    MongoReviewTaskRepository,
)
from tests.mongo_fakes import FakeClient, LogCapture, factory_for  # noqa: E402
from tests.review_task_double import InMemoryReviewTaskRepository  # noqa: E402

UNASSIGNED, CLAIMED, COMPLETED = ReviewTaskStatus.UNASSIGNED, ReviewTaskStatus.CLAIMED, ReviewTaskStatus.COMPLETED
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
KINDS = ["double", "mongo"]


def run(coro):
    return asyncio.run(coro)


def at(minutes, millis=0):
    """Whole-millisecond UTC instants: stored and read back unchanged, so both implementations compare equal."""
    return BASE + timedelta(minutes=minutes, milliseconds=millis)


def make(task_id, *, tenant="tenant_a", document=None, status=UNASSIGNED, owner=None, created=0, completed=None, notes=None):
    if status is CLAIMED and owner is None:
        owner = "rev_owner"
    if status is COMPLETED:
        owner = owner or "rev_owner"
        completed = completed or at(created + 1)
    return ReviewTaskRecord(
        task_id=task_id, document_id=document or f"doc-{task_id}", tenant_id=tenant, status=status, assigned_reviewer_id=owner,
        created_at=at(created), updated_at=at(created), completed_at=completed, reviewer_notes=notes,
    )


class Mongo:
    """A connected adapter over the fake driver, with handles the Mongo-only tests need."""

    def __init__(self, repo, client, connection):
        self.repo, self.client, self.connection = repo, client, connection

    @property
    def collection(self):
        return self.client["doc"]["review_tasks"]

    @property
    def sent(self):
        return self.collection.calls


async def build_mongo(tasks=(), *, indexes=False):
    client = FakeClient()
    connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(client))
    await connection.connect()
    if indexes:
        await ensure_indexes(client["doc"], DEFAULT_REGISTRY)
    repo = MongoReviewTaskRepository(connection)
    for task in tasks:
        await repo.create(task.tenant_id, task)
    client["doc"]["review_tasks"].calls.clear()
    client["doc"]["review_tasks"].session_calls.clear()
    return Mongo(repo, client, connection)


async def build(kind, tasks=()):
    if kind == "double":
        return InMemoryReviewTaskRepository(tasks)
    return (await build_mongo(tasks)).repo


def contract(kind, tasks, body):
    async def go():
        return await body(await build(kind, tasks))

    return asyncio.run(go())


def fields(task):
    return None if task is None else (task.status, task.assigned_reviewer_id, task.updated_at, task.completed_at)


# =============================================================================================================================
# 1. CONTRACT TESTS: the reference double and the MongoDB adapter must agree
# =============================================================================================================================

def test_the_adapter_implements_the_port_with_the_same_signatures():
    assert isinstance(MongoReviewTaskRepository(MongoConnection(MongoSettings(uri="mongodb://localhost", database="d"))),
                      ReviewTaskRepository)
    for name in ("claim", "release", "complete", "list_page", "get_by_id", "get_by_document_id", "create", "update_status",
                 "list_tasks"):
        port = inspect.signature(getattr(ReviewTaskRepository, name))
        adapter = inspect.signature(getattr(MongoReviewTaskRepository, name))
        assert list(port.parameters) == list(adapter.parameters), name
        assert [p.default for p in port.parameters.values()] == [p.default for p in adapter.parameters.values()], name
        assert inspect.iscoroutinefunction(getattr(MongoReviewTaskRepository, name)), name


@pytest.mark.parametrize("kind", KINDS)
def test_claim_gives_an_unassigned_task_to_the_caller(kind):
    async def body(repo):
        result = await repo.claim("tenant_a", "t1", "rev_1", at(100))
        return result, await repo.get_by_id("tenant_a", "t1")

    result, stored = contract(kind, [make("t1")], body)
    assert result.outcome is T.CLAIMED
    assert fields(result.task) == (CLAIMED, "rev_1", at(100), None) == fields(stored)


@pytest.mark.parametrize("kind", KINDS)
def test_claiming_again_as_the_owner_is_idempotent_and_writes_nothing(kind):
    async def body(repo):
        await repo.claim("tenant_a", "t1", "rev_1", at(100))
        again = await repo.claim("tenant_a", "t1", "rev_1", at(200))
        return again, await repo.get_by_id("tenant_a", "t1")

    again, stored = contract(kind, [make("t1")], body)
    assert again.outcome is T.ALREADY_OWNED and fields(again.task) == fields(stored) == (CLAIMED, "rev_1", at(100), None)


@pytest.mark.parametrize("kind", KINDS)
def test_claiming_a_task_owned_by_someone_else_is_refused_and_changes_nothing(kind):
    async def body(repo):
        result = await repo.claim("tenant_a", "t1", "rev_2", at(100))
        return result, await repo.get_by_id("tenant_a", "t1")

    result, stored = contract(kind, [make("t1", status=CLAIMED, owner="rev_1")], body)
    assert result.outcome is T.HELD_BY_OTHER and result.task.assigned_reviewer_id == "rev_1" and fields(result.task) == fields(stored)
    assert stored.updated_at == at(0)


@pytest.mark.parametrize("kind", KINDS)
def test_claim_of_a_completed_or_missing_task(kind):
    async def body(repo):
        return await repo.claim("tenant_a", "done", "rev_1", at(100)), await repo.claim("tenant_a", "nope", "rev_1", at(100))

    done, missing = contract(kind, [make("done", status=COMPLETED, owner="rev_9")], body)
    assert done.outcome is T.COMPLETED and done.task.status is COMPLETED and done.task.assigned_reviewer_id == "rev_9"
    assert missing == ReviewTaskTransitionResult(T.NOT_FOUND, None)


@pytest.mark.parametrize("kind", KINDS)
def test_release_by_the_owner_returns_the_task_to_unassigned(kind):
    async def body(repo):
        result = await repo.release("tenant_a", "t1", "rev_1", at(100))
        return result, await repo.get_by_id("tenant_a", "t1")

    result, stored = contract(kind, [make("t1", status=CLAIMED, owner="rev_1")], body)
    assert result.outcome is T.RELEASED and fields(result.task) == (UNASSIGNED, None, at(100), None) == fields(stored)


@pytest.mark.parametrize("kind", KINDS)
def test_release_outcomes_other_than_the_owners(kind):
    async def body(repo):
        return (
            await repo.release("tenant_a", "free", "rev_1", at(100)),
            await repo.release("tenant_a", "held", "rev_2", at(100)),
            await repo.release("tenant_a", "done", "rev_1", at(100)),
            await repo.release("tenant_a", "nope", "rev_1", at(100)),
            await repo.get_by_id("tenant_a", "held"),
        )

    tasks = [make("free"), make("held", status=CLAIMED, owner="rev_1"), make("done", status=COMPLETED, owner="rev_1")]
    free, held, done, missing, held_after = contract(kind, tasks, body)
    assert free.outcome is T.ALREADY_UNASSIGNED and fields(free.task) == (UNASSIGNED, None, at(0), None)
    assert held.outcome is T.HELD_BY_OTHER and held.task.assigned_reviewer_id == "rev_1" and held_after.assigned_reviewer_id == "rev_1"
    assert done.outcome is T.COMPLETED and done.task.status is COMPLETED
    assert missing.outcome is T.NOT_FOUND and missing.task is None


@pytest.mark.parametrize("kind", KINDS)
def test_complete_by_the_owner_records_the_completion_and_keeps_the_owner(kind):
    async def body(repo):
        result = await repo.complete("tenant_a", "t1", "rev_1", at(100))
        again = await repo.complete("tenant_a", "t1", "rev_1", at(200))
        return result, again, await repo.get_by_id("tenant_a", "t1")

    result, again, stored = contract(kind, [make("t1", status=CLAIMED, owner="rev_1")], body)
    assert result.outcome is T.COMPLETION_RECORDED and fields(result.task) == (COMPLETED, "rev_1", at(100), at(100))
    assert again.outcome is T.ALREADY_COMPLETED and fields(again.task) == fields(stored) == (COMPLETED, "rev_1", at(100), at(100))


@pytest.mark.parametrize("kind", KINDS)
def test_complete_outcomes_other_than_the_owners(kind):
    async def body(repo):
        return (
            await repo.complete("tenant_a", "free", "rev_1", at(100)),
            await repo.complete("tenant_a", "held", "rev_2", at(100)),
            await repo.complete("tenant_a", "done", "rev_2", at(100)),
            await repo.complete("tenant_a", "nope", "rev_1", at(100)),
            await repo.get_by_id("tenant_a", "free"),
            await repo.get_by_id("tenant_a", "held"),
        )

    tasks = [make("free"), make("held", status=CLAIMED, owner="rev_1"), make("done", status=COMPLETED, owner="rev_1")]
    free, held, done, missing, free_after, held_after = contract(kind, tasks, body)
    assert free.outcome is T.NOT_CLAIMED and free.task.status is UNASSIGNED and free_after.status is UNASSIGNED
    assert held.outcome is T.HELD_BY_OTHER and held.task.assigned_reviewer_id == "rev_1" and held_after.status is CLAIMED
    assert done.outcome is T.COMPLETED and done.task.assigned_reviewer_id == "rev_1"      # completed by someone else
    assert missing.outcome is T.NOT_FOUND


@pytest.mark.parametrize("kind", KINDS)
def test_the_full_life_cycle_and_a_released_task_can_be_claimed_by_another(kind):
    async def body(repo):
        steps = [
            await repo.claim("tenant_a", "t1", "rev_1", at(10)),
            await repo.release("tenant_a", "t1", "rev_1", at(20)),
            await repo.claim("tenant_a", "t1", "rev_2", at(30)),
            await repo.complete("tenant_a", "t1", "rev_2", at(40)),
            await repo.release("tenant_a", "t1", "rev_2", at(50)),
            await repo.claim("tenant_a", "t1", "rev_3", at(60)),
        ]
        return [(s.outcome, s.task.status, s.task.assigned_reviewer_id) for s in steps]

    assert contract(kind, [make("t1")], body) == [
        (T.CLAIMED, CLAIMED, "rev_1"), (T.RELEASED, UNASSIGNED, None), (T.CLAIMED, CLAIMED, "rev_2"),
        (T.COMPLETION_RECORDED, COMPLETED, "rev_2"), (T.COMPLETED, COMPLETED, "rev_2"), (T.COMPLETED, COMPLETED, "rev_2"),
    ]


@pytest.mark.parametrize("kind", KINDS)
def test_another_tenants_task_is_absent_for_every_operation_and_is_left_untouched(kind):
    async def body(repo):
        results = (
            await repo.claim("tenant_b", "t1", "rev_1", at(100)),
            await repo.release("tenant_b", "t1", "rev_1", at(100)),
            await repo.complete("tenant_b", "t1", "rev_1", at(100)),
            await repo.get_by_id("tenant_b", "t1"),
            await repo.get_by_document_id("tenant_b", "doc-t1"),
            await repo.update_status("tenant_b", "t1", "completed", "rev_1"),
            await repo.list_page("tenant_b", ReviewTaskListQuery()),
            await repo.list_tasks("tenant_b"),
        )
        return results, await repo.get_by_id("tenant_a", "t1")

    (claim, release, complete, by_id, by_doc, update, page, tasks), original = contract(
        kind, [make("t1", status=CLAIMED, owner="rev_9")], body
    )
    assert [r.outcome for r in (claim, release, complete)] == [T.NOT_FOUND] * 3 and all(r.task is None for r in (claim, release, complete))
    assert by_id is None and by_doc is None and update is None and page.items == () and page.total_items == 0 and tasks == []
    assert fields(original) == (CLAIMED, "rev_9", at(0), None)


@pytest.mark.parametrize("kind", KINDS)
def test_the_same_task_id_in_two_tenants_is_two_independent_tasks(kind):
    async def body(repo):
        await repo.claim("tenant_a", "same", "rev_1", at(100))
        return await repo.get_by_id("tenant_a", "same"), await repo.get_by_id("tenant_b", "same")

    mine, theirs = contract(kind, [make("same", tenant="tenant_a"), make("same", tenant="tenant_b")], body)
    assert mine.status is CLAIMED and theirs.status is UNASSIGNED


@pytest.mark.parametrize("kind", KINDS)
def test_create_and_read_back(kind):
    async def body(repo):
        created = await repo.create("tenant_a", make("t1", document="doc-1", notes="n"))
        return created, await repo.get_by_id("tenant_a", "t1"), await repo.get_by_id("tenant_a", "missing")

    created, stored, missing = contract(kind, [], body)
    assert created == stored and stored.document_id == "doc-1" and stored.reviewer_notes == "n" and missing is None


@pytest.mark.parametrize("kind", KINDS)
def test_create_refuses_a_duplicate_and_a_foreign_tenant_with_a_value_error(kind):
    async def body(repo):
        await repo.create("tenant_a", make("t1"))
        with pytest.raises(ValueError):
            await repo.create("tenant_a", make("t1"))
        with pytest.raises(ValueError):
            await repo.create("tenant_a", make("t2", tenant="tenant_b"))
        return await repo.list_tasks("tenant_a"), await repo.list_tasks("tenant_b")

    # the Mongo variant needs its unique index for the duplicate: build it with the declared registry
    async def go():
        if kind == "double":
            return await body(InMemoryReviewTaskRepository())
        mongo = await build_mongo(indexes=True)
        return await body(mongo.repo)

    a, b = asyncio.run(go())
    assert [t.task_id for t in a] == ["t1"] and b == []


@pytest.mark.parametrize("kind", KINDS)
def test_update_status_is_an_unconditional_validated_write(kind):
    async def body(repo):
        claimed = await repo.update_status("tenant_a", "t1", "claimed", "rev_7", "taking it")
        missing = await repo.update_status("tenant_a", "nope", "claimed", "rev_7")
        with pytest.raises(ValueError):
            await repo.update_status("tenant_a", "t1", "claimed", None)                 # claimed needs an owner
        with pytest.raises(ValueError):
            await repo.update_status("tenant_a", "t1", "unassigned", "rev_7")           # unassigned has no owner
        with pytest.raises(ValueError):
            await repo.update_status("tenant_a", "t1", "completed", "rev_7")            # completed needs a completion time
        with pytest.raises(ValueError):
            await repo.update_status("tenant_a", "t1", "nonsense", None)
        return claimed, missing, await repo.get_by_id("tenant_a", "t1")

    claimed, missing, stored = contract(kind, [make("t1")], body)
    assert claimed.status is CLAIMED and claimed.assigned_reviewer_id == "rev_7" and claimed.reviewer_notes == "taking it"
    assert missing is None and stored == claimed


@pytest.mark.parametrize("kind", KINDS)
def test_update_status_overwrites_whatever_owner_there_was(kind):
    async def body(repo):
        return await repo.update_status("tenant_a", "t1", "claimed", "rev_2")

    result = contract(kind, [make("t1", status=CLAIMED, owner="rev_1", notes="old")], body)
    assert result.assigned_reviewer_id == "rev_2" and result.reviewer_notes is None


def _population(seed=7, count=60):
    rng = random.Random(seed)
    tasks = []
    for n in range(count):
        status = rng.choice([UNASSIGNED, UNASSIGNED, CLAIMED, COMPLETED])
        owner = rng.choice(["rev_1", "rev_2", "rev_3"]) if status is not UNASSIGNED else None
        tasks.append(make(f"t{n:03d}", document=f"doc-{rng.randrange(8)}", status=status, owner=owner,
                          created=rng.randrange(0, 20)))                          # many equal created_at: ties are common
        tasks.append(make(f"x{n:03d}", tenant="tenant_b", document=f"doc-{rng.randrange(8)}", created=rng.randrange(0, 20)))
    rng.shuffle(tasks)
    return tasks


def _queries():
    for status in (None, UNASSIGNED, CLAIMED, COMPLETED):
        for assignee, reviewer in ((ReviewTaskAssignee.ALL, None), (ReviewTaskAssignee.UNASSIGNED, None),
                                   (ReviewTaskAssignee.MINE, "rev_1"), (ReviewTaskAssignee.MINE, "rev_3")):
            for document in (None, "doc-3"):
                for page, size in ((1, 10), (2, 7), (3, 25), (1, 100)):
                    yield ReviewTaskListQuery(status=status, assignee=assignee, reviewer_id=reviewer, document_id=document,
                                              page=page, size=size)


def test_list_page_matches_the_reference_double_for_every_filter_page_and_tie():
    population = _population()

    async def go():
        double = InMemoryReviewTaskRepository(population)
        mongo = (await build_mongo(population)).repo
        mismatches, checked = [], 0
        for tenant in ("tenant_a", "tenant_b", "tenant_c"):
            for query in _queries():
                expected, actual = await double.list_page(tenant, query), await mongo.list_page(tenant, query)
                checked += 1
                if [t.task_id for t in expected.items] != [t.task_id for t in actual.items] or expected.total_items != actual.total_items:
                    mismatches.append((tenant, query))
        return checked, mismatches

    checked, mismatches = run(go())
    assert checked == 3 * 4 * 4 * 2 * 4 and mismatches == []


def test_list_tasks_matches_the_reference_double():
    population = _population(seed=11)

    async def go():
        double = InMemoryReviewTaskRepository(population)
        mongo = (await build_mongo(population)).repo
        bad = []
        for tenant in ("tenant_a", "tenant_b"):
            for status in (None, "unassigned", "claimed", "completed", "bogus"):
                for reviewer in (None, "rev_1", "rev_2"):
                    for limit, offset in ((50, 0), (5, 0), (5, 5), (7, 100), (0, 0), (1, 3)):
                        expected = await double.list_tasks(tenant, status, reviewer, limit, offset)
                        actual = await mongo.list_tasks(tenant, status, reviewer, limit, offset)
                        if [t.task_id for t in expected] != [t.task_id for t in actual]:
                            bad.append((tenant, status, reviewer, limit, offset))
        return bad

    assert run(go()) == []


@pytest.mark.parametrize("kind", KINDS)
def test_pages_are_oldest_first_with_task_id_as_the_tie_breaker_and_past_the_end_is_empty(kind):
    tasks = [make("c", created=5), make("a", created=5), make("b", created=1), make("z", created=9)]

    async def body(repo):
        first = await repo.list_page("tenant_a", ReviewTaskListQuery(page=1, size=3))
        second = await repo.list_page("tenant_a", ReviewTaskListQuery(page=2, size=3))
        beyond = await repo.list_page("tenant_a", ReviewTaskListQuery(page=50, size=3))
        return first, second, beyond

    first, second, beyond = contract(kind, tasks, body)
    assert [t.task_id for t in first.items] == ["b", "a", "c"] and [t.task_id for t in second.items] == ["z"]
    assert first.total_items == second.total_items == beyond.total_items == 4 and beyond.items == ()


# =============================================================================================================================
# 2. MONGO TESTS
# =============================================================================================================================

def with_mongo(tasks, body, **kwargs):
    async def go():
        return await body(await build_mongo(tasks, **kwargs))

    return asyncio.run(go())


# ---- tenant scoping of every driver call ------------------------------------------------------------------------------

def test_every_operation_sends_the_tenant_in_every_driver_filter():
    tasks = [make("t1"), make("t2", status=CLAIMED, owner="rev_1"), make("t3", tenant="tenant_b")]

    async def body(mongo):
        repo = mongo.repo
        await repo.claim("tenant_a", "t1", "rev_1", at(100))
        await repo.claim("tenant_a", "t2", "rev_2", at(100))                    # held: the fall-back read runs too
        await repo.release("tenant_a", "t1", "rev_1", at(100))
        await repo.complete("tenant_a", "t2", "rev_1", at(100))
        await repo.get_by_id("tenant_a", "t1")
        await repo.get_by_document_id("tenant_a", "doc-t1")
        await repo.list_page("tenant_a", ReviewTaskListQuery())
        await repo.list_tasks("tenant_a", status="claimed")
        await repo.update_status("tenant_a", "t1", "unassigned")
        await repo.create("tenant_a", make("t9"))
        return mongo.sent

    calls = with_mongo(tasks, body)
    assert {call[0] for call in calls} == {"find_one_and_update", "find_one", "find", "count_documents", "insert_one"}
    for operation, flt, *_ in calls:
        document = flt                                                          # insert_one's "filter" is the document itself
        assert document["tenant_id"] == "tenant_a", (operation, flt)


def test_the_tenant_cannot_be_chosen_through_any_argument():
    async def body(mongo):
        repo = mongo.repo
        for call in (
            repo.claim({"$ne": None}, "t1", "rev_1", at(1)), repo.get_by_id({"$ne": "x"}, "t1"),
            repo.list_tasks(None), repo.list_page("", ReviewTaskListQuery()), repo.get_by_id("a" * 300, "t1"),
            repo.create("tenant_a", make("tx", tenant="tenant_b")),
        ):
            with pytest.raises((TenantScopeError, UnsafeQueryError)):
                await call
        return mongo.sent

    assert with_mongo([make("t1")], body) == []                                  # nothing reached the driver


def test_operator_injection_through_ids_is_refused_before_the_driver():
    async def body(mongo):
        repo = mongo.repo
        bad = [{"$ne": "x"}, {"$gt": ""}, {"$regex": ".*"}, None, 5, ["a"]]
        for value in bad:
            for call in (repo.get_by_id("tenant_a", value), repo.get_by_document_id("tenant_a", value),
                         repo.claim("tenant_a", value, "rev_1", at(1)), repo.release("tenant_a", value, "rev_1", at(1)),
                         repo.complete("tenant_a", value, "rev_1", at(1)), repo.update_status("tenant_a", value, "claimed", "r")):
                with pytest.raises(ValueError):
                    await call
        for value in bad + ["", "   "]:
            for call in (repo.claim("tenant_a", "t1", value, at(1)), repo.release("tenant_a", "t1", value, at(1)),
                         repo.complete("tenant_a", "t1", value, at(1))):
                with pytest.raises(ValueError):
                    await call
        for value in ("2026-01-01", 5, None, {"$date": 1}):
            with pytest.raises(ValueError):
                await repo.claim("tenant_a", "t1", "rev_1", value)
        return mongo.sent

    assert with_mongo([make("t1")], body) == []


# ---- the shape of the atomic operations ------------------------------------------------------------------------------

def test_claim_is_one_conditional_update_with_the_precondition_in_the_filter():
    async def body(mongo):
        await mongo.repo.claim("tenant_a", "t1", "rev_1", at(100, 123))
        return mongo.sent

    sent = with_mongo([make("t1")], body)
    assert sent == [("find_one_and_update",
                     {"task_id": "t1", "status": "unassigned", "tenant_id": "tenant_a"},
                     {"$set": {"status": "claimed", "assigned_reviewer_id": "rev_1", "updated_at": at(100, 123)}})]


def test_release_and_complete_are_conditional_on_the_owner_and_the_claimed_state():
    async def body(mongo):
        await mongo.repo.release("tenant_a", "t1", "rev_1", at(100))
        await mongo.repo.complete("tenant_a", "t2", "rev_1", at(200))
        return mongo.sent

    sent = with_mongo([make("t1", status=CLAIMED, owner="rev_1"), make("t2", status=CLAIMED, owner="rev_1")], body)
    assert sent == [
        ("find_one_and_update", {"task_id": "t1", "status": "claimed", "assigned_reviewer_id": "rev_1", "tenant_id": "tenant_a"},
         {"$set": {"status": "unassigned", "assigned_reviewer_id": None, "updated_at": at(100)}}),
        ("find_one_and_update", {"task_id": "t2", "status": "claimed", "assigned_reviewer_id": "rev_1", "tenant_id": "tenant_a"},
         {"$set": {"status": "completed", "completed_at": at(200), "updated_at": at(200)}}),
    ]


def test_a_refused_claim_writes_nothing_and_costs_one_failed_update_and_one_read():
    async def body(mongo):
        await mongo.repo.claim("tenant_a", "t1", "rev_2", at(100))
        stored = dict(mongo.collection.docs[0])
        return [call[0] for call in mongo.sent], stored

    operations, stored = with_mongo([make("t1", status=CLAIMED, owner="rev_1")], body)
    assert operations == ["find_one_and_update", "find_one"]
    assert stored["assigned_reviewer_id"] == "rev_1" and stored["updated_at"] == at(0)


def test_claim_never_uses_the_unconditional_write_paths():
    async def body(mongo):
        for n in range(3):
            await mongo.repo.claim("tenant_a", "t1", f"rev_{n}", at(n))
            await mongo.repo.release("tenant_a", "t1", f"rev_{n}", at(n))
            await mongo.repo.complete("tenant_a", "t1", f"rev_{n}", at(n))
        return {call[0] for call in mongo.sent}

    assert with_mongo([make("t1")], body) <= {"find_one_and_update", "find_one"}


# ---- concurrency ------------------------------------------------------------------------------------------------------

def test_of_many_concurrent_claims_by_different_reviewers_exactly_one_wins():
    async def body(mongo):
        results = await asyncio.gather(*(mongo.repo.claim("tenant_a", "t1", f"rev_{n:02d}", at(100 + n)) for n in range(40)))
        return results, await mongo.repo.get_by_id("tenant_a", "t1")

    results, final = with_mongo([make("t1")], body)
    winners = [r for r in results if r.outcome is T.CLAIMED]
    assert len(winners) == 1 and [r.outcome for r in results].count(T.HELD_BY_OTHER) == 39
    assert final.assigned_reviewer_id == winners[0].task.assigned_reviewer_id
    assert {r.task.assigned_reviewer_id for r in results} == {final.assigned_reviewer_id}        # everyone is told who won
    assert final.updated_at == winners[0].task.updated_at


def test_of_many_concurrent_claims_by_one_reviewer_exactly_one_writes():
    async def body(mongo):
        results = await asyncio.gather(*(mongo.repo.claim("tenant_a", "t1", "rev_1", at(100 + n)) for n in range(30)))
        return results, await mongo.repo.get_by_id("tenant_a", "t1")

    results, final = with_mongo([make("t1")], body)
    assert [r.outcome for r in results].count(T.CLAIMED) == 1 and [r.outcome for r in results].count(T.ALREADY_OWNED) == 29
    assert final.updated_at == next(r.task.updated_at for r in results if r.outcome is T.CLAIMED)


def test_concurrent_claims_of_different_tasks_do_not_interfere():
    async def body(mongo):
        results = await asyncio.gather(*(mongo.repo.claim("tenant_a", f"t{n}", f"rev_{n}", at(100)) for n in range(20)))
        return results, [await mongo.repo.get_by_id("tenant_a", f"t{n}") for n in range(20)]

    results, stored = with_mongo([make(f"t{n}") for n in range(20)], body)
    assert all(r.outcome is T.CLAIMED for r in results) and [t.assigned_reviewer_id for t in stored] == [f"rev_{n}" for n in range(20)]


def test_of_many_concurrent_completions_exactly_one_is_recorded():
    async def body(mongo):
        results = await asyncio.gather(*(mongo.repo.complete("tenant_a", "t1", "rev_1", at(100 + n)) for n in range(30)))
        return results, await mongo.repo.get_by_id("tenant_a", "t1")

    results, final = with_mongo([make("t1", status=CLAIMED, owner="rev_1")], body)
    assert [r.outcome for r in results].count(T.COMPLETION_RECORDED) == 1 and [r.outcome for r in results].count(T.ALREADY_COMPLETED) == 29
    assert final.completed_at == next(r.task.completed_at for r in results if r.outcome is T.COMPLETION_RECORDED)


@pytest.mark.parametrize("complete_first", [True, False])
def test_a_completion_racing_a_release_by_the_owner_has_exactly_one_winner(complete_first):
    async def body(mongo):
        release = mongo.repo.release("tenant_a", "t1", "rev_1", at(100))
        complete = mongo.repo.complete("tenant_a", "t1", "rev_1", at(200))
        pair = (complete, release) if complete_first else (release, complete)
        results = await asyncio.gather(*pair)
        by = dict(zip(("complete", "release") if complete_first else ("release", "complete"), results))
        return by, await mongo.repo.get_by_id("tenant_a", "t1")

    by, final = with_mongo([make("t1", status=CLAIMED, owner="rev_1")], body)
    if by["complete"].outcome is T.COMPLETION_RECORDED:
        assert by["release"].outcome is T.COMPLETED and final.status is COMPLETED
    else:
        assert by["release"].outcome is T.RELEASED and by["complete"].outcome is T.NOT_CLAIMED and final.status is UNASSIGNED
    assert (by["complete"].outcome is T.COMPLETION_RECORDED) != (by["release"].outcome is T.RELEASED)


class RacyMongoReviewTaskRepository(MongoReviewTaskRepository):
    """The same adapter with a check-then-act claim (read, yield, blind write). ONLY to prove the tests above can fail."""

    async def claim(self, tenant_id, task_id, reviewer_id, now):
        task = await self.get_by_id(tenant_id, task_id)
        if task is None or task.status is not UNASSIGNED:
            return await super().claim(tenant_id, task_id, reviewer_id, now)
        await self.update_one(tenant_id, {"task_id": task_id},
                              {"$set": {"status": "claimed", "assigned_reviewer_id": reviewer_id, "updated_at": now}})
        return ReviewTaskTransitionResult(T.CLAIMED, await self.get_by_id(tenant_id, task_id))


def test_the_concurrency_tests_can_detect_a_non_atomic_claim():
    async def go():
        mongo = await build_mongo([make("t1")])
        racy = RacyMongoReviewTaskRepository(mongo.connection)
        results = await asyncio.gather(*(racy.claim("tenant_a", "t1", f"rev_{n:02d}", at(100 + n)) for n in range(10)))
        return results

    results = run(go())
    assert [r.outcome for r in results].count(T.CLAIMED) > 1                      # the racy variant lets several "win"


# ---- a lost race is retried, not misreported --------------------------------------------------------------------------

def test_a_claim_that_loses_to_a_concurrent_release_is_retried_and_succeeds():
    async def body(mongo):
        collection = mongo.collection
        original = collection.find_one

        async def released_meanwhile(flt, sort=None, session=None):
            collection.docs[0].update(status="unassigned", assigned_reviewer_id=None)    # the owner released it
            collection.find_one = original
            return await original(flt, sort=sort, session=session)

        collection.find_one = released_meanwhile
        return await mongo.repo.claim("tenant_a", "t1", "rev_2", at(100))

    result = with_mongo([make("t1", status=CLAIMED, owner="rev_1")], body)
    assert result.outcome is T.CLAIMED and result.task.assigned_reviewer_id == "rev_2"


def test_a_state_that_never_settles_is_a_typed_retryable_error_not_a_wrong_answer(monkeypatch):
    monkeypatch.setattr(review_tasks_module, "_BACKOFF_SECONDS", 0)

    async def body(mongo):
        collection = mongo.collection

        async def never_matches(flt, update, return_document=True, sort=None, session=None):
            return None

        collection.find_one_and_update = never_matches                           # the conditional update keeps losing ...
        with pytest.raises(PersistenceUnavailableError) as caught:               # ... while every read shows it free again
            await mongo.repo.claim("tenant_a", "t1", "rev_2", at(100))
        return caught.value

    error = with_mongo([make("t1")], body)
    assert error.details["reason"] == "task_state_did_not_settle"


# ---- get_by_document_id and the (non-)uniqueness rules ----------------------------------------------------------------

def test_get_by_document_id_returns_the_most_recent_task_deterministically():
    tasks = [make("old", document="doc-1", created=1), make("new", document="doc-1", created=9),
             make("mid", document="doc-1", created=5), make("other", document="doc-2", created=99),
             make("foreign", tenant="tenant_b", document="doc-1", created=500)]

    async def body(mongo):
        return await mongo.repo.get_by_document_id("tenant_a", "doc-1"), await mongo.repo.get_by_document_id("tenant_a", "none")

    newest, none = with_mongo(tasks, body)
    assert newest.task_id == "new" and none is None


def test_equal_creation_times_are_broken_by_task_id_descending():
    tasks = [make("a", document="d", created=3), make("c", document="d", created=3), make("b", document="d", created=3)]

    async def body(mongo):
        return await mongo.repo.get_by_document_id("tenant_a", "d")

    assert with_mongo(tasks, body).task_id == "c"


def test_the_document_lookup_sorts_newest_first_in_the_query_itself():
    async def body(mongo):
        await mongo.repo.get_by_document_id("tenant_a", "doc-t1")
        return mongo.sent

    assert with_mongo([make("t1")], body) == [("find_one", {"document_id": "doc-t1", "tenant_id": "tenant_a"})]


def test_a_document_may_have_several_tasks_and_a_task_id_is_unique_per_tenant():
    async def body(mongo):
        await mongo.repo.create("tenant_a", make("t1", document="doc-1"))
        await mongo.repo.create("tenant_a", make("t2", document="doc-1"))                 # no document-level uniqueness
        await mongo.repo.create("tenant_b", make("t1", tenant="tenant_b", document="doc-1"))   # task ids are per tenant
        with pytest.raises(DuplicateReviewTaskError) as caught:
            await mongo.repo.create("tenant_a", make("t1", document="doc-9"))
        return caught.value, [t.task_id for t in await mongo.repo.list_tasks("tenant_a")]

    error, ids = with_mongo([], body, indexes=True)
    assert ids == ["t1", "t2"] and isinstance(error, (DuplicateKeyPersistenceError, ValueError))


def test_no_new_index_and_no_document_uniqueness_were_introduced():
    from doc_extractor.infrastructure.mongo.indexes import REVIEW_TASKS

    specs = DEFAULT_REGISTRY.for_collection(REVIEW_TASKS)
    assert {spec.name for spec in specs} == {"rt_tenant_task_uq", "rt_tenant_document_created", "rt_tenant_status_created_task",
                                              "rt_tenant_reviewer_status_created_task"}
    assert [spec.name for spec in specs if spec.unique] == ["rt_tenant_task_uq"]


def test_create_returns_the_stored_form_and_a_duplicate_error_leaks_nothing():
    async def body(mongo):
        created = await mongo.repo.create("tenant_a", ReviewTaskRecord(
            task_id="t1", document_id="d", tenant_id="tenant_a", created_at=datetime(2026, 1, 1, 0, 0, 0, 999999),
            updated_at=datetime(2026, 1, 1, 0, 0, 0, 999999), metadata={"k": "v"}))
        again = await mongo.repo.get_by_id("tenant_a", "t1")
        with pytest.raises(DuplicateReviewTaskError) as caught:
            await mongo.repo.create("tenant_a", created)
        return created, again, caught.value

    created, again, error = with_mongo([], body, indexes=True)
    assert created == again and created.created_at == datetime(2026, 1, 1, 0, 0, 0, 999000, tzinfo=timezone.utc)
    assert "SECRET" not in str(error) + repr(error.details) and error.details["reason"] == "duplicate_key"


# ---- paging details ---------------------------------------------------------------------------------------------------

def test_pages_larger_than_the_foundations_bound_are_fetched_in_chunks():
    tasks = [make(f"t{n:04d}", created=n % 50) for n in range(1250)]

    async def body(mongo):
        page = await mongo.repo.list_page("tenant_a", ReviewTaskListQuery(page=1, size=1250))
        tasks_ = await mongo.repo.list_tasks("tenant_a", limit=3000, offset=100)
        return page, tasks_, [call for call in mongo.sent if call[0] == "find"]

    page, listed, finds = with_mongo(tasks, body)
    assert len(page.items) == 1250 and page.total_items == 1250 and len(listed) == 1150
    assert len({t.task_id for t in page.items}) == 1250 and len(finds) >= 3


def test_a_page_far_past_the_end_is_empty_without_asking_the_server_for_a_huge_skip():
    async def body(mongo):
        page = await mongo.repo.list_page("tenant_a", ReviewTaskListQuery(page=10 ** 18, size=100))
        return page, [call[0] for call in mongo.sent]

    page, operations = with_mongo([make("t1")], body)
    assert page.items == () and page.total_items == 1 and operations == ["count_documents"]


def test_the_total_never_reports_less_than_the_page_it_returns():
    async def body(mongo):
        async def stale_count(flt, session=None):
            return 2                                                             # tasks were created after the count was taken

        mongo.collection.count_documents = stale_count
        return await mongo.repo.list_page("tenant_a", ReviewTaskListQuery(page=1, size=10))

    page = with_mongo([make(f"t{n}") for n in range(5)], body)
    assert len(page.items) == 5 and page.total_items >= len(page.items)


def test_negative_paging_arguments_are_refused():
    async def body(mongo):
        for kwargs in ({"limit": -1}, {"offset": -1}):
            with pytest.raises(UnsafeQueryError):
                await mongo.repo.list_tasks("tenant_a", **kwargs)
        return mongo.sent

    assert with_mongo([make("t1")], body) == []


def test_the_unassigned_filter_matches_a_missing_owner_field_too():
    async def body(mongo):
        mongo.collection.docs.append({"task_id": "legacy", "document_id": "d", "tenant_id": "tenant_a", "status": "unassigned",
                                      "priority": 0, "created_at": at(0), "updated_at": at(0), "metadata": {}})
        return await mongo.repo.list_page("tenant_a", ReviewTaskListQuery(assignee=ReviewTaskAssignee.UNASSIGNED))

    page = with_mongo([make("t1"), make("t2", status=CLAIMED, owner="rev_1")], body)
    assert sorted(t.task_id for t in page.items) == ["legacy", "t1"]


# ---- failure handling -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("operation", ["claim", "release", "complete", "get", "doc", "page", "tasks", "create", "update"])
def test_a_driver_failure_is_a_typed_persistence_error_that_leaks_nothing(operation):
    calls = {
        "claim": lambda r: r.claim("tenant_a", "t1", "rev_1", at(1)), "release": lambda r: r.release("tenant_a", "t1", "rev_1", at(1)),
        "complete": lambda r: r.complete("tenant_a", "t1", "rev_1", at(1)), "get": lambda r: r.get_by_id("tenant_a", "t1"),
        "doc": lambda r: r.get_by_document_id("tenant_a", "d"), "page": lambda r: r.list_page("tenant_a", ReviewTaskListQuery()),
        "tasks": lambda r: r.list_tasks("tenant_a"), "create": lambda r: r.create("tenant_a", make("t9")),
        "update": lambda r: r.update_status("tenant_a", "t1", "unassigned"),
    }

    async def body(mongo):
        mongo.collection.fail_with = driver.NetworkTimeout("lost host mongo-0 SECRET")
        with LogCapture() as logs:
            with pytest.raises(PersistenceUnavailableError) as caught:
                await calls[operation](mongo.repo)
        return caught.value, logs

    error, logs = with_mongo([make("t1")], body)
    assert "SECRET" not in str(error) + repr(error) + repr(error.details) + logs.text and "mongo-0" not in str(error) + repr(error.details)
    assert error.__cause__ is None and error.details["collection"] == "review_tasks"


def test_other_driver_errors_are_mapped_by_the_foundation():
    async def body(mongo):
        mongo.collection.fail_with = driver.OperationFailure("not authorized SECRET", code=13)
        with pytest.raises(PersistenceError) as caught:
            await mongo.repo.claim("tenant_a", "t1", "rev_1", at(1))
        return caught.value

    error = with_mongo([make("t1")], body)
    assert error.details["reason"] == "authorization_failed" and "SECRET" not in str(error) + repr(error.details)


def test_a_failed_operation_changes_nothing_and_the_next_one_works():
    async def body(mongo):
        mongo.collection.fail_with = driver.AutoReconnect("down")
        with pytest.raises(PersistenceUnavailableError):
            await mongo.repo.claim("tenant_a", "t1", "rev_1", at(1))
        mongo.collection.fail_with = None
        before = fields(await mongo.repo.get_by_id("tenant_a", "t1"))
        return before, await mongo.repo.claim("tenant_a", "t1", "rev_1", at(2))

    before, result = with_mongo([make("t1")], body)
    assert before == (UNASSIGNED, None, at(0), None) and result.outcome is T.CLAIMED


def test_a_disconnected_repository_raises_a_typed_error():
    async def body(mongo):
        await mongo.connection.aclose()
        for call in (mongo.repo.claim("tenant_a", "t1", "rev_1", at(1)), mongo.repo.get_by_id("tenant_a", "t1")):
            with pytest.raises(PersistenceUnavailableError) as caught:
                await call
            assert caught.value.details["reason"] == "not_connected"

    with_mongo([make("t1")], body)


@pytest.mark.parametrize("stored", [
    {"extra_field": 1},                                                          # the record forbids unknown fields
    {"status": "completed", "completed_at": None},                               # breaks the record's invariants
    {"status": "claimed", "assigned_reviewer_id": None},
    {"status": "unassigned", "assigned_reviewer_id": "rev_1"},
    {"status": "archived"},
])
def test_a_stored_document_that_breaks_the_record_is_a_typed_data_error_never_returned(stored):
    async def body(mongo):
        mongo.collection.docs[0].update(stored)
        results = []
        for call in (mongo.repo.get_by_id("tenant_a", "t1"), mongo.repo.get_by_document_id("tenant_a", "doc-t1"),
                     mongo.repo.list_page("tenant_a", ReviewTaskListQuery()), mongo.repo.list_tasks("tenant_a")):
            with pytest.raises(PersistenceDataError) as caught:
                await call
            results.append(caught.value)
        return results

    for error in with_mongo([make("t1")], body):
        assert error.details["reason"] == "stored_document_does_not_match_the_model"
        assert "rev_1" not in str(error) + repr(error.details)


def test_a_corrupt_stored_task_is_not_overwritten_by_a_claim_that_cannot_read_it():
    async def body(mongo):
        mongo.collection.docs[0].update(status="completed", completed_at=None, assigned_reviewer_id="rev_1")
        with pytest.raises(PersistenceDataError):
            await mongo.repo.claim("tenant_a", "t1", "rev_2", at(1))
        return dict(mongo.collection.docs[0])

    stored = with_mongo([make("t1")], body)
    assert stored["status"] == "completed" and stored["assigned_reviewer_id"] == "rev_1"


def test_update_status_cannot_break_the_record_invariants_under_a_concurrent_completion():
    async def body(mongo):
        collection = mongo.collection
        original = collection.find_one_and_update
        state = {"raced": False}

        async def racing(flt, update, return_document=True, sort=None, session=None):
            if not state["raced"]:                                               # the task completes between read and write
                state["raced"] = True
                collection.docs[0].update(status="completed", assigned_reviewer_id="rev_1", completed_at=at(500))
            return await original(flt, update, return_document=return_document, sort=sort, session=session)

        collection.find_one_and_update = racing
        outcome = None
        try:
            outcome = await mongo.repo.update_status("tenant_a", "t1", "claimed", "rev_2")
        except ValueError:
            outcome = "refused"                                                  # re-derived from the fresh (completed) state
        return outcome, dict(collection.docs[0])

    outcome, stored = with_mongo([make("t1")], body)
    assert outcome == "refused"                                                  # claimed + completed_at would break the record
    assert stored["status"] == "completed" and stored["completed_at"] == at(500)


# ---- the service on top of the adapter --------------------------------------------------------------------------------

def test_the_review_task_service_works_on_the_mongo_adapter():
    async def body(mongo):
        first = ReviewTaskService(repository=mongo.repo, tenant_id="tenant_a", principal_id="rev_1")
        second = ReviewTaskService(repository=mongo.repo, tenant_id="tenant_a", principal_id="rev_2")
        claimed = await first.claim("t1")
        with pytest.raises(ReviewTaskClaimedError):
            await second.claim("t1")
        again = await first.claim("t1")
        done = await first.complete("t1")
        with pytest.raises(ReviewTaskCompletedError):
            await second.claim("t1")
        page = await first.list_tasks(status=None, assignee=ReviewTaskAssignee.MINE, document_id=None, page=1, size=10)
        return claimed, again, done, page

    claimed, again, done, page = with_mongo([make("t1")], body)
    assert claimed.status is CLAIMED and again.assigned_reviewer_id == "rev_1" and done.status is COMPLETED
    assert [t.task_id for t in page.items] == ["t1"]


# ---- composition ------------------------------------------------------------------------------------------------------

URI = "mongodb://app:Review-Secret-Marker-1@mongo-0:27017/?replicaSet=rs0"


def container_with(client=None, **kwargs):
    client = client or FakeClient()
    settings = ApiSettings(app_env="development", mongo_uri=URI, mongo_database="doc")
    connection = MongoConnection(MongoSettings.from_api_settings(settings), client_factory=factory_for(client))
    return ApplicationContainer(api_settings=settings, mongo_connection=connection, **kwargs), client


def test_start_up_configures_the_mongo_review_task_repository_after_a_successful_connection():
    container, client = container_with()
    assert container.review_task_repo is None                                    # construction opens nothing and wires nothing
    run(container.startup())
    assert isinstance(container.review_task_repo, MongoReviewTaskRepository)
    assert isinstance(container.review_task_repo, ReviewTaskRepository)
    run(container.startup())                                                     # idempotent: the same adapter stays
    assert isinstance(container.review_task_repo, MongoReviewTaskRepository)


def test_an_injected_repository_always_wins():
    sentinel = InMemoryReviewTaskRepository()
    container, _ = container_with(review_task_repo=sentinel)
    run(container.startup())
    assert container.review_task_repo is sentinel


def test_without_mongodb_the_port_stays_unconfigured():
    container = ApplicationContainer(api_settings=ApiSettings(app_env="development"))
    run(container.startup())
    assert container.review_task_repo is None


def test_a_failed_start_up_leaves_the_port_unconfigured_and_nothing_open():
    client = FakeClient(fail_connect=driver.NetworkTimeout("down"))
    container, _ = container_with(client)
    with pytest.raises(PersistenceUnavailableError):
        run(container.startup())
    assert container.review_task_repo is None and client.closed is True


def test_a_failed_index_bootstrap_leaves_the_port_unconfigured():
    client = FakeClient()
    container, _ = container_with(client)
    client["doc"]["review_tasks"].fail_with = driver.OperationFailure("boom", code=85)
    with pytest.raises(PersistenceError):
        run(container.startup())
    assert container.review_task_repo is None and client.closed is True


def _http_app(tasks):
    """The real application over the Mongo adapter (fake driver): start-up wires the repository; the route needs a principal."""
    from doc_extractor.api.contracts.review_tasks import SCOPE_REVIEW_TASKS_CLAIM, SCOPE_REVIEW_TASKS_READ
    from doc_extractor.api.ports.authentication import ApiPrincipal
    from doc_extractor.infrastructure.mongo import to_document
    from doc_extractor.malware import MockMalwareScanner
    from tests.test_api_documents_page_image import MockAuthProvider

    client = FakeClient()
    settings = ApiSettings(app_env="development", mongo_uri=URI, mongo_database="doc")
    connection = MongoConnection(MongoSettings.from_api_settings(settings), client_factory=factory_for(client))
    container = ApplicationContainer(api_settings=settings, mongo_connection=connection, auth_provider=MockAuthProvider(),
                                     malware_scanner=MockMalwareScanner())
    app = create_app(settings, container)
    scopes = {SCOPE_REVIEW_TASKS_READ, SCOPE_REVIEW_TASKS_CLAIM}
    table = app.state.auth_provider.principals
    for token, principal, tenant in (("rev1", "rev_one", "tenant_alpha"), ("rev2", "rev_two", "tenant_alpha"),
                                     ("beta", "rev_beta", "tenant_beta")):
        table[token] = ApiPrincipal(principal_id=principal, tenant_id=tenant, scopes=set(scopes), is_active=True)
    for n in range(30):
        table[f"r{n}"] = ApiPrincipal(principal_id=f"rev_{n}", tenant_id="tenant_alpha", scopes=set(scopes), is_active=True)
    client["doc"]["review_tasks"].docs.extend(to_document(task) for task in tasks)
    return app, client


def test_the_review_queue_runs_over_http_on_the_mongo_adapter_with_tenant_isolation():
    from fastapi.testclient import TestClient

    tasks = [make("t1", tenant="tenant_alpha"), make("t2", tenant="tenant_alpha", status=CLAIMED, owner="rev_two"),
             make("b1", tenant="tenant_beta")]
    app, client = _http_app(tasks)
    one, two, beta = ({"Authorization": f"Bearer {t}"} for t in ("rev1", "rev2", "beta"))
    base = "/api/v1/review-tasks"
    with TestClient(app, raise_server_exceptions=False) as http:
        listing = http.get(base, headers=one)
        assert listing.status_code == 200 and sorted(i["task_id"] for i in listing.json()["content"]) == ["t1", "t2"]
        assert http.post(f"{base}/t1/claim", headers=one).status_code == 200
        assert http.post(f"{base}/t1/claim", headers=one).status_code == 200            # idempotent for the owner
        assert http.post(f"{base}/t1/claim", headers=two).status_code == 409           # held by another reviewer
        assert http.post(f"{base}/t2/release", headers=one).status_code == 409         # only the owner may release
        assert http.post(f"{base}/t1/release", headers=one).status_code == 200
        assert http.post(f"{base}/b1/claim", headers=one).status_code == 404           # another tenant's task is absent
        assert http.post(f"{base}/t1/claim", headers=beta).status_code == 404
        assert http.post(f"{base}/b1/claim", headers=beta).status_code == 200
    stored = {d["task_id"]: d for d in client["doc"]["review_tasks"].docs}
    assert stored["t1"]["status"] == "unassigned" and stored["t1"]["assigned_reviewer_id"] is None
    assert stored["b1"]["assigned_reviewer_id"] == "rev_beta" and stored["t2"]["assigned_reviewer_id"] == "rev_two"


def test_concurrent_http_claims_on_the_mongo_adapter_have_one_winner_and_the_rest_conflict():
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    app, client = _http_app([make("t1", tenant="tenant_alpha")])
    with TestClient(app, raise_server_exceptions=False) as http:
        def claim(n):
            return http.post("/api/v1/review-tasks/t1/claim", headers={"Authorization": f"Bearer r{n}"}).status_code

        with ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(claim, range(30)))
    assert sorted(codes) == [200] + [409] * 29
    assert client["doc"]["review_tasks"].docs[0]["status"] == "claimed"


def test_the_adapter_names_no_in_memory_substitute():
    source = review_tasks_module.__doc__ + inspect.getsource(review_tasks_module)
    for forbidden in ("InMemory", "mongomock", "review_task_double", "fallback_to"):
        assert forbidden not in source
