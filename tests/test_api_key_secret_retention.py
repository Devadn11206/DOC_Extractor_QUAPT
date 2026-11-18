"""
Security decision D8 is open (idempotent replay of a "shown once" API-key secret), so nothing may
retain or replay the plaintext secret: no cache, no replay, no idempotency parameter on the service.
"""

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[api]')")

import inspect


from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
from doc_extractor.api.services import api_key_service as service_module
from doc_extractor.api.services.api_key_service import ApiKeyService
from tests.test_api_key_service import MockApiKeyRepository, MockAuditLogger, async_test


@pytest.fixture
def repo() -> MockApiKeyRepository:
    return MockApiKeyRepository()


@pytest.fixture
def service(repo: MockApiKeyRepository) -> ApiKeyService:
    return ApiKeyService(repository=repo, audit_logger=MockAuditLogger())


@pytest.fixture
def creator() -> ApiPrincipal:
    return ApiPrincipal(
        principal_id="user_integrator",
        tenant_id="tenant_alpha",
        principal_type=PrincipalType.USER,
        roles={"integrator"},
        scopes={"api_keys:write", "documents:read"},
    )


def _strings_in(obj, seen=None):
    """Every string reachable from an object graph (attributes, dicts, lists, tuples, sets)."""
    seen = set() if seen is None else seen
    if id(obj) in seen:
        return
    seen.add(id(obj))
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings_in(k, seen)
            yield from _strings_in(v, seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for item in obj:
            yield from _strings_in(item, seen)
    elif hasattr(obj, "__dict__"):
        yield from _strings_in(vars(obj), seen)


@pytest.mark.security
def test_the_service_offers_no_idempotency_parameter_and_has_no_cache():
    assert "idempotency_key" not in inspect.signature(ApiKeyService.create_api_key).parameters
    assert "_idempotency_cache" not in inspect.getsource(service_module)


@pytest.mark.security
@async_test
async def test_identical_creation_requests_are_never_replayed(service, creator):
    request = dict(creator=creator, name="Pipeline", scopes=["documents:read"], roles=["integrator"])
    first = await service.create_api_key(**request)
    second = await service.create_api_key(**request)
    assert first.key_id != second.key_id
    assert first.raw_secret != second.raw_secret


@pytest.mark.security
@async_test
async def test_the_service_retains_no_copy_of_the_plaintext_secret(service, creator):
    created = await service.create_api_key(
        creator=creator, name="Pipeline", scopes=["documents:read"], roles=["integrator"]
    )
    assert created.raw_secret
    assert not hasattr(service, "_idempotency_cache")
    assert all(created.raw_secret not in s for s in _strings_in(vars(service)))
