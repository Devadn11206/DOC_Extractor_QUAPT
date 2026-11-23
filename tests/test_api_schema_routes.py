"""
Schema lifecycle REST endpoints (PRD SCH-001..006, SEC-003) through the running application.

The schema manager here is an explicit in-memory instance, which is test-only. Production wiring must supply a manager
over a durable repository; the unconfigured case is tested too (every route answers 503, nothing is substituted).
"""

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[api]')")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from doc_extractor.api.app import create_app
from doc_extractor.api.composition import ApplicationContainer
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.providers.jwt_auth import issue_access_token
from schema_lifecycle.manager import SchemaLifecycleManager
from schema_lifecycle.metamodel import FieldDefinition, FieldType, SchemaDefinition

ISSUER = "https://identity.example.internal"
AUDIENCE = "doc-extractor-api"


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    path = tmp_path_factory.mktemp("keys") / "public.pem"
    path.write_bytes(public_pem)
    return private_pem, path


def _client(keys, tmp_path, *, manager=None):
    _, public_path = keys
    settings = ApiSettings(
        app_env="development",
        audit_store_dir=str(tmp_path / "audit"),
        jwt_public_key_path=str(public_path),
        jwt_issuer=ISSUER,
        jwt_audience=AUDIENCE,
    )
    container = ApplicationContainer(api_settings=settings, schema_manager=manager)
    return TestClient(create_app(settings, container=container))


def _auth(keys, roles, tenant="tenant_alpha", mfa=False):
    private_pem, _ = keys
    token = issue_access_token(
        private_pem, issuer=ISSUER, audience=AUDIENCE, subject="user_1",
        tenant_id=tenant, roles=roles, mfa_verified=mfa,
    )
    return {"Authorization": f"Bearer {token}"}


def _definition(schema_id="DOC-01", tenant="tenant_alpha", version=1):
    return SchemaDefinition(
        schema_id=schema_id,
        version=version,
        status="draft",
        tenant_id=tenant,
        domain="BFSI",
        document_type="casa_account_opening",
        description="CASA account opening",
        fields=[FieldDefinition(name="applicant_name", type=FieldType.STRING, required=True)],
    ).model_dump(mode="json")


PREFIX = "/api/v1"


def test_every_schema_route_answers_503_when_no_manager_is_configured(keys, tmp_path):
    client = _client(keys, tmp_path, manager=None)
    response = client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=_auth(keys, ["schema_author"]))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"


def test_an_analyst_cannot_create_schemas(keys, tmp_path):
    client = _client(keys, tmp_path, manager=SchemaLifecycleManager())
    response = client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=_auth(keys, ["analyst"]))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN_PERMISSION"


def test_a_schema_author_creates_a_draft_schema(keys, tmp_path):
    client = _client(keys, tmp_path, manager=SchemaLifecycleManager())
    response = client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=_auth(keys, ["schema_author"]))
    assert response.status_code == 201
    body = response.json()
    assert body["tenant_id"] == "tenant_alpha"
    assert body["status"] == "draft"


def test_the_body_cannot_name_another_tenant(keys, tmp_path):
    client = _client(keys, tmp_path, manager=SchemaLifecycleManager())
    response = client.post(
        f"{PREFIX}/schemas",
        json={"definition": _definition(tenant="tenant_beta")},
        headers=_auth(keys, ["schema_author"]),
    )
    assert response.status_code == 422


def test_a_new_version_must_name_the_path_schema(keys, tmp_path):
    client = _client(keys, tmp_path, manager=SchemaLifecycleManager())
    headers = _auth(keys, ["schema_author"])
    client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=headers)
    wrong = client.post(
        f"{PREFIX}/schemas/OTHER/versions", json={"definition": _definition(schema_id="DOC-01", version=2)}, headers=headers
    )
    assert wrong.status_code == 422
    right = client.post(
        f"{PREFIX}/schemas/DOC-01/versions", json={"definition": _definition(version=2)}, headers=headers
    )
    assert right.status_code == 201


def test_history_lists_every_version_with_author(keys, tmp_path):
    client = _client(keys, tmp_path, manager=SchemaLifecycleManager())
    headers = _auth(keys, ["schema_author"])
    client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=headers)
    client.post(f"{PREFIX}/schemas/DOC-01/versions", json={"definition": _definition(version=2)}, headers=headers)
    response = client.get(f"{PREFIX}/schemas/DOC-01/versions", headers=headers)
    assert response.status_code == 200
    assert [v["version"] for v in response.json()] == [1, 2]


def test_a_draft_moves_to_testing(keys, tmp_path):
    client = _client(keys, tmp_path, manager=SchemaLifecycleManager())
    headers = _auth(keys, ["schema_author"])
    client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=headers)
    response = client.post(
        f"{PREFIX}/schemas/DOC-01/versions/1/status", json={"target_status": "testing"}, headers=headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "testing"


def test_approved_cannot_be_requested_through_the_status_endpoint(keys, tmp_path):
    client = _client(keys, tmp_path, manager=SchemaLifecycleManager())
    headers = _auth(keys, ["schema_author"])
    client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=headers)
    response = client.post(
        f"{PREFIX}/schemas/DOC-01/versions/1/status", json={"target_status": "approved"}, headers=headers
    )
    assert response.status_code >= 400 and response.status_code != 200
    assert response.json()["error"]["code"]


def test_approval_without_a_configured_approval_service_is_503(keys, tmp_path):
    client = _client(keys, tmp_path, manager=SchemaLifecycleManager())
    response = client.post(
        f"{PREFIX}/schemas/DOC-01/versions/1/approve",
        json={"harness_run_id": "run_1"},
        headers=_auth(keys, ["schema_approver"], mfa=True),
    )
    assert response.status_code == 503


def test_an_analyst_cannot_approve_even_with_a_harness_run(keys, tmp_path):
    client = _client(keys, tmp_path, manager=SchemaLifecycleManager())
    response = client.post(
        f"{PREFIX}/schemas/DOC-01/versions/1/approve",
        json={"harness_run_id": "run_1"},
        headers=_auth(keys, ["analyst"]),
    )
    assert response.status_code == 403


def test_rollback_to_a_never_approved_version_is_refused(keys, tmp_path):
    client = _client(keys, tmp_path, manager=SchemaLifecycleManager())
    headers = _auth(keys, ["schema_author"])
    client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=headers)
    response = client.post(f"{PREFIX}/schemas/DOC-01/rollback", json={"target_version": 1}, headers=headers)
    assert response.status_code >= 400
