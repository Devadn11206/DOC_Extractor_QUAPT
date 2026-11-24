"""
MongoDB ApiKeyRepository tests (Team Member 3, Phase 2A-4).

MOCKED: the MongoDB client is the in-memory fake of ``tests/mongo_fakes.py`` (every call suspends its caller once and then runs
atomically, like a single-document server operation, and its unique indexes are enforced). These tests prove the adapter's
logic and the calls it sends to the driver. They do NOT prove behaviour of a real server: that is
``tests/test_mongo_integration.py`` against a real replica set, which skips when none is available.

1. CONTRACT tests, parametrised over BOTH the reference in-memory double (``MockApiKeyRepository`` of the service tests, the
   contract's executable form) and the MongoDB adapter, including the REAL ``ApiKeyService`` and
   ``ApiKeyAuthenticationProvider`` running on each.
2. MONGO tests: identity constraints, hash-only storage, the one tenant-exempt lookup, tenant scoping of every other call,
   revocation, last-used, concurrency, failure mapping, malformed records and composition.
"""

import asyncio
import hashlib
import inspect
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("pymongo", reason="the 'mongo' extra provides the driver whose error types these tests map")
from pymongo import errors as driver  # noqa: E402

import doc_extractor.infrastructure.mongo.api_keys as api_keys_module  # noqa: E402
from doc_extractor.api.app import create_app  # noqa: E402
from doc_extractor.api.composition import ApplicationContainer  # noqa: E402
from doc_extractor.api.config import ApiSettings  # noqa: E402
from doc_extractor.api.contracts.records import ApiKeyRecord, ApiKeySummary  # noqa: E402
from doc_extractor.api.exceptions import InvalidTokenError, TenantResourceNotFoundError, TokenExpiredError  # noqa: E402
from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType  # noqa: E402
from doc_extractor.api.ports.repositories import ApiKeyRepository  # noqa: E402
from doc_extractor.api.providers.api_key_auth import ApiKeyAuthenticationProvider  # noqa: E402
from doc_extractor.api.services.api_key_service import ApiKeyService  # noqa: E402
from doc_extractor.api.token_core import create_api_key_record, generate_api_secret, hash_api_token  # noqa: E402
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
from doc_extractor.infrastructure.mongo.api_keys import DuplicateApiKeyError, MongoApiKeyRepository  # noqa: E402
from tests.mongo_fakes import FakeClient, LogCapture, factory_for  # noqa: E402
from tests.test_api_key_service import MockApiKeyRepository, MockAuditLogger  # noqa: E402

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
KINDS = ["double", "mongo"]


def run(coro):
    return asyncio.run(coro)


def at(minutes, millis=0):
    """Whole-millisecond UTC instants: stored and read back unchanged, so both implementations compare equal."""
    return BASE + timedelta(minutes=minutes, milliseconds=millis)


def digest(seed):
    return hashlib.sha256(seed.encode()).hexdigest()


def key(key_id, *, tenant="tenant_a", seed=None, created=0, expires=None, scopes=("documents:read",), roles=("integrator",),
        active=True, revoked=None, last_used=None, name="a key"):
    return ApiKeyRecord(
        key_id=key_id, tenant_id=tenant, name=name, key_hash=digest(seed or f"{tenant}/{key_id}"), key_prefix="dex_",
        scopes=list(scopes), roles=list(roles), created_at=at(created), expires_at=expires, revoked_at=revoked,
        is_active=active, created_by="creator", last_used_at=last_used,
    )


class Mongo:
    """A connected adapter over the fake driver, with handles the Mongo-only tests need."""

    def __init__(self, repo, client, connection):
        self.repo, self.client, self.connection = repo, client, connection

    @property
    def collection(self):
        return self.client["doc"]["api_keys"]

    @property
    def sent(self):
        return self.collection.calls


async def build_mongo(keys=(), *, indexes=True, clock=None):
    client = FakeClient()
    connection = MongoConnection(MongoSettings(uri="mongodb://localhost", database="doc"), client_factory=factory_for(client))
    await connection.connect()
    if indexes:
        await ensure_indexes(client["doc"], DEFAULT_REGISTRY)
    repo = MongoApiKeyRepository(connection, **({"clock": clock} if clock else {}))
    for record in keys:
        await repo.create(record.tenant_id, record)
    client["doc"]["api_keys"].calls.clear()
    client["doc"]["api_keys"].session_calls.clear()
    return Mongo(repo, client, connection)


async def build(kind, keys=()):
    if kind == "double":
        repo = MockApiKeyRepository()
        for record in keys:
            await repo.create(record.tenant_id, record)
        return repo
    return (await build_mongo(keys)).repo


def contract(kind, keys, body):
    async def go():
        return await body(await build(kind, keys))

    return asyncio.run(go())


def with_mongo(keys, body, **kwargs):
    async def go():
        return await body(await build_mongo(keys, **kwargs))

    return asyncio.run(go())


# =============================================================================================================================
# 1. CONTRACT TESTS: the reference double and the MongoDB adapter must agree
# =============================================================================================================================

def test_the_adapter_implements_the_port_with_the_same_signatures():
    adapter = MongoApiKeyRepository(MongoConnection(MongoSettings(uri="mongodb://localhost", database="d")))
    assert isinstance(adapter, ApiKeyRepository)
    for name in ("get_by_key_hash", "get_by_id", "create", "revoke", "list_keys", "record_last_used"):
        port = inspect.signature(getattr(ApiKeyRepository, name))
        implemented = inspect.signature(getattr(MongoApiKeyRepository, name))
        assert list(port.parameters) == list(implemented.parameters), name
        assert [p.default for p in port.parameters.values()] == [p.default for p in implemented.parameters.values()], name
        assert inspect.iscoroutinefunction(getattr(MongoApiKeyRepository, name)), name


@pytest.mark.parametrize("kind", KINDS)
def test_create_then_every_lookup_returns_the_same_record(kind):
    record = key("k1", expires=at(500), last_used=None)

    async def body(repo):
        created = await repo.create("tenant_a", record)
        return created, await repo.get_by_id("tenant_a", "k1"), await repo.get_by_key_hash(record.key_hash)

    created, by_id, by_hash = contract(kind, [], body)
    assert created == by_id == by_hash == record
    assert by_id.expires_at == at(500) and by_id.expires_at.tzinfo is not None


@pytest.mark.parametrize("kind", KINDS)
def test_an_unknown_key_is_absent_for_every_lookup(kind):
    async def body(repo):
        return await repo.get_by_id("tenant_a", "nope"), await repo.get_by_key_hash(digest("unknown")), await repo.list_keys("tenant_z")

    by_id, by_hash, listed = contract(kind, [key("k1")], body)
    assert by_id is None and by_hash is None and listed == []


@pytest.mark.parametrize("kind", KINDS)
def test_the_hash_lookup_is_global_and_names_the_owning_tenant(kind):
    keys = [key("k1", tenant="tenant_a"), key("k1", tenant="tenant_b"), key("k2", tenant="tenant_c")]

    async def body(repo):
        return [await repo.get_by_key_hash(k.key_hash) for k in keys]

    found = contract(kind, keys, body)
    assert [(r.tenant_id, r.key_id) for r in found] == [("tenant_a", "k1"), ("tenant_b", "k1"), ("tenant_c", "k2")]


@pytest.mark.parametrize("kind", KINDS)
def test_another_tenants_key_is_absent_and_untouched_by_every_tenant_scoped_operation(kind):
    async def body(repo):
        results = (
            await repo.get_by_id("tenant_b", "k1"),
            await repo.list_keys("tenant_b"),
            await repo.revoke("tenant_b", "k1"),
            await repo.record_last_used("tenant_b", "k1", at(100)),
        )
        return results, await repo.get_by_id("tenant_a", "k1")

    (by_id, listed, revoked, used), original = contract(kind, [key("k1", tenant="tenant_a")], body)
    assert by_id is None and listed == [] and revoked is False and used is False
    assert original.is_active is True and original.revoked_at is None and original.last_used_at is None


@pytest.mark.parametrize("kind", KINDS)
def test_the_same_key_id_in_two_tenants_is_two_independent_keys(kind):
    async def body(repo):
        await repo.revoke("tenant_a", "same")
        return await repo.get_by_id("tenant_a", "same"), await repo.get_by_id("tenant_b", "same")

    mine, theirs = contract(kind, [key("same", tenant="tenant_a"), key("same", tenant="tenant_b")], body)
    assert mine.is_active is False and theirs.is_active is True and theirs.revoked_at is None


@pytest.mark.parametrize("kind", KINDS)
def test_revoke_deactivates_the_key_for_both_lookups_and_is_repeatable(kind):
    record = key("k1")

    async def body(repo):
        first = await repo.revoke("tenant_a", "k1")
        by_id = await repo.get_by_id("tenant_a", "k1")
        by_hash = await repo.get_by_key_hash(record.key_hash)
        again = await repo.revoke("tenant_a", "k1")
        return first, by_id, by_hash, again, await repo.revoke("tenant_a", "missing")

    first, by_id, by_hash, again, missing = contract(kind, [record], body)
    assert first is True and again is True and missing is False
    assert by_id == by_hash and by_id.is_active is False and by_id.revoked_at is not None
    assert by_id.model_copy(update={"is_active": True, "revoked_at": None}) == record      # nothing else changed


@pytest.mark.parametrize("kind", KINDS)
def test_a_revoked_key_stays_listed_and_inspectable(kind):
    async def body(repo):
        await repo.revoke("tenant_a", "k1")
        return await repo.list_keys("tenant_a"), await repo.get_by_id("tenant_a", "k1")

    listed, inspected = contract(kind, [key("k1"), key("k2", created=1)], body)
    assert [k.key_id for k in listed] == ["k1", "k2"] and inspected.is_active is False


@pytest.mark.parametrize("kind", KINDS)
def test_record_last_used_persists_for_an_active_key_and_reports_a_missing_one(kind):
    async def body(repo):
        stored = await repo.record_last_used("tenant_a", "k1", at(100))
        missing = await repo.record_last_used("tenant_a", "nope", at(100))
        return stored, missing, await repo.get_by_id("tenant_a", "k1"), await repo.get_by_key_hash(key("k1").key_hash)

    stored, missing, by_id, by_hash = contract(kind, [key("k1")], body)
    assert stored is True and missing is False and by_id.last_used_at == at(100) == by_hash.last_used_at


@pytest.mark.parametrize("kind", KINDS)
def test_list_keys_is_scoped_ordered_by_creation_and_paged(kind):
    keys = [key(f"k{n}", created=n) for n in (3, 1, 4, 2, 5)] + [key("other", tenant="tenant_b", created=0)]

    async def body(repo):
        # the double lists in insertion order: insert in creation order for it, shuffled for the adapter
        return await repo.list_keys("tenant_a"), await repo.list_keys("tenant_a", limit=2, offset=1), await repo.list_keys("tenant_a", limit=10, offset=9)

    ordered = sorted(keys, key=lambda k: (k.created_at, k.key_id))
    listed, page, beyond = contract(kind, ordered if kind == "double" else keys, body)
    assert [k.key_id for k in listed] == ["k1", "k2", "k3", "k4", "k5"]
    assert [k.key_id for k in page] == ["k2", "k3"] and beyond == []


# ---- the real service and authentication provider on each implementation ------------------------------------------------

def _creator(tenant="tenant_alpha"):
    return ApiPrincipal(principal_id="user_admin", tenant_id=tenant, principal_type=PrincipalType.USER, roles={"admin"},
                        scopes={"api_keys:read", "api_keys:write", "documents:read", "documents:write"})


@pytest.mark.parametrize("kind", KINDS)
def test_the_service_and_the_authentication_provider_work_on_the_implementation(kind):
    async def body(repo):
        audit = MockAuditLogger()
        service = ApiKeyService(repository=repo, audit_logger=audit)
        provider = ApiKeyAuthenticationProvider(repository=repo, audit_logger=audit)
        admin, foreign = _creator("tenant_alpha"), _creator("tenant_beta")
        created = await service.create_api_key(admin, "ci key", ["documents:read"], ["integrator"], expires_in_days=30)

        principal = await provider.authenticate(f"Bearer {created.raw_secret}")
        used = await repo.get_by_id("tenant_alpha", created.key_id)
        listing = await service.list_api_keys(admin)
        summary = await service.get_api_key(admin, created.key_id)
        with pytest.raises(TenantResourceNotFoundError):
            await service.get_api_key(foreign, created.key_id)
        with pytest.raises(TenantResourceNotFoundError):
            await service.revoke_api_key(foreign, created.key_id)
        assert (await service.revoke_api_key(admin, created.key_id)) is True
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(f"Bearer {created.raw_secret}")
        return created, principal, used, listing, summary

    created, principal, used, listing, summary = contract(kind, [], body)
    assert principal.tenant_id == "tenant_alpha" and principal.principal_id == created.key_id and principal.is_active
    assert principal.scopes == {"documents:read"} and used.last_used_at is not None
    assert [s.key_id for s in listing] == [created.key_id] and isinstance(listing[0], ApiKeySummary)
    dumped = summary.model_dump_json() + listing[0].model_dump_json()
    assert created.raw_secret not in dumped and used.key_hash not in dumped and "key_hash" not in dumped


@pytest.mark.parametrize("kind", KINDS)
def test_an_expired_key_is_returned_unchanged_and_the_provider_refuses_it(kind):
    secret = generate_api_secret(nbytes=32)
    expired = ApiKeyRecord(key_id=secret.key_id, tenant_id="tenant_alpha", name="old", key_hash=secret.key_hash,
                           key_prefix=secret.key_prefix, scopes=["documents:read"], roles=["integrator"],
                           created_at=at(0), expires_at=datetime.now(timezone.utc) - timedelta(days=1))

    async def body(repo):
        provider = ApiKeyAuthenticationProvider(repository=repo, audit_logger=MockAuditLogger())
        stored = await repo.get_by_key_hash(secret.key_hash)
        with pytest.raises(TokenExpiredError):
            await provider.authenticate(secret.raw_secret)
        return stored, await repo.get_by_id("tenant_alpha", secret.key_id)

    stored, after = contract(kind, [expired], body)
    assert abs(stored.expires_at - expired.expires_at) < timedelta(milliseconds=1)       # stored at millisecond precision
    assert after.last_used_at is None                                         # a refused credential is never recorded as used


# =============================================================================================================================
# 2. MONGO TESTS
# =============================================================================================================================

# ---- identity constraints --------------------------------------------------------------------------------------------

def test_a_duplicate_tenant_and_key_id_is_refused_with_one_typed_error_that_leaks_nothing():
    async def body(mongo):
        with pytest.raises(DuplicateApiKeyError) as caught:
            await mongo.repo.create("tenant_a", key("k1", seed="a different credential"))
        return caught.value, await mongo.repo.list_keys("tenant_a")

    error, listed = with_mongo([key("k1")], body)
    assert [k.key_id for k in listed] == ["k1"] and isinstance(error, (DuplicateKeyPersistenceError, ValueError, PersistenceError))
    assert error.details["reason"] == "duplicate_key" and digest("tenant_a/k1") not in str(error) + repr(error.details)


def test_a_duplicate_credential_hash_is_refused_across_tenants_with_the_same_error():
    async def body(mongo):
        with pytest.raises(DuplicateApiKeyError) as same_tenant:
            await mongo.repo.create("tenant_a", key("other", seed="shared"))
        with pytest.raises(DuplicateApiKeyError) as cross_tenant:
            await mongo.repo.create("tenant_b", key("other", tenant="tenant_b", seed="shared"))
        return same_tenant.value, cross_tenant.value

    first, second = with_mongo([key("k1", seed="shared")], body)
    # no oracle: a hash that exists in ANOTHER tenant is indistinguishable from a duplicate key identifier
    assert (type(first), first.details, str(first)) == (type(second), second.details, str(second))


def test_the_same_key_id_in_another_tenant_is_allowed_and_nothing_else_is_unique():
    async def body(mongo):
        await mongo.repo.create("tenant_b", key("k1", tenant="tenant_b"))                           # same key_id, other tenant
        await mongo.repo.create("tenant_a", key("k2", name="a key", scopes=["documents:read"]))     # same name and scopes
        await mongo.repo.create("tenant_a", key("k3", name="a key", scopes=["documents:read"]))
        return await mongo.repo.list_keys("tenant_a"), await mongo.repo.list_keys("tenant_b")

    a, b = with_mongo([key("k1")], body)
    assert [k.key_id for k in a] == ["k1", "k2", "k3"] and [k.key_id for k in b] == ["k1"]


def test_only_the_two_declared_identity_indexes_exist_and_none_was_added():
    from doc_extractor.infrastructure.mongo.indexes import API_KEYS

    specs = DEFAULT_REGISTRY.for_collection(API_KEYS)
    assert {s.name: (s.unique, s.tenant_exempt) for s in specs} == {"ak_key_hash_uq": (True, True), "ak_tenant_key_uq": (True, False)}


def test_concurrent_creates_of_one_key_have_exactly_one_winner():
    async def body(mongo):
        same_id = [mongo.repo.create("tenant_a", key("dup", seed=f"s{n}")) for n in range(15)]
        same_hash = [mongo.repo.create(f"tenant_{n}", key(f"id{n}", tenant=f"tenant_{n}", seed="one-hash")) for n in range(15)]
        return await asyncio.gather(*same_id, *same_hash, return_exceptions=True)

    results = with_mongo([], body)
    first, second = results[:15], results[15:]
    assert sum(isinstance(r, ApiKeyRecord) for r in first) == 1 and sum(isinstance(r, ApiKeyRecord) for r in second) == 1
    assert all(isinstance(r, DuplicateApiKeyError) for r in first + second if not isinstance(r, ApiKeyRecord))


def test_create_refuses_a_foreign_tenant_and_a_non_record():
    async def body(mongo):
        with pytest.raises(TenantScopeError):
            await mongo.repo.create("tenant_a", key("k1", tenant="tenant_b"))
        for bad in (None, {"key_id": "x"}, "text"):
            with pytest.raises(UnsafeQueryError):
                await mongo.repo.create("tenant_a", bad)
        return mongo.sent, mongo.collection.docs

    sent, docs = with_mongo([], body)
    assert sent == [] and docs == []


# ---- hash-only storage and redaction ---------------------------------------------------------------------------------

def test_only_the_sha256_digest_is_stored_and_no_raw_credential_reaches_the_driver_or_the_logs():
    async def body(mongo):
        service = ApiKeyService(repository=mongo.repo, audit_logger=MockAuditLogger())
        with LogCapture() as logs, LogCapture("doc_extractor") as app_logs:
            created = await service.create_api_key(_creator(), "ci", ["documents:read"], ["integrator"], expires_in_days=10)
            provider = ApiKeyAuthenticationProvider(repository=mongo.repo, audit_logger=MockAuditLogger())
            await provider.authenticate(created.raw_secret)
            await mongo.repo.revoke("tenant_alpha", created.key_id)
        return created, mongo.collection.docs, mongo.collection.calls, logs.text + app_logs.text

    created, docs, calls, log_text = with_mongo([], body)
    everything = repr(docs) + repr(calls) + log_text
    assert created.raw_secret not in everything and created.raw_secret.split("_")[-1] not in everything
    assert docs[0]["key_hash"] == hash_api_token(created.raw_secret) and len(docs[0]["key_hash"]) == 64
    assert not any(isinstance(v, str) and v == created.raw_secret for v in docs[0].values())
    assert set(docs[0]) == {"key_id", "tenant_id", "name", "key_hash", "key_prefix", "scopes", "roles", "created_at", "expires_at",
                            "revoked_at", "is_active", "created_by", "last_used_at"}


@pytest.mark.parametrize("bad_hash", ["A" * 64, "g" * 64, "dex_live_" + "x" * 55, " " * 64, ("a" * 63) + "-"])
def test_a_key_hash_that_is_not_a_lowercase_sha256_digest_is_refused_before_the_driver(bad_hash):
    """A raw token can be 64 characters too: the length check of the record alone cannot tell a secret from a digest."""
    async def body(mongo):
        record = ApiKeyRecord(key_id="k1", tenant_id="tenant_a", name="n", key_hash=bad_hash, key_prefix="dex_")
        with pytest.raises(PersistenceDataError) as caught:
            await mongo.repo.create("tenant_a", record)
        return caught.value, mongo.sent

    error, sent = with_mongo([], body)
    assert sent == [] and error.details["reason"] == "key_hash_is_not_a_sha256_digest" and (not bad_hash.strip() or bad_hash not in str(error) + repr(error.details))


def test_summaries_built_from_stored_records_never_carry_the_hash():
    async def body(mongo):
        return [ApiKeySummary.from_record(r) for r in await mongo.repo.list_keys("tenant_a")]

    summaries = with_mongo([key("k1"), key("k2", created=1)], body)
    text = "".join(s.model_dump_json() for s in summaries) + repr(summaries)
    assert digest("tenant_a/k1") not in text and digest("tenant_a/k2") not in text and "key_hash" not in text


# ---- the tenant-exempt lookup and the tenant scope of everything else ----------------------------------------------------

def test_the_hash_lookup_is_the_only_call_without_the_tenant_and_it_is_a_single_equality():
    keys = [key("k1"), key("k2", tenant="tenant_b")]

    async def body(mongo):
        repo = mongo.repo
        await repo.get_by_key_hash(keys[1].key_hash)
        hash_calls = list(mongo.sent)
        mongo.sent.clear()
        await repo.get_by_id("tenant_a", "k1")
        await repo.list_keys("tenant_a")
        await repo.revoke("tenant_a", "k1")
        await repo.record_last_used("tenant_a", "k1", at(5))
        await repo.create("tenant_a", key("k9"))
        return hash_calls, list(mongo.sent)

    hash_calls, scoped_calls = with_mongo(keys, body)
    assert hash_calls == [("find_one", {"key_hash": keys[1].key_hash})]
    assert {c[0] for c in scoped_calls} >= {"find_one", "find", "update_one", "insert_one"}
    for operation, flt, *_ in scoped_calls:
        assert flt["tenant_id"] == "tenant_a", (operation, flt)


def test_the_owning_tenant_comes_from_the_stored_record_not_from_the_caller():
    async def body(mongo):
        found = await mongo.repo.get_by_key_hash(key("k1", tenant="tenant_b").key_hash)
        return found, mongo.sent

    found, sent = with_mongo([key("k1", tenant="tenant_a"), key("k1", tenant="tenant_b")], body)
    assert found.tenant_id == "tenant_b" and "tenant_id" not in sent[0][1]


@pytest.mark.parametrize("bad", [{"$ne": ""}, {"$regex": ".*"}, ["a" * 64], None, 5, b"x", "", "a" * 300])
def test_the_hash_lookup_cannot_be_used_for_an_operator_or_a_scan(bad):
    async def body(mongo):
        with pytest.raises((UnsafeQueryError, TenantScopeError)):
            await mongo.repo.get_by_key_hash(bad)
        return mongo.sent

    assert with_mongo([key("k1")], body) == []


def test_a_malformed_or_unknown_hash_finds_nothing():
    async def body(mongo):
        return [await mongo.repo.get_by_key_hash(h) for h in ("x", "A" * 64, digest("nobody"), "' || '1'=='1")]

    assert with_mongo([key("k1")], body) == [None] * 4


def test_the_tenant_cannot_be_chosen_through_any_argument():
    async def body(mongo):
        repo = mongo.repo
        for call in (repo.get_by_id({"$ne": None}, "k1"), repo.list_keys(None), repo.revoke("", "k1"),
                     repo.record_last_used("a" * 300, "k1", at(1)), repo.create({"$gt": ""}, key("k9"))):
            with pytest.raises((TenantScopeError, UnsafeQueryError)):
                await call
        return mongo.sent

    assert with_mongo([key("k1")], body) == []


@pytest.mark.parametrize("bad", [{"$ne": "x"}, None, 5, ["a"]])
def test_operator_injection_through_a_key_id_is_refused_before_the_driver(bad):
    async def body(mongo):
        repo = mongo.repo
        for call in (repo.get_by_id("tenant_a", bad), repo.revoke("tenant_a", bad), repo.record_last_used("tenant_a", bad, at(1))):
            with pytest.raises(UnsafeQueryError):
                await call
        return mongo.sent

    assert with_mongo([key("k1")], body) == []


# ---- revocation ----------------------------------------------------------------------------------------------------------

def test_revoke_is_two_conditional_updates_scoped_to_the_tenant():
    async def body(mongo):
        await mongo.repo.revoke("tenant_a", "k1")
        first = list(mongo.sent)
        mongo.sent.clear()
        await mongo.repo.revoke("tenant_a", "k1")
        return first, list(mongo.sent)

    first, second = with_mongo([key("k1")], body, clock=lambda: at(7))
    assert first == [("update_one", {"key_id": "k1", "revoked_at": None, "tenant_id": "tenant_a"},
                      {"$set": {"revoked_at": at(7), "is_active": False}})]
    assert second[0][1] == {"key_id": "k1", "revoked_at": None, "tenant_id": "tenant_a"}
    assert second[1] == ("update_one", {"key_id": "k1", "tenant_id": "tenant_a"}, {"$set": {"is_active": False}})


def test_a_repeated_revocation_keeps_the_time_of_the_first_one():
    times = iter([at(10), at(20), at(30)])

    async def body(mongo):
        await mongo.repo.revoke("tenant_a", "k1")
        first = (await mongo.repo.get_by_id("tenant_a", "k1")).revoked_at
        assert await mongo.repo.revoke("tenant_a", "k1") is True
        return first, (await mongo.repo.get_by_id("tenant_a", "k1")).revoked_at

    first, second = with_mongo([key("k1")], body, clock=lambda: next(times))
    assert first == second == at(10)


def test_revoke_repairs_an_inconsistent_key_and_never_leaves_it_active():
    async def body(mongo):
        mongo.collection.docs[0].update(revoked_at=at(3), is_active=True)          # revoked time set, still flagged active
        mongo.collection.docs[1].update(revoked_at=None, is_active=False)          # deactivated without a revocation time
        results = [await mongo.repo.revoke("tenant_a", "k1"), await mongo.repo.revoke("tenant_a", "k2")]
        return results, [await mongo.repo.get_by_id("tenant_a", n) for n in ("k1", "k2")]

    results, (k1, k2) = with_mongo([key("k1"), key("k2")], body, clock=lambda: at(9))
    assert results == [True, True] and k1.is_active is False and k1.revoked_at == at(3)
    assert k2.is_active is False and k2.revoked_at == at(9)


def test_a_revoked_key_is_still_returned_by_the_hash_lookup_for_the_provider_to_refuse():
    async def body(mongo):
        await mongo.repo.revoke("tenant_a", "k1")
        provider = ApiKeyAuthenticationProvider(repository=mongo.repo, audit_logger=MockAuditLogger())
        found = await mongo.repo.get_by_key_hash(secret.key_hash)
        with pytest.raises(InvalidTokenError):
            await provider.authenticate(secret.raw_secret)
        return found

    secret = generate_api_secret(nbytes=32)
    record = ApiKeyRecord(key_id="k1", tenant_id="tenant_a", name="n", key_hash=secret.key_hash, key_prefix=secret.key_prefix)
    found = with_mongo([record], body)
    assert found.is_active is False and found.revoked_at is not None


def test_concurrent_revocations_and_uses_never_leave_the_key_active():
    async def body(mongo):
        calls = [mongo.repo.revoke("tenant_a", "k1") for _ in range(10)]
        calls += [mongo.repo.record_last_used("tenant_a", "k1", at(100 + n)) for n in range(10)]
        results = await asyncio.gather(*calls)
        return results[:10], await mongo.repo.get_by_id("tenant_a", "k1")

    revoked, final = with_mongo([key("k1")], body, clock=lambda: at(50))
    assert revoked == [True] * 10 and final.is_active is False and final.revoked_at == at(50)


# ---- last used -----------------------------------------------------------------------------------------------------------

def test_last_used_is_recorded_only_for_an_active_key():
    async def body(mongo):
        results = [await mongo.repo.record_last_used("tenant_a", n, at(100)) for n in ("active", "revoked", "inactive", "gone")]
        return results, [await mongo.repo.get_by_id("tenant_a", n) for n in ("active", "revoked", "inactive")]

    keys = [key("active"), key("revoked", active=False, revoked=at(1)), key("inactive", active=False)]
    results, (active, revoked, inactive) = with_mongo(keys, body)
    assert results == [True, False, False, False]
    assert active.last_used_at == at(100) and revoked.last_used_at is None and inactive.last_used_at is None


def test_last_used_never_moves_backwards():
    async def body(mongo):
        results = [await mongo.repo.record_last_used("tenant_a", "k1", at(m)) for m in (50, 20, 50, 80, 10)]
        return results, (await mongo.repo.get_by_id("tenant_a", "k1")).last_used_at

    results, final = with_mongo([key("k1")], body)
    assert results == [True] * 5 and final == at(80)


def test_out_of_order_concurrent_updates_end_at_the_latest_time():
    async def body(mongo):
        order = [37, 5, 91, 12, 64, 3, 88, 21, 59, 40, 17, 75, 8, 99, 31, 52, 70, 26, 83, 45]
        results = await asyncio.gather(*(mongo.repo.record_last_used("tenant_a", "k1", at(m)) for m in order))
        return results, (await mongo.repo.get_by_id("tenant_a", "k1")).last_used_at

    results, final = with_mongo([key("k1")], body)
    assert all(results) and final == at(99)


def test_last_used_truncates_to_the_millisecond_like_every_stored_time():
    async def body(mongo):
        await mongo.repo.record_last_used("tenant_a", "k1", datetime(2026, 3, 1, 0, 0, 0, 999999, tzinfo=timezone.utc))
        stale = await mongo.repo.record_last_used("tenant_a", "k1", datetime(2026, 3, 1, 0, 0, 0, 999100, tzinfo=timezone.utc))
        return stale, (await mongo.repo.get_by_id("tenant_a", "k1")).last_used_at

    stale, stored = with_mongo([key("k1")], body)
    assert stale is True and stored == datetime(2026, 3, 1, 0, 0, 0, 999000, tzinfo=timezone.utc)


def test_last_used_rejects_a_non_datetime_before_the_driver():
    async def body(mongo):
        for bad in ("2026-01-01", 5, None, {"$date": 1}):
            with pytest.raises(UnsafeQueryError):
                await mongo.repo.record_last_used("tenant_a", "k1", bad)
        return mongo.sent

    assert with_mongo([key("k1")], body) == []


# ---- listing -------------------------------------------------------------------------------------------------------------

def test_lists_larger_than_the_foundations_bound_are_fetched_in_chunks_and_zero_is_empty():
    keys = [key(f"k{n:04d}", created=n % 40) for n in range(1250)]

    async def body(mongo):
        everything = await mongo.repo.list_keys("tenant_a", limit=3000, offset=100)
        return everything, await mongo.repo.list_keys("tenant_a", limit=0), [c for c in mongo.sent if c[0] == "find"]

    listed, nothing, finds = with_mongo(keys, body)
    assert len(listed) == 1150 and nothing == [] and len({k.key_id for k in listed}) == 1150 and len(finds) >= 2


def test_negative_paging_and_an_invalid_tenant_are_refused_even_for_a_zero_limit():
    async def body(mongo):
        for kwargs in ({"limit": -1}, {"offset": -1}):
            with pytest.raises(UnsafeQueryError):
                await mongo.repo.list_keys("tenant_a", **kwargs)
        with pytest.raises(TenantScopeError):
            await mongo.repo.list_keys("", limit=0)
        return mongo.sent

    assert with_mongo([key("k1")], body) == []


# ---- failure handling ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("operation", ["hash", "get", "list", "create", "revoke", "used"])
def test_a_driver_failure_is_a_typed_persistence_error_that_leaks_nothing(operation):
    calls = {
        "hash": lambda r: r.get_by_key_hash(digest("x")), "get": lambda r: r.get_by_id("tenant_a", "k1"),
        "list": lambda r: r.list_keys("tenant_a"), "create": lambda r: r.create("tenant_a", key("k9")),
        "revoke": lambda r: r.revoke("tenant_a", "k1"), "used": lambda r: r.record_last_used("tenant_a", "k1", at(1)),
    }

    async def body(mongo):
        mongo.collection.fail_with = driver.NetworkTimeout(f"lost host mongo-0 SECRET {digest('tenant_a/k1')}")
        with LogCapture() as logs:
            with pytest.raises(PersistenceUnavailableError) as caught:
                await calls[operation](mongo.repo)
        return caught.value, logs.text

    error, logs = with_mongo([key("k1")], body)
    text = str(error) + repr(error) + repr(error.details) + logs
    assert "SECRET" not in text and "mongo-0" not in text and digest("tenant_a/k1") not in text
    assert error.__cause__ is None and error.details["collection"] == "api_keys"


def test_a_driver_duplicate_key_error_is_mapped_without_the_driver_text():
    async def body(mongo):
        mongo.collection.fail_with = driver.DuplicateKeyError(f"E11000 dup key: {{ key_hash: \"{digest('tenant_a/k9')}\" }}", 11000)
        with pytest.raises(DuplicateApiKeyError) as caught:
            await mongo.repo.create("tenant_a", key("k9"))
        return caught.value

    error = with_mongo([], body, indexes=False)
    assert digest("tenant_a/k9") not in str(error) + repr(error.details) and error.details["reason"] == "duplicate_key"


def test_a_failed_revoke_changes_nothing_and_the_retry_succeeds():
    async def body(mongo):
        mongo.collection.fail_with = driver.AutoReconnect("down")
        with pytest.raises(PersistenceUnavailableError):
            await mongo.repo.revoke("tenant_a", "k1")
        mongo.collection.fail_with = None
        before = await mongo.repo.get_by_id("tenant_a", "k1")
        return before, await mongo.repo.revoke("tenant_a", "k1"), await mongo.repo.get_by_id("tenant_a", "k1")

    before, ok, after = with_mongo([key("k1")], body)
    assert before.is_active is True and ok is True and after.is_active is False


def test_a_disconnected_repository_raises_a_typed_error():
    async def body(mongo):
        await mongo.connection.aclose()
        for call in (mongo.repo.get_by_key_hash(digest("x")), mongo.repo.get_by_id("tenant_a", "k1")):
            with pytest.raises(PersistenceUnavailableError) as caught:
                await call
            assert caught.value.details["reason"] == "not_connected"

    with_mongo([key("k1")], body)


MARKER = digest("marker-that-must-not-leak")


@pytest.mark.parametrize("stored", [
    {"extra_field": 1},                                   # the record forbids unknown fields
    {"key_hash": "short"},                                # not a 64-character hash
    {"key_prefix": "x"},                                  # prefix shorter than 4
    {"scopes": "documents:read"},                         # a string where a list belongs
    {"is_active": "yes please"},
    {"created_at": "yesterday"},
    {"name": None},
    {"key_id": ""},
])
def test_a_malformed_stored_record_is_a_typed_data_error_never_returned_or_skipped(stored):
    async def body(mongo):
        mongo.collection.docs[0].update(stored)
        mongo.collection.docs[0]["key_hash"] = stored.get("key_hash", MARKER)
        caught = []
        for call in (mongo.repo.get_by_id("tenant_a", mongo.collection.docs[0]["key_id"] or ""),
                     mongo.repo.get_by_key_hash(mongo.collection.docs[0]["key_hash"]), mongo.repo.list_keys("tenant_a")):
            try:
                await call
            except PersistenceDataError as error:
                caught.append(error)
        return caught

    caught = with_mongo([key("k1"), key("k2", created=1)], body)
    assert len(caught) >= 2 and all(e.details["reason"] == "stored_document_does_not_match_the_model" for e in caught)
    assert all(MARKER not in str(e) + repr(e.details) for e in caught)


def test_a_malformed_record_found_by_hash_fails_authentication_closed_not_open():
    secret = generate_api_secret(nbytes=32)

    async def body(mongo):
        mongo.collection.docs[0]["scopes"] = "documents:read"                   # corrupt after the valid create
        provider = ApiKeyAuthenticationProvider(repository=mongo.repo, audit_logger=MockAuditLogger())
        with pytest.raises(PersistenceDataError):
            await provider.authenticate(secret.raw_secret)

    record = ApiKeyRecord(key_id="k1", tenant_id="tenant_a", name="n", key_hash=secret.key_hash, key_prefix=secret.key_prefix)
    with_mongo([record], body)


def test_a_malformed_revoke_target_does_not_change_the_other_records():
    async def body(mongo):
        mongo.collection.docs[0]["scopes"] = 5
        await mongo.repo.revoke("tenant_a", "k2")
        return [dict(d) for d in mongo.collection.docs]

    docs = with_mongo([key("k1"), key("k2", created=1)], body)
    assert docs[0]["is_active"] is True and docs[1]["is_active"] is False


# ---- composition and the application ---------------------------------------------------------------------------------------

URI = "mongodb://app:ApiKey-Secret-Marker-1@mongo-0:27017/?replicaSet=rs0"


def container_with(client=None, **kwargs):
    client = client or FakeClient()
    settings = ApiSettings(app_env="development", mongo_uri=URI, mongo_database="doc")
    connection = MongoConnection(MongoSettings.from_api_settings(settings), client_factory=factory_for(client))
    return ApplicationContainer(api_settings=settings, mongo_connection=connection, **kwargs), client


def test_start_up_configures_the_mongo_api_key_repository_after_a_successful_connection():
    container, client = container_with()
    assert container.api_key_repo is None
    run(container.startup())
    assert isinstance(container.api_key_repo, MongoApiKeyRepository) and isinstance(container.api_key_repo, ApiKeyRepository)
    run(container.startup())
    assert isinstance(container.api_key_repo, MongoApiKeyRepository)


def test_an_injected_api_key_repository_always_wins():
    sentinel = MockApiKeyRepository()
    container, _ = container_with(api_key_repo=sentinel)
    run(container.startup())
    assert container.api_key_repo is sentinel


def test_without_mongodb_the_api_key_port_stays_unconfigured():
    container = ApplicationContainer(api_settings=ApiSettings(app_env="development"))
    run(container.startup())
    assert container.api_key_repo is None


def test_a_failed_start_up_leaves_no_partially_configured_repository():
    unreachable = FakeClient(fail_connect=driver.NetworkTimeout("down"))
    container, _ = container_with(unreachable)
    with pytest.raises(PersistenceUnavailableError):
        run(container.startup())
    assert container.api_key_repo is None and container.review_task_repo is None and unreachable.closed is True

    client = FakeClient()
    container, _ = container_with(client)
    client["doc"]["api_keys"].fail_with = driver.OperationFailure("boom", code=85)       # the index bootstrap fails
    with pytest.raises(PersistenceError):
        run(container.startup())
    assert container.api_key_repo is None and container.review_task_repo is None and client.closed is True


def _app_with_keys(keys, *, audit=True, state_repo=None):
    from doc_extractor.infrastructure.mongo import to_document
    from doc_extractor.malware import MockMalwareScanner

    client = FakeClient()
    settings = ApiSettings(app_env="development", mongo_uri=URI, mongo_database="doc")
    connection = MongoConnection(MongoSettings.from_api_settings(settings), client_factory=factory_for(client))
    container = ApplicationContainer(api_settings=settings, mongo_connection=connection, malware_scanner=MockMalwareScanner(),
                                     audit_logger=MockAuditLogger() if audit else None)
    app = create_app(settings, container)
    if state_repo is not None:
        app.state.api_key_repo = state_repo
    client["doc"]["api_keys"].docs.extend(to_document(k.model_dump()) for k in keys)
    return app, client, container


def test_the_lifespan_publishes_the_started_repository_to_the_application_state():
    from fastapi.testclient import TestClient

    app, client, container = _app_with_keys([])
    assert app.state.api_key_repo is None                                            # copied before start-up configured it
    with TestClient(app):
        assert app.state.api_key_repo is container.api_key_repo and isinstance(app.state.api_key_repo, MongoApiKeyRepository)


def test_a_repository_set_on_the_state_explicitly_is_not_replaced():
    from fastapi.testclient import TestClient

    explicit = MockApiKeyRepository()
    app, _, container = _app_with_keys([], state_repo=explicit)
    with TestClient(app):
        assert app.state.api_key_repo is explicit and isinstance(container.api_key_repo, MongoApiKeyRepository)


def _http_key(scopes=("review_tasks:read",), **overrides):
    secret = generate_api_secret(nbytes=32)
    record = ApiKeyRecord(key_id=secret.key_id, tenant_id="tenant_alpha", name="http key", key_hash=secret.key_hash,
                          key_prefix=secret.key_prefix, scopes=list(scopes), roles=["integrator"], **overrides)
    return secret, record


def test_an_api_key_authenticates_a_real_request_through_the_mongo_adapter_and_revocation_takes_effect():
    from fastapi.testclient import TestClient

    secret, record = _http_key()
    other_secret, other = _http_key(scopes=("documents:read",))                      # a key without the review scope
    expired_secret, expired = _http_key(expires_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    app, client, container = _app_with_keys([record, other, expired])
    url = "/api/v1/review-tasks"
    bearer = lambda s: {"Authorization": f"Bearer {s.raw_secret}"}                   # noqa: E731
    with TestClient(app, raise_server_exceptions=False) as http:
        assert http.get(url, headers=bearer(secret)).status_code == 200
        assert http.get(url, headers=bearer(other_secret)).status_code == 403          # authenticated, lacks the scope
        assert http.get(url, headers=bearer(expired_secret)).status_code == 401
        assert http.get(url, headers={"Authorization": "Bearer dex_live_" + "x" * 60}).status_code == 401
        assert http.get(url).status_code == 401
        stored = {d["key_id"]: d for d in client["doc"]["api_keys"].docs}
        assert stored[record.key_id]["last_used_at"] is not None                       # recorded by the request
        assert stored[expired.key_id]["last_used_at"] is None                          # a refused credential is not "used"
        client["doc"]["api_keys"].docs[0].update(is_active=False, revoked_at=datetime.now(timezone.utc))
        assert http.get(url, headers=bearer(secret)).status_code == 401                # revocation is immediate


def test_with_mongodb_configured_the_durable_audit_logger_is_wired_so_api_key_authentication_works():
    """Phase 2B: start-up supplies the audit logger the provider requires, so no separate audit configuration is needed."""
    from fastapi.testclient import TestClient

    secret, record = _http_key()
    app, _, container = _app_with_keys([record], audit=False)
    with TestClient(app, raise_server_exceptions=False) as http:
        assert container.audit_logger is not None and app.state.audit_logger is container.audit_logger
        assert http.get("/api/v1/review-tasks", headers={"Authorization": f"Bearer {secret.raw_secret}"}).status_code == 200


def test_without_an_audit_logger_the_provider_fails_closed_with_a_typed_503_not_a_default():
    """No MongoDB, an injected key repository and no audit logger anywhere: authentication answers 503, never a default sink."""
    from fastapi.testclient import TestClient

    from doc_extractor.malware import MockMalwareScanner

    secret, record = _http_key()
    keys = MockApiKeyRepository()
    run(keys.create(record.tenant_id, record))
    container = ApplicationContainer(api_settings=ApiSettings(app_env="development"), malware_scanner=MockMalwareScanner(), api_key_repo=keys)
    app = create_app(container.api_settings, container)
    with TestClient(app, raise_server_exceptions=False) as http:
        assert container.audit_logger is None
        assert http.get("/api/v1/review-tasks", headers={"Authorization": f"Bearer {secret.raw_secret}"}).status_code == 503


def test_the_adapter_names_no_in_memory_substitute_and_logs_nothing():
    source = api_keys_module.__doc__ + inspect.getsource(api_keys_module)
    for forbidden in ("InMemory", "mongomock", "review_task_double", "fallback_to", "logger", "logging"):
        assert forbidden not in source
