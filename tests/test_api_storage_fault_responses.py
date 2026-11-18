"""
Tenant-storage faults are the operator's business (review finding R5).

An isolation failure (alias of another identity, symlink, marker mismatch) or an unreadable/unwritable storage root used
to reach the client as ``TENANT_STORAGE_ISOLATION_FAILED`` plus a ``reason`` such as ``directory_alias_collision``: that
describes the server's storage layout and hints that ANOTHER identity's directory exists. The client now gets the same
generic 500 as any internal fault; the reason and the request id are logged server-side, without the tenant or a path.
"""

import logging
import os

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[dev,api]')")

from fastapi.testclient import TestClient  # noqa: E402

from doc_extractor.api.app import create_app  # noqa: E402
from doc_extractor.api.config import ApiSettings  # noqa: E402
from doc_extractor.api.exceptions import GENERIC_INTERNAL_MESSAGE, get_status_code_for_exception  # noqa: E402
from doc_extractor.exceptions import TenantStorageIsolationError, TenantStorageUnavailableError  # noqa: E402
from doc_extractor.storage import LocalDiskStorage  # noqa: E402

ISOLATION_REASONS = [
    "directory_alias_collision", "tenant_directory_is_a_symlink", "tenant_path_is_not_a_directory",
    "tenant_directory_escapes_storage_root", "identity_marker_missing", "identity_marker_mismatch",
    "identity_marker_unreadable", "identity_marker_corrupt", "symlink_inside_tenant_storage",
    "path_resolves_outside_tenant_directory",
]
UNAVAILABLE_REASONS = ["tenant_directory_not_creatable", "storage_root_unreadable", "directory_not_creatable"]


def build_client():
    app = create_app(ApiSettings(app_env="development"))

    @app.get("/p/isolation")
    def isolation(reason: str):
        raise TenantStorageIsolationError(reason)

    @app.get("/p/unavailable")
    def unavailable(reason: str):
        raise TenantStorageUnavailableError(reason)

    return app, TestClient(app, raise_server_exceptions=False)


def body_without_request_id(response):
    data = response.json()
    data["error"].pop("request_id")
    return data


GENERIC_BODY = {"error": {"code": "INTERNAL_ERROR", "message": GENERIC_INTERNAL_MESSAGE, "details": {}}}


@pytest.mark.parametrize("reason", ISOLATION_REASONS)
def test_an_isolation_failure_answers_with_the_generic_internal_error(reason, caplog):
    _, client = build_client()
    with caplog.at_level(logging.ERROR, logger="doc_extractor.api.errors"):
        response = client.get("/p/isolation", params={"reason": reason})
    assert response.status_code == 500 and body_without_request_id(response) == GENERIC_BODY
    assert reason not in response.text and "TENANT_STORAGE" not in response.text and "alias" not in response.text.lower()
    assert f"reason={reason}" in caplog.text                                  # the diagnostics are on the server
    assert response.headers["X-Request-ID"] in caplog.text                    # and can be correlated with the response


@pytest.mark.parametrize("reason", UNAVAILABLE_REASONS)
def test_an_unavailable_storage_answers_with_the_generic_internal_error(reason, caplog):
    _, client = build_client()
    with caplog.at_level(logging.ERROR, logger="doc_extractor.api.errors"):
        response = client.get("/p/unavailable", params={"reason": reason})
    assert response.status_code == 500 and body_without_request_id(response) == GENERIC_BODY
    assert reason not in response.text and "UNAVAILABLE" not in response.text
    assert f"reason={reason}" in caplog.text


def test_every_reason_is_indistinguishable_to_the_client():
    _, client = build_client()
    bodies = {body_without_request_id(client.get("/p/isolation", params={"reason": r}))["error"]["message"] for r in ISOLATION_REASONS}
    bodies |= {body_without_request_id(client.get("/p/unavailable", params={"reason": r}))["error"]["message"] for r in UNAVAILABLE_REASONS}
    assert bodies == {GENERIC_INTERNAL_MESSAGE}


def test_the_status_stays_500_for_both_kinds():
    assert get_status_code_for_exception(TenantStorageIsolationError("x")) == 500
    assert get_status_code_for_exception(TenantStorageUnavailableError("x")) == 500


def test_the_server_log_never_contains_the_tenant_or_a_path(tmp_path, caplog):
    (tmp_path / "victim_dir").mkdir()
    os.symlink(tmp_path / "victim_dir", tmp_path / "secret-tenant-xyz")
    app, client = build_client()

    @app.get("/p/real")
    def real():
        LocalDiskStorage(tmp_path, tenant_id="secret-tenant-xyz")._get_document_dir("doc_1")

    with caplog.at_level(logging.DEBUG):
        response = client.get("/p/real")
    assert response.status_code == 500 and body_without_request_id(response) == GENERIC_BODY
    assert "reason=tenant_directory_is_a_symlink" in caplog.text
    assert "secret-tenant-xyz" not in caplog.text and str(tmp_path) not in caplog.text
    assert "secret-tenant-xyz" not in response.text and str(tmp_path) not in response.text


def test_an_unwritable_root_over_http_is_generic_not_a_bare_500_with_os_text(tmp_path, monkeypatch, caplog):
    app, client = build_client()

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied: /private/server/layout")

    monkeypatch.setattr(os, "mkdir", refuse)

    @app.get("/p/write")
    def write():
        LocalDiskStorage(tmp_path, tenant_id="Ghost Corp:1").store_original_document("doc_1", b"x")

    with caplog.at_level(logging.DEBUG):
        response = client.get("/p/write")
    assert response.status_code == 500 and body_without_request_id(response) == GENERIC_BODY
    assert "reason=tenant_directory_not_creatable" in caplog.text
    assert "/private/server/layout" not in response.text and "/private/server/layout" not in caplog.text
