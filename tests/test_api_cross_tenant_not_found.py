"""
SEC-006: access to another tenant's resource answers 404, indistinguishable from a resource that does not exist, and never
echoes a tenant identifier. Unrelated permission denials keep their 403 and their codes.

The M4 case runs M4's REAL approval code (a principal of one tenant approving another tenant's schema): M4 reports it as a
plain ForbiddenActionError carrying a marker phrase, so a wording change in M4 fails here instead of silently going back to 403.
"""

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[dev,api]')")

from fastapi import Depends
from fastapi.testclient import TestClient

from doc_extractor.api import dependencies as deps
from doc_extractor.api.app import create_app
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.exceptions import (
    M4_CROSS_TENANT_MARKER,
    ForbiddenError,
    TenantAccessDeniedError,
    TenantResourceNotFoundError,
    concealed_not_found,
    get_status_code_for_exception,
)
from doc_extractor.api.ports.authentication import ApiPrincipal
from doc_extractor.exceptions import CrossTenantStorageAccessError, StorageError, TenantStorageIsolationError
from doc_extractor.storage import LocalDiskStorage
from schema_lifecycle import exceptions as m4
from schema_lifecycle.approval import ApprovalPolicy, BenchmarkResultVerifier, InMemoryBenchmarkReportStore, SchemaApprovalService
from schema_lifecycle.manager import SchemaLifecycleManager
from schema_lifecycle.repository import InMemorySchemaRepository
from schema_lifecycle.roles import AuthenticatedPrincipal, DefaultSchemaAuthorizer, Role


def approval_service():
    authorizer = DefaultSchemaAuthorizer()
    authorizer.register_principal(
        AuthenticatedPrincipal(principal_id="bob", tenant_id="tenant_alpha", roles={Role.SCHEMA_APPROVER}, mfa_verified=True))
    store = InMemoryBenchmarkReportStore()
    return SchemaApprovalService(
        manager=SchemaLifecycleManager(repository=InMemorySchemaRepository()),
        authorizer=authorizer,
        verifier=BenchmarkResultVerifier(report_store=store, policy=ApprovalPolicy()),
        report_store=store,
    )


def build_app(tmp_path):
    app = create_app(ApiSettings(app_env="development"))
    service = approval_service()
    bob = AuthenticatedPrincipal(principal_id="bob", tenant_id="tenant_alpha", roles={Role.SCHEMA_APPROVER}, mfa_verified=True)

    @app.get("/p/m5-denied")
    def m5_denied():
        raise TenantAccessDeniedError("Access to tenant 'tenant_beta' is forbidden.", details={"tenant": "tenant_beta"})

    @app.get("/p/m5-missing")
    def m5_missing():
        raise TenantResourceNotFoundError()

    @app.get("/p/m4-cross-tenant")
    def m4_cross_tenant():
        service.approve_schema(bob, "tenant_beta", "DOC-01", 1, "run_1")      # M4's own cross-tenant check

    @app.get("/p/m4-no-permission")
    def m4_no_permission():
        raise m4.ForbiddenActionError(
            "Schema approval forbidden: MFA required: principal 'bob' lacks verified MFA (SEC-001).",
            principal_id="bob", permission="schema:approve")                   # M4's own wording for an ordinary denial

    @app.get("/p/m4-self-approval")
    def m4_self_approval():
        raise m4.SchemaSelfApprovalError(message="Self-approval is forbidden.", schema_id="DOC-01", version=1, author="alice")

    @app.get("/p/m4-empty-tenant")
    def m4_empty_tenant():
        raise m4.TenantAccessError("tenant_id must be a non-empty string for tenant isolation.")

    @app.get("/p/m5-forbidden")
    def m5_forbidden():
        raise ForbiddenError(message="Access denied. Missing required scope: documents:read", code="FORBIDDEN_SCOPE")

    @app.get("/p/storage-foreign-uri")
    def storage_foreign_uri():
        LocalDiskStorage(tmp_path, tenant_id="tenant_alpha").resolve_uri_to_path("tenant_beta/doc_1/source/original.pdf")

    @app.get("/p/storage-symlink")
    def storage_symlink():
        LocalDiskStorage(tmp_path, tenant_id="tenant_sym")._get_document_dir("doc_1")

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client(tmp_path):
    return build_app(tmp_path)


def body(response):
    error = response.json()["error"]
    return error["code"], error["message"], error["details"]


# -- cross-tenant access is concealed ---------------------------------------------------------------

def test_the_api_cross_tenant_error_answers_exactly_like_a_missing_document(client):
    denied, missing = client.get("/p/m5-denied"), client.get("/p/m5-missing")
    assert denied.status_code == missing.status_code == 404
    assert body(denied) == body(missing) == ("DOCUMENT_NOT_FOUND", "Document not found.", {})


def test_the_cross_tenant_response_never_echoes_a_tenant_or_the_exceptions_details(client):
    text = client.get("/p/m5-denied").text
    assert "tenant_beta" not in text and "forbidden" not in text.lower()


def test_m4s_real_cross_tenant_approval_attempt_is_concealed_as_schema_not_found(client):
    response = client.get("/p/m4-cross-tenant")
    assert response.status_code == 404
    assert body(response) == ("SCHEMA_NOT_FOUND", "Schema not found.", {})
    assert "tenant_alpha" not in response.text and "tenant_beta" not in response.text          # M4's message names both tenants
    assert response.headers["X-Request-ID"] == response.json()["error"]["request_id"]


def test_m4s_real_cross_tenant_error_still_carries_the_marker_this_mapping_relies_on():
    with pytest.raises(m4.ForbiddenActionError) as exc:
        approval_service().approve_schema(
            AuthenticatedPrincipal(principal_id="bob", tenant_id="tenant_alpha", roles={Role.SCHEMA_APPROVER}, mfa_verified=True),
            "tenant_beta", "DOC-01", 1, "run_1")
    assert M4_CROSS_TENANT_MARKER in exc.value.message
    assert concealed_not_found(exc.value) == ("SCHEMA_NOT_FOUND", "Schema not found.")


def test_a_storage_reference_to_another_tenant_is_concealed_as_not_found(client):
    response = client.get("/p/storage-foreign-uri")
    assert response.status_code == 404 and body(response) == ("DOCUMENT_NOT_FOUND", "Document not found.", {})
    assert "tenant_beta" not in response.text


def test_the_storage_error_for_a_foreign_uri_is_a_typed_subclass_of_storage_error(tmp_path):
    with pytest.raises(CrossTenantStorageAccessError) as exc:
        LocalDiskStorage(tmp_path, tenant_id="tenant_alpha").resolve_uri_to_path("tenant_beta/doc_1/x")
    assert isinstance(exc.value, StorageError) and exc.value.code == "CROSS_TENANT_ACCESS_FORBIDDEN"
    assert get_status_code_for_exception(exc.value) == 404


def test_a_symlink_inside_tenant_storage_stays_a_loud_integrity_fault_not_a_not_found(tmp_path):
    (tmp_path / "tenant_other").mkdir()
    import os
    os.symlink(tmp_path / "tenant_other", tmp_path / "tenant_sym")
    with pytest.raises(TenantStorageIsolationError):
        LocalDiskStorage(tmp_path, tenant_id="tenant_sym")._get_document_dir("doc_1")
    assert get_status_code_for_exception(TenantStorageIsolationError("tenant_directory_is_a_symlink")) == 500
    assert concealed_not_found(TenantStorageIsolationError("x")) is None


# -- unrelated permission denials are unchanged ------------------------------------------------------

def test_an_ordinary_missing_scope_is_still_403_with_its_own_code(client):
    response = client.get("/p/m5-forbidden")
    assert response.status_code == 403 and response.json()["error"]["code"] == "FORBIDDEN_SCOPE"


def test_an_ordinary_m4_permission_denial_is_still_403_forbidden_with_its_own_message(client):
    response = client.get("/p/m4-no-permission")
    assert response.status_code == 403
    code, message, details = body(response)
    assert code == "FORBIDDEN" and "MFA required" in message                 # untouched: not concealed, not rewritten


def test_m4_self_approval_is_a_403_with_its_prd_code_and_is_not_concealed(client):
    response = client.get("/p/m4-self-approval")
    assert response.status_code == 403 and response.json()["error"]["code"] == "SCHEMA_SELF_APPROVAL"
    assert concealed_not_found(m4.SchemaSelfApprovalError(message="Self-approval is forbidden.", schema_id="d", version=1, author="a")) is None


def test_forbidden_action_errors_without_the_marker_are_never_concealed():
    for plain in (m4.ForbiddenActionError("Principal lacks required role or permission for this operation."),
                  m4.ForbiddenActionError("Schema approval forbidden: MFA required: principal 'bob' lacks verified MFA (SEC-001)."),
                  m4.ForbiddenActionError("Schema approval forbidden: Principal 'bob' is inactive or revoked.")):
        assert concealed_not_found(plain) is None and get_status_code_for_exception(plain) == 403


def test_m4s_empty_tenant_context_error_is_not_a_cross_tenant_access_and_stays_403(client):
    response = client.get("/p/m4-empty-tenant")
    assert response.status_code == 403 and response.json()["error"]["code"] == "TENANT_ACCESS_ERROR"
    assert concealed_not_found(m4.TenantAccessError("x")) is None


def test_the_other_error_mappings_are_unchanged():
    from doc_extractor.exceptions import UnsupportedFileError
    from doc_extractor.api.exceptions import AuthenticationRequiredError
    assert get_status_code_for_exception(ForbiddenError(message="x")) == 403
    assert get_status_code_for_exception(AuthenticationRequiredError(message="x")) == 401
    assert get_status_code_for_exception(UnsupportedFileError("x")) == 400
    assert get_status_code_for_exception(StorageError("x")) == 500
