"""
Consistency of the API records (review finding R11).

- every model that carries a ``tenant_id`` applies the same rule as the principal, through every way of producing an instance;
- the job message's ``payload`` is bounded, flat and scalar (identifiers and options, never content);
- the audit status allow-list is derived from the authoritative enums;
- the two ``ConflictError`` types are different things with different names, and both codes are unchanged.
"""

import ast
import copy
import inspect
import unicodedata
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[dev,api]')")

from pydantic import BaseModel, ValidationError  # noqa: E402

from doc_extractor import exceptions as engine_exceptions  # noqa: E402
from doc_extractor.api import exceptions as api_exceptions  # noqa: E402
from doc_extractor.api.contracts import records  # noqa: E402
from doc_extractor.api.contracts.tenant_bound import TenantBoundModel  # noqa: E402
from doc_extractor.api.ports import audit as audit_module, authentication  # noqa: E402
from doc_extractor.exceptions import InvalidTenantIdentifierError  # noqa: E402
from doc_extractor.models import DocumentStatus, IngestionState  # noqa: E402

NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
MINIMAL = {
    "ReviewTaskRecord": dict(task_id="t1", document_id="d1"),
    "ApiKeyRecord": dict(key_id="k1", name="n", key_hash="a" * 64, key_prefix="abcd"),
    "ApiKeySummary": dict(key_id="k1", name="n", key_prefix="abcd", created_at=NOW),
    "ApiKeyCreationResult": dict(key_id="k1", name="n", key_prefix="abcd", created_at=NOW, raw_secret="SECRET-VALUE-" + "x" * 24),
    "WebhookSubscriptionRecord": dict(subscription_id="s1", target_url="https://example.com/h", event_types=["document.completed"], secret_hash="h" * 64),
    "WebhookDeliveryRecord": dict(delivery_id="x1", subscription_id="s1", event_id="e1", event_type="document.completed"),
    "AuditEventRecord": dict(event_id="e1", actor_id="a", action="x.y", resource_id="r", resource_type="k"),
    "DocumentProcessingMessage": dict(message_id="m1", document_id="d1", event_id="e1"),
}
UNSAFE = ["tenant_a/../tenant_b", "../x", "a/b", "a\\b", "", ".", "..", "a" * 300, "a\x00b", "a\nb", 5, None]
VALID = ["tenant_alpha", "a..b", "...", "..a", "Acme Corp", "org:123", "测试", unicodedata.normalize("NFD", "café")]


def build(name, tenant_id, **extra):
    return getattr(records, name)(tenant_id=tenant_id, **MINIMAL[name], **extra)


# -- one tenant rule for every tenant-bearing model ---------------------------------------------------------

def test_every_model_with_a_tenant_id_field_inherits_the_tenant_rule():
    models = {}
    for module in (records, authentication):
        for name, obj in vars(module).items():
            if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj.__module__ == module.__name__ and "tenant_id" in obj.model_fields:
                models[name] = obj
    assert set(MINIMAL) | {"TenantContext", "ApiPrincipal"} == set(models)
    not_bound = sorted(name for name, cls in models.items() if not issubclass(cls, TenantBoundModel))
    assert not_bound == [], f"tenant-bearing models without the tenant rule: {not_bound}"


@pytest.mark.parametrize("name", sorted(MINIMAL))
@pytest.mark.parametrize("tenant_id", UNSAFE, ids=lambda v: repr(v)[:14])
def test_an_unsafe_tenant_is_refused_however_the_record_is_produced(name, tenant_id):
    with pytest.raises((ValidationError, InvalidTenantIdentifierError)):
        build(name, tenant_id)
    good = build(name, "tenant_alpha")
    with pytest.raises(InvalidTenantIdentifierError):
        good.model_copy(update={"tenant_id": tenant_id})
    with pytest.raises(InvalidTenantIdentifierError):
        getattr(records, name).model_construct(tenant_id=tenant_id, **MINIMAL[name])
    with pytest.raises(InvalidTenantIdentifierError):
        copy.copy(good).model_copy(update={"tenant_id": tenant_id})


@pytest.mark.parametrize("name", sorted(MINIMAL))
@pytest.mark.parametrize("tenant_id", VALID, ids=lambda v: repr(v)[:14])
def test_any_valid_tenant_style_is_accepted_the_rule_is_not_a_format(name, tenant_id):
    assert build(name, tenant_id).tenant_id == tenant_id


@pytest.mark.parametrize("name", sorted(MINIMAL))
def test_a_rejected_record_does_not_echo_its_input(name):
    with pytest.raises(ValidationError) as raised:
        build(name, "tenant_a/../tenant_b")
    text = str(raised.value)
    assert "tenant_b" not in text and "SECRET-VALUE" not in text and "a" * 64 not in text and "h" * 64 not in text


# -- the job payload -----------------------------------------------------------------------------------

def message(payload):
    return records.DocumentProcessingMessage(tenant_id="tenant_alpha", payload=payload, **MINIMAL["DocumentProcessingMessage"])


@pytest.mark.parametrize("payload", [
    {}, {"schema_id": "casa_account_opening", "schema_version": 3, "ocr_backend": "tesseract", "force": True, "dpi": 200.0, "note": None},
    {"page_numbers": [1, 2, 3], "languages": ["en", "hi"]}, {"s": "x" * 256}, {f"k{i}": i for i in range(32)}, {"a1_b2": 1},
])
def test_a_flat_scalar_payload_of_identifiers_and_options_is_accepted(payload):
    assert message(payload).payload == payload


@pytest.mark.parametrize("payload", [
    {"nested": {"a": 1}}, {"data": b"%PDF-1.4"}, {"data": bytearray(b"x")}, {"text": "x" * 257}, {f"k{i}": i for i in range(33)},
    {"Upper": 1}, {"a b": 1}, {"": 1}, {"1a": 1}, {"a-b": 1}, {"a" * 65: 1}, {5: 1}, {"n": float("nan")}, {"n": float("inf")},
    {"pages": list(range(101))}, {"pages": [[1, 2]]}, {"pages": [{"a": 1}]}, {"obj": object()}, {"s": {1, 2}}, {"t": (1, 2)},
    {f"k{i}": "x" * 200 for i in range(30)},                                      # each part is allowed, the whole is over 4096 bytes
])
def test_a_payload_that_could_carry_content_is_refused(payload):
    with pytest.raises(ValidationError):
        message(payload)


def test_a_refused_payload_does_not_echo_its_content():
    with pytest.raises(ValidationError) as raised:
        message({"text": "CONFIDENTIAL-EXTRACTED-TEXT" * 20})
    assert "CONFIDENTIAL" not in str(raised.value)


def test_the_payload_bounds_are_documented_constants():
    assert (records.PAYLOAD_MAX_KEYS, records.PAYLOAD_MAX_STRING_LENGTH, records.PAYLOAD_MAX_LIST_ITEMS, records.PAYLOAD_MAX_BYTES) == (32, 256, 100, 4096)
    description = records.DocumentProcessingMessage.model_fields["payload"].description
    assert "256" in description and "4096" in description and "No nested objects" in description


# -- statuses derive from the enums ------------------------------------------------------------------------

def test_the_audit_statuses_are_exactly_the_authoritative_enums():
    assert audit_module.APPROVED_DOCUMENT_STATUSES == {s.value for s in DocumentStatus} | {s.value for s in IngestionState}


def test_no_status_value_is_typed_by_hand_in_the_audit_module():
    tree = ast.parse(inspect.getsource(audit_module))
    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    values = {s.value for s in DocumentStatus} | {s.value for s in IngestionState}
    assert literals & values == set(), f"status literals typed by hand: {literals & values}"


def test_every_authoritative_status_passes_the_audit_metadata_allow_list():
    for status in {s.value for s in DocumentStatus} | {s.value for s in IngestionState}:
        assert audit_module.sanitize_audit_metadata({"status": status}) == {"status": status}
    assert audit_module.sanitize_audit_metadata({"status": "not_a_status"}) == {}


# -- the two ConflictError types ---------------------------------------------------------------------------

def test_the_api_conflict_has_its_own_name_and_the_old_name_is_an_alias_of_it():
    assert api_exceptions.ConflictError is api_exceptions.ApiConflictError
    assert api_exceptions.ApiConflictError is not engine_exceptions.ConflictError
    assert api_exceptions.ApiConflictError.__name__ == "ApiConflictError"


def test_the_two_conflicts_are_different_conditions_with_unchanged_codes_and_the_same_status():
    api_conflict = api_exceptions.ApiConflictError()
    ingestion_conflict = engine_exceptions.ConflictError()
    assert api_conflict.code == "CONFLICT" and ingestion_conflict.code == "IDEMPOTENCY_KEY_CONFLICT"      # decision M5-D01 is still open
    assert isinstance(api_conflict, api_exceptions.ApiHttpError) and not isinstance(ingestion_conflict, api_exceptions.ApiHttpError)
    assert api_exceptions.get_status_code_for_exception(api_conflict) == 409
    assert api_exceptions.get_status_code_for_exception(ingestion_conflict) == 409
