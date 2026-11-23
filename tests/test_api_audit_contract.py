"""
Best-effort audit logging is not mandatory durable auditing (review finding R7).

The reference sink swallows its own write failures, so an operation that is "mandatory audit" is fail-closed ONLY with a
logger whose failures propagate. These tests pin the real behaviour, the class contract and the wording of the claims, so
no document can say "fail-closed" for a path where a failure does not actually propagate.
"""

import logging
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[dev,api]')")

from doc_extractor.api.contracts.records import AuditEventRecord  # noqa: E402
from doc_extractor.api.exceptions import AuditUnavailableError  # noqa: E402
from doc_extractor.api.ports.audit import AuditLogger, SafeStructuredAuditLogger  # noqa: E402
from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType  # noqa: E402
from doc_extractor.api.services.api_key_service import ApiKeyService  # noqa: E402
from tests.test_api_key_service import MockApiKeyRepository, MockAuditLogger, async_test  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SECRET = "password=hunter2 /private/server/layout"


@pytest.fixture
def admin() -> ApiPrincipal:
    return ApiPrincipal(principal_id="user_admin", tenant_id="tenant_alpha", principal_type=PrincipalType.USER, roles={"admin"},
                        scopes={"api_keys:read", "api_keys:write", "documents:read"})


class RaisingLogger:
    """A durable-style logger: a failed write propagates."""

    async def log_event(self, event):
        raise RuntimeError(SECRET)

    async def log_action(self, **event):
        raise RuntimeError(SECRET)


def breaking_serialization(monkeypatch):
    def boom(self, *args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(AuditEventRecord, "model_dump", boom)


# -- the contract of the reference sink -------------------------------------------------------------------

def test_the_reference_sink_declares_that_it_does_not_propagate_failures():
    assert SafeStructuredAuditLogger.propagates_failures is False
    doc = SafeStructuredAuditLogger.__doc__
    assert "BEST-EFFORT" in doc and "NOT a durable audit store" in doc and "NOT fail-closed" in doc
    assert isinstance(SafeStructuredAuditLogger(), AuditLogger)


@async_test
async def test_a_failed_write_is_swallowed_and_only_the_exception_type_is_logged(monkeypatch, caplog):
    breaking_serialization(monkeypatch)
    sink = SafeStructuredAuditLogger()
    with caplog.at_level(logging.DEBUG):
        await sink.log_action(actor_id="a", tenant_id="t", action="x.y", resource_id="r", resource_type="k")   # returns normally
    assert "AUDIT_FAILURE" in caplog.text and "RuntimeError" in caplog.text
    assert "hunter2" not in caplog.text and "/private/server/layout" not in caplog.text


@async_test
async def test_an_invalid_event_still_raises_even_from_the_best_effort_sink():
    with pytest.raises(Exception):
        await SafeStructuredAuditLogger().log_action(actor_id="", tenant_id="t", action="x", resource_id="r", resource_type="k")


# -- what that means for a mandatory operation ----------------------------------------------------------------

def test_a_best_effort_sink_is_refused_so_key_creation_cannot_run_unaudited(admin, monkeypatch):
    """SEC-008: a lost audit write must not leave a key issued with no audit record. The service refuses the sink."""
    from doc_extractor.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError) as refused:
        ApiKeyService(repository=MockApiKeyRepository(), audit_logger=SafeStructuredAuditLogger())
    assert "best-effort" in refused.value.message


@async_test
async def test_with_a_logger_that_raises_key_creation_is_rolled_back_and_the_failure_is_reported(admin):
    repo = MockApiKeyRepository()
    service = ApiKeyService(repository=repo, audit_logger=RaisingLogger())
    with pytest.raises(AuditUnavailableError) as raised:
        await service.create_api_key(creator=admin, name="k", scopes=["documents:read"], roles=["integrator"])
    assert raised.value.details == {"operation": "api_key.created", "effect": "rolled_back"}
    assert [k.is_active for k in await repo.list_keys("tenant_alpha")] == [False]


def test_the_service_refuses_a_best_effort_sink_and_says_why():
    from doc_extractor.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError) as refused:
        ApiKeyService(repository=MockApiKeyRepository(), audit_logger=SafeStructuredAuditLogger())
    assert "SafeStructuredAuditLogger" in refused.value.message and "fail closed" in refused.value.message


@pytest.mark.parametrize("logger_factory", [RaisingLogger, MockAuditLogger])
def test_the_service_does_not_warn_for_a_logger_that_propagates_or_does_not_declare(logger_factory, caplog):
    with caplog.at_level(logging.WARNING, logger="doc_extractor.api.services.api_key_service"):
        ApiKeyService(repository=MockApiKeyRepository(), audit_logger=logger_factory())
    assert "best-effort" not in caplog.text


# -- the claims in the documentation ----------------------------------------------------------------------------

def test_the_documents_no_longer_make_the_claims_that_were_false():
    docs = (ROOT / "docs" / "m5-api-ownership.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "never logged or returned" not in docs                                  # the sink used to log str(exc)
    assert "Mandatory audit never downgrades" not in readme
    assert "best-effort" in docs.lower() and "best-effort" in readme.lower()
    assert re.search(r"Fail-closed \| \*\*No\*\*", docs)                          # the table says the sink is not fail-closed
    assert "not** fail-closed" in readme                                          # and so does the README
    assert re.search(r"must raise", docs, re.I)                                    # the contract for mandatory auditing is stated


def test_no_source_comment_claims_fail_closed_for_the_best_effort_sink():
    audit = (ROOT / "src/doc_extractor/api/ports/audit.py").read_text(encoding="utf-8")
    assert "fail-safe error handling" not in audit
    assert audit.count("fail-closed") >= 1 and "NOT fail-closed" in audit
