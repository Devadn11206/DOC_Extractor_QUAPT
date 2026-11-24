"""
AMQP transport tests (Team Member 3, Phase 3).

MOCKED: the connection, channel and queues are in-memory stand-ins for ``aio-pika``'s objects (the real ``aio_pika.Message`` and
``DeliveryMode`` are used). These tests prove HOW the transport talks to the client library: the queue arguments it declares, the
persistence, mandatory and confirm flags it publishes with, the error mapping and the secrecy of the URL. They do NOT prove that a
RabbitMQ accepts those arguments or confirms those publishes: ``tests/test_queue_integration.py`` does, against a real broker, and
skips without one.
"""

import asyncio

import pytest

aio_pika = pytest.importorskip("aio_pika", reason="the 'queue' extra provides the AMQP client")
from pydantic import SecretStr  # noqa: E402

from doc_extractor.infrastructure.queue import (  # noqa: E402
    AioPikaTransport,
    BrokerConfigurationError,
    BrokerPublishError,
    BrokerSettings,
    BrokerUnavailableError,
    BrokerTransport,
    QueueTopology,
)
from doc_extractor.infrastructure.queue.transport import queue_arguments  # noqa: E402
from tests.mongo_fakes import LogCapture  # noqa: E402

URL = "amqp://svc:Sup3rS3cretPW@broker.internal:5672/vh"


def run(coro):
    return asyncio.run(coro)


class DeliveryError(Exception):
    """Named like aio-pika's: the broker answered with a nack or returned the message as unroutable."""


class RawMessage:
    def __init__(self, body=b"{}", headers=None, redelivered=False):
        self.body, self.headers, self.redelivered = body, headers, redelivered
        self.acked = self.rejected = None

    async def ack(self):
        self.acked = True

    async def reject(self, requeue=False):
        self.rejected = requeue


class FakeQueue:
    def __init__(self, name, arguments):
        self.name, self.arguments, self.items, self.get_kwargs = name, arguments, [], None
        self.error = None

    async def get(self, **kwargs):
        self.get_kwargs = kwargs
        if self.error:
            raise self.error
        return self.items.pop(0) if self.items else None


class FakeExchange:
    def __init__(self):
        self.published, self.error = [], None

    async def publish(self, message, routing_key, mandatory, timeout):
        if self.error:
            raise self.error
        self.published.append((message, routing_key, mandatory, timeout))


class FakeChannel:
    def __init__(self):
        self.default_exchange = FakeExchange()
        self.queues, self.qos, self.declare_error = {}, None, None
        self.is_closed = False

    async def set_qos(self, prefetch_count):
        self.qos = prefetch_count

    async def declare_queue(self, name, durable, arguments):
        if self.declare_error:
            raise self.declare_error
        assert durable is True
        self.queues[name] = FakeQueue(name, arguments)
        return self.queues[name]


class FakeConnection:
    def __init__(self):
        self.channel_obj, self.confirms, self.closed_calls, self.is_closed = FakeChannel(), None, 0, False

    async def channel(self, publisher_confirms):
        self.confirms = publisher_confirms
        return self.channel_obj

    async def close(self):
        self.closed_calls += 1
        self.is_closed = True


def settings(**values):
    return BrokerSettings(url=SecretStr(URL), **values)


def transport_with(connection=None, **values):
    connection = connection or FakeConnection()
    seen = {}

    async def connect(url, timeout):
        seen["url"], seen["timeout"] = url, timeout
        return connection

    return AioPikaTransport(settings(**values), connect=connect), connection, seen


def started(**values):
    transport, connection, seen = transport_with(**values)
    run(transport.start())
    return transport, connection, seen


# ---- topology and declarations ------------------------------------------------------------------------------------------------

def test_the_transport_satisfies_the_broker_protocol():
    transport, _, _ = transport_with()
    assert isinstance(transport, BrokerTransport)


def test_topology_names_and_tier_rounding():
    topology = QueueTopology("p", (1, 5, 60))
    assert (topology.main, topology.dead, topology.wait_queue(5)) == ("p.jobs", "p.jobs.dead", "p.jobs.wait.5s")
    assert [topology.tier_for(x) for x in (0.2, 1, 1.01, 5, 59, 60, 61, 10**6)] == [1, 1, 5, 5, 60, 60, 60, 60]       # up, never early
    assert topology.all_queues() == ("p.jobs", "p.jobs.dead", "p.jobs.wait.1s", "p.jobs.wait.5s", "p.jobs.wait.60s")


def test_start_declares_every_queue_durable_and_quorum_with_the_documented_arguments():
    transport, connection, seen = started(queue_prefix="docs", delay_tiers_seconds=(1, 60), delivery_limit=7, max_in_flight=12)
    queues = connection.channel_obj.queues
    assert set(queues) == {"docs.jobs", "docs.jobs.dead", "docs.jobs.wait.1s", "docs.jobs.wait.60s"}
    assert all(q.arguments["x-queue-type"] == "quorum" for q in queues.values())
    assert queues["docs.jobs"].arguments == {
        "x-queue-type": "quorum", "x-delivery-limit": 7, "x-dead-letter-exchange": "", "x-dead-letter-routing-key": "docs.jobs.dead"}
    assert queues["docs.jobs.dead"].arguments == {"x-queue-type": "quorum"}
    assert queues["docs.jobs.wait.60s"].arguments == {
        "x-queue-type": "quorum", "x-message-ttl": 60_000, "x-dead-letter-exchange": "", "x-dead-letter-routing-key": "docs.jobs",
        "x-dead-letter-strategy": "at-least-once", "x-overflow": "reject-publish"}
    assert connection.confirms is True and connection.channel_obj.qos == 12                  # publisher confirms are on
    assert seen["url"] == URL and seen["timeout"] == 10.0


def test_start_is_idempotent():
    transport, connection, _ = started()
    run(transport.start())
    assert len(connection.channel_obj.queues) == 9


def test_queue_arguments_are_pure_data():
    topology = QueueTopology("p", (1, 5))
    assert queue_arguments(topology, "p.jobs.wait.5s", 3)["x-message-ttl"] == 5000


# ---- start-up failures --------------------------------------------------------------------------------------------------------

def test_a_failed_connection_is_a_typed_error_without_the_url_or_driver_text_and_closes_nothing_open():
    async def connect(url, timeout):
        raise ConnectionRefusedError(f"cannot reach {url}")

    transport = AioPikaTransport(settings(), connect=connect)
    with LogCapture("doc_extractor.queue") as logs, pytest.raises(BrokerUnavailableError) as raised:
        run(transport.start())
    text = repr(raised.value) + str(raised.value.details) + logs.text
    assert raised.value.details["reason"] == "broker_connect_failed"
    assert "Sup3rS3cretPW" not in text and "broker.internal" not in text and "ConnectionRefusedError" in logs.text
    assert run(transport.is_healthy()) is False


def test_a_connection_that_times_out_is_a_typed_error():
    async def connect(url, timeout):
        await asyncio.sleep(30)

    transport = AioPikaTransport(settings(connect_timeout_seconds=1.0), connect=connect)

    async def go():
        with pytest.raises(BrokerUnavailableError):
            await asyncio.wait_for(transport.start(), timeout=5)

    run(go())


def test_a_refused_declaration_closes_the_connection_and_says_so():
    connection = FakeConnection()
    refused = type("ChannelPreconditionFailed", (aio_pika.exceptions.AMQPException,), {})("PRECONDITION_FAILED amqp://svc:pw@h")
    connection.channel_obj.declare_error = refused
    transport, _, _ = transport_with(connection)
    with pytest.raises(BrokerUnavailableError) as raised:
        run(transport.start())
    assert raised.value.details["reason"] == "broker_topology_was_refused" and "pw" not in repr(raised.value)
    assert connection.closed_calls == 1 and run(transport.is_healthy()) is False


def test_without_the_client_library_start_is_a_configuration_error(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "aio_pika", None)
    transport = AioPikaTransport(settings(), connect=lambda *a, **k: None)
    with pytest.raises(BrokerConfigurationError) as raised:
        run(transport.start())
    assert raised.value.details["reason"] == "amqp_client_is_not_installed"


def test_operations_before_start_fail_closed():
    transport, _, _ = transport_with()

    async def go():
        with pytest.raises(BrokerUnavailableError):
            await transport.publish("q", b"{}", message_id="m", correlation_id=None, headers={})
        with pytest.raises(BrokerUnavailableError):
            await transport.get("q")

    run(go())


# ---- publish ---------------------------------------------------------------------------------------------------------------------

def test_a_message_is_published_persistent_mandatory_and_with_its_identity_and_headers():
    transport, connection, _ = started(publish_timeout_seconds=7.0)
    run(transport.publish("p.jobs", b'{"a":1}', message_id="msg_1", correlation_id="corr_1", headers={"tenant_id": "tenant_a", "x-schema-version": 1}))
    [(message, routing_key, mandatory, timeout)] = connection.channel_obj.default_exchange.published
    assert routing_key == "p.jobs" and mandatory is True and timeout == 7.0
    assert message.body == b'{"a":1}' and message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert message.message_id == "msg_1" and message.correlation_id == "corr_1" and message.content_type == "application/json"
    assert message.headers["tenant_id"] == "tenant_a"


def test_a_nack_or_an_unroutable_return_is_a_publish_error_and_a_lost_connection_is_unavailable():
    transport, connection, _ = started()
    exchange = connection.channel_obj.default_exchange
    exchange.error = DeliveryError("NACK amqp://svc:Sup3rS3cretPW@h")

    async def publish():
        await transport.publish("q", b"{}", message_id="m", correlation_id=None, headers={})

    with pytest.raises(BrokerPublishError) as refused:
        run(publish())
    assert refused.value.details["reason"] == "publish_refused_by_the_broker" and "Sup3rS3cretPW" not in repr(refused.value)
    for error in (ConnectionResetError("reset"), asyncio.TimeoutError(), aio_pika.exceptions.AMQPException("closed amqp://u:pw@h")):
        exchange.error = error
        with pytest.raises(BrokerUnavailableError) as unavailable:
            run(publish())
        assert "pw" not in repr(unavailable.value)


def test_a_programming_error_is_not_disguised_as_a_broker_failure():
    transport, connection, _ = started()
    connection.channel_obj.default_exchange.error = KeyError("bug")
    with pytest.raises(KeyError):
        run(transport.publish("q", b"{}", message_id="m", correlation_id=None, headers={}))


def test_an_oversized_body_is_refused_before_it_reaches_the_broker():
    transport, connection, _ = started()
    with pytest.raises(BrokerPublishError):
        run(transport.publish("q", b"x" * (64 * 1024 + 1), message_id="m", correlation_id=None, headers={}))
    assert connection.channel_obj.default_exchange.published == []


# ---- get, ack, reject -----------------------------------------------------------------------------------------------------------

def test_get_returns_the_next_delivery_with_body_headers_and_redelivery_flag_or_none():
    transport, connection, _ = started(publish_timeout_seconds=3.0)
    queue = connection.channel_obj.queues["doc_extractor.jobs"]
    assert run(transport.get("doc_extractor.jobs")) is None
    queue.items.append(RawMessage(b"body", {"tenant_id": "tenant_a"}, True))
    delivery = run(transport.get("doc_extractor.jobs"))
    assert (delivery.body, dict(delivery.headers), delivery.redelivered) == (b"body", {"tenant_id": "tenant_a"}, True)
    assert queue.get_kwargs == {"no_ack": False, "fail": False, "timeout": 3.0}              # explicit acknowledgement, no exception when empty
    queue.items.append(RawMessage(b"x", None, False))
    assert dict(run(transport.get("doc_extractor.jobs")).headers) == {}


def test_get_failures_are_typed_and_an_unknown_queue_is_refused():
    transport, connection, _ = started()
    connection.channel_obj.queues["doc_extractor.jobs"].error = ConnectionResetError("reset amqp://u:pw@h")
    with pytest.raises(BrokerUnavailableError) as raised:
        run(transport.get("doc_extractor.jobs"))
    assert "pw" not in repr(raised.value)
    with pytest.raises(BrokerUnavailableError):
        run(transport.get("not.declared"))


def test_ack_and_reject_act_on_the_raw_message_and_map_failures():
    transport, connection, _ = started()
    queue = connection.channel_obj.queues["doc_extractor.jobs"]
    raw = RawMessage()
    queue.items.append(raw)
    delivery = run(transport.get("doc_extractor.jobs"))
    run(transport.ack(delivery))
    assert raw.acked is True
    other = RawMessage()
    queue.items.append(other)
    run(transport.reject(run(transport.get("doc_extractor.jobs")), requeue=True))
    assert other.rejected is True

    class Broken(RawMessage):
        async def ack(self):
            raise ConnectionResetError("channel closed")

    queue.items.append(Broken())
    broken = run(transport.get("doc_extractor.jobs"))
    with pytest.raises(BrokerUnavailableError):
        run(transport.ack(broken))
    with pytest.raises(BrokerUnavailableError):
        run(transport.ack(object()))                                                         # a delivery from another transport


# ---- lifecycle -----------------------------------------------------------------------------------------------------------------

def test_close_is_idempotent_and_health_follows_the_connection():
    transport, connection, _ = started()
    assert run(transport.is_healthy()) is True
    connection.channel_obj.is_closed = True
    assert run(transport.is_healthy()) is False                                              # a channel the robust layer is restoring
    connection.channel_obj.is_closed = False
    run(transport.close())
    run(transport.close())
    assert connection.closed_calls == 1 and run(transport.is_healthy()) is False


def test_a_close_that_fails_never_raises():
    transport, connection, _ = started()

    async def broken():
        raise ConnectionResetError("x")

    connection.close = broken
    run(transport.close())
