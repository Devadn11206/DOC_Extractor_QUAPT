"""
Tests for Phase 5 (Module E: FastAPI Integration, Review Workbench, Webhooks & Prometheus Observability).
"""

from io import BytesIO
import pytest
from fastapi.testclient import TestClient

from doc_extractor.api.app import app, review_task_mgr, audit_log, webhook_mgr
from tests.conftest import create_minimal_pdf


@pytest.fixture
def client():
    return TestClient(app)


def test_health_endpoint(client):
    res = client.get("/api/v1/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "healthy"
    assert "version" in data
    assert data["audit_chain_verified"] is True


def test_metrics_endpoint(client):
    res = client.get("/api/v1/metrics")
    assert res.status_code == 200
    assert "doc_extractor_documents_total" in res.text


def test_document_intake_validation_and_idempotency(client):
    import uuid
    pdf_bytes = create_minimal_pdf()
    files = {"file": ("test_doc.pdf", pdf_bytes, "application/pdf")}
    unique_key = f"idem_phase5_{uuid.uuid4().hex}"
    headers = {"Idempotency-Key": unique_key, "X-Tenant-ID": "tenant_test"}

    # Initial upload
    res = client.post("/api/v1/documents", files=files, headers=headers)
    assert res.status_code == 202
    data = res.json()
    assert "document_id" in data
    assert data["status"] == "queued"
    doc_id = data["document_id"]

    # Replay with same idempotency key
    files_replay = {"file": ("test_doc.pdf", pdf_bytes, "application/pdf")}
    res_replay = client.post("/api/v1/documents", files=files_replay, headers=headers)
    assert res_replay.status_code == 200
    data_replay = res_replay.json()
    assert data_replay["document_id"] == doc_id
    assert data_replay["idempotent_replay"] is True


def test_document_intake_unsupported_file(client):
    files = {"file": ("evil.exe", b"MZ_FAKE_EXECUTABLE_BYTES", "application/octet-stream")}
    res = client.post("/api/v1/documents", files=files)
    assert res.status_code == 400
    data = res.json()
    assert data["error"]["code"] == "UNSUPPORTED_FILE"


def test_review_task_lifecycle_and_validation(client):
    # Create task
    task = review_task_mgr.create_task(
        document_id="doc_rev_001",
        tenant_id="tenant_test",
        flagged_fields=["applicant.pan_card_number"],
        flag_reasons={"INVALID_PAN_FORMAT": "Wrong format"},
    )

    # List tasks
    res = client.get("/api/v1/review-tasks", headers={"X-Tenant-ID": "tenant_test"})
    assert res.status_code == 200
    assert any(t["task_id"] == task.task_id for t in res.json()["tasks"])

    # Claim task
    res = client.post(
        f"/api/v1/review-tasks/{task.task_id}/claim",
        headers={"X-User-ID": "reviewer_a", "X-User-Name": "Alice"},
    )
    assert res.status_code == 200
    assert res.json()["claimed"] is True

    # Conflicting claim returns 409
    res_conflict = client.post(
        f"/api/v1/review-tasks/{task.task_id}/claim",
        headers={"X-User-ID": "reviewer_b", "X-User-Name": "Bob"},
    )
    assert res_conflict.status_code == 409
    assert res_conflict.json()["error"]["code"] == "TASK_CLAIMED"

    # Complete task with INVALID correction (fails Module D validation)
    res_invalid_complete = client.post(
        f"/api/v1/review-tasks/{task.task_id}/complete",
        json={"corrections": [{"field_name": "applicant.pan_card_number", "value": "INVALID_PAN"}]},
        headers={"X-User-ID": "reviewer_a", "X-Tenant-ID": "tenant_test"},
    )
    assert res_invalid_complete.status_code == 400
    assert res_invalid_complete.json()["error"]["code"] == "VALIDATION_FAILED"

    # Complete task with VALID correction
    res_valid_complete = client.post(
        f"/api/v1/review-tasks/{task.task_id}/complete",
        json={"corrections": [{"field_name": "applicant.pan_card_number", "value": "ABCDE1234F"}]},
        headers={"X-User-ID": "reviewer_a", "X-Tenant-ID": "tenant_test"},
    )
    assert res_valid_complete.status_code == 200
    assert res_valid_complete.json()["status"] == "reviewed"


def test_audit_log_hash_chain_and_tamper_detection(client):
    audit_log.record_event(
        event_type="test.event.1",
        tenant_id="tenant_1",
        actor_id="user_1",
        resource_type="doc",
        resource_id="doc_1",
    )
    audit_log.record_event(
        event_type="test.event.2",
        tenant_id="tenant_1",
        actor_id="user_1",
        resource_type="doc",
        resource_id="doc_2",
    )

    res = client.get("/api/v1/audit-events", headers={"X-Tenant-ID": "tenant_1"})
    assert res.status_code == 200
    assert res.json()["integrity_verified"] is True

    # Tamper with event details
    audit_log._events[-1].details = {"tampered": True}
    assert audit_log.verify_integrity() is False


def test_webhooks_and_hmac_signatures(client):
    res = client.post(
        "/api/v1/webhooks",
        json={
            "target_url": "https://callback.internal/webhook",
            "secret": "top_secret_key",
            "events": ["document.completed"],
        },
        headers={"X-Tenant-ID": "tenant_wh"},
    )
    assert res.status_code == 200
    sub_id = res.json()["subscription_id"]

    # Dispatch event
    deliveries = webhook_mgr.dispatch_event(
        tenant_id="tenant_wh",
        event_type="document.completed",
        payload={"doc_id": "123", "status": "completed"},
    )
    assert len(deliveries) == 1
    assert deliveries[0].signature.startswith("sha256=")


def test_kill_switch_pause_and_resume(client):
    # Pause auto-approval
    res_pause = client.post("/api/v1/admin/auto-approval/pause", headers={"X-User-ID": "admin"})
    assert res_pause.status_code == 200
    assert res_pause.json()["auto_approval_paused"] is True

    # Resume auto-approval
    res_resume = client.post("/api/v1/admin/resume", headers={"X-User-ID": "admin"})
    assert res_resume.status_code == 200
    assert res_resume.json()["auto_approval_paused"] is False
