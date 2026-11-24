"""
Tenant-scoped repository base tests (Team Member 3, Phase 2A-2).

MOCKED: the in-memory fake of ``tests/mongo_fakes.py`` records every call the base makes, so these tests prove WHAT is sent to
the driver (the tenant is in every filter, nothing unsafe is) and how failures are mapped. They do not prove server behaviour.
"""

import asyncio
import inspect
from datetime import datetime, timezone

import pytest

pytest.importorskip("pymongo", reason="the 'mongo' extra provides the driver whose error types these tests map")
from pymongo import errors as driver  # noqa: E402

from doc_extractor.infrastructure.mongo import (  # noqa: E402
    DuplicateKeyPersistenceError,
    MongoConnection,
    MongoSettings,
    PersistenceDataError,
    PersistenceUnavailableError,
    TenantScopedRepository,
    TenantScopeError,
    UnsafeQueryError,
)
from doc_extractor.infrastructure.mongo.repository import MAX_PAGE_SIZE, validate_filter, validate_update  # noqa: E402
from tests.mongo_fakes import FakeClient, factory_for  # noqa: E402

SECRET = "Doc-Value-Marker-9921"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo_and_collection():
    client = FakeClient()
    connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(client))
    run(connection.connect())
    repo = TenantScopedRepository(connection, "things")
    collection = client["doc"]["things"]
    return repo, collection


def seed(repo, collection):
    async def go():
        await collection.create_index([("tenant_id", 1), ("key", 1)], name="uq", unique=True)
        for tenant, key, value in (("tenant_a", "k1", 1), ("tenant_a", "k2", 2), ("tenant_b", "k1", 10), ("tenant_b", "k3", 30)):
            await repo.insert_one(tenant, {"key": key, "value": value})

    run(go())


# ---- tenant isolation ------------------------------------------------------------------------------------------------

def test_every_read_is_confined_to_the_tenant(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    assert run(repo.find_one("tenant_a", {"key": "k1"}))["value"] == 1
    assert run(repo.find_one("tenant_b", {"key": "k1"}))["value"] == 10
    assert run(repo.find_one("tenant_a", {"key": "k3"})) is None          # exists only for tenant_b
    assert {d["key"] for d in run(repo.find_many("tenant_a"))} == {"k1", "k2"}
    assert {d["key"] for d in run(repo.find_many("tenant_b"))} == {"k1", "k3"}
    assert run(repo.count("tenant_a")) == 2 and run(repo.count("tenant_b", {"key": "k3"})) == 1


def test_the_tenant_is_in_every_filter_sent_to_the_driver(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    collection.calls.clear()
    run(repo.find_one("tenant_a", {"key": "k1"}))
    run(repo.find_many("tenant_a", {"key": {"$in": ["k1", "k2"]}}))
    run(repo.count("tenant_a"))
    run(repo.update_one("tenant_a", {"key": "k1"}, {"$set": {"value": 5}}))
    run(repo.find_one_and_update("tenant_a", {"key": "k2"}, {"$inc": {"value": 1}}))
    run(repo.delete_one("tenant_a", {"key": "k2"}))
    assert len(collection.calls) == 6 and all(call[1]["tenant_id"] == "tenant_a" for call in collection.calls)


def test_writes_and_deletes_cannot_cross_tenants(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    assert run(repo.update_one("tenant_a", {"key": "k3"}, {"$set": {"value": 99}})) == 0
    assert run(repo.find_one_and_update("tenant_a", {"key": "k3"}, {"$set": {"value": 99}})) is None
    assert run(repo.delete_one("tenant_a", {"key": "k3"})) == 0
    assert run(repo.find_one("tenant_b", {"key": "k3"}))["value"] == 30


def test_a_filter_cannot_name_or_override_the_tenant(repo_and_collection):
    repo, _ = repo_and_collection
    for method in (repo.find_one, repo.find_many, repo.count, repo.delete_one):
        with pytest.raises(TenantScopeError):
            run(method("tenant_a", {"tenant_id": "tenant_b"}))
    with pytest.raises(TenantScopeError):
        run(repo.find_one("tenant_a", {"$or": [{"tenant_id": "tenant_b"}]}))
    with pytest.raises(TenantScopeError):
        run(repo.find_one("tenant_a", {"tenant_id": {"$ne": "tenant_a"}}))


def test_a_logical_operator_cannot_escape_the_tenant(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    found = run(repo.find_many("tenant_a", {"$or": [{"key": "k1"}, {"key": "k3"}]}))
    assert [d["key"] for d in found] == ["k1"]                            # k3 exists only for tenant_b


@pytest.mark.parametrize("tenant", [None, "", " ", 5, b"t", ["t"], "../x", "a/b", "a\\b", "a\x00b", "x" * 300, ".", ".."])
def test_an_invalid_tenant_is_refused_before_anything_is_sent(repo_and_collection, tenant):
    repo, collection = repo_and_collection
    for call in (lambda: repo.find_one(tenant), lambda: repo.find_many(tenant), lambda: repo.count(tenant),
                 lambda: repo.insert_one(tenant, {"a": 1}), lambda: repo.update_one(tenant, {}, {"$set": {"a": 1}}),
                 lambda: repo.delete_one(tenant, {})):
        with pytest.raises(TenantScopeError) as caught:
            run(call())
        assert "x" * 300 not in str(caught.value)
    assert collection.calls == []


def test_the_tenant_key_cannot_be_shadowed_by_the_filter_order(repo_and_collection):
    repo, _ = repo_and_collection
    scoped = repo.scoped_filter("tenant_a", {"key": "k1"})
    assert scoped["tenant_id"] == "tenant_a" and list(scoped)[-1] == "tenant_id"


# ---- inserts ----------------------------------------------------------------------------------------------------------

def test_an_insert_adds_the_tenant_and_accepts_the_same_tenant(repo_and_collection):
    repo, collection = repo_and_collection
    run(repo.insert_one("tenant_a", {"key": "k1"}))
    run(repo.insert_one("tenant_a", {"key": "k2", "tenant_id": "tenant_a"}))
    assert [d["tenant_id"] for d in collection.docs] == ["tenant_a", "tenant_a"]


def test_an_insert_naming_another_tenant_is_refused(repo_and_collection):
    repo, collection = repo_and_collection
    with pytest.raises(TenantScopeError) as caught:
        run(repo.insert_one("tenant_a", {"key": "k1", "tenant_id": "tenant_b"}))
    assert caught.value.details["reason"] == "document_names_another_tenant" and collection.docs == []


def test_an_insert_serializes_safely_and_refuses_unsafe_documents(repo_and_collection):
    repo, collection = repo_and_collection
    with pytest.raises(PersistenceDataError):
        run(repo.insert_one("tenant_a", {"$where": "1"}))
    with pytest.raises(PersistenceDataError):
        run(repo.insert_one("tenant_a", {"v": float("nan")}))
    assert collection.docs == []


def test_a_duplicate_key_is_a_typed_error_that_never_echoes_the_key(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    with pytest.raises(DuplicateKeyPersistenceError) as caught:
        run(repo.insert_one("tenant_a", {"key": "k1"}))
    text = str(caught.value) + repr(caught.value.details)
    assert "SECRET-VALUE-IN-DRIVER-TEXT" not in text and "k1" not in text and caught.value.details["collection"] == "things"
    run(repo.insert_one("tenant_c", {"key": "k1"}))                      # the same key in another tenant is fine


# ---- filter and update allow-lists --------------------------------------------------------------------------------------

@pytest.mark.parametrize("filter_", [
    {"$where": "this.a == 1"}, {"$expr": {"$eq": ["$a", 1]}}, {"a": {"$regex": ".*"}}, {"a": {"$function": {}}},
    {"a": {"$accumulator": {}}}, {"a": {"$elemMatch": {"b": 1}}}, {"a": {"$not": {"$eq": 1}}}, {"a": {"$in": "text"}},
    {"a": {"$in": [{"x": 1}]}}, {"a": {"$in": list(range(1001))}}, {"a": {"$exists": "yes"}}, {"a": {"$eq": {"x": 1}}},
    {"a": {"b": 1}}, {"a": [1, 2]}, {"a": {}}, {"$and": []}, {"$or": "x"}, {"$nor": [1]}, {"$text": {"$search": "x"}}, {"": 1}, {5: 1},
])
def test_unsafe_filters_are_refused(filter_):
    with pytest.raises((UnsafeQueryError, TenantScopeError)):
        validate_filter(filter_)


@pytest.mark.parametrize("filter_", [
    {"a": 1}, {"a": "x"}, {"a": None}, {"a": True}, {"a": {"$gte": 1, "$lt": 5}}, {"a": {"$in": ["x", "y"]}}, {"a": {"$ne": None}},
    {"a": {"$exists": True}}, {"$or": [{"a": 1}, {"b": {"$gt": 2}}]}, {"$and": [{"a": 1}, {"$nor": [{"b": 2}]}]},
])
def test_ordinary_filters_pass_and_are_copied(filter_):
    checked = validate_filter(filter_)
    assert checked == filter_ and checked is not filter_


def test_a_deeply_nested_filter_is_refused():
    deep = {"a": 1}
    for _ in range(10):
        deep = {"$and": [deep]}
    with pytest.raises(UnsafeQueryError):
        validate_filter(deep)


def test_a_none_filter_means_the_tenant_only(repo_and_collection):
    repo, _ = repo_and_collection
    assert repo.scoped_filter("tenant_a", None) == {"tenant_id": "tenant_a"}


@pytest.mark.parametrize("update", [
    {}, {"a": 1}, {"$rename": {"a": "b"}}, {"$set": {}}, {"$set": "x"}, {"$set": {"tenant_id": "t"}}, {"$set": {"_id": 1}},
    {"$unset": {"tenant_id": ""}}, {"$inc": {"tenant_id.x": 1}}, {"$set": {"$bad": 1}}, {"$set": {"": 1}}, {"$where": {"a": 1}},
    {"$currentDate": {"_id": True}}, {"$set": {"a": float("nan")}},
])
def test_unsafe_updates_are_refused(update):
    with pytest.raises((UnsafeQueryError, TenantScopeError, PersistenceDataError)):
        validate_update(update)


def test_an_ordinary_update_passes_and_is_serialized():
    from datetime import datetime, timezone

    checked = validate_update({"$set": {"status": "claimed", "updated_at": datetime(2026, 1, 1, 0, 0, 0, 999, tzinfo=timezone.utc)},
                               "$inc": {"n": 1}})
    assert checked["$set"]["updated_at"].microsecond == 0 and checked["$inc"] == {"n": 1}


def test_an_update_cannot_change_the_tenant(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    with pytest.raises(TenantScopeError):
        run(repo.update_one("tenant_a", {"key": "k1"}, {"$set": {"tenant_id": "tenant_b"}}))
    with pytest.raises(TenantScopeError):
        run(repo.find_one_and_update("tenant_a", {"key": "k1"}, {"$set": {"tenant_id": "tenant_b"}}))
    assert run(repo.count("tenant_a")) == 2


# ---- results, ids, paging ---------------------------------------------------------------------------------------------

def test_results_never_expose_the_internal_id(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    collection.docs[0]["_id"] = "internal"
    assert "_id" not in run(repo.find_one("tenant_a", {"key": "k1"}))
    assert all("_id" not in d for d in run(repo.find_many("tenant_a")))
    assert "_id" not in run(repo.find_one_and_update("tenant_a", {"key": "k1"}, {"$set": {"value": 3}}))


def test_find_many_pages_and_sorts(repo_and_collection):
    repo, collection = repo_and_collection

    async def fill():
        for number in range(5):
            await repo.insert_one("tenant_a", {"key": f"k{number}", "rank": number})

    run(fill())
    assert [d["rank"] for d in run(repo.find_many("tenant_a", sort=[("rank", -1)], limit=2))] == [4, 3]
    assert [d["rank"] for d in run(repo.find_many("tenant_a", sort=[("rank", 1)], skip=3, limit=10))] == [3, 4]


@pytest.mark.parametrize("skip, limit", [(-1, 10), (0, 0), (0, -5), (0, MAX_PAGE_SIZE + 1), (0, "10"), ("0", 10), (0, None), (1.5, 5)])
def test_page_bounds_are_enforced(repo_and_collection, skip, limit):
    repo, collection = repo_and_collection
    with pytest.raises(UnsafeQueryError):
        run(repo.find_many("tenant_a", skip=skip, limit=limit))
    assert collection.calls == []


def test_the_maximum_page_size_is_accepted(repo_and_collection):
    repo, _ = repo_and_collection
    assert run(repo.find_many("tenant_a", limit=MAX_PAGE_SIZE)) == []


def test_find_one_and_update_is_one_conditional_call_returning_the_document(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    collection.calls.clear()
    after = run(repo.find_one_and_update("tenant_a", {"key": "k1", "value": 1}, {"$set": {"value": 7}}))
    assert after["value"] == 7 and [call[0] for call in collection.calls] == ["find_one_and_update"]
    assert run(repo.find_one_and_update("tenant_a", {"key": "k1", "value": 1}, {"$set": {"value": 8}})) is None  # condition no longer holds


def test_a_session_is_passed_through_to_the_driver(repo_and_collection):
    repo, collection = repo_and_collection
    seen = []
    original = collection.insert_one

    async def spy(doc, session=None):
        seen.append(session)
        return await original(doc)

    collection.insert_one = spy
    run(repo.insert_one("tenant_a", {"key": "k"}, session="SESSION"))
    assert seen == ["SESSION"]


# ---- failures ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("failure, expected", [
    (driver.AutoReconnect("host mongo-0 lost SECRET"), PersistenceUnavailableError),
    (driver.ServerSelectionTimeoutError("no servers SECRET"), PersistenceUnavailableError),
    (driver.NetworkTimeout("timeout SECRET"), PersistenceUnavailableError),
    (driver.ExecutionTimeout("maxTimeMS SECRET"), PersistenceUnavailableError),
])
def test_driver_failures_become_typed_errors_without_driver_text(repo_and_collection, failure, expected):
    repo, collection = repo_and_collection
    collection.fail_with = failure
    for call in (lambda: repo.find_one("tenant_a"), lambda: repo.count("tenant_a"), lambda: repo.insert_one("tenant_a", {"a": 1}),
                 lambda: repo.update_one("tenant_a", {}, {"$set": {"a": 1}}), lambda: repo.delete_one("tenant_a", {})):
        with pytest.raises(expected) as caught:
            run(call())
        assert "SECRET" not in str(caught.value) and "mongo-0" not in repr(caught.value.details)
        assert caught.value.details["collection"] == "things"


def test_a_failure_while_reading_a_cursor_is_mapped_too(repo_and_collection):
    repo, collection = repo_and_collection

    class Broken:
        def sort(self, spec): return self
        def skip(self, n): return self
        def limit(self, n): return self
        async def to_list(self, length=None): raise driver.NetworkTimeout("cursor died SECRET")

    collection.find = lambda flt: Broken()
    with pytest.raises(PersistenceUnavailableError):
        run(repo.find_many("tenant_a"))


def test_a_server_side_rejection_is_a_typed_error(repo_and_collection):
    repo, collection = repo_and_collection
    collection.fail_with = driver.OperationFailure("not authorized on doc to execute command SECRET", code=13)
    from doc_extractor.infrastructure.mongo import PersistenceError

    with pytest.raises(PersistenceError) as caught:
        run(repo.find_one("tenant_a"))
    assert caught.value.details["reason"] == "authorization_failed" and "SECRET" not in str(caught.value)


def test_an_unexpected_exception_is_wrapped_not_leaked(repo_and_collection):
    repo, collection = repo_and_collection
    collection.fail_with = RuntimeError("boom SECRET")
    from doc_extractor.infrastructure.mongo import PersistenceError

    with pytest.raises(PersistenceError) as caught:
        run(repo.find_one("tenant_a"))
    assert caught.value.details["reason"] == "unexpected_error" and "SECRET" not in str(caught.value) and caught.value.__cause__ is None


def test_persistence_errors_are_server_side_only_storage_errors():
    """The REST API answers a StorageError with a generic 500 and logs only the fixed reason (see api/exceptions.py)."""
    from doc_extractor.exceptions import StorageError
    from doc_extractor.infrastructure.mongo import PersistenceError

    assert issubclass(PersistenceError, StorageError)
    assert PersistenceError().details == {"reason": "persistence_failed"}


@pytest.mark.parametrize("name", ["", "a$b", "system.users", "$cmd"])
def test_a_repository_needs_a_plain_collection_name(name):
    with pytest.raises(ValueError):
        TenantScopedRepository(object(), name)  # type: ignore[arg-type]


def test_a_repository_cannot_be_used_before_the_connection_is_established():
    connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(FakeClient()))
    repo = TenantScopedRepository(connection, "things")
    with pytest.raises(PersistenceUnavailableError):
        run(repo.find_one("tenant_a"))


# ---- reads inside a transaction (remediation of review finding M2) ----------------------------------------------------------

class Marker:
    """A stand-in session: only its identity matters to the fake."""


def calls_for(collection, operation):
    return [call for call in collection.calls if call[0] == operation]


def test_reads_without_a_session_pass_none_to_the_driver_exactly_as_before(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    collection.session_calls.clear()
    run(repo.find_one("tenant_a", {"key": "k1"}))
    run(repo.find_many("tenant_a"))
    run(repo.count("tenant_a"))
    assert collection.session_calls == [("find_one", None), ("find", None), ("count_documents", None)]


def test_a_session_reaches_the_driver_for_every_read(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    collection.session_calls.clear()
    session = Marker()
    assert run(repo.find_one("tenant_a", {"key": "k1"}, session=session))["value"] == 1
    assert {d["key"] for d in run(repo.find_many("tenant_a", session=session))} == {"k1", "k2"}
    assert run(repo.count("tenant_a", session=session)) == 2
    assert [(op, s is session) for op, s in collection.session_calls] == [("find_one", True), ("find", True), ("count_documents", True)]


def test_the_tenant_predicate_is_enforced_on_reads_that_carry_a_session(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    session = Marker()
    collection.calls.clear()
    assert run(repo.find_one("tenant_a", {"key": "k3"}, session=session)) is None          # k3 exists for tenant_b only
    assert {d["key"] for d in run(repo.find_many("tenant_b", session=session))} == {"k1", "k3"}
    assert run(repo.count("tenant_a", {"key": "k3"}, session=session)) == 0
    for _, query, *_ in collection.calls:
        assert query["tenant_id"] in {"tenant_a", "tenant_b"}
    assert [call[1]["tenant_id"] for call in collection.calls] == ["tenant_a", "tenant_b", "tenant_a"]


def test_a_session_does_not_relax_filter_validation_or_the_tenant_rule(repo_and_collection):
    repo, collection = repo_and_collection
    session = Marker()
    with pytest.raises(TenantScopeError):
        run(repo.find_one("tenant_a", {"tenant_id": "tenant_b"}, session=session))
    with pytest.raises(UnsafeQueryError):
        run(repo.find_many("tenant_a", {"$where": "1"}, session=session))
    with pytest.raises(UnsafeQueryError):
        run(repo.count("tenant_a", {"key": {"$regex": ".*"}}, session=session))
    with pytest.raises(TenantScopeError):
        run(repo.find_one(None, {"key": "k1"}, session=session))
    with pytest.raises(TenantScopeError):
        run(repo.count("", session=session))
    assert collection.calls == []                                                 # nothing reached the driver


def test_a_read_and_a_write_inside_one_transaction_share_the_session_and_stay_scoped():
    client = FakeClient()
    connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(client))
    repo = TenantScopedRepository(connection, "things")
    collection = client["doc"]["things"]
    seen = {}

    async def go():
        await connection.connect()
        await repo.insert_one("tenant_b", {"key": "k1", "value": 10})

        async def work(session):
            await repo.insert_one("tenant_a", {"key": "k1", "value": 1}, session=session)
            seen["own"] = await repo.find_one("tenant_a", {"key": "k1"}, session=session)
            seen["other"] = await repo.count("tenant_b", {"key": "k1"}, session=session)
            await repo.update_one("tenant_a", {"key": "k1"}, {"$inc": {"value": 1}}, session=session)
            seen["session"] = session

        await connection.run_in_transaction(work)

    run(go())
    assert seen["own"]["value"] == 1 and seen["other"] == 1
    used = [s for op, s in collection.session_calls if op in {"find_one", "count_documents", "update_one"}]
    assert used and all(s is seen["session"] for s in used)
    assert client.sessions[0].committed == 1


def test_the_real_driver_accepts_a_session_on_every_operation_the_base_uses():
    """Offline check of the REAL signatures (no server): the base passes ``session=`` to each of these."""
    from pymongo.asynchronous.collection import AsyncCollection
    from pymongo.asynchronous.cursor import AsyncCursor

    for name in ("count_documents", "insert_one", "update_one", "find_one_and_update", "delete_one"):
        assert "session" in inspect.signature(getattr(AsyncCollection, name)).parameters, name
    assert "session" in inspect.signature(AsyncCursor.__init__).parameters        # ``find`` / ``find_one`` forward it to the cursor


# ---- caller-supplied _id (remediation of review finding L2) ----------------------------------------------------------------

def test_a_root_id_is_refused_before_anything_reaches_the_driver(repo_and_collection):
    repo, collection = repo_and_collection
    for bad_id in ("shared-id", 1, None, 0, ""):
        with pytest.raises(PersistenceDataError) as caught:
            run(repo.insert_one("tenant_a", {"_id": bad_id, "key": "k"}))
        assert caught.value.details["reason"] == "document_must_not_set_the_id"
    assert collection.docs == [] and collection.calls == []


def test_a_refused_root_id_is_the_same_error_whether_or_not_another_tenant_owns_it(repo_and_collection):
    """No existence oracle: the answer cannot depend on what is stored."""
    repo, collection = repo_and_collection
    seed(repo, collection)
    collection.docs[0]["_id"] = "taken"                                           # a stored record of tenant_a
    taken = None
    for candidate in ("taken", "free"):
        with pytest.raises(PersistenceDataError) as caught:
            run(repo.insert_one("tenant_b", {"_id": candidate, "key": "new"}))
        taken = taken or (type(caught.value), caught.value.details, str(caught.value))
        assert (type(caught.value), caught.value.details, str(caught.value)) == taken


def test_a_nested_field_named_id_is_data_not_the_root_id(repo_and_collection):
    repo, collection = repo_and_collection
    run(repo.insert_one("tenant_a", {"key": "k", "meta": {"_id": "inner"}}))
    assert run(repo.find_one("tenant_a", {"key": "k"}))["meta"] == {"_id": "inner"}


def test_a_normal_insert_still_works_adds_the_tenant_and_does_not_touch_the_callers_document(repo_and_collection):
    repo, collection = repo_and_collection
    document = {"key": "k", "value": 7}
    run(repo.insert_one("tenant_a", document))
    assert document == {"key": "k", "value": 7}
    assert collection.docs[0] == {"key": "k", "value": 7, "tenant_id": "tenant_a"} and "_id" not in collection.docs[0]
    with pytest.raises(TenantScopeError):
        run(repo.insert_one("tenant_a", {"key": "x", "tenant_id": "tenant_b"}))
    assert run(repo.count("tenant_b")) == 0


def test_tenant_isolation_holds_after_the_id_rule(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    with pytest.raises(PersistenceDataError):
        run(repo.insert_one("tenant_a", {"_id": "tenant_b-doc", "key": "z"}))
    assert {d["key"] for d in run(repo.find_many("tenant_a"))} == {"k1", "k2"}
    assert {d["key"] for d in run(repo.find_many("tenant_b"))} == {"k1", "k3"}


# ---- update targets versus values (remediation of review finding L1) -------------------------------------------------------

def test_the_dotted_set_example_from_the_review_is_accepted():
    assert validate_update({"$set": {"metadata.count": 1}}) == {"$set": {"metadata.count": 1}}


def test_the_push_each_example_from_the_review_is_accepted_and_its_items_are_serialized():
    assert validate_update({"$push": {"flags": {"$each": ["a", "b"]}}}) == {"$push": {"flags": {"$each": ["a", "b"]}}}
    checked = validate_update({"$addToSet": {"seen": {"$each": [datetime(2026, 1, 1, 0, 0, 0, 999, tzinfo=timezone.utc)]}}})
    assert checked["$addToSet"]["seen"]["$each"][0].microsecond == 0


@pytest.mark.parametrize("update", [
    {"$set": {"a.b.c": 1}}, {"$set": {"items.0.status": "x"}}, {"$set": {"metadata.tenant_id": "x"}},   # nested names are data
    {"$unset": {"metadata.count": ""}}, {"$inc": {"counters.reads": 1}}, {"$push": {"flags": "x"}}, {"$push": {"flags": {"a": 1}}},
    {"$push": {"flags": {"$each": []}}}, {"$setOnInsert": {"meta.created": 1}}, {"$pull": {"flags": "x"}},
    {"$set": {"a": 1}, "$push": {"b": {"$each": [1]}}},
])
def test_legitimate_update_syntax_is_supported(update):
    assert validate_update(update)


@pytest.mark.parametrize("update, expected", [
    ({"$set": {"tenant_id": "t"}}, TenantScopeError), ({"$set": {"_id": 1}}, TenantScopeError),
    ({"$set": {"tenant_id.x": 1}}, TenantScopeError), ({"$set": {"_id.x": 1}}, TenantScopeError),
    ({"$push": {"tenant_id": {"$each": ["t"]}}}, TenantScopeError), ({"$unset": {"_id": ""}}, TenantScopeError),
    ({"$rename": {"a": "tenant_id"}}, UnsafeQueryError), ({"$where": {"a": 1}}, UnsafeQueryError),
    ({"$set": {"$bad": 1}}, UnsafeQueryError), ({"$set": {"a.$b": 1}}, UnsafeQueryError), ({"$set": {"a.$[]": 1}}, UnsafeQueryError),
    ({"$set": {"a..b": 1}}, UnsafeQueryError), ({"$set": {"a.": 1}}, UnsafeQueryError), ({"$set": {".a": 1}}, UnsafeQueryError),
    ({"$set": {"a\x00b": 1}}, UnsafeQueryError), ({"$set": {"": 1}}, UnsafeQueryError), ({"$set": {1: 1}}, UnsafeQueryError),
    ({"$set": {"x": {"$each": [1]}}}, PersistenceDataError),                       # $each is a modifier of push and addToSet only
    ({"$set": {"x": {"$where": "1"}}}, PersistenceDataError), ({"$set": {"x": {"a.b": 1}}}, PersistenceDataError),
    ({"$push": {"x": {"$each": [1], "$position": 0}}}, UnsafeQueryError),          # no other modifiers
    ({"$push": {"x": {"$each": [1], "$slice": 1}}}, UnsafeQueryError),
    ({"$push": {"x": {"$each": "abc"}}}, UnsafeQueryError), ({"$push": {"x": {"$each": {"a": 1}}}}, UnsafeQueryError),
    ({"$push": {"x": {"$each": list(range(1001))}}}, UnsafeQueryError),
    ({"$push": {"x": {"$each": [{"$where": "1"}]}}}, PersistenceDataError),         # items are checked as values
    ({"$push": {"x": {"$gt": 1}}}, PersistenceDataError),
    ({"$set": {"a": float("inf")}}, PersistenceDataError), ({"$set": {"a": object()}}, PersistenceDataError),
    ({"$currentDate": {"_id": True}}, TenantScopeError), ({}, UnsafeQueryError), ({"$set": {}}, UnsafeQueryError),
])
def test_unsafe_update_syntax_is_still_refused(update, expected):
    with pytest.raises(expected):
        validate_update(update)


def test_the_validated_update_is_a_copy_and_the_callers_update_is_unchanged():
    update = {"$set": {"metadata.count": 1}, "$push": {"flags": {"$each": [1]}}}
    snapshot = {"$set": {"metadata.count": 1}, "$push": {"flags": {"$each": [1]}}}
    checked = validate_update(update)
    assert update == snapshot and checked is not update and checked["$set"] is not update["$set"]


def test_dotted_and_each_updates_reach_the_driver_through_the_tenant_scoped_base(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    collection.calls.clear()
    assert run(repo.update_one("tenant_a", {"key": "k1"}, {"$set": {"metadata.count": 3}})) == 1
    run(repo.find_one_and_update("tenant_a", {"key": "k1"}, {"$push": {"flags": {"$each": ["a", "b"]}}}))
    for _, query, update in collection.calls:
        assert query["tenant_id"] == "tenant_a"
    sent = [call[2] for call in collection.calls]
    assert sent == [{"$set": {"metadata.count": 3}}, {"$push": {"flags": {"$each": ["a", "b"]}}}]
    stored = run(repo.find_one("tenant_a", {"key": "k1"}))
    assert stored["metadata"] == {"count": 3} and stored["flags"] == ["a", "b"]
    assert run(repo.find_one("tenant_b", {"key": "k1"})).get("metadata") is None  # the other tenant's record is untouched


def test_an_update_cannot_reach_the_tenant_through_a_dotted_path_or_each(repo_and_collection):
    repo, collection = repo_and_collection
    seed(repo, collection)
    for update in ({"$set": {"tenant_id.x": 1}}, {"$push": {"tenant_id": {"$each": ["tenant_b"]}}}, {"$set": {"_id.a": 1}}):
        with pytest.raises(TenantScopeError):
            run(repo.update_one("tenant_a", {"key": "k1"}, update))
    assert run(repo.count("tenant_a")) == 2 and run(repo.count("tenant_b")) == 2


# ---- the one tenant-exempt read, and windows (Phase 2A-4) ---------------------------------------------------------------

def _keyed(global_fields=("token_hash",)):
    client = FakeClient()
    connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(client))
    run(connection.connect())
    repo = TenantScopedRepository(connection, "things", global_unique_fields=global_fields)
    collection = client["doc"]["things"]
    for tenant, key, token in (("tenant_a", "k1", "tok-1"), ("tenant_b", "k1", "tok-2"), ("tenant_b", "k2", "tok-3")):
        run(repo.insert_one(tenant, {"key": key, "token_hash": token}))
    collection.calls.clear()
    return repo, collection


def test_a_declared_global_unique_field_is_found_across_tenants_and_names_its_owner():
    repo, collection = _keyed()
    assert run(repo.find_one_by_global_unique_key("token_hash", "tok-2"))["tenant_id"] == "tenant_b"
    assert run(repo.find_one_by_global_unique_key("token_hash", "tok-1"))["tenant_id"] == "tenant_a"
    assert run(repo.find_one_by_global_unique_key("token_hash", "nobody")) is None
    assert collection.calls[0] == ("find_one", {"token_hash": "tok-2"})                  # the single equality, nothing else
    assert "_id" not in run(repo.find_one_by_global_unique_key("token_hash", "tok-3"))


@pytest.mark.parametrize("field", ["tenant_id", "_id", "key", "other", "", "token_hash.x", "$where"])
def test_an_undeclared_field_cannot_be_used_for_a_tenant_exempt_read(field):
    repo, collection = _keyed()
    with pytest.raises(TenantScopeError):
        run(repo.find_one_by_global_unique_key(field, "tok-1"))
    assert collection.calls == []


def test_a_repository_without_declared_global_fields_has_no_tenant_exempt_read():
    repo, collection = _keyed(global_fields=())
    with pytest.raises(TenantScopeError):
        run(repo.find_one_by_global_unique_key("token_hash", "tok-1"))
    assert collection.calls == []


@pytest.mark.parametrize("value", [{"$ne": ""}, {"$regex": ".*"}, ["tok-1"], None, 5, b"tok-1", "", "x" * 257])
def test_the_global_value_must_be_a_plain_string_so_no_operator_or_scan_is_possible(value):
    repo, collection = _keyed()
    with pytest.raises(UnsafeQueryError):
        run(repo.find_one_by_global_unique_key("token_hash", value))
    assert collection.calls == []


@pytest.mark.parametrize("bad", ["tenant_id", "_id", "", "$x", "a.b", 5, None])
def test_the_global_field_declaration_itself_is_validated(bad):
    client = FakeClient()
    connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(client))
    with pytest.raises(ValueError):
        TenantScopedRepository(connection, "things", global_unique_fields=[bad])


def test_a_global_field_does_not_loosen_any_tenant_scoped_operation():
    repo, collection = _keyed()
    assert [d["key"] for d in run(repo.find_many("tenant_a"))] == ["k1"]
    assert run(repo.count("tenant_b")) == 2
    with pytest.raises(TenantScopeError):
        run(repo.find_one("tenant_a", {"tenant_id": "tenant_b"}))
    with pytest.raises(UnsafeQueryError):
        run(repo.find_one("tenant_a", {"token_hash": {"$regex": "."}}))
    assert run(repo.find_one("tenant_a", {"token_hash": "tok-2"})) is None               # scoped: another tenant's token is absent


def test_a_driver_failure_on_the_global_read_is_a_typed_error_that_leaks_nothing():
    repo, collection = _keyed()
    collection.fail_with = driver.NetworkTimeout("lost mongo-0 SECRET tok-1")
    with pytest.raises(PersistenceUnavailableError) as caught:
        run(repo.find_one_by_global_unique_key("token_hash", "tok-1"))
    assert "SECRET" not in str(caught.value) + repr(caught.value.details) and "tok-1" not in str(caught.value) + repr(caught.value.details)


def test_find_window_is_tenant_scoped_chunked_and_bounded(repo_and_collection):
    repo, collection = repo_and_collection

    async def go():
        for n in range(1300):
            await repo.insert_one("tenant_a", {"key": f"k{n:05d}", "value": n})
        await repo.insert_one("tenant_b", {"key": "other", "value": -1})
        collection.calls.clear()
        everything = await repo.find_window("tenant_a", None, sort=[("key", 1)], skip=50, limit=5000)
        some = await repo.find_window("tenant_a", {"value": {"$lt": 10}}, sort=[("value", 1)], limit=3)
        return everything, some, [c for c in collection.calls if c[0] == "find"]

    everything, some, finds = run(go())
    assert len(everything) == 1250 and everything[0]["key"] == "k00050" and all(d["key"] != "other" for d in everything)
    assert [d["value"] for d in some] == [0, 1, 2] and len(finds) == 3
    assert all(call[1]["tenant_id"] == "tenant_a" for call in finds)
    assert run(repo.find_window("tenant_a", None, limit=0)) == []
    for kwargs in ({"limit": -1}, {"skip": -1}, {"limit": "5"}, {"skip": 1.5}):
        with pytest.raises(UnsafeQueryError):
            run(repo.find_window("tenant_a", None, **kwargs))
