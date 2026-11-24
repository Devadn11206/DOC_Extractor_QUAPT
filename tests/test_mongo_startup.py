"""
Production composition and start-up validation tests for the MongoDB foundation (Team Member 3, Phase 2A).

MOCKED: the MongoDB client is the in-memory fake of ``tests/mongo_fakes.py``. These tests prove the composition and lifecycle
wiring (what is built, when it connects, that start-up fails closed, that nothing falls back to another store, that health
and logs reveal nothing). They do not prove behaviour against a real server (``tests/test_mongo_integration.py``).
"""

import asyncio
import pathlib

import pytest

pytest.importorskip("pymongo", reason="the 'mongo' extra provides the driver whose error types these tests map")
from fastapi.testclient import TestClient  # noqa: E402
from pymongo import errors as driver  # noqa: E402

from doc_extractor.api.app import create_app  # noqa: E402
from doc_extractor.api.composition import ApplicationContainer  # noqa: E402
from doc_extractor.api.config import ApiSettings  # noqa: E402
from doc_extractor.infrastructure.mongo import (  # noqa: E402
    DEFAULT_REGISTRY,
    IndexConflictError,
    MongoConnection,
    MongoSettings,
    PersistenceConfigurationError,
    PersistenceError,
    PersistenceUnavailableError,
    ensure_indexes,
)
from doc_extractor.infrastructure.mongo.indexes import REVIEW_TASKS  # noqa: E402
from tests.mongo_fakes import FakeClient, LogCapture, factory_for  # noqa: E402

PASSWORD = "Start-Secret-Marker-5530"
URI = f"mongodb://app:{PASSWORD}@mongo-0:27017/?replicaSet=rs0"
PROD_URI = f"mongodb://app:{PASSWORD}@mongo-0:27017,mongo-1:27017/?replicaSet=rs0&tls=true"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


def api_settings(**overrides):
    values = {"app_env": "development", "mongo_uri": URI, "mongo_database": "doc"}
    values.update(overrides)
    return ApiSettings(**values)


def container_with(client=None, **overrides):
    client = client or FakeClient()
    settings = api_settings(**overrides)
    connection = MongoConnection(MongoSettings.from_api_settings(settings), client_factory=factory_for(client))
    return ApplicationContainer(api_settings=settings, mongo_connection=connection), client


# ---- composition ------------------------------------------------------------------------------------------------------

def test_without_mongodb_nothing_changes():
    container = ApplicationContainer(api_settings=ApiSettings(app_env="development"))
    assert container.mongo is None and container.index_registry is DEFAULT_REGISTRY
    run(container.startup())
    run(container.ashutdown())
    for port in ("document_metadata_repo", "review_task_repo", "api_key_repo", "audit_repo", "webhook_subscription_repo"):
        assert getattr(container, port) is None, port


def test_the_container_builds_a_connection_from_the_settings_without_opening_anything(monkeypatch):
    created = []
    monkeypatch.setattr("doc_extractor.infrastructure.mongo.connection.default_client_factory",
                        lambda uri, **options: created.append(uri))
    container = ApplicationContainer(api_settings=api_settings())
    assert isinstance(container.mongo, MongoConnection) and container.mongo.is_connected is False and created == []
    assert container.mongo.settings.database == "doc" and container.mongo.settings.uri.get_secret_value() == URI


def test_the_container_reads_a_mounted_secret_file(tmp_path, monkeypatch):
    secret = tmp_path / "uri"
    secret.write_text(URI + "\n", encoding="utf-8")
    container = ApplicationContainer(api_settings=ApiSettings(app_env="development", mongo_uri_file=str(secret), mongo_database="doc"))
    assert container.mongo.settings.uri.get_secret_value() == URI


def test_an_unreadable_secret_file_fails_composition_with_a_fixed_reason(tmp_path):
    settings = ApiSettings(app_env="development", mongo_uri_file=str(tmp_path / "absent"), mongo_database="doc")
    with pytest.raises(PersistenceConfigurationError) as caught:
        ApplicationContainer(api_settings=settings)
    assert caught.value.details["reason"] == "uri_file_is_unreadable" and str(tmp_path) not in str(caught.value)


def test_a_hardened_secret_file_with_a_weak_string_fails_composition(tmp_path):
    secret = tmp_path / "uri"
    secret.write_text(f"mongodb://u:{PASSWORD}@h/", encoding="utf-8")
    settings = ApiSettings(app_env="production", mongo_uri_file=str(secret), mongo_database="doc")
    with pytest.raises(Exception) as caught:
        ApplicationContainer(api_settings=settings)
    assert PASSWORD not in str(caught.value)


def test_the_foundation_never_provides_an_in_memory_or_fallback_store():
    """Not configured means the persistence ports stay unconfigured (503). There is no substitute in any environment."""
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "src/doc_extractor/infrastructure").rglob("*.py"))
    for forbidden in ("InMemory", "mongomock", "mongo_mock", "fallback_to", "review_task_double", "LocalMemory"):
        assert forbidden not in source, forbidden
    composition = (ROOT / "src/doc_extractor/api/composition.py").read_text(encoding="utf-8")
    assert "InMemoryReviewTask" not in composition and "InMemoryApiKey" not in composition


@pytest.mark.parametrize("env", ["staging", "production"])
def test_a_hardened_environment_without_mongodb_builds_no_adapter_and_cannot_start(env):
    """Construction is pure and builds nothing; START-UP is where the database requirement is enforced (Phase 2A-5)."""
    container = ApplicationContainer(api_settings=ApiSettings(app_env=env))
    assert container.mongo is None
    assert container.review_task_repo is None and container.api_key_repo is None and container.document_metadata_repo is None
    with pytest.raises(PersistenceConfigurationError):
        run(container.startup())
    assert container.review_task_repo is None and container.api_key_repo is None


# ---- start-up ---------------------------------------------------------------------------------------------------------

def test_startup_connects_and_applies_the_declared_indexes():
    container, client = container_with()
    run(container.startup())
    assert container.mongo.is_connected and client.commands == ["ping", "hello"]
    collection = client["doc"][REVIEW_TASKS]
    assert "rt_tenant_task_uq" in collection.indexes and collection.indexes["rt_tenant_document_created"]["unique"] is False


def test_startup_is_idempotent_and_safe_to_repeat():
    container, client = container_with()
    run(container.startup())
    snapshot = {name: dict(c.indexes) for name, c in client["doc"].collections.items()}
    run(container.startup())
    assert {name: dict(c.indexes) for name, c in client["doc"].collections.items()} == snapshot and client.commands == ["ping", "hello", ]


def test_indexes_can_be_left_to_a_migration_job_but_only_once_the_audit_chain_index_exists():
    """Bootstrap off: start-up still verifies the ONE index the audit chain's correctness depends on (Phase 2B)."""
    container, client = container_with(mongo_ensure_indexes_on_startup=False)
    with pytest.raises(PersistenceConfigurationError) as caught:
        run(container.startup())
    assert caught.value.details["reason"] == "audit_sequence_index_is_missing" and client["doc"].collections.get("review_tasks") is None
    assert client.closed is True and container.review_task_repo is None and container.audit_repo is None and container.audit_logger is None

    migrated, client = container_with(mongo_ensure_indexes_on_startup=False)       # the migration job created the indexes
    run(ensure_indexes(client["doc"], DEFAULT_REGISTRY))
    run(migrated.startup())
    assert migrated.mongo.is_connected and migrated.audit_repo is not None and migrated.audit_logger is not None


def test_an_unreachable_database_stops_start_up_with_a_typed_error_and_leaves_nothing_open():
    client = FakeClient(fail_connect=driver.ServerSelectionTimeoutError("no servers mongo-0 SECRET"))
    container, _ = container_with(client)
    with pytest.raises(PersistenceUnavailableError) as caught:
        run(container.startup())
    assert "mongo-0" not in str(caught.value) + repr(caught.value.details) and PASSWORD not in str(caught.value)
    assert container.mongo.is_connected is False and client.closed is True


def test_a_standalone_server_stops_start_up_when_a_replica_set_is_required():
    container, client = container_with(client=FakeClient(hello={"isWritablePrimary": True}), mongo_replica_set="rs0")
    with pytest.raises(PersistenceConfigurationError):
        run(container.startup())
    assert container.mongo.is_connected is False


def test_an_index_conflict_stops_start_up_and_closes_the_connection():
    client = FakeClient()
    client["doc"][REVIEW_TASKS].indexes["rt_tenant_task_uq"] = {"keys": [("tenant_id", 1), ("task_id", 1)], "unique": False,
                                                                 "partial": None, "ttl": None}
    container, _ = container_with(client)
    with pytest.raises(IndexConflictError):
        run(container.startup())
    assert container.mongo.is_connected is False and client.closed is True


def test_a_production_database_failure_is_fatal_not_a_silent_fallback():
    client = FakeClient(fail_connect=driver.NetworkTimeout("down"))
    settings = ApiSettings(app_env="production", mongo_uri=PROD_URI, mongo_database="doc")
    connection = MongoConnection(MongoSettings.from_api_settings(settings), client_factory=factory_for(client))
    container = ApplicationContainer(api_settings=settings, mongo_connection=connection)
    with pytest.raises(PersistenceUnavailableError):
        run(container.startup())
    assert container.review_task_repo is None                 # still unconfigured: no substitute appeared


def test_shutdown_closes_the_connection_and_is_idempotent():
    container, client = container_with()
    run(container.startup())
    run(container.ashutdown())
    run(container.ashutdown())
    assert client.closed is True and container.mongo.is_connected is False


# ---- the application lifespan ---------------------------------------------------------------------------------------------

def test_the_lifespan_connects_at_start_and_closes_at_shutdown_for_an_application_owned_container(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("doc_extractor.infrastructure.mongo.connection.default_client_factory", factory_for(client))
    app = create_app(api_settings())
    with TestClient(app):
        assert app.state.container.mongo.is_connected is True and "rt_tenant_task_uq" in client["doc"][REVIEW_TASKS].indexes
    assert client.closed is True and app.state.container.mongo.is_connected is False


def test_a_failing_database_stops_the_application_from_starting(monkeypatch):
    client = FakeClient(fail_connect=driver.ServerSelectionTimeoutError("no servers SECRET"))
    monkeypatch.setattr("doc_extractor.infrastructure.mongo.connection.default_client_factory", factory_for(client))
    app = create_app(api_settings())
    with pytest.raises(PersistenceUnavailableError):
        with TestClient(app):
            pytest.fail("the application must not start")


def test_a_caller_owned_container_is_connected_but_not_closed_by_the_application():
    container, client = container_with()
    app = create_app(api_settings(), container)
    with TestClient(app):
        assert container.mongo.is_connected is True
    assert client.closed is False and container.mongo.is_connected is True       # the caller owns its lifecycle
    run(container.ashutdown())
    assert client.closed is True


def test_the_application_without_mongodb_starts_exactly_as_before():
    app = create_app(ApiSettings(app_env="development"))
    with TestClient(app) as client:
        body = client.get("/api/v1/health").json()
    assert "database" not in body and body["status"] == "healthy"


# ---- health -----------------------------------------------------------------------------------------------------------

def test_health_reports_the_database_component_only_when_mongodb_is_configured():
    container, client = container_with()
    app = create_app(api_settings(), container)
    with TestClient(app) as http:
        response = http.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy" and body["database"]["status"] == "ok" and body["database"]["replica_set"] == "rs0"
    assert set(body["database"]) == {"status", "replica_set", "latency_ms"}
    assert PASSWORD not in response.text and "mongo-0" not in response.text


def test_an_outage_degrades_health_but_keeps_the_liveness_probe_up_and_leaks_nothing():
    container, client = container_with()
    app = create_app(api_settings(), container)
    with TestClient(app) as http:
        client.fail_connect = driver.AutoReconnect("host mongo-0 lost SECRET")
        response = http.get("/api/v1/health")
    body = response.json()
    assert response.status_code == 200 and body["status"] == "degraded"
    assert body["database"] == {"status": "unavailable", "replica_set": "rs0", "reason": "persistence_unavailable"}
    assert "mongo-0" not in response.text and "SECRET" not in response.text and PASSWORD not in response.text


class _Clock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now


def container_with_clock(client=None):
    client = client or FakeClient()
    clock = _Clock()
    settings = api_settings()
    connection = MongoConnection(MongoSettings.from_api_settings(settings), client_factory=factory_for(client), clock=clock)
    return ApplicationContainer(api_settings=settings, mongo_connection=connection), client, clock


def test_repeated_health_requests_do_not_ping_the_database_each_time():
    container, client, clock = container_with_clock()
    with TestClient(create_app(api_settings(), container)) as http:
        before = client.commands.count("ping")
        bodies = [http.get("/api/v1/health").json() for _ in range(25)]
        after = client.commands.count("ping")
    assert after - before == 1 and all(body["database"]["status"] == "ok" for body in bodies)


def test_health_during_an_outage_is_served_from_the_cache_and_recovery_shows_after_expiry():
    container, client, clock = container_with_clock()
    with TestClient(create_app(api_settings(), container)) as http:
        client.fail_connect = driver.AutoReconnect("host mongo-0 lost SECRET")
        first = http.get("/api/v1/health")
        pings = client.commands.count("ping")
        repeated = [http.get("/api/v1/health") for _ in range(10)]
        assert client.commands.count("ping") == pings                      # the outage is not re-probed per request
        assert all(r.status_code == 200 and r.json()["status"] == "degraded" for r in [first, *repeated])
        client.fail_connect = None                                         # recovery
        assert http.get("/api/v1/health").json()["status"] == "degraded"   # still inside the interval
        clock.now += 6
        http.get("/api/v1/health")                                         # starts the refresh
        for _ in range(50):
            body = http.get("/api/v1/health").json()
            if body["status"] == "healthy":
                break
    assert body["status"] == "healthy" and body["database"]["status"] == "ok"
    texts = " ".join(r.text for r in [first, *repeated])
    assert "mongo-0" not in texts and "SECRET" not in texts and PASSWORD not in texts


def test_the_health_response_contract_is_unchanged_by_the_cache():
    container, client, clock = container_with_clock()
    with TestClient(create_app(api_settings(), container)) as http:
        body = http.get("/api/v1/health").json()
    assert set(body["database"]) == {"status", "replica_set", "latency_ms"} and body["status"] == "healthy"


def test_the_route_surface_is_unchanged_by_the_foundation():
    schema_paths = set(create_app(ApiSettings(app_env="development")).openapi()["paths"])
    assert schema_paths == {
        "/health", "/api/v1/health", "/api/v1/documents", "/api/v1/documents/{document_id}",
        "/api/v1/documents/{document_id}/pages/{page_number}", "/api/v1/documents/{document_id}/extract-sync",
        "/api/v1/review-tasks", "/api/v1/review-tasks/{task_id}/claim", "/api/v1/review-tasks/{task_id}/release",
    } | {p for p in schema_paths if "schema" in p}


# ---- logging ----------------------------------------------------------------------------------------------------------

def test_start_up_and_shutdown_logs_contain_no_connection_detail():
    container, client = container_with()
    with LogCapture() as logs:
        run(container.startup())
        run(container.ashutdown())
    assert "MongoDB connected" in logs.text and "index bootstrap finished" in logs.text and "MongoDB connection closed" in logs.text
    assert PASSWORD not in logs.text and "mongo-0" not in logs.text and "app:" not in logs.text


# =============================================================================================================================
# Phase 2A-5: the database start-up gate. Development: MongoDB optional. Staging and production: required, fail closed.
# =============================================================================================================================

import logging  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from fastapi.testclient import TestClient as _TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from doc_extractor.api.app import publish_started_ports  # noqa: E402
from doc_extractor.exceptions import ConfigurationError  # noqa: E402
from doc_extractor.infrastructure.mongo.api_keys import MongoApiKeyRepository  # noqa: E402
from doc_extractor.infrastructure.mongo.review_tasks import MongoReviewTaskRepository  # noqa: E402
from doc_extractor.malware import MockMalwareScanner  # noqa: E402
from tests.review_task_double import InMemoryReviewTaskRepository  # noqa: E402
from tests.test_api_key_service import MockApiKeyRepository  # noqa: E402

HARDENED = ["staging", "production"]
SECURE_URI = f"mongodb://app:{PASSWORD}@mongo-0.internal:27017,mongo-1.internal:27017/?replicaSet=rs0&tls=true"
MISSING_REASON = "database_is_required_in_staging_and_production"


def hardened_settings(env, **overrides):
    values = {"app_env": env, "mongo_uri": SECURE_URI, "mongo_database": "doc"}
    values.update(overrides)
    return ApiSettings(**values)


def hardened_container(env, client=None, **kwargs):
    client = client or FakeClient()
    settings = hardened_settings(env)
    connection = MongoConnection(MongoSettings.from_api_settings(settings), client_factory=factory_for(client))
    kwargs.setdefault("malware_scanner", MockMalwareScanner())
    return ApplicationContainer(api_settings=settings, mongo_connection=connection, **kwargs), client


def hardened_app(env, client=None, **kwargs):
    container, client = hardened_container(env, client, **kwargs)
    return create_app(container.api_settings, container), container, client


def no_leak(text):
    for secret in (PASSWORD, "mongo-0", "mongo-1", "app:", "mongodb://"):
        assert secret not in text, secret


# ---- development: MongoDB stays optional --------------------------------------------------------------------------------

def test_development_starts_without_mongodb_through_the_lifespan_with_every_port_unconfigured():
    container = ApplicationContainer(api_settings=ApiSettings(app_env="development"), malware_scanner=MockMalwareScanner())
    app = create_app(container.api_settings, container)
    with _TestClient(app) as http:
        assert http.get("/api/v1/health").status_code == 200
        assert app.state.review_task_repo is None and app.state.api_key_repo is None
    assert container.mongo is None


def test_development_with_injected_repositories_and_no_mongodb_is_unchanged():
    review, keys = InMemoryReviewTaskRepository(), MockApiKeyRepository()
    container = ApplicationContainer(api_settings=ApiSettings(app_env="development"), malware_scanner=MockMalwareScanner(),
                                     review_task_repo=review, api_key_repo=keys)
    app = create_app(container.api_settings, container)
    with _TestClient(app):
        assert app.state.review_task_repo is review and app.state.api_key_repo is keys


def test_development_with_mongodb_configured_still_connects_and_fails_closed_when_it_is_unreachable():
    container, client = container_with()
    run(container.startup())
    assert container.mongo.is_connected and isinstance(container.review_task_repo, MongoReviewTaskRepository)
    down = FakeClient(fail_connect=driver.NetworkTimeout("down"))
    container, _ = container_with(down)
    with pytest.raises(PersistenceUnavailableError):
        run(container.startup())
    assert container.review_task_repo is None and container.api_key_repo is None


# ---- staging and production: missing configuration ----------------------------------------------------------------------

@pytest.mark.parametrize("env", HARDENED)
def test_a_hardened_environment_without_a_database_refuses_to_start_with_a_typed_sanitised_error(env):
    container = ApplicationContainer(api_settings=ApiSettings(app_env=env), malware_scanner=MockMalwareScanner())
    with LogCapture("doc_extractor.api.composition") as logs:
        with pytest.raises(PersistenceConfigurationError) as caught:
            run(container.startup())
    error = caught.value
    assert isinstance(error, ConfigurationError)
    assert error.details == {"reason": MISSING_REASON, "setting": "mongo_uri"}
    assert str(error) == "The MongoDB configuration is not valid."
    assert "reason=database_is_required" in logs.text and len(logs.records) == 1
    assert container.mongo is None and container.review_task_repo is None and container.api_key_repo is None


@pytest.mark.parametrize("env", HARDENED)
def test_injected_repositories_do_not_waive_the_database_requirement(env):
    """No bypass: in-memory or mock repositories cannot stand in for the database in staging or production."""
    container = ApplicationContainer(api_settings=ApiSettings(app_env=env), malware_scanner=MockMalwareScanner(),
                                     review_task_repo=InMemoryReviewTaskRepository(), api_key_repo=MockApiKeyRepository())
    with pytest.raises(PersistenceConfigurationError) as caught:
        run(container.startup())
    assert caught.value.details["reason"] == MISSING_REASON


@pytest.mark.parametrize("env", HARDENED)
def test_the_lifespan_refuses_to_start_a_hardened_application_without_a_database_and_publishes_nothing(env):
    container = ApplicationContainer(api_settings=ApiSettings(app_env=env), malware_scanner=MockMalwareScanner())
    app = create_app(container.api_settings, container)
    with pytest.raises(PersistenceConfigurationError):
        with _TestClient(app):
            pytest.fail("the application must not start")
    assert app.state.review_task_repo is None and app.state.api_key_repo is None
    assert getattr(app.state, "_startup_published_ports", {}) == {}


@pytest.mark.parametrize("env", HARDENED)
def test_there_is_no_setting_that_turns_the_gate_off(env):
    from doc_extractor.api.config import ApiSettings as Settings

    forbidden = ("allow", "optional", "bypass", "skip", "disable", "fallback", "memory")
    mongo_fields = [name for name in Settings.model_fields if "mongo" in name or "database" in name or "persist" in name]
    assert not [name for name in mongo_fields if any(word in name for word in forbidden)]
    container = ApplicationContainer(api_settings=ApiSettings(app_env=env, debug=False), malware_scanner=MockMalwareScanner())
    with pytest.raises(PersistenceConfigurationError):
        run(container.startup())


# ---- staging and production: malformed or insecure configuration (refused when the settings are built) ------------------

@pytest.mark.parametrize("env", HARDENED)
@pytest.mark.parametrize("overrides, reason", [
    ({"mongo_database": None}, "database_is_required"),
    ({"mongo_uri": None, "mongo_database": "doc"}, "mongo_settings_without_a_connection_string"),
    ({"mongo_uri": None, "mongo_database": None, "mongo_replica_set": "rs0"}, "mongo_settings_without_a_connection_string"),
    ({"mongo_uri": f"mongodb://app:{PASSWORD}@mongo-0:27017/?replicaSet=rs0"}, "tls_is_required"),
    ({"mongo_uri": f"mongodb://mongo-0:27017/?replicaSet=rs0&tls=true"}, "authentication_is_required"),
    ({"mongo_uri": f"mongodb://app:{PASSWORD}@mongo-0:27017/?replicaSet=rs0&tls=true&tlsAllowInvalidCertificates=true"}, "insecure_tls"),
    ({"mongo_uri": f"mongodb://app:{PASSWORD}@mongo-0:27017/?replicaSet=rs0&tls=true&directConnection=true"}, "direct_connection"),
    ({"mongo_uri": f"not a connection string {PASSWORD}"}, ""),
    ({"mongo_database": "bad name!"}, ""),
    ({"mongo_uri_file": "/definitely/not/here"}, "mutually_exclusive"),
])
def test_malformed_or_insecure_database_settings_are_refused_with_a_sanitised_error(env, overrides, reason):
    with pytest.raises((ValidationError, ValueError, PersistenceConfigurationError)) as caught:
        hardened_settings(env, **overrides)
    text = str(caught.value) + repr(caught.value)
    no_leak(text)
    assert reason in text


@pytest.mark.parametrize("env", HARDENED)
def test_an_unreadable_secret_file_stops_the_container_without_naming_the_path(env, tmp_path):
    missing = tmp_path / "secret-path-marker" / "uri"
    settings = ApiSettings(app_env=env, mongo_uri_file=str(missing), mongo_database="doc")
    with pytest.raises(PersistenceConfigurationError) as caught:
        ApplicationContainer(api_settings=settings, malware_scanner=MockMalwareScanner())
    assert "secret-path-marker" not in str(caught.value) + repr(caught.value.details)


@pytest.mark.parametrize("env", HARDENED)
def test_a_valid_secret_file_is_accepted_and_never_echoed(env, tmp_path):
    secret_file = tmp_path / "uri"
    secret_file.write_text(SECURE_URI, encoding="utf-8")
    settings = ApiSettings(app_env=env, mongo_uri_file=str(secret_file), mongo_database="doc")
    container = ApplicationContainer(api_settings=settings, malware_scanner=MockMalwareScanner())
    assert container.mongo is not None and container.mongo.settings.database == "doc"
    no_leak(repr(settings) + str(settings.model_dump()) + repr(container.mongo.settings) + str(container.mongo.settings.summary()))


# ---- staging and production: an injected connection must satisfy the environment ------------------------------------------

@pytest.mark.parametrize("env", HARDENED)
def test_a_connection_built_for_development_cannot_be_injected_into_a_hardened_container(env):
    """No test bypass: a plain, unauthenticated, non-TLS connection object does not satisfy the gate."""
    client = FakeClient()
    weak = MongoConnection(MongoSettings(uri="mongodb://localhost:27017", database="doc", app_env="development"),
                           client_factory=factory_for(client))
    container = ApplicationContainer(api_settings=ApiSettings(app_env=env), mongo_connection=weak, malware_scanner=MockMalwareScanner())
    with LogCapture("doc_extractor.api.composition") as logs:
        with pytest.raises(PersistenceConfigurationError) as caught:
            run(container.startup())
    assert caught.value.details["reason"] == "database_settings_do_not_match_the_environment"
    assert client.commands == [] and weak.is_connected is False                     # it never even connected
    assert container.review_task_repo is None and "reason=settings_mismatch" in logs.text


def test_a_staging_connection_cannot_serve_a_production_container():
    staging = MongoConnection(MongoSettings.from_api_settings(hardened_settings("staging")), client_factory=factory_for(FakeClient()))
    container = ApplicationContainer(api_settings=ApiSettings(app_env="production"), mongo_connection=staging,
                                     malware_scanner=MockMalwareScanner())
    with pytest.raises(PersistenceConfigurationError):
        run(container.startup())


@pytest.mark.parametrize("env", HARDENED)
def test_a_connection_built_for_the_same_environment_is_accepted(env):
    container, client = hardened_container(env)
    run(container.startup())
    assert container.mongo.is_connected and client.commands == ["ping", "hello"]


# ---- staging and production: connection, replica set and index failures --------------------------------------------------

@pytest.mark.parametrize("env", HARDENED)
def test_an_unreachable_database_stops_start_up_and_leaves_nothing_configured_or_open(env):
    client = FakeClient(fail_connect=driver.ServerSelectionTimeoutError("mongo-0.internal:27017 timed out " + PASSWORD))
    app, container, _ = hardened_app(env, client)
    with LogCapture() as logs:
        with pytest.raises(PersistenceUnavailableError) as caught:
            with _TestClient(app):
                pytest.fail("the application must not start")
    no_leak(str(caught.value) + repr(caught.value) + repr(caught.value.details) + logs.text)
    assert len(logs.records) >= 1                                                    # the records exist: the absence checks mean something
    assert container.mongo.is_connected is False and client.closed is True
    assert container.review_task_repo is None and container.api_key_repo is None
    assert app.state.review_task_repo is None and app.state.api_key_repo is None


@pytest.mark.parametrize("env", HARDENED)
def test_a_standalone_server_where_a_replica_set_is_required_is_refused_and_closed(env):
    client = FakeClient(hello={"isWritablePrimary": True})                         # no setName: not a replica set
    container, _ = hardened_container(env, client)
    with pytest.raises(PersistenceConfigurationError) as caught:
        run(container.startup())
    assert caught.value.details["reason"] == "deployment_is_not_a_replica_set"
    assert client.closed is True and container.mongo.is_connected is False and container.api_key_repo is None


@pytest.mark.parametrize("env", HARDENED)
def test_the_wrong_replica_set_is_refused_and_closed(env):
    client = FakeClient(hello={"setName": "some-other-set", "isWritablePrimary": True})
    container, _ = hardened_container(env, client)
    with pytest.raises(PersistenceConfigurationError) as caught:
        run(container.startup())
    assert caught.value.details["reason"] == "replica_set_name_does_not_match" and client.closed is True


@pytest.mark.parametrize("env", HARDENED)
@pytest.mark.parametrize("failure", [driver.OperationFailure("index conflict mongo-0 " + PASSWORD, code=85),
                                     driver.NetworkTimeout("lost mongo-1"), driver.OperationFailure("not authorized", code=13)])
def test_a_failed_index_bootstrap_stops_start_up_and_leaves_nothing_configured_or_open(env, failure):
    client = FakeClient()
    app, container, _ = hardened_app(env, client)
    client["doc"][REVIEW_TASKS].fail_with = failure
    with LogCapture() as logs:
        with pytest.raises(PersistenceError) as caught:
            with _TestClient(app):
                pytest.fail("the application must not start")
    no_leak(str(caught.value) + repr(caught.value.details) + logs.text)
    assert client.closed is True and container.mongo.is_connected is False
    assert container.review_task_repo is None and container.api_key_repo is None
    assert app.state.review_task_repo is None and app.state.api_key_repo is None


@pytest.mark.parametrize("env", HARDENED)
def test_a_failed_start_up_can_be_retried_once_the_database_is_back_without_a_half_configured_state(env):
    client = FakeClient(fail_connect=driver.AutoReconnect("down"))
    container, _ = hardened_container(env, client)
    with pytest.raises(PersistenceUnavailableError):
        run(container.startup())
    assert container.review_task_repo is None and container.api_key_repo is None
    client.fail_connect, client.closed = None, False                                  # the database recovers
    run(container.startup())
    assert container.mongo.is_connected
    assert isinstance(container.review_task_repo, MongoReviewTaskRepository) and isinstance(container.api_key_repo, MongoApiKeyRepository)


# ---- staging and production: successful start-up ---------------------------------------------------------------------------

@pytest.mark.parametrize("env", HARDENED)
def test_a_hardened_application_starts_publishes_both_repositories_and_reports_a_healthy_database(env):
    app, container, client = hardened_app(env)
    with _TestClient(app) as http:
        assert isinstance(app.state.review_task_repo, MongoReviewTaskRepository) and app.state.review_task_repo is container.review_task_repo
        assert isinstance(app.state.api_key_repo, MongoApiKeyRepository) and app.state.api_key_repo is container.api_key_repo
        health = http.get("/api/v1/health")
    body = health.json()
    assert health.status_code == 200 and body["status"] == "healthy" and body["database"]["status"] == "ok"
    assert set(body["database"]) == {"status", "replica_set", "latency_ms"}
    no_leak(health.text)
    assert "index bootstrap finished" not in health.text
    assert {"rt_tenant_task_uq", "rt_tenant_document_created"} <= set(client["doc"][REVIEW_TASKS].indexes)


@pytest.mark.parametrize("env", HARDENED)
def test_an_injected_repository_still_wins_in_a_hardened_environment_with_a_verified_database(env):
    """Injection is honoured only on top of a verified database: it can add to the gate's requirements, never remove them."""
    mine = MockApiKeyRepository()
    app, container, _ = hardened_app(env, api_key_repo=mine)
    with _TestClient(app):
        assert container.api_key_repo is mine and app.state.api_key_repo is mine
        assert isinstance(app.state.review_task_repo, MongoReviewTaskRepository)


@pytest.mark.parametrize("env", HARDENED)
def test_an_explicit_application_state_override_is_preserved_after_a_successful_start_up(env):
    app, container, _ = hardened_app(env)
    explicit = MockApiKeyRepository()
    app.state.api_key_repo = explicit
    with _TestClient(app):
        assert app.state.api_key_repo is explicit
        assert isinstance(app.state.review_task_repo, MongoReviewTaskRepository)


def test_the_logs_of_a_successful_hardened_start_up_hold_no_connection_detail():
    app, container, _ = hardened_app("production")
    with LogCapture() as logs, LogCapture("doc_extractor.api.composition") as composition_logs:
        with _TestClient(app):
            pass
    assert len(logs.records) >= 2 and "MongoDB connected" in logs.text                # the records exist: the absence checks mean something
    no_leak(logs.text + composition_logs.text)


# ---- publishing to the application state ----------------------------------------------------------------------------------

def test_publish_fills_empty_slots_and_remembers_what_it_published():
    state = SimpleNamespace(review_task_repo=None, api_key_repo=None)
    container = SimpleNamespace(review_task_repo=object(), api_key_repo=object())
    publish_started_ports(state, container)
    assert state.review_task_repo is container.review_task_repo and state.api_key_repo is container.api_key_repo


def test_publish_never_replaces_an_explicit_state_override_or_publishes_an_unconfigured_port():
    explicit = object()
    state = SimpleNamespace(review_task_repo=explicit, api_key_repo=None)
    publish_started_ports(state, SimpleNamespace(review_task_repo=object(), api_key_repo=None))
    assert state.review_task_repo is explicit and state.api_key_repo is None


def test_publish_replaces_what_an_earlier_start_up_published_when_the_container_was_rebuilt():
    """A second lifespan of an app-owned container: the old adapter is bound to a closed connection and must not linger."""
    state = SimpleNamespace(review_task_repo=None, api_key_repo=None)
    first = SimpleNamespace(review_task_repo=object(), api_key_repo=object())
    publish_started_ports(state, first)
    second = SimpleNamespace(review_task_repo=object(), api_key_repo=object())
    publish_started_ports(state, second)
    assert state.review_task_repo is second.review_task_repo and state.api_key_repo is second.api_key_repo


def test_publish_after_an_explicit_override_of_a_published_slot_keeps_the_override():
    state = SimpleNamespace(review_task_repo=None, api_key_repo=None)
    publish_started_ports(state, SimpleNamespace(review_task_repo=object(), api_key_repo=object()))
    override = object()
    state.api_key_repo = override                                                     # the operator replaces it afterwards
    publish_started_ports(state, SimpleNamespace(review_task_repo=object(), api_key_repo=object()))
    assert state.api_key_repo is override


def test_the_gate_names_no_in_memory_substitute_and_the_composition_stays_clean():
    composition = (ROOT / "src/doc_extractor/api/composition.py").read_text(encoding="utf-8")
    for forbidden in ("InMemoryReviewTask", "InMemoryApiKey", "mongomock", "fallback_to"):
        assert forbidden not in composition
