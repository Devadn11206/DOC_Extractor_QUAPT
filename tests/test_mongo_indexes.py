"""
Index registry and bootstrap tests (Team Member 3, Phase 2A-2).

MOCKED: the fake collection of ``tests/mongo_fakes.py`` records which index operations the bootstrap issues. These prove the
registry's content and the bootstrap's behaviour (idempotent, never drops, conflicts are typed); real index behaviour on a
server is checked by ``tests/test_mongo_integration.py``.
"""

import asyncio

import pytest

pytest.importorskip("pymongo", reason="the 'mongo' extra provides the driver whose error types these tests map")
from pymongo import errors as driver  # noqa: E402

from doc_extractor.infrastructure.mongo import (  # noqa: E402
    DEFAULT_REGISTRY,
    IndexConflictError,
    IndexRegistry,
    IndexSpec,
    PersistenceUnavailableError,
    ensure_indexes,
)
from doc_extractor.infrastructure.mongo.indexes import (  # noqa: E402
    API_KEYS, AUDIT_EVENTS, IDEMPOTENCY_RECORDS, PROCESSING_JOBS, REVIEW_TASKS, build_default_registry,
)
from tests.mongo_fakes import FakeDatabase, LogCapture  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ---- the declared indexes -----------------------------------------------------------------------------------------------

def by_name(collection):
    return {spec.name: spec for spec in DEFAULT_REGISTRY.for_collection(collection)}


def test_the_registry_declares_the_known_collections_and_nothing_else():
    assert DEFAULT_REGISTRY.collections() == (REVIEW_TASKS, API_KEYS, AUDIT_EVENTS, PROCESSING_JOBS, IDEMPOTENCY_RECORDS)
    assert set(by_name(REVIEW_TASKS)) == {"rt_tenant_task_uq", "rt_tenant_document_created", "rt_tenant_status_created_task",
                                          "rt_tenant_reviewer_status_created_task"}
    assert set(by_name(API_KEYS)) == {"ak_key_hash_uq", "ak_tenant_key_uq"}


def test_every_index_is_tenant_first_except_the_declared_exemptions():
    exempt = [spec for spec in DEFAULT_REGISTRY.all() if spec.keys[0][0] != "tenant_id"]
    assert [(spec.collection, spec.name) for spec in exempt] == [
        (API_KEYS, "ak_key_hash_uq"), (PROCESSING_JOBS, "pj_expires_ttl"), (IDEMPOTENCY_RECORDS, "idem_expires_ttl")]
    assert all(spec.tenant_exempt for spec in exempt)
    assert exempt[0].unique is True                                      # the credential hash
    assert all(spec.expire_after_seconds == 0 and not spec.unique and len(spec.keys) == 1 for spec in exempt[1:])   # TTL housekeeping only


def test_review_task_identity_is_unique_per_tenant():
    spec = by_name(REVIEW_TASKS)["rt_tenant_task_uq"]
    assert spec.unique is True and spec.keys == (("tenant_id", 1), ("task_id", 1))


def test_the_review_task_document_lookup_is_non_unique_until_the_product_rule_is_approved():
    """DR-2 of the Phase 1.2 approval package: no document-level unique constraint exists while the rule is open."""
    lookup = by_name(REVIEW_TASKS)["rt_tenant_document_created"]
    assert lookup.unique is False and lookup.partial_filter is None
    assert lookup.keys[:2] == (("tenant_id", 1), ("document_id", 1))
    for spec in by_name(REVIEW_TASKS).values():
        if spec.unique:
            assert [key for key, _ in spec.keys] == ["tenant_id", "task_id"]        # the ONLY unique review-task index
    assert not any(spec.unique and "document_id" in dict(spec.keys) for spec in DEFAULT_REGISTRY.all())


def test_the_api_key_hash_is_globally_unique_and_key_ids_are_unique_per_tenant():
    keys = by_name(API_KEYS)
    assert keys["ak_key_hash_uq"].keys == (("key_hash", 1),) and keys["ak_key_hash_uq"].unique is True
    assert keys["ak_tenant_key_uq"].keys == (("tenant_id", 1), ("key_id", 1)) and keys["ak_tenant_key_uq"].unique is True


def test_the_only_unique_indexes_are_the_intrinsic_identity_constraints():
    assert sorted(spec.name for spec in DEFAULT_REGISTRY.all() if spec.unique) == [
        "ak_key_hash_uq", "ak_tenant_key_uq", "audit_tenant_seq_uq", "idem_tenant_key_uq", "pj_tenant_job_uq", "rt_tenant_task_uq"]


def test_the_listing_indexes_end_with_the_task_id_tie_breaker():
    for name in ("rt_tenant_status_created_task", "rt_tenant_reviewer_status_created_task"):
        assert by_name(REVIEW_TASKS)[name].keys[-2:] == (("created_at", 1), ("task_id", 1))


# ---- spec and registry validation ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    dict(collection="", keys=(("tenant_id", 1),), name="n"), dict(collection="c", keys=(), name="n"),
    dict(collection="c", keys=(("tenant_id", 1),), name=""), dict(collection="c", keys=(("tenant_id", 2),), name="n"),
    dict(collection="c", keys=((" ", 1),), name="n"), dict(collection="c", keys=(("a", 1), ("tenant_id", 1)), name="n"),
    dict(collection="c", keys=(("a", 1),), name="n", tenant_exempt=True),                      # exempt but not unique
])
def test_an_invalid_index_spec_is_refused(kwargs):
    with pytest.raises(ValueError):
        IndexSpec(**kwargs)


def test_the_registry_refuses_duplicate_names_and_duplicate_key_patterns():
    base = IndexSpec("c", (("tenant_id", 1), ("a", 1)), "n1")
    registry = IndexRegistry([base])
    with pytest.raises(ValueError):
        registry.register(IndexSpec("c", (("tenant_id", 1), ("b", 1)), "n1"))
    with pytest.raises(ValueError):
        registry.register(IndexSpec("c", (("tenant_id", 1), ("a", 1)), "n2"))
    registry.register(IndexSpec("other", (("tenant_id", 1), ("a", 1)), "n1"))                  # per collection
    registry.register(IndexSpec("c", (("tenant_id", 1), ("a", 1)), "n3", unique=True))        # a different uniqueness is a different index


def test_the_default_registry_builds_identically_each_time():
    assert build_default_registry().all() == DEFAULT_REGISTRY.all()


def test_create_kwargs_carry_every_option():
    spec = IndexSpec("c", (("tenant_id", 1),), "n", partial_filter={"x": {"$exists": True}}, expire_after_seconds=60)
    assert spec.create_kwargs() == {"name": "n", "unique": False, "partialFilterExpression": {"x": {"$exists": True}}, "expireAfterSeconds": 60}
    assert IndexSpec("c", (("tenant_id", 1),), "n").create_kwargs() == {"name": "n", "unique": False}


# ---- bootstrap ------------------------------------------------------------------------------------------------------------

def test_the_bootstrap_creates_every_declared_index():
    database = FakeDatabase("doc")
    report = run(ensure_indexes(database))
    assert len(report.created) == len(DEFAULT_REGISTRY.all()) and report.existing == () and report.changed is True
    assert set(database[REVIEW_TASKS].indexes) == set(by_name(REVIEW_TASKS)) and set(database[API_KEYS].indexes) == set(by_name(API_KEYS))
    assert database[REVIEW_TASKS].indexes["rt_tenant_task_uq"]["unique"] is True
    assert database[REVIEW_TASKS].indexes["rt_tenant_document_created"]["unique"] is False


def test_running_the_bootstrap_again_changes_nothing():
    database = FakeDatabase("doc")
    run(ensure_indexes(database))
    snapshot = {name: dict(collection.indexes) for name, collection in database.collections.items()}
    second = run(ensure_indexes(database))
    third = run(ensure_indexes(database))
    assert second.created == () and third.created == () and second.changed is False
    assert len(second.existing) == len(DEFAULT_REGISTRY.all())
    assert {name: dict(collection.indexes) for name, collection in database.collections.items()} == snapshot


def test_a_partially_bootstrapped_database_gets_only_the_missing_indexes():
    database = FakeDatabase("doc")
    run(ensure_indexes(database, only_collections=[API_KEYS]))
    report = run(ensure_indexes(database))
    assert set(report.created) == {f"{c}.{name}" for c in (REVIEW_TASKS, AUDIT_EVENTS, PROCESSING_JOBS, IDEMPOTENCY_RECORDS) for name in by_name(c)}
    assert set(report.existing) == {f"{API_KEYS}.{name}" for name in by_name(API_KEYS)}


def test_the_bootstrap_can_be_limited_to_some_collections():
    database = FakeDatabase("doc")
    run(ensure_indexes(database, only_collections=[REVIEW_TASKS]))
    assert REVIEW_TASKS in database.collections and API_KEYS not in database.collections


def test_the_bootstrap_only_ever_creates_indexes_and_never_drops_one():
    database = FakeDatabase("doc")
    run(ensure_indexes(database))
    collection = database[REVIEW_TASKS]
    assert not any(hasattr(collection, name) for name in ("drop_index", "drop_indexes", "drop"))
    assert collection.indexes                                   # and the fake has no way to lose one


def test_a_conflicting_existing_index_is_a_typed_error_and_is_left_untouched():
    database = FakeDatabase("doc")
    database[REVIEW_TASKS].indexes["rt_tenant_task_uq"] = {"keys": [("tenant_id", 1), ("task_id", 1)], "unique": False, "partial": None,
                                                            "ttl": None}
    with pytest.raises(IndexConflictError) as caught:
        run(ensure_indexes(database))
    assert caught.value.details["reason"] == "index_conflict" and "SECRET" not in str(caught.value)
    assert database[REVIEW_TASKS].indexes["rt_tenant_task_uq"]["unique"] is False        # not rebuilt, not dropped


def test_a_database_failure_during_the_bootstrap_is_a_typed_error():
    database = FakeDatabase("doc")
    database[REVIEW_TASKS].fail_with = driver.NetworkTimeout("lost mongo-0 SECRET")
    with pytest.raises(PersistenceUnavailableError) as caught:
        run(ensure_indexes(database))
    assert "SECRET" not in str(caught.value) and "mongo-0" not in repr(caught.value.details)


def test_a_custom_registry_is_applied_in_declaration_order():
    registry = IndexRegistry([IndexSpec("zeta", (("tenant_id", 1), ("a", 1)), "z1"), IndexSpec("alpha", (("tenant_id", 1), ("a", 1)), "a1")])
    report = run(ensure_indexes(FakeDatabase("doc"), registry))
    assert report.created == ("zeta.z1", "alpha.a1")


def test_the_bootstrap_logs_counts_only():
    with LogCapture() as logs:
        run(ensure_indexes(FakeDatabase("doc")))
    assert f"created={len(DEFAULT_REGISTRY.all())} existing=0" in logs.text and "tenant_id" not in logs.text
