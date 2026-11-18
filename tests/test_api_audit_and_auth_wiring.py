"""
Mandatory audit must never silently downgrade to log-only, and an unconfigured authentication provider is a typed 503.

- ``get_auth_provider`` / ``get_audit_logger`` raise a typed 503 instead of inventing a provider or a log-only logger.
- ``ApiKeyService`` requires an audit logger. If the audit record of a key creation cannot be written the new key is revoked
  and its secret is never returned; a failed audit of a revocation is reported (the key stays revoked).
"""

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[dev,api]')")

import logging

from fastapi import Depends
from fastapi.testclient import TestClient

from doc_extractor.api import dependencies as deps
from doc_extractor.api.app import create_app
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.exceptions import AuditUnavailableError, PortNotConfiguredError, get_status_code_for_exception
from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
from doc_extractor.api.providers.api_key_auth import ApiKeyAuthenticationProvider
from doc_extractor.api.services.api_key_service import ApiKeyService
from doc_extractor.exceptions import ConfigurationError
from tests.test_api_key_service import MockApiKeyRepository, MockAuditLogger, async_test


class FailingAuditLogger:
    """An audit sink that is down. Its error text contains a marker that must never reach a response or a log."""

    async def log_event(self, event):
        raise RuntimeError("audit db down (password=hunter2)")

    async def log_action(self, **event):
        raise RuntimeError("audit db down (password=hunter2)")


@pytest.fixture
def admin() -> ApiPrincipal:
    return ApiPrincipal(principal_id="user_admin", tenant_id="tenant_alpha", principal_type=PrincipalType.USER,
                        roles={"admin"}, scopes={"api_keys:read", "api_keys:write", "documents:read"})


def build_client(*, repo=None, audit=None, provider=None):
    app = create_app(ApiSettings(app_env="development"))
    app.state.api_key_repo, app.state.audit_logger, app.state.auth_provider = repo, audit, provider

    @app.get("/probe/principal")
    async def principal(p=Depends(deps.get_current_principal)):
        return {"ok": True}

    @app.get("/probe/audit")
    async def audit_logger(a=Depends(deps.get_audit_logger)):
        return {"logger": type(a).__name__}

    @app.get("/probe/service")
    async def service(s=Depends(deps.get_api_key_service)):
        return {"service": type(s).__name__}

    return app, TestClient(app, raise_server_exceptions=False)


H = {"Authorization": "Bearer de_live_x"}


# -- typed 503 for authentication and audit ---------------------------------------------------------

def test_no_provider_and_no_key_repository_is_a_typed_503_naming_the_authentication_provider():
    app, client = build_client()
    response = client.get("/probe/principal", headers=H)
    assert response.status_code == 503
    assert response.json()["error"] == {**response.json()["error"], "code": "SERVICE_UNAVAILABLE", "details": {"port": "AuthenticationProvider"}}
    assert app.state.auth_provider is None                               # nothing was invented


def test_a_key_repository_without_an_audit_logger_does_not_get_a_silent_log_only_provider():
    app, client = build_client(repo=MockApiKeyRepository())
    response = client.get("/probe/principal", headers=H)
    assert response.status_code == 503 and response.json()["error"]["details"] == {"port": "AuditLogger"}
    assert app.state.auth_provider is None                               # no provider was constructed with a fallback logger


def test_with_a_repository_and_an_explicit_audit_logger_the_provider_uses_that_logger():
    audit, repo = MockAuditLogger(), MockApiKeyRepository()
    app, client = build_client(repo=repo, audit=audit)
    client.get("/probe/principal", headers=H)                            # a malformed key: 401, but the provider now exists
    provider = app.state.auth_provider
    assert isinstance(provider, ApiKeyAuthenticationProvider) and provider._audit_logger is audit and provider._repository is repo


def test_an_explicitly_registered_provider_is_used_as_is():
    class Provider:
        async def authenticate(self, credentials, **kwargs):
            return ApiPrincipal(principal_id="p", tenant_id="tenant_alpha")
    app, client = build_client(provider=Provider())
    assert client.get("/probe/principal", headers=H).status_code == 200


def test_an_unconfigured_audit_logger_is_a_typed_503_not_a_log_only_fallback():
    _, client = build_client()
    response = client.get("/probe/audit")
    assert response.status_code == 503 and response.json()["error"]["details"] == {"port": "AuditLogger"}


def test_a_configured_audit_logger_is_handed_over_unchanged():
    audit = MockAuditLogger()
    app, client = build_client(audit=audit)
    assert client.get("/probe/audit").json() == {"logger": "MockAuditLogger"}


def test_the_key_service_dependency_needs_both_the_repository_and_the_audit_logger():
    _, client = build_client()
    assert client.get("/probe/service").json()["error"]["details"] == {"port": "ApiKeyRepository"}
    _, client = build_client(repo=MockApiKeyRepository())
    assert client.get("/probe/service").json()["error"]["details"] == {"port": "AuditLogger"}
    _, client = build_client(repo=MockApiKeyRepository(), audit=MockAuditLogger())
    assert client.get("/probe/service").json() == {"service": "ApiKeyService"}


def test_port_not_configured_error_is_what_these_paths_raise():
    class State:
        api_key_repo = None
        auth_provider = None
        audit_logger = None

    class Req:
        app = type("A", (), {"state": State})()

    with pytest.raises(PortNotConfiguredError) as exc:
        deps.get_auth_provider(Req())
    assert exc.value.port == "AuthenticationProvider" and get_status_code_for_exception(exc.value) == 503
    with pytest.raises(PortNotConfiguredError):
        deps.get_audit_logger(Req())


# -- the key service: mandatory audit ---------------------------------------------------------------

def test_the_key_service_cannot_be_built_without_an_audit_logger():
    with pytest.raises(TypeError):
        ApiKeyService(repository=MockApiKeyRepository())                  # type: ignore[call-arg]
    with pytest.raises(ConfigurationError):
        ApiKeyService(repository=MockApiKeyRepository(), audit_logger=None)  # type: ignore[arg-type]


@async_test
async def test_when_the_audit_write_fails_the_new_key_is_revoked_and_no_secret_is_returned(admin, caplog):
    repo = MockApiKeyRepository()
    service = ApiKeyService(repository=repo, audit_logger=FailingAuditLogger())
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(AuditUnavailableError) as exc:
            await service.create_api_key(creator=admin, name="k", scopes=["documents:read"], roles=["integrator"])
    assert exc.value.details == {"operation": "api_key.created", "effect": "rolled_back"} and exc.value.code == "AUDIT_UNAVAILABLE"
    keys = await repo.list_keys("tenant_alpha")
    assert len(keys) == 1 and keys[0].revoked_at is not None and keys[0].is_active is False     # no usable credential exists
    assert "hunter2" not in str(exc.value) and "hunter2" not in caplog.text                       # the sink's error text is not leaked or logged


@async_test
async def test_a_failed_rollback_is_logged_for_manual_revocation_and_still_reports_the_audit_failure(admin, caplog):
    class RevokeBroken(MockApiKeyRepository):
        async def revoke(self, tenant_id, key_id):
            raise RuntimeError("revoke down")

    service = ApiKeyService(repository=RevokeBroken(), audit_logger=FailingAuditLogger())
    with caplog.at_level(logging.ERROR):
        with pytest.raises(AuditUnavailableError):
            await service.create_api_key(creator=admin, name="k", scopes=["documents:read"], roles=["integrator"])
    assert "revoke it manually" in caplog.text


@async_test
async def test_when_the_audit_of_a_revocation_fails_the_key_stays_revoked_and_the_caller_is_told(admin):
    repo, audit = MockApiKeyRepository(), MockAuditLogger()
    created = await ApiKeyService(repository=repo, audit_logger=audit).create_api_key(
        creator=admin, name="k", scopes=["documents:read"], roles=["integrator"])
    failing = ApiKeyService(repository=repo, audit_logger=FailingAuditLogger())
    with pytest.raises(AuditUnavailableError) as exc:
        await failing.revoke_api_key(admin, created.key_id)
    assert exc.value.details == {"operation": "api_key.revoked", "effect": "applied"}
    stored = await repo.get_by_id("tenant_alpha", created.key_id)
    assert stored.revoked_at is not None                                  # a reduction of access is never rolled back


@async_test
async def test_the_audited_happy_path_is_unchanged(admin):
    audit, repo = MockAuditLogger(), MockApiKeyRepository()
    service = ApiKeyService(repository=repo, audit_logger=audit)
    created = await service.create_api_key(creator=admin, name="k", scopes=["documents:read"], roles=["integrator"])
    assert await service.revoke_api_key(admin, created.key_id) is True
    assert [e["action"] for e in audit.events] == ["api_key.created", "api_key.revoked"]
    assert created.raw_secret and "raw_secret" not in str(audit.events)


def test_audit_unavailable_is_a_typed_503_over_http():
    app = create_app(ApiSettings(app_env="development"))

    @app.get("/probe/audit-down")
    def audit_down():
        raise AuditUnavailableError("api_key.created")

    response = TestClient(app, raise_server_exceptions=False).get("/probe/audit-down")
    error = response.json()["error"]
    assert response.status_code == 503 and error["code"] == "AUDIT_UNAVAILABLE"
    assert error["details"] == {"operation": "api_key.created", "effect": "rolled_back"}
