"""
SEC-006 at the API's principal boundary (needs the optional ``api`` extra). The storage-side isolation tests need
no FastAPI and live in ``tests/test_tenant_isolation.py``.

Reproduced regression: ``ApiPrincipal(tenant_id="tenant_a/../tenant_b")`` was accepted. The guard rejects only values
that can leave their directory; it does NOT define a tenant-ID format, so ordinary identifiers keep working.
"""

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[api]')")

import asyncio

from pydantic import ValidationError

from doc_extractor.api.exceptions import InvalidTokenError
from doc_extractor.api.ports.authentication import ApiPrincipal, TenantContext
from doc_extractor.api.providers.api_key_auth import ApiKeyAuthenticationProvider
from doc_extractor.api.token_core import create_api_key_record, generate_api_secret
from tests.test_api_key_service import MockApiKeyRepository, MockAuditLogger

UNSAFE = [
    "", ".", "..", "tenant_a/../tenant_b", "../x", "a/b", "/abs", "a\\b", "..\\x",
    "a\x00b", "a\nb", "a\tb", "a\x7fb", "x" * 400,
]
# Valid tenant IDs of any style stay valid: the guard is not a format rule.
ORDINARY = ["tenant_alpha", "default_tenant", "t-1", "org:123", "Acme Corp", "a..b", "...", "tenant.alpha", "测试"]


# -- principal boundary ---------------------------------------------------------------------------

@pytest.mark.parametrize("tenant_id", UNSAFE[1:])           # "" is already rejected by min_length
def test_a_principal_with_an_unsafe_tenant_id_cannot_be_created(tenant_id):
    with pytest.raises(ValidationError):
        ApiPrincipal(principal_id="k", tenant_id=tenant_id)
    with pytest.raises(ValidationError):
        TenantContext(tenant_id=tenant_id)


@pytest.mark.parametrize("tenant_id", ORDINARY)
def test_a_principal_accepts_ordinary_tenant_ids(tenant_id):
    principal = ApiPrincipal(principal_id="k", tenant_id=tenant_id)
    assert principal.tenant_context.tenant_id == tenant_id


def test_the_m4_bridge_cannot_smuggle_an_unsafe_tenant_id_in():
    from schema_lifecycle.roles import AuthenticatedPrincipal
    m4 = AuthenticatedPrincipal(principal_id="u", tenant_id="tenant_a/../tenant_b")
    with pytest.raises(ValidationError):
        ApiPrincipal.from_m4_principal(m4)


def test_the_validation_error_does_not_echo_the_tenant_id():
    with pytest.raises(ValidationError) as exc:
        ApiPrincipal(principal_id="k", tenant_id="tenant_a/../tenant_b")
    assert "tenant_a" not in str(exc.value).replace("tenant_id", "")


def test_an_api_key_record_with_an_unsafe_tenant_is_refused_with_401_not_500():
    repo, audit = MockApiKeyRepository(), MockAuditLogger()
    token = generate_api_secret(nbytes=32)
    record = create_api_key_record(
        token=token, tenant_id="tenant_a/../tenant_b", name="k", scopes=["documents:read"], roles=["integrator"]
    )
    asyncio.run(repo.create("tenant_a/../tenant_b", record))
    provider = ApiKeyAuthenticationProvider(repository=repo, audit_logger=audit)
    with pytest.raises(InvalidTokenError) as exc:
        asyncio.run(provider.authenticate(f"Bearer {token.raw_secret}"))
    assert exc.value.code == "INVALID_CREDENTIALS"
    assert token.raw_secret not in repr(audit.__dict__)


# -- typed HTTP behaviour of the tenant errors ------------------------------------------------------

def test_tenant_errors_have_explicit_typed_http_statuses():
    from doc_extractor.api.exceptions import get_status_code_for_exception
    from doc_extractor.exceptions import InvalidTenantIdentifierError, StorageError, TenantStorageIsolationError
    assert get_status_code_for_exception(InvalidTenantIdentifierError("too_long")) == 422     # not the 500 of its StorageError parent
    assert get_status_code_for_exception(TenantStorageIsolationError("directory_alias_collision")) == 500
    assert get_status_code_for_exception(StorageError("x")) == 500


def test_an_oversized_tenant_id_reaching_storage_answers_422_with_a_typed_code_over_http(tmp_path):
    from fastapi.testclient import TestClient
    from doc_extractor.api.app import create_app
    from doc_extractor.api.config import ApiSettings
    from doc_extractor.storage import LocalDiskStorage

    app = create_app(ApiSettings(app_env="development"))

    @app.get("/probe/oversized")
    def oversized():
        LocalDiskStorage(tmp_path, tenant_id="x" * 5000)

    response = TestClient(app, raise_server_exceptions=False).get("/probe/oversized")
    error = response.json()["error"]
    assert response.status_code == 422 and error["code"] == "INVALID_TENANT_IDENTIFIER"
    assert error["details"] == {"reason": "too_long"} and "xxxx" not in response.text           # never echoed


def test_an_isolation_failure_over_http_is_a_typed_500_that_names_no_tenant(tmp_path):
    from fastapi.testclient import TestClient
    from doc_extractor.api.app import create_app
    from doc_extractor.api.config import ApiSettings
    from doc_extractor.storage import LocalDiskStorage

    (tmp_path / "tenant_v").mkdir()
    import os
    os.symlink(tmp_path / "tenant_v", tmp_path / "tenant_s")
    app = create_app(ApiSettings(app_env="development"))

    @app.get("/probe/symlink")
    def symlink():
        LocalDiskStorage(tmp_path, tenant_id="tenant_s")._get_document_dir("doc_1")

    response = TestClient(app, raise_server_exceptions=False).get("/probe/symlink")
    error = response.json()["error"]
    assert response.status_code == 500 and error["code"] == "TENANT_STORAGE_ISOLATION_FAILED"
    assert error["details"] == {"reason": "tenant_directory_is_a_symlink"} and "tenant_v" not in response.text
