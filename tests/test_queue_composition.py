"""
Durable queue and idempotency composition tests (Team Member 3, Phase 3).

MOCKED: MongoDB is the in-memory fake of ``tests/mongo_fakes.py``, the broker is the double of ``tests/queue_fakes.py`` and Redis is
``fakeredis``. These tests prove the composition and lifecycle wiring: what is built and when, precedence of injected adapters, that
start-up fails closed and leaves nothing configured or open, and that secrets stay out of logs and errors. They do not prove behaviour
against a real broker, Redis or MongoDB (``tests/test_queue_integration.py``, ``tests/test_mongo_integration.py``).
"""

import asyncio
import logging

import pytest

pytest.importorskip("pymongo", reason="the 'mongo' extra provides the driver whose error types these tests map")
fakeredis = pytest.importorskip("fakeredis", reason="fakeredis stands in for Redis")
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from doc_extractor.api.app import create_app  # noqa: E402
from doc_extractor.api.composition import ApplicationContainer  # noqa: E402
from doc_extractor.api.config import ApiSettings, settings_from_environment  # noqa: E402
from doc_extractor.distributed_state import RedisIdempotencyStore, RedisLockProvider  # noqa: E402
from doc_extractor.idempotency import LocalIdempotencyStore  # noqa: E402
from doc_extractor.infrastructure.mongo import (  # noqa: E402
    MongoConnection,
    MongoSettings,
    PersistenceConfigurationError,
)
from doc_extractor.infrastructure.mongo.idempotency import MongoIdempotencyStore  # noqa: E402
from doc_extractor.infrastructure.mongo.processing import MongoProcessingLeaseStore  # noqa: E402
from doc_extractor.infrastructure.queue import BrokerConfigurationError, BrokerUnavailableError, RabbitMQQueue  # noqa: E402
from tests.mongo_fakes import FakeClient, LogCapture, factory_for  # noqa: E402
from tests.queue_fakes import FakeBroker  # noqa: E402

PASSWORD = "Mongo-Secret-Marker-5530"
BROKER_PASSWORD = "Broker-Secret-Marker-8841"
URI = f"mongodb://app:{PASSWORD}@mongo-0:27017/?replicaSet=rs0"
BROKER_URL = f"amqp://svc:{BROKER_PASSWORD}@rabbit-0:5672/vh"


def run(coro):
    return asyncio.run(coro)


def settings(**overrides):
    values = {"app_env": "development", "mongo_uri": URI, "mongo_database": "doc"}
    values.update(overrides)
    return ApiSettings(**values)


def container_with(client=None, *, api=None, **kwargs):
    client = client or FakeClient()
    api = api or settings()
    connection = MongoConnection(MongoSettings.from_api_settings(api), client_factory=factory_for(client))
    return ApplicationContainer(api_settings=api, mongo_connection=connection, **kwargs), client


# ---- settings ---------------------------------------------------------------------------------------------------------------

def test_no_broker_url_means_no_broker_and_the_queue_ports_stay_unconfigured():
    container, _ = container_with()
    assert container.broker_settings is None
    run(container.startup())
    assert container.queue_producer is None and container.queue_consumer is None and container.processing_lease_store is None
    run(container.ashutdown())


def test_a_broker_without_mongodb_is_refused_by_the_settings():
    with pytest.raises(ValueError) as raised:
        ApiSettings(app_env="development", broker_url=SecretStr(BROKER_URL))
    assert "mongodb_is_required_for_processing_state" in str(raised.value) and BROKER_PASSWORD not in str(raised.value)


def test_a_broker_url_that_breaks_the_policy_is_refused_by_the_settings_without_echoing_it():
    with pytest.raises(ValueError) as raised:
        settings(app_env="production", broker_url=SecretStr(BROKER_URL),
                 mongo_uri=f"mongodb://app:{PASSWORD}@m0:27017,m1:27017/?replicaSet=rs0&tls=true")
    assert "tls_is_required_outside_development" in str(raised.value)
    assert BROKER_PASSWORD not in str(raised.value) and "rabbit-0" not in str(raised.value)


def test_a_url_and_a_url_file_together_are_refused_by_the_settings(tmp_path):
    path = tmp_path / "url"
    path.write_text(BROKER_URL)
    with pytest.raises(ValueError):
        settings(broker_url=SecretStr(BROKER_URL), broker_url_file=str(path))


def test_the_broker_can_be_configured_from_the_environment_and_secrets_are_not_listed_back():
    values = settings_from_environment({"DOC_API_BROKER_URL": BROKER_URL, "DOC_API_BROKER_MAX_IN_FLIGHT": "7", "DOC_API_BROKER_QUEUE_PREFIX": "docs"})
    parsed = settings(**values)
    assert parsed.broker_max_in_flight == 7 and parsed.broker_queue_prefix == "docs"
    assert BROKER_PASSWORD not in repr(parsed) and BROKER_PASSWORD not in parsed.model_dump_json()


def test_the_container_reads_the_broker_url_file_at_construction(tmp_path):
    path = tmp_path / "url"
    path.write_text(BROKER_URL + "\n")
    container, _ = container_with(api=settings(broker_url_file=str(path)))
    assert container.broker_settings.url.get_secret_value() == BROKER_URL


# ---- idempotency --------------------------------------------------------------------------------------------------------------

def test_with_mongodb_the_idempotency_store_becomes_the_durable_one_bound_to_the_loop():
    container, client = container_with()
    assert isinstance(container.idempotency_store, LocalIdempotencyStore)
    run(container.startup())
    store = container.idempotency_store
    assert isinstance(store, MongoIdempotencyStore) and store._loop is not None
    run(container.ashutdown())
    assert store._loop is None                                          # shutdown: later synchronous calls fail closed


def test_an_injected_idempotency_store_is_never_replaced():
    injected = LocalIdempotencyStore("/tmp/unused-idempotency")
    container, _ = container_with(idempotency_store=injected)
    run(container.startup())
    assert container.idempotency_store is injected
    run(container.ashutdown())


def test_with_a_redis_url_redis_coordinates_the_lock_but_mongodb_holds_the_records():
    container, _ = container_with(api=settings(redis_url="redis://localhost:6379/0"))
    assert isinstance(container.idempotency_store, RedisIdempotencyStore)          # before start-up: the Redis-only store
    run(container.startup())
    store = container.idempotency_store
    assert isinstance(store, MongoIdempotencyStore) and isinstance(store._locks, RedisLockProvider)
    run(container.ashutdown())


def test_without_the_idempotency_index_start_up_fails_closed_and_leaves_nothing_configured_or_open():
    api = settings(mongo_ensure_indexes_on_startup=False)
    container, client = container_with(api=api)
    original = container.idempotency_store
    with pytest.raises(PersistenceConfigurationError) as raised:
        run(container.startup())
    # the audit gate runs first when its index is missing too: either gate refuses, with a fixed reason
    assert raised.value.details["reason"] in ("audit_sequence_index_is_missing", "idempotency_index_is_missing")
    assert container.idempotency_store is original and container.review_task_repo is None and container.audit_repo is None
    assert client.closed is True


# ---- the durable queue ----------------------------------------------------------------------------------------------------------

def test_a_configured_broker_is_connected_at_start_up_and_wired_as_producer_and_consumer():
    broker = FakeBroker()
    container, client = container_with(api=settings(broker_url=SecretStr(BROKER_URL)), broker_transport=broker)
    run(container.startup())
    queue = container.queue_producer
    assert isinstance(queue, RabbitMQQueue) and container.queue_consumer is queue
    assert broker.started is True and isinstance(container.processing_lease_store, MongoProcessingLeaseStore)
    run(container.ashutdown())
    assert broker.closed is True and client.closed is True


def test_the_transport_is_built_from_the_settings_when_none_is_injected(monkeypatch):
    built = {}
    broker = FakeBroker()

    def factory(broker_settings):
        built["settings"] = broker_settings
        return broker

    monkeypatch.setattr("doc_extractor.infrastructure.queue.transport.AioPikaTransport", factory)
    container, _ = container_with(api=settings(broker_url=SecretStr(BROKER_URL), broker_max_in_flight=4, broker_queue_prefix="docs"))
    run(container.startup())
    assert built["settings"].queue_prefix == "docs" and built["settings"].url.get_secret_value() == BROKER_URL
    assert container.queue_consumer._max_in_flight == 4
    run(container.ashutdown())


def test_injected_producer_and_consumer_win_and_the_broker_is_not_even_contacted():
    class Producer:
        async def publish(self, message, delay_seconds=0):
            return "x"

        async def publish_batch(self, messages):
            return []

    class Consumer:
        async def receive_messages(self, max_messages=10, visibility_timeout_seconds=60):
            return []

        async def ack(self, handle):
            return None

        async def nack(self, handle, requeue=True, backoff_seconds=0):
            return None

        async def extend_lease(self, handle, extension_seconds=60):
            return None

    broker = FakeBroker()
    producer, consumer = Producer(), Consumer()
    container, _ = container_with(api=settings(broker_url=SecretStr(BROKER_URL)), broker_transport=broker,
                                  queue_producer=producer, queue_consumer=consumer)
    run(container.startup())
    assert container.queue_producer is producer and container.queue_consumer is consumer
    assert broker.started is False and container.processing_lease_store is None
    run(container.ashutdown())


def test_an_injected_producer_stays_and_only_the_missing_consumer_is_filled_in():
    class Producer:
        async def publish(self, message, delay_seconds=0):
            return "x"

        async def publish_batch(self, messages):
            return []

    producer = Producer()
    container, _ = container_with(api=settings(broker_url=SecretStr(BROKER_URL)), broker_transport=FakeBroker(), queue_producer=producer)
    run(container.startup())
    assert container.queue_producer is producer and isinstance(container.queue_consumer, RabbitMQQueue)
    run(container.ashutdown())


def test_an_unreachable_broker_stops_start_up_and_leaves_nothing_configured_or_open():
    broker = FakeBroker()
    broker.fail["start"] = BrokerUnavailableError("broker_connect_failed")
    container, client = container_with(api=settings(broker_url=SecretStr(BROKER_URL)), broker_transport=broker)
    original_store = container.idempotency_store
    with pytest.raises(BrokerUnavailableError):
        run(container.startup())
    assert container.queue_producer is None and container.queue_consumer is None and container.processing_lease_store is None
    assert container.review_task_repo is None and container.audit_repo is None and container.idempotency_store is original_store
    assert client.closed is True and broker.started is False


def test_without_the_job_index_the_broker_is_never_started():
    api = settings(broker_url=SecretStr(BROKER_URL), mongo_ensure_indexes_on_startup=False)
    broker = FakeBroker()
    container, client = container_with(api=api, broker_transport=broker)
    # migrate every index EXCEPT the job index: the gates of audit and idempotency pass, the queue gate refuses
    from doc_extractor.infrastructure.mongo import DEFAULT_REGISTRY, ensure_indexes
    from doc_extractor.infrastructure.mongo.indexes import PROCESSING_JOBS

    async def migrate():
        await ensure_indexes(client["doc"], DEFAULT_REGISTRY, only_collections=[c for c in DEFAULT_REGISTRY.collections() if c != PROCESSING_JOBS])

    run(migrate())
    with pytest.raises(PersistenceConfigurationError) as raised:
        run(container.startup())
    assert raised.value.details["reason"] == "processing_job_index_is_missing"
    assert broker.started is False and container.queue_producer is None and client.closed is True


def test_a_broker_without_mongodb_in_the_container_is_a_configuration_error():
    container = ApplicationContainer(api_settings=ApiSettings(app_env="development"), broker_transport=FakeBroker())
    with pytest.raises(BrokerConfigurationError) as raised:
        run(container.startup())
    assert raised.value.details["reason"] == "mongodb_is_required_for_processing_state"


def test_start_up_and_failures_never_log_a_credential():
    broker = FakeBroker()
    broker.fail["start"] = BrokerUnavailableError("broker_connect_failed")
    container, _ = container_with(api=settings(broker_url=SecretStr(BROKER_URL)), broker_transport=broker)
    with LogCapture("doc_extractor") as logs, pytest.raises(BrokerUnavailableError) as raised:
        run(container.startup())
    text = logs.text + repr(raised.value) + str(raised.value.details)
    assert BROKER_PASSWORD not in text and PASSWORD not in text and "rabbit-0" not in text


# ---- the application lifespan --------------------------------------------------------------------------------------------------

def test_the_lifespan_publishes_the_queue_ports_and_the_durable_store_and_closes_everything():
    broker = FakeBroker()
    api = settings(broker_url=SecretStr(BROKER_URL))
    container, client = container_with(api=api, broker_transport=broker)
    app = create_app(api, container)
    with TestClient(app):
        assert isinstance(app.state.queue_producer, RabbitMQQueue) and app.state.queue_consumer is app.state.queue_producer
        assert isinstance(app.state.idempotency_store, MongoIdempotencyStore) and broker.started
    assert not broker.closed and not client.closed                    # a caller-owned container is the caller's to shut down
    run(container.ashutdown())
    assert broker.closed and client.closed


def test_an_application_owned_container_closes_the_broker_and_the_database_at_shutdown(monkeypatch):
    broker = FakeBroker()
    client = FakeClient()
    monkeypatch.setattr("doc_extractor.infrastructure.mongo.connection.default_client_factory", factory_for(client))
    monkeypatch.setattr("doc_extractor.infrastructure.queue.transport.AioPikaTransport", lambda broker_settings: broker)
    app = create_app(settings(broker_url=SecretStr(BROKER_URL)))
    with TestClient(app):
        assert broker.started and isinstance(app.state.queue_producer, RabbitMQQueue)
    assert broker.closed and client.closed


def test_an_explicit_application_state_port_is_not_overridden_by_the_lifespan():
    broker = FakeBroker()
    api = settings(broker_url=SecretStr(BROKER_URL))
    container, _ = container_with(api=api, broker_transport=broker)
    app = create_app(api, container)
    sentinel = object()
    app.state.queue_producer = sentinel
    with TestClient(app):
        assert app.state.queue_producer is sentinel and isinstance(app.state.queue_consumer, RabbitMQQueue)


def test_the_durable_store_works_from_a_worker_thread_while_the_application_runs(tmp_path):
    """As FastAPI runs a synchronous route: a worker thread calls the store, whose connection lives on the application loop."""
    api = settings(storage_base_dir=str(tmp_path / "storage"))
    container, client = container_with(api=api)
    app = create_app(api, container)
    with TestClient(app):
        store = container.idempotency_store
        assert isinstance(store, MongoIdempotencyStore)
        from datetime import datetime, timezone

        from doc_extractor.models import IdempotencyRecord

        now = datetime.now(timezone.utc).timestamp()
        record = IdempotencyRecord("k1", "tenant_a", "fp", "doc_1", {"document_id": "doc_1"}, now, now + 60)
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(1) as pool:                       # a worker thread, as FastAPI runs a sync route
            pool.submit(store.save_record, record).result(5)
            assert pool.submit(store.get_record, "tenant_a", "k1").result(5) == record
