"""
MongoDB connection lifecycle and replica-set tests (Team Member 3, Phase 2A-1).

MOCKED: these tests drive ``MongoConnection`` with the in-memory fake client of ``tests/mongo_fakes.py``. They verify the
foundation's own behaviour (options, lifecycle, checks, error mapping, health, transactions). They do not prove anything about
a real server; that needs ``tests/test_mongo_integration.py`` against a real replica set.
"""

import asyncio
import inspect

import pytest

pytest.importorskip("pymongo", reason="the 'mongo' extra provides the driver whose error types these tests map")
from bson import errors as bson_errors  # noqa: E402
from pymongo import errors as driver  # noqa: E402

from doc_extractor.infrastructure.mongo import (  # noqa: E402
    MongoConnection,
    DuplicateKeyPersistenceError,
    MongoSettings,
    PersistenceConfigurationError,
    PersistenceDataError,
    PersistenceError,
    PersistenceUnavailableError,
    TenantScopeError,
    TransactionUnavailableError,
    build_client_options,
)
from tests.mongo_fakes import FakeClient, LogCapture, factory_for  # noqa: E402

PASSWORD = "Conn-Secret-Marker-7781"
URI = f"mongodb://app:{PASSWORD}@mongo-0:27017,mongo-1:27017/?replicaSet=rs0"


def settings(**overrides):
    values = {"uri": URI, "database": "doc", "app_env": "development"}
    values.update(overrides)
    return MongoSettings(**values)


def run(coro):
    return asyncio.run(coro)


def connection(client=None, **overrides):
    client = client or FakeClient()
    return MongoConnection(settings(**overrides), client_factory=factory_for(client)), client


# ---- options ------------------------------------------------------------------------------------------------------------

def test_the_client_options_impose_the_approved_defaults_over_the_string():
    options = build_client_options(settings(replica_set="rs0"))
    assert options["w"] == "majority" and options["journal"] is True
    assert options["readConcernLevel"] == "majority" and options["readPreference"] == "primary"
    assert options["retryWrites"] is True and options["retryReads"] is True
    assert options["tz_aware"] is True and options["uuidRepresentation"] == "standard"
    assert options["replicaSet"] == "rs0" and options["appname"] == "doc-extractor-api"


def test_the_client_options_carry_every_bounded_timeout_and_pool_size():
    options = build_client_options(settings(server_selection_timeout_ms=2000, connect_timeout_ms=2100, socket_timeout_ms=9000,
                                            max_pool_size=7, min_pool_size=1))
    assert options["serverSelectionTimeoutMS"] == 2000 and options["waitQueueTimeoutMS"] == 2000
    assert (options["connectTimeoutMS"], options["socketTimeoutMS"], options["maxPoolSize"], options["minPoolSize"]) == (2100, 9000, 7, 1)


def test_the_replica_set_option_is_omitted_when_none_is_configured():
    assert "replicaSet" not in build_client_options(settings(uri="mongodb://localhost"))


def test_construction_opens_nothing_and_needs_no_driver():
    created = []
    conn = MongoConnection(settings(), client_factory=lambda uri, **o: created.append(uri))
    assert created == [] and conn.is_connected is False and conn.replica_set_name is None and conn.supports_transactions is False


# ---- connect / close ----------------------------------------------------------------------------------------------------

def test_connect_pings_verifies_the_replica_set_and_exposes_the_database():
    conn, client = connection()
    run(conn.connect())
    assert client.commands == ["ping", "hello"] and conn.is_connected and conn.replica_set_name == "rs0"
    assert conn.supports_transactions is True and conn.database.name == "doc" and conn.collection("x").name == "x"
    assert client.uri == URI                       # the real string reaches the driver, and only the driver


def test_connect_is_idempotent():
    conn, client = connection()
    run(conn.connect())
    run(conn.connect())
    assert client.commands == ["ping", "hello"]


def test_connect_twice_concurrently_creates_one_client():
    conn, client = connection()
    factory = conn._factory

    async def both():
        await asyncio.gather(conn.connect(), conn.connect())

    run(both())
    assert len(factory.calls) == 1


def test_close_is_idempotent_and_releases_the_client():
    conn, client = connection()

    async def lifecycle():
        await conn.connect()
        await conn.aclose()
        await conn.aclose()

    run(lifecycle())
    assert client.closed is True and conn.is_connected is False and conn.replica_set_name is None


def test_close_without_connect_is_harmless():
    conn, _ = connection()
    run(conn.aclose())


def test_the_async_context_manager_connects_and_closes():
    conn, client = connection()

    async def use():
        async with conn as active:
            assert active.is_connected
        assert client.closed

    run(use())


def test_using_the_database_before_connecting_is_a_typed_error():
    conn, _ = connection()
    for access in (lambda: conn.client, lambda: conn.database, lambda: conn.collection("x")):
        with pytest.raises(PersistenceUnavailableError) as caught:
            access()
        assert caught.value.details["reason"] == "not_connected"


def test_it_can_reconnect_after_a_close():
    conn, client = connection()

    async def cycle():
        await conn.connect()
        await conn.aclose()
        client.closed = False
        await conn.connect()

    run(cycle())
    assert conn.is_connected is True


# ---- failures -----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("failure", [driver.ServerSelectionTimeoutError("no servers found host=mongo-0 SECRET"),
                                     driver.AutoReconnect("connection reset by mongo-1"), driver.NetworkTimeout("timed out"),
                                     driver.ConnectionFailure("refused")])
def test_an_unreachable_server_is_a_typed_unavailable_error_with_no_driver_text(failure):
    client = FakeClient(fail_connect=failure)
    conn = MongoConnection(settings(), client_factory=factory_for(client))
    with pytest.raises(PersistenceUnavailableError) as caught:
        run(conn.connect())
    text = str(caught.value) + repr(caught.value.details)
    assert "mongo-0" not in text and "SECRET" not in text and PASSWORD not in text
    assert client.closed is True and conn.is_connected is False        # nothing is left open


def test_an_authentication_failure_is_typed_and_reveals_nothing():
    client = FakeClient(fail_connect=driver.OperationFailure("Authentication failed for user app", code=18))
    conn = MongoConnection(settings(), client_factory=factory_for(client))
    with pytest.raises(PersistenceError) as caught:
        run(conn.connect())
    assert caught.value.details["reason"] == "authorization_failed" and "app" not in str(caught.value)


def test_a_standalone_server_is_refused_when_a_replica_set_is_required():
    client = FakeClient(hello={"isWritablePrimary": True})
    conn = MongoConnection(settings(replica_set="rs0"), client_factory=factory_for(client))
    with pytest.raises(PersistenceConfigurationError) as caught:
        run(conn.connect())
    assert caught.value.details["reason"] == "deployment_is_not_a_replica_set"
    assert client.closed is True and conn.is_connected is False


def test_the_wrong_replica_set_is_refused():
    client = FakeClient(hello={"setName": "other"})
    conn = MongoConnection(settings(), client_factory=factory_for(client))
    with pytest.raises(PersistenceConfigurationError) as caught:
        run(conn.connect())
    assert caught.value.details["reason"] == "replica_set_name_does_not_match" and client.closed is True


def test_development_accepts_a_standalone_server_but_has_no_transactions():
    client = FakeClient(hello={"isWritablePrimary": True})
    conn = MongoConnection(settings(uri="mongodb://localhost:27017"), client_factory=factory_for(client))
    run(conn.connect())
    assert conn.is_connected and conn.replica_set_name is None and conn.supports_transactions is False

    async def attempt():
        await conn.run_in_transaction(lambda session: asyncio.sleep(0))

    with pytest.raises(TransactionUnavailableError):
        run(attempt())


def test_production_requires_a_replica_set_even_without_a_name_in_the_settings():
    prod = MongoSettings(uri=f"mongodb://u:{PASSWORD}@h1,h2/?tls=true", database="d", app_env="production")
    client = FakeClient(hello={"isWritablePrimary": True})
    with pytest.raises(PersistenceConfigurationError):
        run(MongoConnection(prod, client_factory=factory_for(client)).connect())


def test_a_missing_driver_is_a_configuration_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "pymongo":
            raise ImportError("no pymongo")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    from doc_extractor.infrastructure.mongo.connection import default_client_factory

    with pytest.raises(PersistenceConfigurationError) as caught:
        default_client_factory("mongodb://localhost")
    assert caught.value.details["reason"] == "mongo_driver_is_not_installed"


def test_a_failing_close_never_raises_from_shutdown():
    conn, client = connection()

    async def broken_close():
        raise RuntimeError("close exploded with SECRET")

    client.close = broken_close

    async def lifecycle():
        await conn.connect()
        await conn.aclose()

    run(lifecycle())
    assert conn.is_connected is False


# ---- logging ------------------------------------------------------------------------------------------------------------

def test_logs_never_contain_the_connection_string_or_host():
    conn, client = connection()
    failing = FakeClient(fail_connect=driver.ServerSelectionTimeoutError("mongo-0 unreachable"))
    bad = MongoConnection(settings(), client_factory=factory_for(failing))
    with LogCapture() as logs:
        run(conn.connect())
        run(conn.aclose())
        with pytest.raises(PersistenceUnavailableError):
            run(bad.connect())
    assert len(logs.records) >= 3                                         # the records exist, so the absence checks below mean something
    assert "MongoDB connected" in logs.text and "MongoDB connection closed" in logs.text and "reason=persistence_unavailable" in logs.text
    assert PASSWORD not in logs.text and "mongo-0" not in logs.text and "mongo-1" not in logs.text and "app:" not in logs.text
    assert "database=doc" in logs.text


# ---- health -------------------------------------------------------------------------------------------------------------

def test_health_before_connecting_is_not_connected_and_never_raises():
    conn, _ = connection()
    health = run(conn.health())
    assert health.status == "not_connected" and health.ok is False and health.to_public() == {"status": "not_connected", "reason": "not_connected"}


def test_health_when_connected_reports_the_replica_set_and_latency():
    conn, _ = connection()

    async def probe():
        await conn.connect()
        return await conn.health()

    health = run(probe())
    assert health.ok and health.replica_set == "rs0" and health.latency_ms is not None
    assert set(health.to_public()) == {"status", "replica_set", "latency_ms"}


def test_health_reports_an_outage_without_raising_or_leaking():
    conn, client = connection()

    async def probe():
        await conn.connect()
        client.fail_connect = driver.AutoReconnect("host mongo-0 gone SECRET")
        return await conn.health()

    health = run(probe())
    assert health.status == "unavailable" and health.ok is False and health.reason == "persistence_unavailable"
    assert "mongo-0" not in repr(health.to_public()) and "SECRET" not in repr(health.to_public())


def test_health_reports_a_hung_server_as_a_timeout():
    conn, client = connection(server_selection_timeout_ms=100)

    async def hang(name):
        if name == "ping" and client.hello.get("hang"):
            await asyncio.sleep(30)
        return {"ok": 1} if name == "ping" else dict(client.hello)

    async def probe():
        await conn.connect()
        client.hello["hang"] = True
        client.admin.command = hang
        return await conn.health()

    health = run(probe())
    assert health.status == "unavailable" and health.reason == "timeout"


# ---- transactions -------------------------------------------------------------------------------------------------------

def test_a_transaction_runs_the_callback_with_majority_concerns_on_the_primary():
    conn, client = connection()

    async def callback(session):
        return "done"

    async def use():
        await conn.connect()
        return await conn.run_in_transaction(callback, max_commit_time_ms=5000)

    assert run(use()) == "done"
    options = client.sessions[0].transactions[0]
    assert options["read_concern"].level == "majority" and options["write_concern"].document == {"w": "majority", "j": True}
    assert options["max_commit_time_ms"] == 5000


def test_a_transaction_without_a_connection_is_a_typed_error():
    conn, _ = connection()
    with pytest.raises(TransactionUnavailableError):
        run(conn.run_in_transaction(lambda session: asyncio.sleep(0)))


def test_a_driver_error_inside_a_transaction_is_mapped_to_a_typed_error():
    conn, _ = connection()

    async def callback(session):
        raise driver.NetworkTimeout("lost mongo-0 SECRET")

    async def use():
        await conn.connect()
        await conn.run_in_transaction(callback)

    with pytest.raises(PersistenceUnavailableError) as caught:
        run(use())
    assert "SECRET" not in str(caught.value)


# ---- transactions: session handling and error propagation (remediation of review findings H1 and M1) ---------------------

def test_the_fake_session_has_the_drivers_shape_a_plain_method_that_is_not_awaitable():
    session = FakeClient().start_session()
    assert not inspect.isawaitable(session) and hasattr(session, "__aenter__")


def test_a_transaction_uses_the_session_as_an_async_context_manager_and_does_not_await_it():
    """The H1 regression: ``await client.start_session()`` is a TypeError against the real driver (and the shape-faithful fake)."""
    conn, client = connection()

    async def callback(session):
        return "done"

    async def use():
        await conn.connect()
        return await conn.run_in_transaction(callback)

    assert run(use()) == "done"
    session = client.sessions[0]
    assert session.committed == 1 and session.aborted == 0 and session.ended is True


def test_a_failed_start_of_the_session_is_not_masked_as_an_unexpected_error():
    conn, client = connection()

    def broken():
        raise driver.ConnectionFailure("host mongo-0 unreachable SECRET")

    async def use():
        await conn.connect()
        client.start_session = broken
        await conn.run_in_transaction(lambda session: asyncio.sleep(0))

    with pytest.raises(PersistenceUnavailableError):
        run(use())


class _ConnectedFakeWithRealSessions(FakeClient):
    """Fake server commands, but the sessions come from the REAL driver: no server is needed to start and finish one."""

    real = None

    def start_session(self):
        if self.real is None:                              # created inside the running loop, like a real application would
            from pymongo import AsyncMongoClient

            self.real = AsyncMongoClient("mongodb://127.0.0.1:1/", serverSelectionTimeoutMS=300)
        return self.real.start_session()

    async def close(self):
        if self.real is not None:
            await self.real.close()
        await super().close()


def test_the_transaction_helper_works_with_the_real_driver_session_object():
    """
    Real driver, no server: ``with_transaction`` starts the transaction lazily, so a callback that does no database work
    commits an empty transaction locally. This exercises the REAL ``start_session`` / ``async with`` / ``with_transaction``
    shapes (the fake cannot): the pre-fix code failed here with ``TypeError: ... can't be awaited``.
    """
    from pymongo.asynchronous.client_session import AsyncClientSession

    conn, client = connection(_ConnectedFakeWithRealSessions())
    seen = []

    async def callback(session):
        seen.append(session)
        return "committed-locally"

    async def use():
        await conn.connect()
        try:
            return await conn.run_in_transaction(callback)
        finally:
            await conn.aclose()

    assert run(use()) == "committed-locally"
    assert isinstance(seen[0], AsyncClientSession)


def test_a_domain_exception_through_the_real_driver_session_keeps_its_type():
    class Conflict(Exception):
        pass

    conn, _ = connection(_ConnectedFakeWithRealSessions())

    async def callback(session):
        raise Conflict("task already claimed")

    async def use():
        await conn.connect()
        try:
            await conn.run_in_transaction(callback)
        finally:
            await conn.aclose()

    with pytest.raises(Conflict, match="task already claimed"):
        run(use())


class DomainConflict(Exception):
    """An application exception (a business-rule conflict), not a driver error."""


@pytest.mark.parametrize("raised", [
    DomainConflict("task already claimed"), ValueError("bad status"), KeyError("task"), RuntimeError("boom"),
    TenantScopeError("invalid_tenant_identifier"), PersistenceDataError("invalid_data"),
])
def test_an_application_exception_in_the_callback_propagates_with_its_own_type_and_aborts(raised):
    conn, client = connection()

    async def callback(session):
        raise raised

    async def use():
        await conn.connect()
        await conn.run_in_transaction(callback)

    with pytest.raises(type(raised)) as caught:
        run(use())
    assert caught.value is raised                                          # the same object: nothing wrapped or replaced
    session = client.sessions[0]
    assert session.aborted == 1 and session.committed == 0 and session.ended is True


@pytest.mark.parametrize("raised, expected_type, expected_reason", [
    (driver.DuplicateKeyError("E11000 dup key {k: 'SECRET'}", 11000), DuplicateKeyPersistenceError, "duplicate_key"),
    (driver.NetworkTimeout("lost mongo-0 SECRET"), PersistenceUnavailableError, "persistence_unavailable"),
    (driver.OperationFailure("not authorized SECRET", code=13), PersistenceError, "authorization_failed"),
    (driver.PyMongoError("some driver failure SECRET"), PersistenceError, "driver_error"),
    (bson_errors.InvalidDocument("cannot encode SECRET"), PersistenceDataError, "document_cannot_be_encoded"),
])
def test_a_recognised_driver_error_in_the_callback_is_translated_without_leaking(raised, expected_type, expected_reason):
    conn, client = connection()

    async def callback(session):
        raise raised

    async def use():
        await conn.connect()
        await conn.run_in_transaction(callback)

    with pytest.raises(PersistenceError) as caught:
        run(use())
    error = caught.value
    assert isinstance(error, expected_type)
    if expected_reason is not None:
        assert error.details["reason"] == expected_reason
    text = " ".join([str(error), repr(error), repr(error.details)])
    assert "SECRET" not in text and PASSWORD not in text and "mongo-0" not in text and error.__cause__ is None
    session = client.sessions[0]
    assert session.aborted == 1 and session.committed == 0


def test_a_connection_string_in_a_driver_error_does_not_reach_the_translated_error():
    conn, _ = connection()

    async def callback(session):
        raise driver.ConfigurationError(f"cannot parse {URI}")

    async def use():
        await conn.connect()
        await conn.run_in_transaction(callback)

    with pytest.raises(PersistenceError) as caught:
        run(use())
    assert PASSWORD not in str(caught.value) and PASSWORD not in repr(caught.value.details) and "mongo-0" not in str(caught.value)


def test_cancellation_inside_a_transaction_is_not_swallowed_and_aborts():
    conn, client = connection()

    async def use():
        await conn.connect()
        entered = asyncio.Event()

        async def callback(session):
            entered.set()
            await asyncio.sleep(30)

        task = asyncio.ensure_future(conn.run_in_transaction(callback))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task

    task = run(use())
    assert task.cancelled() and client.sessions[0].aborted == 1 and client.sessions[0].committed == 0


# ---- health cache (remediation of review finding M3) ---------------------------------------------------------------------

class Clock:
    """A controllable monotonic clock."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def cached_connection(client=None, **kwargs):
    client = client or FakeClient()
    clock = Clock()
    conn = MongoConnection(settings(), client_factory=factory_for(client), clock=clock, **kwargs)
    return conn, client, clock


def pings(client):
    return client.commands.count("ping")


async def settle():
    for _ in range(8):
        await asyncio.sleep(0)


def test_cached_health_serves_the_cache_without_another_ping_and_reports_the_same_contract():
    conn, client, clock = cached_connection()

    async def go():
        await conn.connect()
        first = await conn.cached_health()
        clock.advance(4.9)
        second = await conn.cached_health()
        return first, second

    first, second = run(go())
    assert first.ok and second is first and pings(client) == 2             # connect's ping + ONE health probe
    assert set(first.to_public()) == {"status", "replica_set", "latency_ms"}


def test_concurrent_callers_share_one_probe_in_flight():
    conn, client, clock = cached_connection()

    async def go():
        await conn.connect()
        client.ping_gate = asyncio.Event()
        callers = [asyncio.ensure_future(conn.cached_health()) for _ in range(25)]
        await settle()
        probes_while_held = pings(client)
        client.ping_gate.set()
        results = await asyncio.gather(*callers)
        return probes_while_held, results

    held, results = run(go())
    assert held == 2 and pings(client) == 2                                # connect + exactly one shared probe
    assert all(result.ok for result in results) and len({id(result) for result in results}) == 1


def test_an_unhealthy_result_is_cached_too_and_leaks_nothing():
    conn, client, clock = cached_connection()

    async def go():
        await conn.connect()
        client.fail_connect = driver.AutoReconnect("host mongo-0 gone SECRET")
        first = await conn.cached_health()
        for _ in range(10):
            again = await conn.cached_health()
            assert again is first
        return first

    first = run(go())
    assert first.status == "unavailable" and pings(client) == 2
    assert "mongo-0" not in repr(first.to_public()) and "SECRET" not in repr(first.to_public())


def test_the_cache_expires_on_the_monotonic_clock_and_recovery_becomes_visible():
    conn, client, clock = cached_connection()

    async def go():
        await conn.connect()
        client.fail_connect = driver.AutoReconnect("down")
        outage = await conn.cached_health()
        client.fail_connect = None                                         # the database comes back
        clock.advance(4.9)
        still_cached = await conn.cached_health()                          # inside the interval: not re-probed
        clock.advance(0.2)                                                 # interval over
        refreshing = await conn.cached_health()                            # starts ONE refresh, answers from the cache
        await settle()
        recovered = await conn.cached_health()
        return outage, still_cached, refreshing, recovered

    outage, still_cached, refreshing, recovered = run(go())
    assert outage.status == "unavailable" and still_cached is outage and refreshing is outage
    assert recovered.ok and pings(client) == 3


def test_a_new_outage_becomes_visible_after_the_interval_too():
    conn, client, clock = cached_connection()

    async def go():
        await conn.connect()
        healthy = await conn.cached_health()
        client.fail_connect = driver.AutoReconnect("down")
        clock.advance(6)
        await conn.cached_health()
        await settle()
        return healthy, await conn.cached_health()

    healthy, after = run(go())
    assert healthy.ok and after.status == "unavailable"


def test_an_outage_does_not_hold_requests_for_a_server_selection_timeout():
    """A hung database: callers inside the staleness bound get the last result at once; only ONE probe is ever outstanding."""
    conn, client, clock = cached_connection()

    async def go():
        await conn.connect()
        first = await conn.cached_health()
        client.ping_gate = asyncio.Event()                                 # from now on a ping never returns
        clock.advance(6)
        answers = [await asyncio.wait_for(conn.cached_health(), 0.5) for _ in range(20)]
        await settle()
        client.ping_gate.set()
        await settle()
        return first, answers

    first, answers = run(go())
    assert all(answer is first for answer in answers)
    assert pings(client) == 3                                              # connect, first probe, ONE refresh for 20 callers


def test_without_a_usable_cached_result_callers_wait_for_the_shared_probe_and_a_departing_caller_does_not_cancel_it():
    conn, client, clock = cached_connection()

    async def go():
        await conn.connect()
        await conn.cached_health()
        clock.advance(31)                                                  # past the staleness bound: must wait for fresh
        client.ping_gate = asyncio.Event()
        impatient = asyncio.ensure_future(conn.cached_health())
        patient = asyncio.ensure_future(conn.cached_health())
        await settle()
        impatient.cancel()
        with pytest.raises(asyncio.CancelledError):
            await impatient
        client.ping_gate.set()
        return await patient

    result = run(go())
    assert result.ok and pings(client) == 3


def test_cached_health_when_not_connected_answers_without_io_and_is_not_cached():
    conn, client, clock = cached_connection()

    async def go():
        before = await conn.cached_health()
        await conn.connect()
        after = await conn.cached_health()
        return before, after

    before, after = run(go())
    assert before.status == "not_connected" and after.ok


def test_closing_discards_the_cache_and_a_reconnect_does_not_serve_old_results():
    conn, client, clock = cached_connection()

    async def go():
        await conn.connect()
        assert (await conn.cached_health()).ok
        await conn.aclose()
        closed = await conn.cached_health()
        client.closed = False
        await conn.connect()
        return closed, await conn.cached_health()

    closed, reopened = run(go())
    assert closed.status == "not_connected" and reopened.ok


def test_cache_state_is_per_connection_not_shared():
    first, client_one, _ = cached_connection()
    second, client_two, _ = cached_connection()

    async def go():
        await first.connect()
        await second.connect()
        await first.cached_health()
        await second.cached_health()
        await first.cached_health()

    run(go())
    assert pings(client_one) == 2 and pings(client_two) == 2


@pytest.mark.parametrize("ttl, stale", [(0, 30), (-1, 30), (61, 100), (5, 4), (5, 301)])
def test_unreasonable_cache_bounds_are_refused(ttl, stale):
    with pytest.raises(ValueError):
        MongoConnection(settings(), health_cache_ttl=ttl, health_max_stale=stale)


def test_the_uncached_probe_still_probes_every_time():
    conn, client, clock = cached_connection()

    async def go():
        await conn.connect()
        for _ in range(3):
            await conn.health()

    run(go())
    assert pings(client) == 4


def test_only_driver_and_bson_errors_are_recognised_as_driver_errors():
    from doc_extractor.infrastructure.mongo.errors import is_driver_error

    assert is_driver_error(driver.PyMongoError("x")) and is_driver_error(driver.DuplicateKeyError("x"))
    assert is_driver_error(driver.AutoReconnect("x")) and is_driver_error(bson_errors.InvalidDocument("x"))
    assert not is_driver_error(ValueError("x")) and not is_driver_error(KeyError("x")) and not is_driver_error(DomainConflict("x"))
    assert not is_driver_error(TenantScopeError("invalid_tenant_identifier"))
