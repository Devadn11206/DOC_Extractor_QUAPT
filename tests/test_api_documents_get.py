"""
Tests for API-002: GET /api/v1/documents/{document_id} (Milestone 2D, persisted metadata only).

Scope and PROVISIONAL limitations verified here (see docs/m5-open-decisions.md, M2D-D01 / M2D-D02 / M2D-D04):
- Only fields of Module A's persisted ``document_record.json`` are returned, through the sanctioned ``DocumentResource``
  fields plus the reserved ``result`` key, which is ALWAYS JSON null (no envelope is persisted anywhere).
- ``status`` is returned exactly as persisted and the timestamps keep their persisted instants (Module A writes
  ``processing`` once; nothing updates it; the serialized text of a timestamp may be normalized by the DTO).
- Tenant isolation: the tenant comes only from the authenticated principal; every "not yours / not there" case is the
  same concealed 404 DOCUMENT_NOT_FOUND.
- The record is read through the hardened descriptor-relative walk (O_NOFOLLOW per component, O_NONBLOCK leaf, fstat
  regular-file check, os.fdopen ownership transfer), never through the path-based ``LocalDiskStorage.get_document_record``.
- Nothing internal or sensitive is exposed (tenant id, internal state, idempotency key, storage URIs, paths, raw
  diagnostics), and stored error text is replaced by an allow-list.

The tests run the real route against the real storage mapping and records written through ``LocalDiskStorage``.
"""

import asyncio
import inspect
import io
import json
import logging
import os
import shutil
import socket
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from doc_extractor.api.app import create_app
from doc_extractor.api.composition import ApplicationContainer
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.contracts import documents as documents_contract
from doc_extractor.api.contracts.documents import (
    SCOPE_DOCUMENTS_READ,
    DocumentDetailResponse,
    document_record_from_stored,
    public_error,
)
from doc_extractor.api.contracts.records import DocumentResource
from doc_extractor.api.exceptions import GENERIC_INTERNAL_MESSAGE, TenantResourceNotFoundError
from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
from doc_extractor.api.routers.documents import get_document_detail
from doc_extractor.api.services import document_read_service as read_service_module
from doc_extractor.api.services.document_read_service import DocumentReadService
from doc_extractor.config import Settings
from doc_extractor.exceptions import (
    CrossTenantStorageAccessError,
    StorageError,
    TenantStorageIsolationError,
)
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.ingestion import IngestionService
from doc_extractor.malware import MockMalwareScanner
from doc_extractor.models import DocumentRecord, DocumentStatus, IngestionState
from doc_extractor.storage import LocalDiskStorage
from tests.conftest import create_minimal_pdf
from tests.test_api_documents_page_image import (
    ALPHA_HEADERS,
    MockAuthProvider,
    _assert_descriptors_clean,
    _call_with_timeout,
    _FailingReadFile,
    _install_proxy,
    _LogCapture,
    _once,
    _open_fd_count,
    _OsProxy,
    _swap_for_fifo,
    _swap_for_symlink,
)

PUBLIC_KEYS = {
    "document_id", "status", "filename", "mime_type", "file_size_bytes", "page_count",
    "sha256", "created_at", "updated_at", "error_code", "error_message", "result",
}
# Keys that must NEVER appear: internal, or data that no persistence capability can supply yet.
FORBIDDEN_KEYS = {
    "tenant_id", "internal_state", "idempotency_key", "pages", "payload_a", "payload_b", "storage_uri",
    "document_type", "schema_id", "schema_version", "classification_confidence", "confidence", "model_versions",
    "run_id", "prompt_version", "fields", "flags", "evidence", "envelope", "document_metadata", "run", "details",
    "request_id",
}
SECRET_IDEMPOTENCY_KEY = "idem-secret-key-123"


def _record(doc_id: str = "doc_rec_1", tenant_id: str = "tenant_alpha", **overrides) -> DocumentRecord:
    values = dict(
        document_id=doc_id,
        tenant_id=tenant_id,
        file_hash_sha256="ab" * 32,
        claimed_filename="invoice_2026.pdf",
        file_size_bytes=2048,
        page_count=3,
        mime_type="application/pdf",
        status=DocumentStatus.PROCESSING,
        internal_state=IngestionState.QUEUED,
        created_at="2026-11-19T10:00:00+00:00",
        updated_at="2026-11-19T10:00:00+00:00",
        idempotency_key=SECRET_IDEMPOTENCY_KEY,
        error=None,
    )
    values.update(overrides)
    return DocumentRecord(**values)


@pytest.fixture
def api(tmp_path):
    settings = ApiSettings(app_env="development", storage_base_dir=str(tmp_path))
    container = ApplicationContainer(api_settings=settings, auth_provider=MockAuthProvider())
    app = create_app(settings=settings, container=container)
    # Default TestClient: an untyped exception escaping the route would fail the test instead of becoming a 500.
    return {"app": app, "client": TestClient(app), "container": container, "tmp_path": tmp_path}


def _store(api, record: DocumentRecord) -> Path:
    """Persist ``record`` through the REAL storage mapping and return its document directory."""
    storage = LocalDiskStorage(api["tmp_path"], tenant_id=record.tenant_id)
    storage.save_document_record(record)
    return storage._get_document_dir(record.document_id)


def _url(doc_id: str) -> str:
    return f"/api/v1/documents/{doc_id}"


def _get(api, doc_id: str, headers=ALPHA_HEADERS):
    return api["client"].get(_url(doc_id), headers=headers)


def _rewrite_record(doc_dir: Path, mutate) -> None:
    record_file = doc_dir / "document_record.json"
    data = json.loads(record_file.read_text(encoding="utf-8"))
    mutate(data)
    record_file.write_text(json.dumps(data), encoding="utf-8")


def _service_for(api, tenant_id: str = "tenant_alpha") -> DocumentReadService:
    return DocumentReadService(LocalDiskStorage(api["tmp_path"], tenant_id=tenant_id), tenant_id)


def _make_decoy_document(root: Path, tenant_id: str, doc_id: str) -> Path:
    """A fully valid document record living OUTSIDE the tenant's real storage; following a symlink to it would serve it."""
    root.mkdir(parents=True, exist_ok=True)
    storage = LocalDiskStorage(root, tenant_id=tenant_id)
    storage.save_document_record(_record(doc_id, tenant_id, claimed_filename="DECOY-SECRET.pdf", idempotency_key=None))
    return storage._get_document_dir(doc_id)


def _concealed_body(response) -> dict:
    assert response.status_code == 404, response.text
    body = response.json()["error"]
    assert body["code"] == "DOCUMENT_NOT_FOUND" and body["message"] == "Document not found." and body["details"] == {}
    return {k: v for k, v in body.items() if k != "request_id"}


def _assert_generic_500(response, tmp_path: Path, *leaks: str) -> None:
    assert response.status_code == 500, response.text
    error = response.json()["error"]
    assert error["code"] == "INTERNAL_ERROR" and error["message"] == GENERIC_INTERNAL_MESSAGE and error["details"] == {}
    assert str(tmp_path) not in response.text
    for leak in leaks:
        assert leak not in response.text, f"{leak!r} leaked into a 500 response"


# ============================================================================
# Persisted metadata
# ============================================================================

def test_api002_valid_record_returns_persisted_public_metadata(api):
    record = _record(updated_at="2026-11-19T11:30:00+00:00")
    _store(api, record)

    response = _get(api, record.document_id)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == PUBLIC_KEYS
    assert body["document_id"] == record.document_id
    assert body["filename"] == "invoice_2026.pdf"
    assert body["mime_type"] == "application/pdf"
    assert body["file_size_bytes"] == 2048
    assert body["page_count"] == 3
    assert body["sha256"] == "ab" * 32
    assert body["error_code"] is None and body["error_message"] is None
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
    assert parse(body["created_at"]) == datetime.fromisoformat(record.created_at)
    assert parse(body["updated_at"]) == datetime.fromisoformat("2026-11-19T11:30:00+00:00")   # persisted, not synthesized
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Request-ID"]
    assert "Content-Disposition" not in response.headers


def test_api002_status_is_exactly_the_persisted_value_and_is_never_inferred(api):
    """A current Module A record is `processing`; other stored values are returned as stored, never 'advanced'."""
    doc_dir = _store(api, _record())
    assert _get(api, "doc_rec_1").json()["status"] == "processing"
    # a handoff that exists, and a later internal state, do not change the reported status
    (doc_dir / "module_a_handoff.json").write_text("{}", encoding="utf-8")
    _rewrite_record(doc_dir, lambda d: d.update(internal_state="ready_for_extraction"))
    assert _get(api, "doc_rec_1").json()["status"] == "processing"


@pytest.mark.parametrize("status", [s.value for s in DocumentStatus])
def test_api002_every_enum_status_is_returned_as_stored(api, status):
    _store(api, _record(f"doc_status_{status}", status=DocumentStatus(status)))
    body = _get(api, f"doc_status_{status}").json()
    assert body["status"] == status
    assert body["result"] is None


def test_api002_result_is_json_null_never_an_object(api):
    _store(api, _record())
    response = _get(api, "doc_rec_1")
    assert response.json()["result"] is None
    assert '"result":null' in response.text.replace(" ", "")
    assert response.json()["result"] != {}


def test_api002_no_unsupported_or_internal_keys_appear(api):
    _store(api, _record())
    body = _get(api, "doc_rec_1").json()
    assert set(body) == PUBLIC_KEYS
    assert not (set(body) & FORBIDDEN_KEYS)


def test_api002_real_module_a_ingestion_record_is_served(api):
    """The record Module A actually writes (not a hand-built one) is parseable and served unchanged."""
    tmp = api["tmp_path"]
    storage = LocalDiskStorage(tmp, tenant_id="tenant_alpha")
    ingestion = IngestionService(
        settings=Settings(storage_base_dir=str(tmp)),
        scanner=MockMalwareScanner(is_ready=True),
        storage=storage,
        idempotency_store=LocalIdempotencyStore(tmp),
        tenant_id="tenant_alpha",
    )
    result = ingestion.ingest_document(io.BytesIO(create_minimal_pdf(2)), "scan_001.pdf")
    assert result.is_accepted

    persisted = json.loads((storage._get_document_dir(result.document_id) / "document_record.json").read_text("utf-8"))
    response = _get(api, result.document_id)

    assert response.status_code == 200
    body = response.json()
    assert (persisted["status"], persisted["internal_state"]) == ("processing", "queued")   # what Module A really writes
    assert body["status"] == "processing"
    assert body["filename"] == result.filename == "scan_001.pdf"
    assert body["sha256"] == result.file_hash_sha256
    assert body["file_size_bytes"] == result.file_size_bytes
    assert body["page_count"] == result.page_count == 2
    assert body["mime_type"] == result.mime_type
    assert body["result"] is None and set(body) == PUBLIC_KEYS
    assert persisted["internal_state"] not in response.text and "queued" not in response.text


def _ingest_for_real(api, claimed_name: str, marker: bytes):
    """Run the REAL Module A ingestion (real validation, hashing, storage mapping, record write) and return its result."""
    tmp = api["tmp_path"]
    ingestion = IngestionService(
        settings=Settings(storage_base_dir=str(tmp)),
        scanner=MockMalwareScanner(is_ready=True),
        storage=LocalDiskStorage(tmp, tenant_id="tenant_alpha"),
        idempotency_store=LocalIdempotencyStore(tmp),
        tenant_id="tenant_alpha",
    )
    # a distinct trailing comment keeps every ingested file's hash unique inside one test tenant
    return ingestion.ingest_document(io.BytesIO(create_minimal_pdf(1) + b"\n%" + marker + b"\n"), claimed_name)


def _persisted_record(api, document_id: str) -> dict:
    storage = LocalDiskStorage(api["tmp_path"], tenant_id="tenant_alpha")
    return json.loads((storage._get_document_dir(document_id) / "document_record.json").read_text(encoding="utf-8"))


def test_api002_long_filename_accepted_and_persisted_by_module_a_is_served(api):
    """
    Regression: Module A places no length limit on the claimed filename (``Path(name).name``), so a document with a
    2,000-character basename is legitimately accepted and persisted. API-002 must serve it, not answer a permanent 500.
    """
    claimed = "a" * 2000 + ".pdf"
    result = _ingest_for_real(api, claimed, b"long-2000")

    assert result.is_accepted, "Module A must accept the long filename (this is the persisted-by-Module-A premise)"
    persisted = _persisted_record(api, result.document_id)
    assert persisted["claimed_filename"] == claimed and len(persisted["claimed_filename"]) == 2004
    assert (persisted["status"], persisted["internal_state"]) == ("processing", "queued")

    response = _get(api, result.document_id)

    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == claimed and len(body["filename"]) == 2004
    assert body["document_id"] == result.document_id
    assert body["status"] == "processing" and body["result"] is None
    assert set(body) == PUBLIC_KEYS


@pytest.mark.parametrize(
    "label, claimed",
    [
        ("unicode-300-chars", "履" * 300 + ".pdf"),
        ("unicode-and-spaces", "Jänè Röe 2026.pdf"),
        ("embedded-newline", "line1\nline2.pdf"),
        ("single-char", "a.pdf"),
    ],
)
def test_api002_other_filenames_module_a_accepts_are_served_verbatim(api, label, claimed):
    result = _ingest_for_real(api, claimed, label.encode())
    assert result.is_accepted
    assert _persisted_record(api, result.document_id)["claimed_filename"] == claimed
    response = _get(api, result.document_id)
    assert response.status_code == 200
    assert response.json()["filename"] == claimed


def test_api002_reads_through_the_hardened_walk_not_the_path_based_storage_reader(api):
    _store(api, _record())
    with patch.object(LocalDiskStorage, "get_document_record", side_effect=AssertionError("path-based reader used")):
        assert _get(api, "doc_rec_1").status_code == 200


# ============================================================================
# Tenant and identity boundaries
# ============================================================================

def test_api002_unknown_malformed_and_foreign_ids_share_one_concealed_404(api):
    _store(api, _record())
    bodies = [
        _concealed_body(_get(api, "doc_does_not_exist")),
        _concealed_body(_get(api, "doc@bad")),
        _concealed_body(_get(api, "doc..traversal")),
        _concealed_body(_get(api, "a" * 129)),
        _concealed_body(_get(api, "doc_rec_1", headers={"Authorization": "Bearer token-beta-reader"})),   # foreign tenant
        _concealed_body(_get(api, "DOC_REC_1")),                                                          # wrong case
    ]
    assert all(body == bodies[0] for body in bodies)


def test_api002_record_with_another_tenants_id_is_concealed(api):
    doc_dir = _store(api, _record("doc_tenant_swap"))
    _rewrite_record(doc_dir, lambda d: d.update(tenant_id="tenant_beta"))
    assert _concealed_body(_get(api, "doc_tenant_swap")) == _concealed_body(_get(api, "doc_does_not_exist"))


def test_api002_record_with_another_document_id_is_concealed(api):
    doc_a = _store(api, _record("doc_identity_a"))
    _store(api, _record("doc_identity_b_private"))
    shutil.copyfile(doc_a.parent / "doc_identity_b_private" / "document_record.json", doc_a / "document_record.json")
    response = _get(api, "doc_identity_a")
    assert _concealed_body(response) == _concealed_body(_get(api, "doc_does_not_exist"))
    assert "doc_identity_b_private" not in response.text


@pytest.mark.parametrize("missing_key", ["tenant_id", "document_id"])
def test_api002_record_without_identity_is_concealed(api, missing_key):
    doc_dir = _store(api, _record("doc_no_identity"))
    _rewrite_record(doc_dir, lambda d: d.pop(missing_key))
    _concealed_body(_get(api, "doc_no_identity"))


def test_api002_missing_record_and_empty_document_directory_are_concealed(api):
    doc_dir = _store(api, _record("doc_no_record"))
    (doc_dir / "document_record.json").unlink()
    (doc_dir / "module_a_handoff.json").write_text("{}", encoding="utf-8")        # other Module A artifacts do not count
    _concealed_body(_get(api, "doc_no_record"))
    (doc_dir.parent / "doc_empty_dir").mkdir()
    _concealed_body(_get(api, "doc_empty_dir"))


def test_api002_never_provisioned_tenant_is_concealed_and_creates_nothing(api):
    _store(api, _record())
    before = sorted(p.relative_to(api["tmp_path"]).as_posix() for p in api["tmp_path"].rglob("*"))
    fresh = {"Authorization": "Bearer token-fresh-tenant"}
    for doc_id in ("doc_rec_1", "doc_never_there"):
        _concealed_body(_get(api, doc_id, headers=fresh))
    _concealed_body(_get(api, "doc_never_there"))                                   # provisioned tenant, unknown id
    assert not (api["tmp_path"] / "tenant_never_provisioned").exists()
    assert sorted(p.relative_to(api["tmp_path"]).as_posix() for p in api["tmp_path"].rglob("*")) == before


def test_api002_hashed_tenant_directory_is_served_and_its_name_is_not_exposed(api):
    tenant_id = "Tenant.A"
    record = _record("doc_hashed_tenant", tenant_id)
    doc_dir = _store(api, record)
    api["app"].state.auth_provider.principals["token-hashed"] = ApiPrincipal(
        principal_id="user_hashed", tenant_id=tenant_id, principal_type=PrincipalType.USER,
        scopes={SCOPE_DOCUMENTS_READ}, is_active=True,
    )
    mapped_dir = doc_dir.parent.name
    assert mapped_dir != tenant_id and mapped_dir.startswith("h+") and not (api["tmp_path"] / tenant_id).exists()

    response = _get(api, "doc_hashed_tenant", headers={"Authorization": "Bearer token-hashed"})

    assert response.status_code == 200
    assert response.json()["document_id"] == "doc_hashed_tenant"
    assert mapped_dir not in response.text and tenant_id not in response.text
    _concealed_body(_get(api, "doc_hashed_tenant"))                                  # another tenant's principal: 404


def test_api002_tenant_comes_only_from_the_principal_an_injected_service_is_ignored(api):
    _store(api, _record())
    api["app"].state.document_read_service = _service_for(api, "tenant_alpha")
    _concealed_body(_get(api, "doc_rec_1", headers={"Authorization": "Bearer token-beta-reader"}))


def test_api002_authentication_and_scope_are_enforced(api):
    _store(api, _record())
    no_auth = api["client"].get(_url("doc_rec_1"))
    assert no_auth.status_code == 401 and no_auth.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    bad = api["client"].get(_url("doc_rec_1"), headers={"Authorization": "Bearer unknown-token"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "INVALID_CREDENTIALS"
    forbidden = api["client"].get(_url("doc_rec_1"), headers={"Authorization": "Bearer token-alpha-writer-only"})
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "FORBIDDEN_SCOPE"
    assert forbidden.json()["error"]["details"].get("required_scope") == SCOPE_DOCUMENTS_READ
    assert "invoice_2026" not in forbidden.text and "invoice_2026" not in no_auth.text


# ============================================================================
# PII and path leakage
# ============================================================================

def test_api002_response_exposes_exactly_the_public_dto_whitelist(api):
    _store(api, _record())
    body = _get(api, "doc_rec_1").json()
    assert set(body) == set(DocumentDetailResponse.model_fields) == PUBLIC_KEYS
    assert set(DocumentDetailResponse.model_fields) == set(DocumentResource.model_fields) | {"result"}


def test_api002_nothing_internal_is_leaked_in_body_or_headers(api):
    doc_dir = _store(api, _record())
    mapped_dir = doc_dir.parent.name
    (doc_dir / "module_a_handoff.json").write_text(
        json.dumps({"pages": [{"original_storage_uri": "tenant_alpha/doc_rec_1/pages/page_0001.png"}]}), encoding="utf-8"
    )
    response = _get(api, "doc_rec_1")
    haystack = response.text + "".join(f"{k}:{v}" for k, v in response.headers.items())
    for leak in (
        "tenant_alpha", "tenant_id", "internal_state", "queued", "idempotency", SECRET_IDEMPOTENCY_KEY,
        str(api["tmp_path"]), mapped_dir, "module_a_handoff", "document_record.json", "page_0001", "storage_uri", "/pages/",
    ):
        assert leak not in haystack, f"{leak!r} leaked"


@pytest.mark.parametrize(
    "stored_error, expected_code, expected_message",
    [
        ({"code": "UNSUPPORTED_FILE", "message": "raw: /srv/x/secret.pdf"}, "UNSUPPORTED_FILE", "This file type isn't supported."),
        ({"code": "FILE_TOO_LARGE", "message": "raw text"}, "FILE_TOO_LARGE", "The file is larger than the limit."),
        ({"code": "MALWARE_DETECTED", "message": "raw text"}, "MALWARE_DETECTED", "This file was blocked by the safety scan."),
        ({"code": "STORAGE_FAILED", "message": "open('/srv/data/tenant_alpha/x') failed: PermissionError"}, "INTERNAL_ERROR", GENERIC_INTERNAL_MESSAGE),
        ({"code": "/srv/data/path-as-code", "message": "m"}, "INTERNAL_ERROR", GENERIC_INTERNAL_MESSAGE),
    ],
)
def test_api002_stored_error_text_is_replaced_by_the_allow_list(api, stored_error, expected_code, expected_message):
    stored_error = {**stored_error, "request_id": "req_secret_id_77", "details": {"path": "/srv/secret-details"}}
    _store(api, _record("doc_failed", status=DocumentStatus.FAILED, internal_state=IngestionState.FAILED, error=stored_error))
    response = _get(api, "doc_failed")
    assert response.status_code == 200
    body = response.json()
    assert (body["error_code"], body["error_message"]) == (expected_code, expected_message)
    for leak in ("/srv/", "PermissionError", "req_secret_id_77", "secret-details", "raw", "tenant_alpha/x"):
        assert leak not in response.text


def test_api002_an_empty_stored_error_means_no_error(api):
    _store(api, _record("doc_empty_error", error={}))
    body = _get(api, "doc_empty_error").json()
    assert body["error_code"] is None and body["error_message"] is None


def _stored_error(**fields):
    """A stored error carrying the legitimate internal fields (request_id, details) whose values must never surface."""
    return {"request_id": "SECRET-ERR-REQ", "details": {"path": "/srv/SECRET-ERR-DETAILS"}, **fields}


_MALFORMED_STORED_ERRORS = [
    pytest.param("SECRET-ERR-MSG boom", id="string"),
    pytest.param("", id="empty-string"),
    pytest.param([], id="empty-list"),
    pytest.param(["SECRET-ERR-EXTRA"], id="non-empty-list"),
    pytest.param(0, id="zero"),
    pytest.param(7, id="integer"),
    pytest.param(False, id="false"),
    pytest.param(True, id="true"),
    pytest.param(_stored_error(code=5, message="SECRET-ERR-MSG"), id="code-int"),
    pytest.param(_stored_error(code=True, message="SECRET-ERR-MSG"), id="code-bool"),
    pytest.param(_stored_error(code=["SECRET-ERR-CODE"], message="SECRET-ERR-MSG"), id="code-list"),
    pytest.param(_stored_error(code={"a": "SECRET-ERR-CODE"}), id="code-dict"),
    pytest.param(_stored_error(code=None, message="SECRET-ERR-MSG"), id="code-null"),
    pytest.param(_stored_error(code="", message="SECRET-ERR-MSG"), id="code-empty-string"),
    pytest.param({"foo": "SECRET-ERR-EXTRA"}, id="no-code-unknown-key"),
    pytest.param({"message": "SECRET-ERR-MSG"}, id="message-only"),
    pytest.param({"message": 12345}, id="message-int-no-code"),
    pytest.param(_stored_error(code="UNSUPPORTED_FILE", message=12345), id="valid-code-message-int"),
    pytest.param(_stored_error(code="UNSUPPORTED_FILE", message=["SECRET-ERR-MSG"]), id="valid-code-message-list"),
    pytest.param(_stored_error(code="SECRET-ERR-CODE", message={"nested": "SECRET-ERR-MSG"}), id="unknown-code-message-dict"),
]


@pytest.mark.parametrize("stored_error", _MALFORMED_STORED_ERRORS)
def test_api002_malformed_stored_error_is_a_sanitized_500_never_a_fabricated_success(api, captured_logs, stored_error):
    doc_dir = _store(api, _record("doc_bad_error"))
    _rewrite_record(doc_dir, _set("error", stored_error))

    response = _get(api, "doc_bad_error")

    _assert_generic_500(response, api["tmp_path"], "SECRET-ERR", "/srv/")
    body = response.json()
    assert set(body) == {"error"}, "a corrupt record must not produce a success-shaped body"
    assert "error_code" not in response.text and "document_id" not in response.text and "doc_bad_error" not in response.text
    joined = "\n".join(captured_logs.messages)
    assert "reason=record_error_invalid" in joined
    assert "SECRET-ERR" not in joined and "/srv/" not in joined
    assert all("\n" not in m and "\r" not in m for m in captured_logs.messages)
    # service level: the same fixed reason, and the typed fault never contains a stored value
    with pytest.raises(StorageError) as excinfo:
        _service_for(api).get_document("doc_bad_error")
    assert excinfo.value.details == {"reason": "record_error_invalid"}
    assert "SECRET-ERR" not in str(excinfo.value) and "SECRET-ERR" not in json.dumps(excinfo.value.details)


_GENERIC = ("INTERNAL_ERROR", GENERIC_INTERNAL_MESSAGE)
_UNSUPPORTED = ("UNSUPPORTED_FILE", "This file type isn't supported.")


@pytest.mark.parametrize(
    "stored_error, expected",
    [
        pytest.param(None, (None, None), id="none"),
        pytest.param({}, (None, None), id="empty-object"),
        pytest.param({"code": "UNSUPPORTED_FILE"}, _UNSUPPORTED, id="code-only"),
        pytest.param({"code": "UNSUPPORTED_FILE", "message": None}, _UNSUPPORTED, id="message-null"),
        pytest.param({"code": "UNSUPPORTED_FILE", "message": "SECRET-ERR-MSG"}, _UNSUPPORTED, id="message-string"),
        pytest.param(_stored_error(code="MALWARE_DETECTED", message="SECRET-ERR-MSG"),
                     ("MALWARE_DETECTED", "This file was blocked by the safety scan."), id="internal-fields-present"),
        pytest.param(_stored_error(code="SECRET-ERR-CODE-UNKNOWN", message="SECRET-ERR-MSG"), _GENERIC, id="unknown-code-with-message"),
        pytest.param({"code": "SECRET-ERR-CODE-UNKNOWN"}, _GENERIC, id="unknown-code-only"),
        pytest.param(_stored_error(code="STORAGE_FAILED", message=None), _GENERIC, id="known-internal-code-not-allow-listed"),
    ],
)
def test_api002_valid_stored_error_shapes_are_accepted_and_only_allow_listed_text_is_exposed(api, captured_logs, stored_error, expected):
    _store(api, _record("doc_ok_error", status=DocumentStatus.FAILED, internal_state=IngestionState.FAILED, error=stored_error))

    response = _get(api, "doc_ok_error")

    assert response.status_code == 200
    body = response.json()
    assert (body["error_code"], body["error_message"]) == expected
    assert set(body) == PUBLIC_KEYS
    for leak in ("SECRET-ERR", "/srv/", "request_id", "details"):
        assert leak not in response.text
    assert "SECRET-ERR" not in "\n".join(captured_logs.messages)


def test_api002_filename_is_returned_as_a_string_and_never_resolved(api, monkeypatch):
    relative_names, all_paths = [], []

    def observe(path, dir_fd):
        all_paths.append(path)
        if dir_fd is not None:
            relative_names.append(path)

    proxy = _install_proxy(monkeypatch, before_open=observe)
    for doc_id, filename in (("doc_fn_traversal", "../../etc/passwd.pdf"), ("doc_fn_abs", "/etc/shadow"), ("doc_fn_uni", "Jänè_Röe.pdf")):
        _store(api, _record(doc_id, claimed_filename=filename))
        response = _get(api, doc_id)
        assert response.status_code == 200 and response.json()["filename"] == filename
        assert isinstance(response.json()["filename"], str)
    monkeypatch.undo()
    # Per request exactly four opens: the configured storage root, then tenant, document and record RELATIVE to it.
    # No open ever involves the client-supplied filename.
    assert proxy.open_calls == len(all_paths) == 12
    assert all_paths.count(str(api["tmp_path"])) == 3
    assert set(relative_names) == {"tenant_alpha", "document_record.json", "doc_fn_traversal", "doc_fn_abs", "doc_fn_uni"}
    assert not any("passwd" in p or "shadow" in p or "Jänè" in p for p in all_paths)


# ============================================================================
# Malformed or untrustworthy stored records: sanitized generic 500
# ============================================================================

def _set(key, value):
    return lambda d: d.__setitem__(key, value)


def _drop(key):
    return lambda d: d.pop(key)


_MALFORMED_RECORDS = [
    pytest.param(_drop("file_hash_sha256"), "hash-missing"),
    pytest.param(_drop("claimed_filename"), "filename-missing"),
    pytest.param(_drop("file_size_bytes"), "size-missing"),
    pytest.param(_drop("page_count"), "page-count-missing"),
    pytest.param(_drop("mime_type"), "mime-missing"),
    pytest.param(_drop("status"), "status-missing"),
    pytest.param(_drop("internal_state"), "internal-state-missing"),
    pytest.param(_drop("created_at"), "created-missing"),
    pytest.param(_drop("updated_at"), "updated-missing"),
    pytest.param(_set("file_size_bytes", "12"), "size-as-string"),
    pytest.param(_set("file_size_bytes", True), "size-as-bool"),
    pytest.param(_set("file_size_bytes", -1), "size-negative"),
    pytest.param(_set("page_count", 1.5), "page-count-float"),
    pytest.param(_set("page_count", None), "page-count-null"),
    pytest.param(_set("claimed_filename", 5), "filename-int"),
    pytest.param(_set("claimed_filename", ""), "filename-empty"),
    pytest.param(_set("mime_type", None), "mime-null"),
    pytest.param(_set("status", 5), "status-int"),
    pytest.param(_set("status", "queued"), "status-queued-not-in-enum"),
    pytest.param(_set("status", "PROCESSING"), "status-wrong-case"),
    pytest.param(_set("status", "x\nFORGED LOG LINE"), "status-newline"),
    pytest.param(_set("internal_state", "bogus"), "internal-state-unknown"),
    pytest.param(_set("internal_state", 7), "internal-state-int"),
    pytest.param(_set("created_at", "not-a-date"), "created-not-a-date"),
    pytest.param(_set("created_at", 12345), "created-int"),
    pytest.param(_set("updated_at", None), "updated-null"),
    pytest.param(_set("file_hash_sha256", "abc"), "hash-short"),
    pytest.param(_set("file_hash_sha256", "G" * 64), "hash-not-hex"),
    pytest.param(_set("idempotency_key", 5), "idempotency-key-int"),
    pytest.param(_set("error", "boom"), "error-not-a-dict"),
]


@pytest.mark.parametrize("mutate, case", _MALFORMED_RECORDS)
def test_api002_malformed_record_is_a_sanitized_generic_500(api, mutate, case):
    doc_id = "doc_malformed"
    _rewrite_record(_store(api, _record(doc_id)), mutate)
    response = _get(api, doc_id)
    _assert_generic_500(response, api["tmp_path"], "not-a-date", "bogus", "FORGED", "boom", "invoice_2026", "xxxxxxxx")


@pytest.mark.parametrize(
    "content",
    [b"\xff\xfe\x00not-utf8", b"{not json", b"[1, 2]", b"null", b'"a string"', b"123", b""],
    ids=["invalid-utf8", "invalid-json", "json-array", "json-null", "json-string", "json-number", "empty-file"],
)
def test_api002_unreadable_or_non_object_record_is_a_sanitized_generic_500(api, content):
    doc_dir = _store(api, _record("doc_garbage"))
    (doc_dir / "document_record.json").write_bytes(content)
    _assert_generic_500(_get(api, "doc_garbage"), api["tmp_path"], "not-utf8", "not json")


def test_api002_corrupt_record_reasons_are_fixed_codes_without_stored_values(api):
    """Service level: the typed fault carries only a fixed reason (it is what the server logs), never a stored value."""
    doc_dir = _store(api, _record("doc_reason"))
    expectations = [
        (_set("status", "SECRET-STATUS-VALUE"), "record_status_invalid"),
        (_set("internal_state", "SECRET-STATE-VALUE"), "record_internal_state_invalid"),
        (_drop("page_count"), "record_field_missing"),
        (_set("created_at", "SECRET-TIMESTAMP"), "record_timestamp_invalid"),
        (_set("file_hash_sha256", "SECRET-HASH"), "record_hash_invalid"),
        (_set("page_count", "SECRET-COUNT"), "record_field_type_invalid"),
        (_set("error", "SECRET-ERROR"), "record_error_invalid"),
        (_set("error", {"code": 5, "message": "SECRET-ERROR"}), "record_error_invalid"),
        (_set("error", {"foo": "SECRET-ERROR"}), "record_error_invalid"),
        (_set("updated_at", "2026-W47-3"), "record_timestamp_invalid"),
        (_set("created_at", "20261119T100000Z"), "record_timestamp_invalid"),
    ]
    service = _service_for(api)
    for mutate, reason in expectations:
        _store(api, _record("doc_reason"))
        _rewrite_record(doc_dir, mutate)
        with pytest.raises(StorageError) as excinfo:
            service.get_document("doc_reason")
        assert excinfo.value.details == {"reason": reason}
        assert "SECRET" not in str(excinfo.value) and "SECRET" not in json.dumps(excinfo.value.details)


# Timestamp text that parses in some ISO 8601 readers but that the public DTO rejects, or that is plainly invalid.
_DTO_REJECTED_TIMESTAMPS = [
    "20261119T100000Z", "2026-W47-3", "2026-11-19T10", "2026-11-19T24:00:00",
    " 2026-11-19T10:00:00+00:00", "2026-11-19T10:00:00+25:00", "garbage", "２０２６-11-19T10:00:00+00:00",
]
_DTO_ACCEPTED_TIMESTAMPS = [
    "2026-11-19T10:00:00+00:00", "2026-11-19T10:00:00Z", "2026-11-19T10:00:00", "2026-11-19", "20261119",
    "2026-11-19 10:00:00", "2026-11-19T10:00:00.123456789+00:00", "0001-01-01T00:00:00+00:00",
    "9999-12-31T23:59:59.999999+00:00",
]


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
@pytest.mark.parametrize("value", _DTO_REJECTED_TIMESTAMPS)
def test_api002_timestamps_the_dto_rejects_fail_under_the_timestamp_reason(api, captured_logs, field, value):
    """Rejected by the converter itself, with the fixed timestamp reason: never later by the DTO under another reason."""
    doc_dir = _store(api, _record("doc_bad_ts"))
    _rewrite_record(doc_dir, _set(field, value))

    response = _get(api, "doc_bad_ts")

    _assert_generic_500(response, api["tmp_path"], value.strip())
    joined = "\n".join(captured_logs.messages)
    assert "reason=record_timestamp_invalid" in joined
    assert "record_field_type_invalid" not in joined
    assert value.strip() not in joined
    with pytest.raises(StorageError) as excinfo:
        _service_for(api).get_document("doc_bad_ts")
    assert excinfo.value.details == {"reason": "record_timestamp_invalid"}


@pytest.mark.parametrize("value", _DTO_ACCEPTED_TIMESTAMPS)
def test_api002_timestamps_the_dto_accepts_are_served(api, value):
    """Existing valid persisted timestamps keep working (the same instant is preserved; the text may be normalized)."""
    doc_dir = _store(api, _record("doc_good_ts"))
    _rewrite_record(doc_dir, lambda d: d.update(created_at=value, updated_at=value))
    response = _get(api, "doc_good_ts")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["created_at"], str) and isinstance(body["updated_at"], str)
    assert body["created_at"] == body["updated_at"]


def test_api002_converter_and_dto_agree_on_every_timestamp():
    """No gap: text the converter accepts can never be rejected by the DTO, and every rejection uses one reason."""
    for value in _DTO_ACCEPTED_TIMESTAMPS + _DTO_REJECTED_TIMESTAMPS + ["", "2026-13-01T00:00:00", "2026-02-30", "T10:00:00"]:
        stored = _record().to_dict()
        stored["updated_at"] = value
        try:
            record = document_record_from_stored(stored)
        except StorageError as exc:
            assert exc.details == {"reason": "record_timestamp_invalid"}, (value, exc.details)
            continue
        DocumentDetailResponse.from_record(record)          # must not raise: the converter vouched for this text


# ============================================================================
# Special files and symlinks at the record path, with VALID decoy records
# ============================================================================

def test_api002_symlinked_record_file_to_a_valid_decoy_is_refused(api):
    tmp_path = api["tmp_path"]
    doc_dir = _store(api, _record("doc_link_record"))
    decoy = _make_decoy_document(tmp_path.parent / (tmp_path.name + "_decoy_record"), "tenant_alpha", "doc_link_record")
    (doc_dir / "document_record.json").unlink()
    (doc_dir / "document_record.json").symlink_to(decoy / "document_record.json")
    response = _get(api, "doc_link_record")
    _assert_generic_500(response, tmp_path, "DECOY")


def test_api002_symlinked_document_directory_to_a_valid_decoy_is_refused(api):
    tmp_path = api["tmp_path"]
    tenant_dir = _store(api, _record("doc_seed")).parent
    decoy = _make_decoy_document(tmp_path.parent / (tmp_path.name + "_decoy_doc"), "tenant_alpha", "doc_link_dir")
    (tenant_dir / "doc_link_dir").symlink_to(decoy)
    _assert_generic_500(_get(api, "doc_link_dir"), tmp_path, "DECOY")


def test_api002_symlinked_tenant_directory_is_refused_through_the_route(api):
    tmp_path = api["tmp_path"]
    outside = tmp_path.parent / (tmp_path.name + "_decoy_tenant")
    _make_decoy_document(outside, "tenant_symlink_attack", "doc_victim")
    (tmp_path / "tenant_symlink_attack").symlink_to(outside / "tenant_symlink_attack")
    api["app"].state.auth_provider.principals["token-attack"] = ApiPrincipal(
        principal_id="attacker", tenant_id="tenant_symlink_attack", scopes={SCOPE_DOCUMENTS_READ}, is_active=True
    )
    _assert_generic_500(_get(api, "doc_victim", headers={"Authorization": "Bearer token-attack"}), tmp_path, "DECOY")


def test_api002_symlinked_tenant_directory_is_refused_by_the_descriptor_walk_alone(api, monkeypatch):
    """The path-based tenant resolver is bypassed on purpose, so removing O_NOFOLLOW on the tenant component is caught."""
    tmp_path = api["tmp_path"]
    outside = tmp_path.parent / (tmp_path.name + "_decoy_tenant_walk")
    _make_decoy_document(outside, "tenant_walk_attack", "doc_victim")
    (tmp_path / "tenant_walk_attack").symlink_to(outside / "tenant_walk_attack")
    monkeypatch.setattr(LocalDiskStorage, "_tenant_dir", lambda self, create=False: self.base_dir / self.tenant_id)
    with pytest.raises(TenantStorageIsolationError) as excinfo:
        _service_for(api, "tenant_walk_attack").get_document("doc_victim")
    assert excinfo.value.details["reason"] == "tenant_directory_is_a_symlink"


@pytest.mark.parametrize("kind", ["fifo", "directory", "socket"])
def test_api002_special_file_at_the_record_path_is_refused_without_blocking(api, kind):
    doc_dir = _store(api, _record("doc_special"))
    target = doc_dir / "document_record.json"
    target.unlink()
    held = None
    if kind == "fifo":
        os.mkfifo(str(target))
    elif kind == "directory":
        target.mkdir()
    else:
        held = socket.socket(socket.AF_UNIX)
        cwd = os.getcwd()
        os.chdir(doc_dir)                                  # AF_UNIX paths are short: bind by relative name
        try:
            held.bind("document_record.json")
        finally:
            os.chdir(cwd)
    try:
        response = _call_with_timeout(lambda: _get(api, "doc_special"), fifo_paths=[target] if kind == "fifo" else ())
    finally:
        if held is not None:
            held.close()
    _assert_generic_500(response, api["tmp_path"])


# ============================================================================
# Deterministic swap-at-open races (the swap happens right before the relative open of that component)
# ============================================================================

def _race_setup(api, doc_id: str):
    tmp_path = api["tmp_path"]
    doc_dir = _store(api, _record(doc_id))
    outside_root = tmp_path.parent / (tmp_path.name + "_race_" + doc_id)
    outside_doc = _make_decoy_document(outside_root, "tenant_alpha", doc_id)
    return doc_dir, outside_doc, outside_root


@pytest.mark.parametrize("component", ["tenant", "document", "record"])
def test_api002_swap_at_open_is_refused(api, monkeypatch, component):
    tmp_path = api["tmp_path"]
    doc_id = "doc_race_" + component
    doc_dir, outside_doc, outside_root = _race_setup(api, doc_id)
    plans = {
        "tenant": ("tenant_alpha", lambda: _swap_for_symlink(tmp_path / "tenant_alpha", outside_root / "tenant_alpha")),
        "document": (doc_id, lambda: _swap_for_symlink(doc_dir, outside_doc)),
        "record": ("document_record.json", lambda: _swap_for_symlink(doc_dir / "document_record.json", outside_doc / "document_record.json")),
    }
    trigger, make_swap = plans[component]
    restore = {}
    hook = _once(trigger, lambda: restore.setdefault("fn", make_swap()))
    api["client"].get(_url("doc_warm_up"), headers=ALPHA_HEADERS)                      # warm-up before the fd baseline
    proxy = _install_proxy(monkeypatch, before_open=hook)
    baseline = _open_fd_count()
    try:
        response = _get(api, doc_id)
    finally:
        monkeypatch.undo()
        if "fn" in restore:
            restore["fn"]()
    assert hook.state["done"], "the swap hook never fired: the race was not exercised"
    _assert_generic_500(response, tmp_path, "DECOY")
    _assert_descriptors_clean(proxy, baseline)


@pytest.mark.parametrize("replaced", ["tenant", "document"])
def test_api002_parent_replaced_after_its_descriptor_was_acquired_still_reads_the_original(api, monkeypatch, replaced):
    """Descriptor-relative semantics: replacing a parent's PATH after its descriptor is held cannot redirect the read."""
    tmp_path = api["tmp_path"]
    doc_id = "doc_after_" + replaced
    doc_dir, outside_doc, outside_root = _race_setup(api, doc_id)
    if replaced == "tenant":      # swap the tenant path right before the document directory is opened relative to it
        trigger, make_swap = doc_id, lambda: _swap_for_symlink(tmp_path / "tenant_alpha", outside_root / "tenant_alpha")
    else:                         # swap the document path right before the record is opened relative to it
        trigger, make_swap = "document_record.json", lambda: _swap_for_symlink(doc_dir, outside_doc)
    restore = {}
    hook = _once(trigger, lambda: restore.setdefault("fn", make_swap()))
    proxy = _install_proxy(monkeypatch, before_open=hook)
    baseline = _open_fd_count()
    try:
        record = _service_for(api).get_document(doc_id)
    finally:
        monkeypatch.undo()
        if "fn" in restore:
            restore["fn"]()
    assert hook.state["done"]
    assert record.claimed_filename == "invoice_2026.pdf" and record.claimed_filename != "DECOY-SECRET.pdf"
    _assert_descriptors_clean(proxy, baseline)


def test_api002_record_swapped_for_a_fifo_at_open_is_refused_without_blocking(api, monkeypatch):
    doc_dir = _store(api, _record("doc_race_fifo"))
    leaf = doc_dir / "document_record.json"
    restore = {}
    hook = _once("document_record.json", lambda: restore.setdefault("fn", _swap_for_fifo(leaf)))
    proxy = _install_proxy(monkeypatch, before_open=hook)
    baseline = _open_fd_count()
    try:
        with pytest.raises(TenantStorageIsolationError) as excinfo:
            _call_with_timeout(lambda: _service_for(api).get_document("doc_race_fifo"), fifo_paths=[leaf])
    finally:
        monkeypatch.undo()
        if "fn" in restore:
            restore["fn"]()
    assert hook.state["done"] and excinfo.value.details["reason"] == "not_a_regular_file"
    _assert_descriptors_clean(proxy, baseline)


# ============================================================================
# Descriptor ownership: no double close, no leak, on every success and failure path
# ============================================================================

class _TextReadFailsOs(_OsProxy):
    """Like ``_OsProxy`` but a TEXT-mode read fails after the descriptor was handed to its file object."""

    def _fdopen(self, fd, *args, **kwargs):
        file_obj = super()._fdopen(fd, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if "b" not in mode:
            wrapped = _FailingReadFile(file_obj)
            self.transferred_files[-1] = wrapped
            return wrapped
        return file_obj


def _prep_none(doc_dir): pass
def _prep_invalid_utf8(doc_dir): (doc_dir / "document_record.json").write_bytes(b"\xff\xfe\x00bad")
def _prep_invalid_json(doc_dir): (doc_dir / "document_record.json").write_text("{bad", encoding="utf-8")
def _prep_not_an_object(doc_dir): (doc_dir / "document_record.json").write_text("[1]", encoding="utf-8")
def _prep_tenant_mismatch(doc_dir): _rewrite_record(doc_dir, _set("tenant_id", "tenant_beta"))
def _prep_doc_mismatch(doc_dir): _rewrite_record(doc_dir, _set("document_id", "doc_someone_else"))
def _prep_missing_record(doc_dir): (doc_dir / "document_record.json").unlink()
def _prep_bad_status(doc_dir): _rewrite_record(doc_dir, _set("status", "queued"))
def _prep_bad_state(doc_dir): _rewrite_record(doc_dir, _set("internal_state", "bogus"))
def _prep_missing_key(doc_dir): _rewrite_record(doc_dir, _drop("page_count"))


def _prep_record_symlink(doc_dir):
    decoy = doc_dir.parent.parent / "outside_record_for_fd_test.json"
    decoy.write_text(json.dumps({"document_id": "x"}), encoding="utf-8")
    (doc_dir / "document_record.json").unlink()
    (doc_dir / "document_record.json").symlink_to(decoy)


def _prep_record_fifo(doc_dir):
    (doc_dir / "document_record.json").unlink()
    os.mkfifo(str(doc_dir / "document_record.json"))


def _prep_record_directory(doc_dir):
    (doc_dir / "document_record.json").unlink()
    (doc_dir / "document_record.json").mkdir()


_OWNERSHIP_SCENARIOS = [
    pytest.param(_prep_none, None, "own", None, id="success"),
    pytest.param(_prep_invalid_utf8, StorageError, "own", None, id="invalid-utf8"),
    pytest.param(_prep_invalid_json, StorageError, "own", None, id="invalid-json"),
    pytest.param(_prep_not_an_object, StorageError, "own", None, id="not-an-object"),
    pytest.param(_prep_tenant_mismatch, CrossTenantStorageAccessError, "own", None, id="tenant-mismatch"),
    pytest.param(_prep_doc_mismatch, TenantResourceNotFoundError, "own", None, id="document-mismatch"),
    pytest.param(_prep_missing_record, TenantResourceNotFoundError, "own", None, id="record-missing"),
    pytest.param(_prep_none, TenantResourceNotFoundError, "doc_not_there", None, id="document-directory-missing"),
    pytest.param(_prep_bad_status, StorageError, "own", None, id="status-not-in-enum"),
    pytest.param(_prep_bad_state, StorageError, "own", None, id="internal-state-not-in-enum"),
    pytest.param(_prep_missing_key, StorageError, "own", None, id="required-key-missing"),
    pytest.param(_prep_record_symlink, TenantStorageIsolationError, "own", None, id="record-symlink"),
    pytest.param(_prep_record_fifo, TenantStorageIsolationError, "own", None, id="record-fifo"),
    pytest.param(_prep_record_directory, TenantStorageIsolationError, "own", None, id="record-directory"),
    pytest.param(_prep_none, StorageError, "own", _TextReadFailsOs, id="read-error-after-transfer"),
]


@pytest.mark.parametrize("prepare, expected_error, which, proxy_class", _OWNERSHIP_SCENARIOS)
def test_api002_descriptor_ownership_on_every_path(api, monkeypatch, prepare, expected_error, which, proxy_class):
    doc_id = "doc_fd_" + (expected_error.__name__.lower() if expected_error else "ok") + f"_{id(prepare) % 9973}"
    doc_dir = _store(api, _record(doc_id))
    prepare(doc_dir)
    request_id = doc_id if which == "own" else which
    proxy = (proxy_class or _OsProxy)()
    monkeypatch.setattr(read_service_module, "os", proxy)
    baseline = _open_fd_count()
    fifo_paths = [doc_dir / "document_record.json"]
    try:
        if expected_error is None:
            record = _call_with_timeout(lambda: _service_for(api).get_document(request_id), fifo_paths=fifo_paths)
            assert record.document_id == doc_id
            assert len([e for e in proxy.events if e[0] == "transfer"]) == 1, "the record descriptor must go to a file object"
        else:
            with pytest.raises(expected_error):
                _call_with_timeout(lambda: _service_for(api).get_document(request_id), fifo_paths=fifo_paths)
    finally:
        monkeypatch.undo()
    _assert_descriptors_clean(proxy, baseline)


def test_api002_invalid_utf8_record_does_not_double_close_through_the_route(api, monkeypatch):
    doc_dir = _store(api, _record("doc_utf8_http"))
    _prep_invalid_utf8(doc_dir)
    api["client"].get(_url("doc_warm_up"), headers=ALPHA_HEADERS)
    proxy = _install_proxy(monkeypatch)
    baseline = _open_fd_count()
    try:
        response = _get(api, "doc_utf8_http")
    finally:
        monkeypatch.undo()
    _assert_generic_500(response, api["tmp_path"])
    _assert_descriptors_clean(proxy, baseline)


def test_api002_no_descriptor_leak_after_repeated_mixed_requests(api):
    cases = {"doc_loop_ok": _prep_none, "doc_loop_utf8": _prep_invalid_utf8, "doc_loop_json": _prep_invalid_json,
             "doc_loop_status": _prep_bad_status, "doc_loop_fifo": _prep_record_fifo, "doc_loop_gone": _prep_missing_record}
    for doc_id, prepare in cases.items():
        prepare(_store(api, _record(doc_id)))
    fifo = LocalDiskStorage(api["tmp_path"], tenant_id="tenant_alpha")._get_document_dir("doc_loop_fifo") / "document_record.json"

    def sweep():
        return [_call_with_timeout(lambda d=d: _get(api, d), fifo_paths=[fifo]).status_code for d in cases]

    expected = [200, 500, 500, 500, 500, 404]
    assert sweep() == expected
    baseline = _open_fd_count()
    for _ in range(30):
        assert sweep() == expected
    assert _open_fd_count() == baseline


# ============================================================================
# Log hygiene
# ============================================================================

@pytest.fixture
def captured_logs():
    handler = _LogCapture()
    attached = []
    for name in ("doc_extractor.api.read_service", "doc_extractor.api.errors"):
        target = logging.getLogger(name)
        attached.append((target, target.level))
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        for target, level in attached:
            target.removeHandler(handler)
            target.setLevel(level)


def test_api002_untrusted_record_content_never_reaches_the_logs(api, captured_logs):
    forged = "FORGED LOG LINE level=CRITICAL"
    doc_a = _store(api, _record("doc_log_status"))
    _rewrite_record(doc_a, _set("status", f"x\n{forged}"))
    doc_b = _store(api, _record("doc_log_tenant"))
    _rewrite_record(doc_b, _set("tenant_id", "tenant_secret_value_xyz"))
    doc_c = _store(api, _record("doc_log_docid"))
    _rewrite_record(doc_c, _set("document_id", "doc_foreign_secret_abc"))
    doc_d = _store(api, _record("doc_log_utf8"))
    _prep_invalid_utf8(doc_d)
    doc_e = _store(api, _record("doc_log_key"))
    _rewrite_record(doc_e, _set("evil\nFORGED KEY", 1))                     # extra keys are ignored, never logged
    doc_f = _store(api, _record("doc_log_filename", claimed_filename="secret_person_name.pdf"))

    codes = [_get(api, d).status_code for d in ("doc_log_status", "doc_log_tenant", "doc_log_docid", "doc_log_utf8", "doc_log_key", "doc_log_filename")]
    assert codes == [500, 404, 404, 500, 200, 200]
    assert captured_logs.messages, "refusals must be logged"
    joined = "\n".join(captured_logs.messages)
    for forbidden in ("FORGED", "tenant_secret_value_xyz", "doc_foreign_secret_abc", "doc_log_", "secret_person_name",
                      "0xff", "invalid start byte", "evil", "tenant_alpha", str(api["tmp_path"])):
        assert forbidden not in joined, f"{forbidden!r} leaked into the logs"
    assert all("\n" not in m and "\r" not in m for m in captured_logs.messages)
    assert "reason=record_unreadable" in joined and "reason=record_tenant_mismatch" in joined


def test_api002_contract_module_does_no_logging():
    """The converter signals defects only through fixed reasons on typed errors (the server logs the reason)."""
    source = inspect.getsource(documents_contract)
    assert "import logging" not in source and "logger." not in source


# ============================================================================
# Input validation and platform guard run BEFORE any file access
# ============================================================================

@pytest.mark.parametrize("bad_id", ["../tenant_beta", "a/b", "..", ".", "doc\\x", "doc\x00x", "", " ", "d" * 129, None, 5, True])
def test_api002_unsafe_identifiers_are_refused_before_any_file_is_opened(api, monkeypatch, bad_id):
    """Path-unsafe ids never reach the filesystem layer (the HTTP router cannot even carry '/' or '..')."""
    proxy = _install_proxy(monkeypatch)
    try:
        with pytest.raises(TenantResourceNotFoundError):
            _service_for(api).get_document(bad_id)
    finally:
        monkeypatch.undo()
    assert proxy.open_calls == 0


@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"])
def test_api002_missing_required_platform_flag_fails_closed_before_touching_storage(api, monkeypatch, flag):
    from doc_extractor.exceptions import ComponentConfigurationError

    _store(api, _record())
    proxy = _install_proxy(monkeypatch, hidden=(flag,))
    try:
        with pytest.raises(ComponentConfigurationError) as excinfo:
            _service_for(api).get_document("doc_rec_1")
        response = _get(api, "doc_rec_1")
    finally:
        monkeypatch.undo()
    assert excinfo.value.details["reason"] == "platform_unsupported"
    assert proxy.open_calls == 0
    _assert_generic_500(response, api["tmp_path"], flag, "invoice_2026")


def test_api002_missing_dir_fd_support_fails_closed(api, monkeypatch):
    from doc_extractor.exceptions import ComponentConfigurationError

    _store(api, _record())
    proxy = _install_proxy(monkeypatch, dir_fd_open_supported=False)
    try:
        with pytest.raises(ComponentConfigurationError):
            _service_for(api).get_document("doc_rec_1")
    finally:
        monkeypatch.undo()
    assert proxy.open_calls == 0


# ============================================================================
# Lifecycle, threading, architecture
# ============================================================================

def test_api002_shut_down_container_returns_the_typed_503(api):
    _store(api, _record())
    api["container"].shutdown()
    response = _get(api, "doc_rec_1")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["error"]["details"].get("port") == "ApplicationContainer"


def test_api002_route_is_synchronous_so_blocking_io_runs_in_the_threadpool():
    assert not inspect.iscoroutinefunction(get_document_detail)


def test_api002_slow_read_does_not_block_the_event_loop(api):
    _store(api, _record())
    original = DocumentReadService.get_document

    def slow(self, document_id):
        time.sleep(0.5)
        return original(self, document_id)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api["app"]), base_url="http://t") as client:
            delay = {}

            async def detail():
                assert (await client.get(_url("doc_rec_1"), headers=ALPHA_HEADERS)).status_code == 200

            async def health():
                started = time.monotonic()
                await asyncio.sleep(0.05)
                await client.get("/api/v1/health")
                delay["seconds"] = time.monotonic() - started

            await asyncio.gather(detail(), health())
            return delay["seconds"]

    with patch.object(DocumentReadService, "get_document", slow):
        elapsed = asyncio.run(scenario())
    assert elapsed < 0.3, f"/health was delayed {elapsed:.2f}s by a blocking read (event loop blocked)"


def test_api002_read_constructs_no_heavy_components(api):
    _store(api, _record())

    def boom(*args, **kwargs):
        raise AssertionError("a heavy component was constructed during a metadata read")

    traps = [patch("doc_extractor.pipeline.ModuleAPipeline.__init__", boom),
             patch("doc_extractor.pipeline_b.ModuleBPipeline.__init__", boom),
             patch("doc_extractor.extractor.SchemaGuidedExtractor.__init__", boom),
             patch("doc_extractor.processing_service.DocumentProcessingService.__init__", boom),
             patch.object(ApplicationContainer, "get_tenant_components", boom)]
    for trap in traps:
        trap.start()
    try:
        assert _get(api, "doc_rec_1").status_code == 200
    finally:
        for trap in traps:
            trap.stop()
    assert len(api["container"].tenant_cache) == 0


# ============================================================================
# Contract / DTO
# ============================================================================

def test_api002_dto_reserves_result_as_always_null():
    assert issubclass(DocumentDetailResponse, DocumentResource)
    assert DocumentDetailResponse.model_fields["result"].annotation is type(None)
    response = DocumentDetailResponse.from_record(_record())
    assert response.result is None and response.model_dump(mode="json")["result"] is None
    for not_null in ({}, {"fields": {}}, "x", 0):
        with pytest.raises(ValidationError):
            DocumentDetailResponse(**{**response.model_dump(), "result": not_null})
    with pytest.raises(ValidationError):
        DocumentDetailResponse(**{**response.model_dump(), "tenant_id": "tenant_alpha"})  # extra keys are forbidden
    with pytest.raises(ValidationError):
        response.result = None                                                            # frozen


def test_api002_openapi_documents_the_public_shape_only(api):
    schema = api["app"].openapi()
    operation = schema["paths"]["/api/v1/documents/{document_id}"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/DocumentDetailResponse")
    properties = schema["components"]["schemas"]["DocumentDetailResponse"]["properties"]
    assert set(properties) == PUBLIC_KEYS
    assert properties["result"].get("type") == "null"
    assert not (set(properties) & FORBIDDEN_KEYS)
    assert {"401", "403", "404", "500", "503"} <= set(operation["responses"])


def test_api002_converter_accepts_a_real_record_dict_and_returns_a_document_record():
    stored = _record().to_dict()
    converted = document_record_from_stored(stored)
    assert isinstance(converted, DocumentRecord) and converted == _record()
    assert converted.status is DocumentStatus.PROCESSING and converted.internal_state is IngestionState.QUEUED
    stored["unknown_future_key"] = "ignored"
    assert document_record_from_stored(stored) == converted


def test_api002_public_error_allow_list_unit():
    assert public_error(None) == (None, None) and public_error({}) == (None, None)
    assert public_error({"code": "UNSUPPORTED_FILE", "message": "/srv/leak"}) == ("UNSUPPORTED_FILE", "This file type isn't supported.")
    # a valid non-empty string code that is not allow-listed becomes the generic error, without exposing that code
    for unknown in ({"code": "STORAGE_FAILED"}, {"code": "/srv/path-as-code", "message": None}):
        assert public_error(unknown) == ("INTERNAL_ERROR", GENERIC_INTERNAL_MESSAGE)
    detail = DocumentDetailResponse.from_record(_record(error={"code": "X", "message": "open('/srv/data/x') failed"}))
    assert detail.error_message == GENERIC_INTERNAL_MESSAGE and "/srv" not in detail.model_dump_json()


@pytest.mark.parametrize("malformed", [
    {"code": None}, {"code": ["x"]}, {"code": ""}, {"code": 5}, {"code": True}, {"message": "m"}, {"foo": "bar"},
    {"code": "UNSUPPORTED_FILE", "message": 12345}, "boom", [], 0, False, "",
])
def test_api002_malformed_stored_error_is_refused_by_every_entry_point(malformed):
    """Defence in depth: neither the allow-list nor the DTO mapper turns a malformed error into a fabricated answer."""
    with pytest.raises(StorageError) as excinfo:
        public_error(malformed)
    assert excinfo.value.details == {"reason": "record_error_invalid"}
    with pytest.raises(StorageError) as excinfo:
        DocumentDetailResponse.from_record(_record(error=malformed))
    assert excinfo.value.details == {"reason": "record_error_invalid"}
    stored = _record().to_dict()
    stored["error"] = malformed
    with pytest.raises(StorageError) as excinfo:
        document_record_from_stored(stored)
    assert excinfo.value.details == {"reason": "record_error_invalid"}


def test_api002_converter_normalizes_an_empty_error_object_to_no_error():
    stored = _record().to_dict()
    stored["error"] = {}
    assert document_record_from_stored(stored).error is None
    stored["error"] = {"code": "UNSUPPORTED_FILE", "message": None, "request_id": "r", "details": {"x": 1}}
    assert document_record_from_stored(stored).error["code"] == "UNSUPPORTED_FILE"
