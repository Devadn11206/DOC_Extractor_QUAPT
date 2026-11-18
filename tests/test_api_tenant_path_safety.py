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


def test_an_api_key_record_with_an_unsafe_tenant_cannot_even_be_created():
    from pydantic import ValidationError
    token = generate_api_secret(nbytes=32)
    with pytest.raises(ValidationError) as exc:
        create_api_key_record(token=token, tenant_id="tenant_a/../tenant_b", name="k", scopes=["documents:read"], roles=["integrator"])
    assert token.raw_secret not in str(exc.value) and "tenant_b" not in str(exc.value)           # the rejected input is not echoed


def test_a_corrupt_stored_key_row_with_an_unsafe_tenant_is_still_refused_with_401_not_500():
    """Second boundary: a repository adapter may hand back a row that was never validated (a corrupt or tampered store)."""
    repo, audit = MockApiKeyRepository(), MockAuditLogger()
    token = generate_api_secret(nbytes=32)
    record = create_api_key_record(token=token, tenant_id="tenant_a", name="k", scopes=["documents:read"], roles=["integrator"])
    asyncio.run(repo.create("tenant_a", record))
    record.tenant_id = "tenant_a/../tenant_b"                        # the stored row is altered after validation (no validate_assignment)
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
    # R5: the client gets the generic internal error; the reason is logged server-side only (tests/test_api_storage_fault_responses.py)
    assert response.status_code == 500 and error["code"] == "INTERNAL_ERROR" and error["details"] == {}
    assert "tenant_v" not in response.text and "symlink" not in response.text


# -- the validator cannot be bypassed by copying or constructing a principal ------------------------

import copy
import sys

import pytest

UNSAFE_FOR_COPY = ["tenant_a/../tenant_b", "../x", "a\\b", "", "x" * 400, "a\x00b"]
MODELS = [
    pytest.param(lambda tid="tenant_a": ApiPrincipal(principal_id="k", tenant_id=tid), id="ApiPrincipal"),
    pytest.param(lambda tid="tenant_a": TenantContext(tenant_id=tid), id="TenantContext"),
]


@pytest.mark.parametrize("make", MODELS)
@pytest.mark.parametrize("tenant_id", UNSAFE_FOR_COPY)
def test_model_copy_cannot_install_an_unsafe_tenant_id(make, tenant_id):
    original = make()
    with pytest.raises(ValueError):
        original.model_copy(update={"tenant_id": tenant_id})
    assert original.tenant_id == "tenant_a"                      # the original is untouched


@pytest.mark.parametrize("make", MODELS)
@pytest.mark.parametrize("tenant_id", UNSAFE_FOR_COPY)
def test_the_deprecated_copy_and_model_construct_cannot_install_an_unsafe_tenant_id(make, tenant_id):
    original = make()
    with pytest.raises(ValueError):
        original.copy(update={"tenant_id": tenant_id})
    with pytest.raises(ValueError):
        type(original).model_construct(**{**original.model_dump(), "tenant_id": tenant_id})


@pytest.mark.parametrize("make", MODELS)
def test_model_construct_without_a_tenant_is_refused_not_left_half_built(make):
    with pytest.raises(ValueError):
        type(make()).model_construct(principal_id="k")


@pytest.mark.parametrize("make", MODELS)
def test_replace_cannot_install_an_unsafe_tenant_id(make):
    """``__replace__`` is what ``copy.replace`` calls (Python 3.13+); test it directly on every version."""
    with pytest.raises(ValueError):
        make().__replace__(tenant_id="tenant_a/../tenant_b")
    if hasattr(copy, "replace"):                       # no skip: the extra assertion simply applies where it exists
        with pytest.raises(ValueError):
            copy.replace(make(), tenant_id="tenant_a/../tenant_b")
    assert make().__replace__(tenant_id="org:9").tenant_id == "org:9"


@pytest.mark.parametrize("make", MODELS)
def test_a_valid_change_through_copy_is_still_allowed_and_leaves_the_original_alone(make):
    original = make()
    changed = original.model_copy(update={"tenant_id": "Org:7"})
    assert changed.tenant_id == "Org:7" and original.tenant_id == "tenant_a"
    assert type(original.copy(update={"tenant_id": "tenant_b"})) is type(original)


def test_other_fields_are_unaffected_by_the_copy_guard():
    principal = ApiPrincipal(principal_id="k", tenant_id="tenant_a", scopes={"documents:read"})
    clone = principal.model_copy(update={"scopes": {"documents:write"}})
    assert clone.scopes == {"documents:write"} and principal.scopes == {"documents:read"} and clone.tenant_id == "tenant_a"


def test_a_frozen_principal_still_refuses_attribute_assignment():
    principal = ApiPrincipal(principal_id="k", tenant_id="tenant_a")
    with pytest.raises(ValidationError):
        principal.tenant_id = "tenant_b"


def test_object_setattr_cannot_be_intercepted_so_storage_is_the_independent_second_boundary(tmp_path):
    """Documented limit: a forged instance is still refused where the tenant becomes a directory."""
    from doc_extractor.exceptions import InvalidTenantIdentifierError
    from doc_extractor.storage import LocalDiskStorage
    principal = ApiPrincipal(principal_id="k", tenant_id="tenant_a")
    object.__setattr__(principal, "tenant_id", "tenant_a/../tenant_b")
    with pytest.raises(InvalidTenantIdentifierError):
        LocalDiskStorage(tmp_path, tenant_id=principal.tenant_id)
