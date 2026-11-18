"""
SEC-006: a tenant identifier names a storage directory, so no tenant ID may address another tenant's.
Reproduced regression: ``ApiPrincipal(tenant_id="tenant_a/../tenant_b")`` was accepted and
``LocalDiskStorage`` resolved it into tenant_b's folder; ``LocalIdempotencyStore`` silently collapsed it
to "tenant_b" (``Path(tenant_id).name``).

The guard rejects only values that can leave their directory. It does NOT define a tenant-ID format, so
ordinary identifiers must keep working (several are asserted below).
"""

import asyncio
from datetime import datetime, timezone

from pydantic import ValidationError
import pytest

from doc_extractor.api.exceptions import InvalidTokenError
from doc_extractor.api.ports.authentication import ApiPrincipal, TenantContext
from doc_extractor.api.providers.api_key_auth import ApiKeyAuthenticationProvider
from doc_extractor.api.token_core import create_api_key_record, generate_api_secret
from doc_extractor.exceptions import StorageError
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.models import IdempotencyRecord
from doc_extractor.storage import LocalDiskStorage
from doc_extractor.tenancy import require_path_safe_tenant_id, tenant_id_path_problem
from tests.test_api_key_service import MockApiKeyRepository, MockAuditLogger

UNSAFE = [
    "", ".", "..", "tenant_a/../tenant_b", "../x", "a/b", "/abs", "a\\b", "..\\x",
    "a\x00b", "a\nb", "a\tb", "a\x7fb",
]
# Valid tenant IDs of any style stay valid: the guard is not a format rule.
ORDINARY = ["tenant_alpha", "default_tenant", "t-1", "org:123", "Acme Corp", "a..b", "...", "tenant.alpha", "测试"]


# -- the shared guard -----------------------------------------------------------------------------

@pytest.mark.parametrize("tenant_id", UNSAFE)
def test_unsafe_tenant_ids_are_rejected(tenant_id):
    assert tenant_id_path_problem(tenant_id, windows=False) is not None
    with pytest.raises(ValueError):
        require_path_safe_tenant_id(tenant_id, windows=False)


@pytest.mark.parametrize("tenant_id", ORDINARY)
def test_ordinary_tenant_ids_are_not_restricted(tenant_id):
    assert tenant_id_path_problem(tenant_id, windows=False) is None
    assert tenant_id_path_problem(tenant_id, windows=True) is None or ":" in tenant_id
    assert require_path_safe_tenant_id(tenant_id, windows=False) == tenant_id


def test_a_drive_prefix_is_rejected_only_under_windows_semantics():
    assert tenant_id_path_problem("C:", windows=True) == "drive_prefix"
    assert tenant_id_path_problem("C:", windows=False) is None          # a plain name on POSIX
    assert tenant_id_path_problem("org:123", windows=True) is None      # not a drive


def test_non_strings_are_rejected_and_the_value_is_never_echoed():
    assert tenant_id_path_problem(None) == "not_a_string"
    assert tenant_id_path_problem(123) == "not_a_string"
    with pytest.raises(ValueError) as exc:
        require_path_safe_tenant_id("tenant_a/../tenant_b")
    assert "tenant_a" not in str(exc.value)


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


# -- storage boundary -----------------------------------------------------------------------------

@pytest.mark.parametrize("tenant_id", UNSAFE)
def test_storage_refuses_an_unsafe_tenant_id(tmp_path, tenant_id):
    with pytest.raises(StorageError):
        LocalDiskStorage(tmp_path, tenant_id=tenant_id)


def test_the_reproduced_traversal_no_longer_reaches_another_tenants_documents(tmp_path):
    victim = LocalDiskStorage(tmp_path, tenant_id="tenant_b")
    victim._get_document_dir("doc_1").mkdir(parents=True)
    (victim._get_document_dir("doc_1") / "document_record.json").write_text("{}")
    with pytest.raises(StorageError):
        LocalDiskStorage(tmp_path, tenant_id="tenant_a/../tenant_b")


def test_a_tenant_id_reassigned_after_construction_is_still_checked(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_a")
    storage.tenant_id = "tenant_a/../tenant_b"
    with pytest.raises(StorageError):
        storage._get_document_dir("doc_1")


def test_two_ordinary_tenants_still_get_disjoint_directories(tmp_path):
    a = LocalDiskStorage(tmp_path, tenant_id="tenant_a")._get_document_dir("doc_1")
    b = LocalDiskStorage(tmp_path, tenant_id="org:123")._get_document_dir("doc_1")
    assert a != b and a.parent != b.parent
    assert tmp_path.resolve() in a.parents and tmp_path.resolve() in b.parents


def test_storage_error_does_not_echo_the_tenant_id(tmp_path):
    with pytest.raises(StorageError) as exc:
        LocalDiskStorage(tmp_path, tenant_id="tenant_a/../tenant_b")
    assert "tenant_b" not in exc.value.message and "tenant_b" not in str(exc.value.details)


# -- idempotency store boundary -------------------------------------------------------------------

def _record(tenant_id):
    now = datetime.now(timezone.utc).timestamp()
    return IdempotencyRecord(
        idempotency_key="k1", tenant_id=tenant_id, request_fingerprint="f", document_id="doc_1",
        response_payload={}, created_at_utc=now, expires_at_utc=now + 3600,
    )


@pytest.mark.parametrize("tenant_id", ["tenant_a/../tenant_b", "../x", "a/b", ".."])
def test_the_idempotency_store_rejects_instead_of_collapsing_the_tenant_id(tmp_path, tenant_id):
    store = LocalIdempotencyStore(tmp_path)
    with pytest.raises(StorageError):
        store.get_record(tenant_id, "k1")
    with pytest.raises(StorageError):
        store.save_record(_record(tenant_id))
    with pytest.raises(StorageError):
        store.delete_record(tenant_id, "k1")


def test_idempotency_records_of_two_tenants_stay_separate(tmp_path):
    store = LocalIdempotencyStore(tmp_path)
    store.save_record(_record("tenant_a"))
    assert store.get_record("tenant_a", "k1") is not None
    assert store.get_record("tenant_b", "k1") is None          # the old collapse would have exposed it here
