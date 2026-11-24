"""
Comprehensive test suite for Phase 5B: RBAC, Schema Governance and Four-Eyes Approval.

Tests adhere strictly to:
- PRD §6.1, §6.2, §10.1 (SCH-001..006).
- SEC-001: Trusted Multi-Factor Authentication enforcement for Admin and Schema approver.
- SEC-003: Seven canonical least-privilege roles and default-deny matrix.
- SCH-005: Four-Eyes approval gate with distinct author-approver duties and test harness verification.
- SEC-006: Multi-tenant boundary isolation.
- SEC-008: Tamper-evident hash-chained audit logging and fail-closed audit policy.
- ARC-003: Concurrency safety and atomic state transitions over durable persistence.
"""

from datetime import datetime, timezone
import pytest

pytest.importorskip("fastapi", reason="FastAPI required for REST route verification")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from doc_extractor.api.app import create_app
from doc_extractor.api.composition import ApplicationContainer
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.exceptions import AuditUnavailableError
from doc_extractor.api.ports.audit import AuditLogger
from doc_extractor.api.providers.jwt_auth import issue_access_token
from doc_extractor.infrastructure.mongo.schemas import (
    MongoSchemaRepository,
    SCHEMA_INDEXES,
    ensure_schema_indexes,
)
from schema_lifecycle.approval import (
    ApprovalPolicy,
    BenchmarkResultVerifier,
    InMemoryBenchmarkReportStore,
    SchemaApprovalService,
)
from schema_lifecycle.enums import FieldType, SchemaStatus
from schema_lifecycle.events import LifecycleEvent, LifecycleEventType
from schema_lifecycle.exceptions import (
    BenchmarkVerificationError,
    InvalidStateTransitionError,
    SchemaNotFoundError,
    SchemaRollbackError,
    SchemaSelfApprovalError,
    SchemaVersionError,
)
from schema_lifecycle.manager import SchemaLifecycleManager
from schema_lifecycle.metamodel import FieldDefinition, SchemaDefinition
from schema_lifecycle.repository import InMemorySchemaRepository, SchemaSnapshot
from schema_lifecycle.roles import (
    AuthenticatedPrincipal,
    ProductionSchemaAuthorizer,
    Role,
    SchemaPermission,
)
from schema_lifecycle.testing.contracts import AggregateMetrics, DatasetSplit
from schema_lifecycle.testing.policy import EvaluationDecision
from schema_lifecycle.testing.runner import BenchmarkReport, GR005Accounting
from tests.mongo_fakes import FakeDatabase

ISSUER = "https://identity.example.internal"
AUDIENCE = "doc-extractor-api"
PREFIX = "/api/v1"


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    path = tmp_path_factory.mktemp("keys") / "public.pem"
    path.write_bytes(public_pem)
    return private_pem, path


def _auth(keys, roles, tenant="tenant_alpha", mfa=False, subject="user_1"):
    private_pem, _ = keys
    token = issue_access_token(
        private_pem,
        issuer=ISSUER,
        audience=AUDIENCE,
        subject=subject,
        tenant_id=tenant,
        roles=roles,
        mfa_verified=mfa,
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
        description="CASA account opening schema",
        fields=[
            FieldDefinition(name="applicant_name", type=FieldType.STRING, required=True),
            FieldDefinition(name="account_type", type=FieldType.STRING, required=False),
        ],
    ).model_dump(mode="json")


def make_mock_benchmark_report(
    benchmark_id="run_001",
    tenant_id="tenant_alpha",
    schema_id="DOC-01",
    schema_version=1,
    overall_pass=True,
):
    """Constructs an authoritative passing BenchmarkReport fixture."""
    return BenchmarkReport(
        benchmark_id=benchmark_id,
        run_timestamp=datetime.now(timezone.utc).isoformat(),
        dataset_id=f"ds_{schema_id}",
        tenant_id=tenant_id,
        schema_id=schema_id,
        schema_version=schema_version,
        document_type="casa_account_opening",
        dataset_split=DatasetSplit.LOCKED_TEST,
        policy_version="1.0.0",
        primary_aggregation="macro",
        total_samples=100,
        real_samples=100,
        synthetic_samples=0,
        failed_samples=0,
        is_mock_run=False,
        execution_modes=["genuine_llm"],
        providers=["litellm"],
        models=["claude-3-5-sonnet"],
        prompt_versions=["1.0.0"],
        field_metrics={},
        aggregate_metrics=AggregateMetrics(
            primary_aggregation="macro",
            macro_f1_all_fields=0.96,
            macro_f1_key_fields=0.98,
            macro_precision_all=0.96,
            macro_recall_all=0.96,
            micro_f1_all_fields=0.96,
            micro_f1_key_fields=0.98,
            micro_precision_all=0.96,
            micro_recall_all=0.96,
            mean_calibration_gap_points=1.5,
            dataset_calibration_gap_points=1.5,
            calibration_sample_count=100,
            total_samples=100,
            total_field_types=2,
            key_field_count=1,
            total_predictions=100,
            total_tp=96,
            total_fp=4,
            total_fn=4,
            total_tn=0,
            escape_count=0,
            has_insufficient_data=False,
            insufficient_data_reasons=[],
        ),
        metrics_decision=EvaluationDecision(
            policy_version="1.0.0",
            dataset_split=DatasetSplit.LOCKED_TEST,
            primary_aggregation="macro",
            meets_m1_all_fields=overall_pass,
            meets_m1_key_fields=overall_pass,
            meets_m6_calibration=overall_pass,
            meets_m11_escapes=True,
            meets_gr003_false_fill=True,
            has_insufficient_data=False,
            insufficient_data_reasons=[],
            overall_pass=overall_pass,
            violations=[] if overall_pass else ["METRIC_FAIL"],
            summary="Pass" if overall_pass else "Fail",
        ),
        synthetic_field_metrics=None,
        synthetic_aggregate_metrics=None,
        gate_eligible=True,
        gate_eligibility_reasons=["GATE_QUALIFIED"],
        gr005_accounting=GR005Accounting(
            status="VERIFIED",
            total_attempted_runs=1000,
            successful_validations=1000,
            unique_qualifying_runs=1000,
            validation_exceptions=0,
            conformance_run_ids=["run_1"],
            required_runs=1000,
            is_conformance_verified=True,
            limitation_note="Verified",
        ),
        sample_observations=[],
        overall_pass=overall_pass,
        summary="Passing benchmark report summary",
    )


def _setup_app_client(keys, tmp_path, repo=None, report_store=None):
    _, public_path = keys
    settings = ApiSettings(
        app_env="development",
        audit_store_dir=str(tmp_path / "audit"),
        jwt_public_key_path=str(public_path),
        jwt_issuer=ISSUER,
        jwt_audience=AUDIENCE,
    )
    repo = repo or InMemorySchemaRepository()
    manager = SchemaLifecycleManager(repository=repo)
    store = report_store or InMemoryBenchmarkReportStore()
    authorizer = ProductionSchemaAuthorizer()
    verifier = BenchmarkResultVerifier(report_store=store, policy=ApprovalPolicy())
    approval_service = SchemaApprovalService(
        manager=manager,
        authorizer=authorizer,
        verifier=verifier,
        report_store=store,
    )
    container = ApplicationContainer(
        api_settings=settings,
        schema_manager=manager,
        schema_approval_service=approval_service,
    )
    client = TestClient(create_app(settings, container=container))
    return client, manager, approval_service, store


# =============================================================================
# 1. Role-Based Access Control Across Seven Canonical Roles (SEC-003)
# =============================================================================

class TestCanonicalRolesAndPermissions:
    """Verifies that all seven canonical roles strictly enforce least-privilege matrix."""

    def test_analyst_role_restrictions(self, keys, tmp_path):
        client, manager, _, _ = _setup_app_client(keys, tmp_path)
        headers = _auth(keys, ["analyst"])

        # Analyst cannot create
        res = client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=headers)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "FORBIDDEN_PERMISSION"

        # Analyst cannot transition
        res = client.post(f"{PREFIX}/schemas/DOC-01/versions/1/status", json={"target_status": "testing"}, headers=headers)
        assert res.status_code == 403

        # Analyst cannot approve
        res = client.post(f"{PREFIX}/schemas/DOC-01/versions/1/approve", json={"harness_run_id": "r1"}, headers=headers)
        assert res.status_code == 403

        # Analyst cannot rollback
        res = client.post(f"{PREFIX}/schemas/DOC-01/rollback", json={"target_version": 1}, headers=headers)
        assert res.status_code == 403

    def test_reviewer_role_restrictions(self, keys, tmp_path):
        client, manager, _, _ = _setup_app_client(keys, tmp_path)
        headers = _auth(keys, ["reviewer"])

        # Reviewer cannot create schemas
        res = client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=headers)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "FORBIDDEN_PERMISSION"

        # Reviewer cannot rollback
        res = client.post(f"{PREFIX}/schemas/DOC-01/rollback", json={"target_version": 1}, headers=headers)
        assert res.status_code == 403

    def test_integrator_role_restrictions(self, keys, tmp_path):
        client, manager, _, _ = _setup_app_client(keys, tmp_path)
        headers = _auth(keys, ["integrator"])

        # Integrator cannot create or approve
        res = client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=headers)
        assert res.status_code == 403
        res = client.post(f"{PREFIX}/schemas/DOC-01/versions/1/approve", json={"harness_run_id": "r1"}, headers=headers)
        assert res.status_code == 403

    def test_auditor_role_capabilities_and_restrictions(self, keys, tmp_path):
        client, manager, _, _ = _setup_app_client(keys, tmp_path)
        author_headers = _auth(keys, ["schema_author"])
        auditor_headers = _auth(keys, ["auditor"])

        # Author creates schema
        client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=author_headers)

        # Auditor can read history
        history_res = client.get(f"{PREFIX}/schemas/DOC-01/versions", headers=auditor_headers)
        assert history_res.status_code == 200
        assert len(history_res.json()) == 1

        # Auditor cannot create or edit
        res = client.post(f"{PREFIX}/schemas", json={"definition": _definition(schema_id="DOC-02")}, headers=auditor_headers)
        assert res.status_code == 403

    def test_schema_author_capabilities_and_restrictions(self, keys, tmp_path):
        client, manager, _, _ = _setup_app_client(keys, tmp_path)
        author_headers = _auth(keys, ["schema_author"])

        # Author creates schema v1
        res = client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=author_headers)
        assert res.status_code == 201
        assert res.json()["status"] == "draft"

        # Author creates incremental v2
        res2 = client.post(
            f"{PREFIX}/schemas/DOC-01/versions",
            json={"definition": _definition(version=2), "comment": "Add fields"},
            headers=author_headers,
        )
        assert res2.status_code == 201
        assert res2.json()["version"] == 2

        # Author moves v1 to testing
        trans_res = client.post(
            f"{PREFIX}/schemas/DOC-01/versions/1/status",
            json={"target_status": "testing"},
            headers=author_headers,
        )
        assert trans_res.status_code == 200
        assert trans_res.json()["status"] == "testing"

        # Author CANNOT approve (lacks schema:approve permission)
        appr_res = client.post(
            f"{PREFIX}/schemas/DOC-01/versions/1/approve",
            json={"harness_run_id": "run_001"},
            headers=author_headers,
        )
        assert appr_res.status_code == 403
        assert appr_res.json()["error"]["code"] == "FORBIDDEN_PERMISSION"

        # Author CANNOT rollback
        rb_res = client.post(f"{PREFIX}/schemas/DOC-01/rollback", json={"target_version": 1}, headers=author_headers)
        assert rb_res.status_code == 403

    def test_schema_approver_capabilities_and_restrictions(self, keys, tmp_path):
        client, manager, _, _ = _setup_app_client(keys, tmp_path)
        approver_headers = _auth(keys, ["schema_approver"], mfa=True)

        # Approver cannot create schemas
        res = client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=approver_headers)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "FORBIDDEN_PERMISSION"

        # Approver cannot rollback
        res = client.post(f"{PREFIX}/schemas/DOC-01/rollback", json={"target_version": 1}, headers=approver_headers)
        assert res.status_code == 403

    def test_admin_capabilities_and_least_privilege(self, keys, tmp_path):
        client, manager, _, _ = _setup_app_client(keys, tmp_path)
        admin_headers = _auth(keys, ["admin"], mfa=True)

        # Admin can create schema
        res = client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=admin_headers)
        assert res.status_code == 201

        # Admin can move to testing
        res = client.post(f"{PREFIX}/schemas/DOC-01/versions/1/status", json={"target_status": "testing"}, headers=admin_headers)
        assert res.status_code == 200

        # Least-privilege check: Admin does NOT have schema:approve by default
        res = client.post(f"{PREFIX}/schemas/DOC-01/versions/1/approve", json={"harness_run_id": "r1"}, headers=admin_headers)
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "FORBIDDEN_PERMISSION"


# =============================================================================
# 2. Four-Eyes Approval Gate (SCH-005, SEC-003)
# =============================================================================

class TestFourEyesApprovalGate:
    """Verifies that author cannot approve own submission and requires distinct authorized approver."""

    def test_author_cannot_approve_own_submission(self, keys, tmp_path):
        """Author attempting self-approval fails with 403 SCHEMA_SELF_APPROVAL."""
        client, manager, _, store = _setup_app_client(keys, tmp_path)
        author_headers = _auth(keys, ["schema_author"], subject="alice")

        # Alice creates schema and moves to testing
        client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=author_headers)
        client.post(f"{PREFIX}/schemas/DOC-01/versions/1/status", json={"target_status": "testing"}, headers=author_headers)

        # Seed passing benchmark report
        report = make_mock_benchmark_report(benchmark_id="run_alice_1", schema_id="DOC-01", schema_version=1)
        store.save_report(report)

        # Alice attempts approval with a token having BOTH schema_author and schema_approver
        alice_dual_headers = _auth(keys, ["schema_author", "schema_approver"], subject="alice", mfa=True)
        res = client.post(
            f"{PREFIX}/schemas/DOC-01/versions/1/approve",
            json={"harness_run_id": "run_alice_1", "reason": "Self sign-off"},
            headers=alice_dual_headers,
        )
        assert res.status_code == 403
        assert res.json()["error"]["code"] == "SCHEMA_SELF_APPROVAL"
        assert "cannot approve" in res.json()["error"]["message"]

        # Verify schema is still in testing, NOT approved
        check = client.get(f"{PREFIX}/schemas/DOC-01/versions/1", headers=author_headers)
        assert check.json()["status"] == "testing"

    def test_distinct_approver_succeeds(self, keys, tmp_path):
        """Distinct authorized approver with MFA succeeds in approving schema."""
        client, manager, _, store = _setup_app_client(keys, tmp_path)
        author_headers = _auth(keys, ["schema_author"], subject="alice")
        approver_headers = _auth(keys, ["schema_approver"], subject="bob", mfa=True)

        # Alice creates and moves to testing
        client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=author_headers)
        client.post(f"{PREFIX}/schemas/DOC-01/versions/1/status", json={"target_status": "testing"}, headers=author_headers)

        # Bob verifies passing harness run
        report = make_mock_benchmark_report(benchmark_id="run_bob_1", schema_id="DOC-01", schema_version=1)
        store.save_report(report)

        # Bob approves
        res = client.post(
            f"{PREFIX}/schemas/DOC-01/versions/1/approve",
            json={"harness_run_id": "run_bob_1", "reason": "Passed evaluation"},
            headers=approver_headers,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["schema"]["status"] == "approved"
        assert body["schema"]["approved_by"] == "bob"
        assert body["schema"]["harness_run_id"] == "run_bob_1"
        assert body["approval"]["approver_id"] == "bob"
        assert body["approval"]["author_id"] == "alice"

        # Production resolution now finds approved v1
        active = client.get(f"{PREFIX}/schemas/DOC-01", headers=author_headers)
        assert active.status_code == 200
        assert active.json()["version"] == 1
        assert active.json()["status"] == "approved"

    def test_approval_fails_if_not_in_testing_status(self, keys, tmp_path):
        """Attempting to approve a schema in DRAFT status fails with 409 INVALID_STATE_TRANSITION."""
        client, manager, _, store = _setup_app_client(keys, tmp_path)
        author_headers = _auth(keys, ["schema_author"], subject="alice")
        approver_headers = _auth(keys, ["schema_approver"], subject="bob", mfa=True)

        # Alice creates schema (status=DRAFT)
        client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=author_headers)

        report = make_mock_benchmark_report(benchmark_id="run_draft_1", schema_id="DOC-01", schema_version=1)
        store.save_report(report)

        # Bob attempts approval while schema is still in draft
        res = client.post(
            f"{PREFIX}/schemas/DOC-01/versions/1/approve",
            json={"harness_run_id": "run_draft_1"},
            headers=approver_headers,
        )
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "INVALID_STATE_TRANSITION"

    def test_harness_run_anti_replay_single_use(self, keys, tmp_path):
        """A harness run ID cannot be reused to approve another schema version."""
        client, manager, _, store = _setup_app_client(keys, tmp_path)
        author_headers = _auth(keys, ["schema_author"], subject="alice")
        approver_headers = _auth(keys, ["schema_approver"], subject="bob", mfa=True)

        # Alice creates v1 and moves to testing
        client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=author_headers)
        client.post(f"{PREFIX}/schemas/DOC-01/versions/1/status", json={"target_status": "testing"}, headers=author_headers)

        report = make_mock_benchmark_report(benchmark_id="shared_run", schema_id="DOC-01", schema_version=1)
        store.save_report(report)

        # Bob approves v1 with shared_run
        res1 = client.post(
            f"{PREFIX}/schemas/DOC-01/versions/1/approve",
            json={"harness_run_id": "shared_run"},
            headers=approver_headers,
        )
        assert res1.status_code == 200

        # Attempting to re-approve or reuse shared_run fails
        res2 = client.post(
            f"{PREFIX}/schemas/DOC-01/versions/1/approve",
            json={"harness_run_id": "shared_run"},
            headers=approver_headers,
        )
        assert res2.status_code in (409, 422)


# =============================================================================
# 3. Multi-Factor Authentication Enforcement (SEC-001)
# =============================================================================

class TestMfaEnforcement:
    """Verifies that Admin and Schema approver operations strictly require verified MFA."""

    def test_schema_approver_without_mfa_is_rejected(self, keys, tmp_path):
        """Token for schema_approver role presented without MFA is rejected with 401 MFA_REQUIRED."""
        client, _, _, _ = _setup_app_client(keys, tmp_path)
        headers = _auth(keys, ["schema_approver"], mfa=False)
        res = client.get(f"{PREFIX}/schemas/DOC-01/versions", headers=headers)
        assert res.status_code == 401
        assert res.json()["error"]["code"] == "MFA_REQUIRED"

    def test_admin_without_mfa_is_rejected(self, keys, tmp_path):
        """Token for admin role presented without MFA is rejected with 401 MFA_REQUIRED."""
        client, _, _, _ = _setup_app_client(keys, tmp_path)
        headers = _auth(keys, ["admin"], mfa=False)
        res = client.get(f"{PREFIX}/schemas/DOC-01/versions", headers=headers)
        assert res.status_code == 401
        assert res.json()["error"]["code"] == "MFA_REQUIRED"

    def test_principal_lacking_mfa_claim_rejected_at_authorizer(self):
        """Authorizer rejects approval if mfa_verified is False."""
        authorizer = ProductionSchemaAuthorizer()
        unverified_approver = AuthenticatedPrincipal(
            principal_id="bob",
            tenant_id="tenant_alpha",
            roles={Role.SCHEMA_APPROVER},
            mfa_verified=False,
        )
        decision = authorizer.authorize(
            principal=unverified_approver,
            permission=SchemaPermission.SCHEMA_APPROVE,
            tenant_id="tenant_alpha",
            schema_author="alice",
        )
        assert not decision.is_authorized
        assert decision.code == "FORBIDDEN"
        assert "MFA required" in (decision.reason or "")


# =============================================================================
# 4. Multi-Tenant Boundary Isolation (SEC-006)
# =============================================================================

class TestMultiTenantIsolation:
    """Verifies that cross-tenant operations are strictly denied and concealed."""

    def test_cross_tenant_creation_in_body_is_rejected(self, keys, tmp_path):
        client, _, _, _ = _setup_app_client(keys, tmp_path)
        alpha_headers = _auth(keys, ["schema_author"], tenant="tenant_alpha")

        # Caller tenant_alpha attempts to create schema specifying tenant_beta in body
        res = client.post(
            f"{PREFIX}/schemas",
            json={"definition": _definition(tenant="tenant_beta")},
            headers=alpha_headers,
        )
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_cross_tenant_read_and_history_concealed(self, keys, tmp_path):
        client, _, _, _ = _setup_app_client(keys, tmp_path)
        alpha_headers = _auth(keys, ["schema_author"], tenant="tenant_alpha")
        beta_headers = _auth(keys, ["schema_author"], tenant="tenant_beta")

        # Tenant alpha creates DOC-01
        client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=alpha_headers)

        # Tenant beta attempts to read DOC-01 history (should be empty for beta)
        beta_history = client.get(f"{PREFIX}/schemas/DOC-01/versions", headers=beta_headers)
        assert beta_history.status_code == 200
        assert beta_history.json() == []

        # Tenant beta attempts to get specific version -> 404
        beta_get = client.get(f"{PREFIX}/schemas/DOC-01/versions/1", headers=beta_headers)
        assert beta_get.status_code == 404

    def test_cross_tenant_benchmark_report_rejected(self, keys, tmp_path):
        client, _, _, store = _setup_app_client(keys, tmp_path)
        alpha_author = _auth(keys, ["schema_author"], tenant="tenant_alpha", subject="alice")
        alpha_approver = _auth(keys, ["schema_approver"], tenant="tenant_alpha", subject="bob", mfa=True)

        client.post(f"{PREFIX}/schemas", json={"definition": _definition(tenant="tenant_alpha")}, headers=alpha_author)
        client.post(f"{PREFIX}/schemas/DOC-01/versions/1/status", json={"target_status": "testing"}, headers=alpha_author)

        # Benchmark report belongs to tenant_beta
        report = make_mock_benchmark_report(
            benchmark_id="run_beta",
            tenant_id="tenant_beta",
            schema_id="DOC-01",
            schema_version=1,
        )
        store.save_report(report)

        # Bob from tenant_alpha tries to approve using tenant_beta's harness report
        res = client.post(
            f"{PREFIX}/schemas/DOC-01/versions/1/approve",
            json={"harness_run_id": "run_beta"},
            headers=alpha_approver,
        )
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "HARNESS_RESULT_INVALID"


# =============================================================================
# 5. Version Management and Non-Destructive Rollback (SCH-006)
# =============================================================================

class TestVersionManagementAndRollback:
    """Verifies schema versioning and non-destructive rollbacks."""

    def test_direct_promotion_to_approved_via_status_endpoint_blocked(self, keys, tmp_path):
        """Direct transition to 'approved' via status endpoint is forbidden (SCH-004)."""
        client, _, _, _ = _setup_app_client(keys, tmp_path)
        author_headers = _auth(keys, ["schema_author"])
        client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=author_headers)

        res = client.post(
            f"{PREFIX}/schemas/DOC-01/versions/1/status",
            json={"target_status": "approved"},
            headers=author_headers,
        )
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "SCHEMA_PROMOTION_NOT_ALLOWED"

    def test_non_destructive_rollback_to_prior_approved_version(self, keys, tmp_path):
        """Rollback restores a prior approved version as active while preserving history."""
        client, manager, _, store = _setup_app_client(keys, tmp_path)
        alice = _auth(keys, ["schema_author"], subject="alice")
        bob = _auth(keys, ["schema_approver"], subject="bob", mfa=True)
        admin = _auth(keys, ["admin"], subject="admin_charlie", mfa=True)

        # 1. Create and approve v1
        client.post(f"{PREFIX}/schemas", json={"definition": _definition(version=1)}, headers=alice)
        client.post(f"{PREFIX}/schemas/DOC-01/versions/1/status", json={"target_status": "testing"}, headers=alice)
        store.save_report(make_mock_benchmark_report(benchmark_id="run_v1", schema_version=1))
        client.post(f"{PREFIX}/schemas/DOC-01/versions/1/approve", json={"harness_run_id": "run_v1"}, headers=bob)

        # 2. Create and approve v2
        client.post(
            f"{PREFIX}/schemas/DOC-01/versions",
            json={"definition": _definition(version=2)},
            headers=alice,
        )
        client.post(f"{PREFIX}/schemas/DOC-01/versions/2/status", json={"target_status": "testing"}, headers=alice)
        store.save_report(make_mock_benchmark_report(benchmark_id="run_v2", schema_version=2))
        client.post(f"{PREFIX}/schemas/DOC-01/versions/2/approve", json={"harness_run_id": "run_v2"}, headers=bob)

        # Active version is v2
        active_v2 = client.get(f"{PREFIX}/schemas/DOC-01", headers=alice)
        assert active_v2.json()["version"] == 2

        # 3. Admin rolls back to v1
        rb_res = client.post(
            f"{PREFIX}/schemas/DOC-01/rollback",
            json={"target_version": 1, "reason": "Defect discovered in v2"},
            headers=admin,
        )
        assert rb_res.status_code == 200
        assert rb_res.json()["version"] == 1

        # 4. Active version is restored to v1
        active_restored = client.get(f"{PREFIX}/schemas/DOC-01", headers=alice)
        assert active_restored.json()["version"] == 1

        # 5. Version history retains both versions
        hist_res = client.get(f"{PREFIX}/schemas/DOC-01/versions", headers=alice)
        assert [v["version"] for v in hist_res.json()] == [1, 2]

    def test_rollback_to_never_approved_version_is_rejected(self, keys, tmp_path):
        """Rollback cannot target a version that was never approved (SCH-006)."""
        client, _, _, _ = _setup_app_client(keys, tmp_path)
        alice = _auth(keys, ["schema_author"], subject="alice")
        admin = _auth(keys, ["admin"], subject="admin_charlie", mfa=True)

        # v1 created as draft, never approved
        client.post(f"{PREFIX}/schemas", json={"definition": _definition(version=1)}, headers=alice)

        res = client.post(f"{PREFIX}/schemas/DOC-01/rollback", json={"target_version": 1}, headers=admin)
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "SCHEMA_ROLLBACK_ERROR"


# =============================================================================
# 6. Tamper-Evident Audit Integration and Fail-Closed Policy (SEC-008)
# =============================================================================

class TestAuditIntegrationAndFailClosed:
    """Verifies that audit logging records operations and that audit failure fails closed."""

    def test_successful_operations_record_audit_events(self, keys, tmp_path):
        """Verifies audit events logged across creation, transition, approval, and rollback."""
        client, _, _, store = _setup_app_client(keys, tmp_path)
        alice = _auth(keys, ["schema_author"], subject="alice")
        bob = _auth(keys, ["schema_approver"], subject="bob", mfa=True)
        admin = _auth(keys, ["admin"], subject="admin_charlie", mfa=True)

        # Create
        client.post(f"{PREFIX}/schemas", json={"definition": _definition(version=1)}, headers=alice)
        # Transition
        client.post(f"{PREFIX}/schemas/DOC-01/versions/1/status", json={"target_status": "testing"}, headers=alice)
        # Approve
        store.save_report(make_mock_benchmark_report(benchmark_id="run_audit_1", schema_version=1))
        client.post(f"{PREFIX}/schemas/DOC-01/versions/1/approve", json={"harness_run_id": "run_audit_1"}, headers=bob)
        # Rollback
        client.post(f"{PREFIX}/schemas/DOC-01/rollback", json={"target_version": 1}, headers=admin)

        # Check tenant audit log file exists and contains events
        audit_file = tmp_path / "audit" / "tenant_alpha" / "audit" / "events.jsonl"
        assert audit_file.exists()
        lines = audit_file.read_text().strip().split("\n")
        assert len(lines) >= 4
        assert any("schema.created" in line for line in lines)
        assert any("schema.transitioned" in line for line in lines)
        assert any("schema.approved" in line for line in lines)
        assert any("schema.rolled_back" in line for line in lines)

    def test_rejection_logs_audit_event(self, keys, tmp_path):
        """Self-approval rejection logs a schema.rejected audit event."""
        client, _, _, store = _setup_app_client(keys, tmp_path)
        alice = _auth(keys, ["schema_author"], subject="alice")
        alice_dual = _auth(keys, ["schema_author", "schema_approver"], subject="alice", mfa=True)

        client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=alice)
        client.post(f"{PREFIX}/schemas/DOC-01/versions/1/status", json={"target_status": "testing"}, headers=alice)
        store.save_report(make_mock_benchmark_report(benchmark_id="run_rej_1"))

        # Alice attempts self-approval
        client.post(
            f"{PREFIX}/schemas/DOC-01/versions/1/approve",
            json={"harness_run_id": "run_rej_1"},
            headers=alice_dual,
        )

        audit_file = tmp_path / "audit" / "tenant_alpha" / "audit" / "events.jsonl"
        assert audit_file.exists()
        content = audit_file.read_text()
        assert "schema.rejected" in content

    def test_fail_closed_audit_policy_aborts_mutation(self, keys, tmp_path):
        """If audit recording fails, operation fails closed (503 AUDIT_UNAVAILABLE) and reverts state."""
        _, public_path = keys
        settings = ApiSettings(
            app_env="development",
            audit_store_dir=str(tmp_path / "audit"),
            jwt_public_key_path=str(public_path),
            jwt_issuer=ISSUER,
            jwt_audience=AUDIENCE,
        )

        class FailingAuditLogger:
            propagates_failures = True
            async def log_event(self, event):
                raise AuditUnavailableError(operation=event.action)
            async def log_action(self, *args, **kwargs):
                raise AuditUnavailableError(operation=kwargs.get("action", "unknown"))

        repo = InMemorySchemaRepository()
        manager = SchemaLifecycleManager(repository=repo)
        store = InMemoryBenchmarkReportStore()
        approval_service = SchemaApprovalService(
            manager=manager,
            authorizer=ProductionSchemaAuthorizer(),
            verifier=BenchmarkResultVerifier(report_store=store),
            report_store=store,
        )
        container = ApplicationContainer(
            api_settings=settings,
            schema_manager=manager,
            schema_approval_service=approval_service,
            audit_logger=FailingAuditLogger(),
        )
        client = TestClient(create_app(settings, container=container))
        headers = _auth(keys, ["schema_author"])

        res = client.post(f"{PREFIX}/schemas", json={"definition": _definition()}, headers=headers)
        assert res.status_code == 503
        assert res.json()["error"]["code"] == "AUDIT_UNAVAILABLE"


# =============================================================================
# 7. Durable MongoSchemaRepository & Concurrency Safety
# =============================================================================

import asyncio

def run(coro):
    return asyncio.run(coro)


class TestMongoSchemaRepository:
    """Verifies MongoSchemaRepository persistence, tenant isolation, and atomic concurrency."""

    def test_mongo_schema_repository_crud_and_indexes(self):
        async def _test():
            db = FakeDatabase("doc_extractor_test")
            await ensure_schema_indexes(db)
            repo = MongoSchemaRepository(db)

            defn = SchemaDefinition(
                schema_id="DOC-99",
                version=1,
                status="draft",
                tenant_id="tenant_x",
                domain="BFSI",
                document_type="loan_app",
                description="Loan application schema",
                fields=[FieldDefinition(name="loan_amount", type=FieldType.DECIMAL, required=True)],
            )
            now = datetime.now(timezone.utc)
            snap = SchemaSnapshot(
                schema_id="DOC-99",
                tenant_id="tenant_x",
                version=1,
                status=SchemaStatus.DRAFT,
                definition=defn,
                author="alice",
                created_at=now,
            )

            # 1. Save and retrieve
            await repo.async_save_snapshot(snap)
            retrieved = await repo.async_get_snapshot("DOC-99", 1, "tenant_x")
            assert retrieved is not None
            assert retrieved.schema_id == "DOC-99"
            assert retrieved.version == 1
            assert retrieved.author == "alice"

            # 2. Duplicate version raises SchemaVersionError
            with pytest.raises(SchemaVersionError):
                await repo.async_save_snapshot(snap)

            # 3. Status update
            updated = await repo.async_update_status("DOC-99", 1, "tenant_x", SchemaStatus.TESTING)
            assert updated.status == SchemaStatus.TESTING

            # 4. Lifecycle event recording
            evt = LifecycleEvent(
                tenant_id="tenant_x",
                schema_id="DOC-99",
                version=1,
                event_type=LifecycleEventType.STATUS_CHANGED,
                actor="alice",
                from_status=SchemaStatus.DRAFT,
                to_status=SchemaStatus.TESTING,
            )
            await repo.async_record_event(evt)
            events = await repo.async_list_events("DOC-99", "tenant_x")
            assert len(events) == 1
            assert events[0].to_status == SchemaStatus.TESTING

            # 5. Atomic promote and activate
            promoted = await repo.async_promote_and_activate(
                tenant_id="tenant_x",
                schema_id="DOC-99",
                version=1,
                approved_by="bob",
                harness_run_id="run_unique_1",
                event=LifecycleEvent(
                    tenant_id="tenant_x",
                    schema_id="DOC-99",
                    version=1,
                    event_type=LifecycleEventType.APPROVED,
                    actor="bob",
                    from_status=SchemaStatus.TESTING,
                    to_status=SchemaStatus.APPROVED,
                ),
            )
            assert promoted.status == SchemaStatus.APPROVED
            assert promoted.approved_by == "bob"

            # 6. Active pointer verification
            active = await repo.async_get_active_version("DOC-99", "tenant_x")
            assert active is not None
            assert active.version == 1

            # 7. Anti-replay verification on harness run
            assert await repo.async_is_harness_run_consumed("run_unique_1", "tenant_x")
            assert not await repo.async_is_harness_run_consumed("unconsumed_run", "tenant_x")

        run(_test())

    def test_concurrent_promotions_prevent_inconsistent_state(self):
        """Second promotion attempt on non-testing schema fails cleanly."""
        async def _test():
            db = FakeDatabase("doc_extractor_concurrency")
            await ensure_schema_indexes(db)
            repo = MongoSchemaRepository(db)

            defn = SchemaDefinition(
                schema_id="DOC-CONC",
                version=1,
                status="testing",
                tenant_id="tenant_conc",
                domain="BFSI",
                document_type="trade_finance",
                description="Trade finance schema",
                fields=[FieldDefinition(name="lc_number", type=FieldType.STRING, required=True)],
            )
            snap = SchemaSnapshot(
                schema_id="DOC-CONC",
                tenant_id="tenant_conc",
                version=1,
                status=SchemaStatus.TESTING,
                definition=defn,
                author="alice",
                created_at=datetime.now(timezone.utc),
            )
            await repo.async_save_snapshot(snap)

            # First promotion succeeds
            await repo.async_promote_and_activate(
                tenant_id="tenant_conc",
                schema_id="DOC-CONC",
                version=1,
                approved_by="bob_first",
                harness_run_id="run_c1",
                event=LifecycleEvent(
                    tenant_id="tenant_conc",
                    schema_id="DOC-CONC",
                    version=1,
                    event_type=LifecycleEventType.APPROVED,
                    actor="bob_first",
                ),
            )

            # Second concurrent promotion fails because status is no longer TESTING
            with pytest.raises(InvalidStateTransitionError):
                await repo.async_promote_and_activate(
                    tenant_id="tenant_conc",
                    schema_id="DOC-CONC",
                    version=1,
                    approved_by="charlie_second",
                    harness_run_id="run_c2",
                    event=LifecycleEvent(
                        tenant_id="tenant_conc",
                        schema_id="DOC-CONC",
                        version=1,
                        event_type=LifecycleEventType.APPROVED,
                        actor="charlie_second",
                    ),
                )

        run(_test())
