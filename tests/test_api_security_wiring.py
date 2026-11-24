"""
Security wiring of the running API (PRD SEC-001, SEC-002, SEC-003, SEC-008).

These tests go through the real application: JWT bearer tokens over HTTP, role-derived scopes on real routes, the
durable audit logger chosen by configuration, and the credential router. Nothing here calls a provider directly.
"""

import asyncio
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[api]')")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from doc_extractor.api.app import create_app
from doc_extractor.api.audit_chain import DurableAuditLogger
from doc_extractor.api.composition import ApplicationContainer
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.exceptions import AuthenticationRequiredError
from doc_extractor.api.providers.composite_auth import CompositeAuthenticationProvider
from doc_extractor.api.providers.jwt_auth import JwtAuthenticationProvider, issue_access_token
from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType

ISSUER = "https://identity.example.internal"
AUDIENCE = "doc-extractor-api"


@pytest.fixture(scope="module")
def key_pair(tmp_path_factory):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    directory = tmp_path_factory.mktemp("keys")
    (directory / "public.pem").write_bytes(public_pem)
    return private_pem, directory / "public.pem"


@pytest.fixture
def client(key_pair, tmp_path):
    _, public_path = key_pair
    settings = ApiSettings(
        app_env="development",
        audit_store_dir=str(tmp_path / "audit"),
        jwt_public_key_path=str(public_path),
        jwt_issuer=ISSUER,
        jwt_audience=AUDIENCE,
    )
    return TestClient(create_app(settings))


def _bearer(key_pair, roles, mfa=False, scope=None):
    private_pem, _ = key_pair
    token = issue_access_token(
        private_pem,
        issuer=ISSUER,
        audience=AUDIENCE,
        subject="user_1",
        tenant_id="tenant_a",
        roles=roles,
        scopes=scope or [],
        mfa_verified=mfa,
    )
    return {"Authorization": f"Bearer {token}"}


def _principal(key_pair, roles, scope=None, mfa=False):
    private_pem, public_path = key_pair
    provider = JwtAuthenticationProvider(public_path.read_bytes(), issuer=ISSUER, audience=AUDIENCE)
    token = issue_access_token(
        private_pem, issuer=ISSUER, audience=AUDIENCE, subject="u", tenant_id="tenant_a",
        roles=roles, scopes=scope or [], mfa_verified=mfa,
    )
    return asyncio.run(provider.authenticate(f"Bearer {token}"))


# -- JWT over real HTTP ----------------------------------------------------------------------------------------

def test_a_valid_analyst_token_reaches_the_document_routes(client, key_pair):
    response = client.get("/api/v1/documents", headers=_bearer(key_pair, ["analyst"]))
    assert response.status_code not in (401, 403, 404)  # 404 would mean the route is not mounted at all


def test_a_forged_bearer_is_refused_with_401(client):
    response = client.get("/api/v1/documents", headers={"Authorization": "Bearer a.b.c"})
    assert response.status_code == 401


def test_an_auditor_cannot_claim_review_tasks(client, key_pair):
    response = client.post("/api/v1/review-tasks/task_1/claim", headers=_bearer(key_pair, ["auditor"]))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN_SCOPE"


def test_a_reviewer_passes_the_claim_scope_check(client, key_pair):
    response = client.post("/api/v1/review-tasks/task_1/claim", headers=_bearer(key_pair, ["reviewer"]))
    assert response.status_code != 403


def test_every_non_health_route_refuses_anonymous_callers(client):
    """Route-guard completeness (SEC-003): every endpoint except health answers 401 without a credential.
    Endpoints come from the OpenAPI schema, which lists every mounted router."""
    import re

    schema = client.app.openapi()
    checked = []
    for path, operations in schema["paths"].items():
        if "/health" in path:
            continue
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        for method in operations:
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            response = client.request(method.upper(), concrete)
            assert response.status_code == 401, f"{method.upper()} {path} answered {response.status_code} without a credential"
            checked.append(f"{method.upper()} {path}")
    assert len(checked) >= 8, checked


# -- role to scope matrix --------------------------------------------------------------------------------------

def test_analyst_may_read_and_write_documents_but_not_claim_reviews(key_pair):
    scopes = _principal(key_pair, ["analyst"]).scopes
    assert scopes == {"documents:read", "documents:write"}


def test_explicit_scope_claim_narrows_role_scopes_and_never_widens_them(key_pair):
    narrowed = _principal(key_pair, ["analyst"], scope=["documents:read"]).scopes
    assert narrowed == {"documents:read"}
    widened = _principal(key_pair, ["auditor"], scope=["documents:write", "review_tasks:claim"]).scopes
    assert widened == set()


def test_schema_approver_is_read_only_on_documents(key_pair):
    assert _principal(key_pair, ["schema_approver"], scope=None, mfa=True).scopes == {"documents:read"}


# -- durable audit by configuration -------------------------------------------------------------------------------

def test_audit_store_dir_selects_the_durable_hash_chained_logger(tmp_path):
    container = ApplicationContainer(api_settings=ApiSettings(app_env="development", audit_store_dir=str(tmp_path)))
    try:
        assert isinstance(container.audit_logger, DurableAuditLogger)
        assert container.audit_logger.propagates_failures is True
    finally:
        container.shutdown()


def test_without_an_audit_store_no_audit_logger_is_invented(tmp_path):
    container = ApplicationContainer(api_settings=ApiSettings(app_env="development"))
    try:
        assert container.audit_logger is None
    finally:
        container.shutdown()


# -- credential router -----------------------------------------------------------------------------------------

class _Stub:
    def __init__(self, label):
        self.label = label
        self.calls = 0

    async def authenticate(self, credentials, **kwargs):
        self.calls += 1
        return ApiPrincipal(principal_id=self.label, tenant_id="t", principal_type=PrincipalType.USER)


def test_three_part_tokens_go_to_jwt_and_others_to_the_api_key_provider():
    jwt_stub, key_stub = _Stub("jwt"), _Stub("key")
    router = CompositeAuthenticationProvider(jwt_provider=jwt_stub, api_key_provider=key_stub)
    asyncio.run(router.authenticate("Bearer aaa.bbb.ccc"))
    asyncio.run(router.authenticate("Bearer de_live_" + "x" * 40))
    assert (jwt_stub.calls, key_stub.calls) == (1, 1)


def test_missing_credentials_fail_closed_in_the_router():
    router = CompositeAuthenticationProvider(jwt_provider=_Stub("jwt"), api_key_provider=_Stub("key"))
    with pytest.raises(AuthenticationRequiredError):
        asyncio.run(router.authenticate(""))


def test_router_needs_at_least_one_provider():
    with pytest.raises(ValueError):
        CompositeAuthenticationProvider()
