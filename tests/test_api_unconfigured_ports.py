"""
An infrastructure port with no adapter (repository, queue, ...) means the service cannot serve the
request yet: HTTP 503 with the existing SERVICE_UNAVAILABLE code and the API-010 envelope, not a bare
NotImplementedError surfacing as a generic 500. Team Member 3's adapters are not implemented here, so
these ports are deliberately unconfigured.
"""

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[api]')")

from fastapi import Depends
from fastapi.testclient import TestClient

from doc_extractor.api import dependencies as deps
from doc_extractor.api.app import create_app
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.exceptions import PortNotConfiguredError, get_status_code_for_exception

PORTS = {
    "DocumentMetadataRepository": ("document_metadata_repo", deps.get_document_metadata_repository),
    "ReviewTaskRepository": ("review_task_repo", deps.get_review_task_repository),
    "ApiKeyRepository": ("api_key_repo", deps.get_api_key_repository),
    "WebhookSubscriptionRepository": ("webhook_subscription_repo", deps.get_webhook_subscription_repository),
    "WebhookDeliveryRepository": ("webhook_delivery_repo", deps.get_webhook_delivery_repository),
    "AuditRepository": ("audit_repo", deps.get_audit_repository),
    "QueueProducer": ("queue_producer", deps.get_queue_producer),
    "QueueConsumer": ("queue_consumer", deps.get_queue_consumer),
}


def _app():
    app = create_app(ApiSettings(app_env="development"))
    for name, (_state, provider) in PORTS.items():
        def _route(port=Depends(provider)):
            return {"configured": True}
        app.get(f"/probe/{name}")(_route)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


@pytest.mark.parametrize("name", PORTS)
def test_an_unconfigured_port_answers_503_with_a_typed_error(client, name):
    response = client.get(f"/probe/{name}")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"            # the existing code; nothing is renamed
    assert error["details"] == {"port": name}
    assert set(error) == {"code", "message", "request_id", "details"}
    assert response.headers["X-Request-ID"] == error["request_id"]


@pytest.mark.parametrize("name", PORTS)
def test_the_error_is_not_a_leaked_notimplementederror_or_500(client, name):
    response = client.get(f"/probe/{name}")
    assert response.status_code != 500
    assert "NotImplemented" not in response.text and "Traceback" not in response.text


@pytest.mark.parametrize("name", PORTS)
def test_a_configured_port_is_served_normally(name):
    app = _app()
    state_attr = PORTS[name][0]
    setattr(app.state, state_attr, object())                  # any adapter object: the dependency only hands it over
    assert TestClient(app, raise_server_exceptions=False).get(f"/probe/{name}").status_code == 200


def test_the_typed_error_is_a_domain_error_with_status_503():
    exc = PortNotConfiguredError("QueueProducer")
    assert exc.status_code == 503 and exc.port == "QueueProducer"
    assert get_status_code_for_exception(exc) == 503
    assert not isinstance(exc, NotImplementedError)


def test_the_message_names_the_port_but_no_document_content(client):
    message = client.get("/probe/QueueProducer").json()["error"]["message"]
    assert "QueueProducer" in message


def test_an_unconfigured_authentication_provider_keeps_its_existing_failure_mode():
    """Not part of this change: the auth provider is not an infrastructure port and stays 500 AUTH_PROVIDER_UNCONFIGURED."""
    app = create_app(ApiSettings(app_env="development"))

    @app.get("/probe/guarded")
    async def guarded(principal=Depends(deps.get_current_principal)):
        return {}

    response = TestClient(app, raise_server_exceptions=False).get("/probe/guarded", headers={"Authorization": "Bearer x"})
    assert (response.status_code, response.json()["error"]["code"]) == (500, "AUTH_PROVIDER_UNCONFIGURED")
