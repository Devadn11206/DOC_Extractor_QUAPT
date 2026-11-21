"""
FastAPI Application for Phase 5 (Module E: API Gateway, Review Workbench, Webhooks, Observability).
Adheres strictly to PRD Sections 5.6, 5.7, 6, 7, 18, 21 and API-001..024.
"""

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from doc_extractor import __version__
from doc_extractor.api.audit import TamperEvidentAuditLog
from doc_extractor.api.metrics import (
    DOCUMENT_PROCESSED_TOTAL,
    FIELD_CONFIDENCE_HISTOGRAM,
    PAGE_EXTRACTION_LATENCY,
    get_prometheus_metrics,
)
from doc_extractor.api.review import ReviewCorrectionItem, ReviewTaskManager
from doc_extractor.api.webhooks import WebhookManager
from doc_extractor.config import Settings, load_settings
from doc_extractor.contracts_b import MVPClassificationDomain, MVPDocumentType
from doc_extractor.contracts_c import CanonicalExtractionEnvelope
from doc_extractor.models import DocumentStatus
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.storage import LocalDiskStorage


def error_response(code: str, message: str, status_code: int = 400, details: Optional[Dict[str, Any]] = None, request_id: Optional[str] = None):
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id or str(uuid.uuid4()),
                "details": details or {},
            }
        },
    )


# System state singletons
audit_log = TamperEvidentAuditLog()
webhook_mgr = WebhookManager()
review_task_mgr = ReviewTaskManager()
import tempfile
idempotency_store = LocalIdempotencyStore(base_dir=tempfile.gettempdir())

# Global emergency kill switch state (GR-015, API-022)
KILL_SWITCH_AUTO_APPROVAL_PAUSED = False

# Document database cache
DOCUMENT_STORE: Dict[str, Dict[str, Any]] = {}
PAGE_IMAGES: Dict[str, Dict[int, bytes]] = {}

app = FastAPI(
    title="Document Extraction Engine API",
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.middleware("http")
async def error_handling_middleware(request: Request, call_next):
    req_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        return response
    except HTTPException as e:
        return error_response(
            code=getattr(e, "code", "HTTP_EXCEPTION"),
            message=str(e.detail),
            status_code=e.status_code,
            request_id=req_id,
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return error_response(
            code="INTERNAL_SERVER_ERROR",
            message=f"An unexpected internal server error occurred: {str(e)}",
            status_code=500,
            request_id=req_id,
        )


# --- API-009: Health & Metrics ---

@app.get("/api/v1/health")
def get_health():
    return {
        "status": "healthy",
        "service": "doc-extractor-api",
        "version": __version__,
        "auto_approval_paused": KILL_SWITCH_AUTO_APPROVAL_PAUSED,
        "audit_chain_verified": audit_log.verify_integrity(),
    }


@app.get("/api/v1/metrics")
def get_metrics():
    content = get_prometheus_metrics()
    return Response(content=content, media_type="text/plain; version=0.0.4")


# --- API-001, API-002, API-003, API-004, API-015: Document Intake & Processing ---

@app.post("/api/v1/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    document_type_hint: Optional[str] = Form(None),
    schema_id: Optional[str] = Form(None),
    callback_url: Optional[str] = Form(None),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    tenant_id: str = Header("default_tenant", alias="X-Tenant-ID"),
):
    req_id = str(uuid.uuid4())
    raw_bytes = await file.read()
    if not raw_bytes:
        return error_response("EMPTY_FILE", "The uploaded file is empty.", 400, request_id=req_id)

    # FR-ING-002 / GR-001 Size check (50 MB)
    if len(raw_bytes) > 50 * 1024 * 1024:
        return error_response("FILE_TOO_LARGE", "File exceeds 50 MB limit.", 413, request_id=req_id)

    # Magic byte check (FR-ING-001 / FR-ING-002)
    from doc_extractor.validation import detect_magic_format
    detected_fmt = detect_magic_format(raw_bytes)
    if not detected_fmt:
        return error_response("UNSUPPORTED_FILE", "Unsupported or corrupted file format.", 400, request_id=req_id)

    # Idempotency check (FR-ING-006, API-013)
    import hashlib
    import time
    sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if idempotency_key:
        cached_record = idempotency_store.get_record(tenant_id, idempotency_key)
        if cached_record and cached_record.request_fingerprint == sha256:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=cached_record.response_payload,
            )

    doc_id = f"doc_{uuid.uuid4().hex[:12]}"
    DOCUMENT_STORE[doc_id] = {
        "document_id": doc_id,
        "tenant_id": tenant_id,
        "status": "queued",
        "filename": file.filename,
        "document_type_hint": document_type_hint,
        "schema_id": schema_id,
        "callback_url": callback_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sha256": sha256,
        "envelope": None,
    }

    if idempotency_key:
        from doc_extractor.models import IdempotencyRecord
        now = time.time()
        idempotency_store.save_record(
            IdempotencyRecord(
                idempotency_key=idempotency_key,
                tenant_id=tenant_id,
                request_fingerprint=sha256,
                document_id=doc_id,
                response_payload={
                    "document_id": doc_id,
                    "status": "queued",
                    "idempotent_replay": True,
                },
                created_at_utc=now,
                expires_at_utc=now + 86400.0,
            )
        )

    # Record intake audit event
    audit_log.record_event(
        event_type="document.ingested",
        tenant_id=tenant_id,
        actor_id="api_client",
        resource_type="document",
        resource_id=doc_id,
        details={"sha256": sha256, "filename": file.filename},
    )

    # Execute async pipeline
    def process_task():
        try:
            DOCUMENT_STORE[doc_id]["status"] = "processing"
            settings = load_settings()
            mod_a = ModuleAPipeline(settings=settings, tenant_id=tenant_id)
            stream = io.BytesIO(raw_bytes)
            handoff = mod_a.process_document(stream, file.filename or "upload.pdf")

            # Store page image bytes for API-003
            PAGE_IMAGES[doc_id] = {}
            for p in handoff.pages:
                try:
                    img_path = mod_a.storage.resolve_uri_to_path(p.preprocessed_storage_uri)
                    if img_path.exists():
                        PAGE_IMAGES[doc_id][p.page_number] = img_path.read_bytes()
                except Exception:
                    pass

            # Execute Module B
            mod_b = ModuleBPipeline(storage=mod_a.storage, tenant_id=tenant_id)
            mod_b_out = mod_b.process_handoff(handoff)

            # Execute Module C
            mod_c = ModuleCPipeline()
            env = mod_c.process(mod_b_out)

            # Enforce Emergency Kill Switch (GR-015, API-022)
            if KILL_SWITCH_AUTO_APPROVAL_PAUSED:
                env.document_metadata.status = DocumentStatus.NEEDS_REVIEW
                env.flags.append({"code": "KILL_SWITCH_ACTIVE", "message": "Auto-approval paused by administrator."})

            final_status = env.document_metadata.status.value if hasattr(env.document_metadata.status, "value") else str(env.document_metadata.status)
            DOCUMENT_STORE[doc_id]["status"] = final_status
            DOCUMENT_STORE[doc_id]["envelope"] = env.model_dump()
            DOCUMENT_STORE[doc_id]["completed_at"] = datetime.now(timezone.utc).isoformat()

            # Metrics
            doc_type_name = env.document_metadata.document_type
            DOCUMENT_PROCESSED_TOTAL.labels(tenant_id=tenant_id, document_type=doc_type_name, status=final_status).inc()

            # Create review task if needs_review
            if final_status == "needs_review":
                flagged = [f for f, field in env.fields.items() if field.review_required or field.validation_status == "validation_failed"]
                review_task_mgr.create_task(
                    document_id=doc_id,
                    tenant_id=tenant_id,
                    flagged_fields=flagged,
                    flag_reasons={flag.code: flag.message for flag in env.flags},
                )

            # Webhook dispatch
            webhook_mgr.dispatch_event(
                tenant_id=tenant_id,
                event_type=f"document.{final_status}",
                payload={"document_id": doc_id, "status": final_status},
            )

        except Exception as exc:
            DOCUMENT_STORE[doc_id]["status"] = "failed"
            DOCUMENT_STORE[doc_id]["error"] = str(exc)
            webhook_mgr.dispatch_event(
                tenant_id=tenant_id,
                event_type="document.failed",
                payload={"document_id": doc_id, "error": str(exc)},
            )

    background_tasks.add_task(process_task)

    return {"document_id": doc_id, "status": "queued"}


@app.get("/api/v1/documents")
def list_documents(
    status_filter: Optional[str] = Query(None, alias="status"),
    document_type: Optional[str] = Query(None, alias="documentType"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    tenant_id: str = Header("default_tenant", alias="X-Tenant-ID"),
):
    docs = [d for d in DOCUMENT_STORE.values() if d["tenant_id"] == tenant_id]
    if status_filter:
        docs = [d for d in docs if d["status"] == status_filter]
    if document_type:
        docs = [d for d in docs if d.get("envelope", {}) and d["envelope"].get("document_type") == document_type]

    # Stat strip (API-015, DOCX-SCR-002)
    stats = {
        "total": len(docs),
        "processing": sum(1 for d in docs if d["status"] == "processing"),
        "needs_review": sum(1 for d in docs if d["status"] == "needs_review"),
        "auto_approved": sum(1 for d in docs if d["status"] == "auto_approved"),
        "failed": sum(1 for d in docs if d["status"] == "failed"),
    }

    start = (page - 1) * page_size
    paged_items = docs[start : start + page_size]
    return {
        "items": paged_items,
        "stats": stats,
        "page": page,
        "page_size": page_size,
        "total": len(docs),
    }


@app.get("/api/v1/documents/{id}")
def get_document(id: str, tenant_id: str = Header("default_tenant", alias="X-Tenant-ID")):
    doc = DOCUMENT_STORE.get(id)
    if not doc or doc["tenant_id"] != tenant_id:
        return error_response("DOCUMENT_NOT_FOUND", f"Document {id} not found.", 404)
    return doc


@app.get("/api/v1/documents/{id}/pages/{n}")
def get_page_image(id: str, n: int, tenant_id: str = Header("default_tenant", alias="X-Tenant-ID")):
    doc = DOCUMENT_STORE.get(id)
    if not doc or doc["tenant_id"] != tenant_id:
        return error_response("DOCUMENT_NOT_FOUND", f"Document {id} not found.", 404)
    pages = PAGE_IMAGES.get(id, {})
    img_bytes = pages.get(n)
    if not img_bytes:
        # Fallback 1x1 PNG image
        return Response(content=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82", media_type="image/png")
    return Response(content=img_bytes, media_type="image/png")


# --- API-017: Document Export with PII Masking ---

@app.get("/api/v1/documents/{id}/export")
def export_document(
    id: str,
    format: str = Query("json", pattern="^(json|csv|xml)$"),
    unmask: bool = Query(False),
    tenant_id: str = Header("default_tenant", alias="X-Tenant-ID"),
):
    doc = DOCUMENT_STORE.get(id)
    if not doc or doc["tenant_id"] != tenant_id:
        return error_response("DOCUMENT_NOT_FOUND", f"Document {id} not found.", 404)

    env = doc.get("envelope") or {}
    fields = dict(env.get("fields", {}))

    # Mask PII unless unmask=True (SEC-007)
    if not unmask:
        for k, v in fields.items():
            if any(term in k.lower() for term in ["pan", "aadhaar", "phone", "account"]):
                v_copy = dict(v)
                raw_val = str(v_copy.get("value", ""))
                v_copy["value"] = raw_val[:2] + "X" * max(0, len(raw_val) - 4) + raw_val[-2:] if len(raw_val) > 4 else "XXXX"
                fields[k] = v_copy

    if format == "json":
        return {"document_id": id, "fields": fields}
    elif format == "csv":
        import csv
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["field", "value", "confidence"])
        for k, v in fields.items():
            writer.writerow([k, v.get("value"), v.get("calibrated_confidence")])
        return Response(content=output.getvalue(), media_type="text/csv")
    elif format == "xml":
        xml_lines = ["<document>", f"  <id>{id}</id>", "  <fields>"]
        for k, v in fields.items():
            xml_lines.append(f"    <{k}>{v.get('value')}</{k}>")
        xml_lines.extend(["  </fields>", "</document>"])
        return Response(content="\n".join(xml_lines), media_type="application/xml")


# --- API-005, API-016: Review Workbench Tasks ---

@app.get("/api/v1/review-tasks")
def list_review_tasks(
    status: Optional[str] = Query(None),
    tenant_id: str = Header("default_tenant", alias="X-Tenant-ID"),
):
    tasks = review_task_mgr.list_tasks(tenant_id=tenant_id, status=status)
    return {"tasks": tasks, "count": len(tasks)}


@app.post("/api/v1/review-tasks/{id}/claim")
def claim_review_task(
    id: str,
    user_id: str = Header("reviewer_1", alias="X-User-ID"),
    user_name: str = Header("Reviewer Alpha", alias="X-User-Name"),
):
    task, err = review_task_mgr.claim_task(task_id=id, user_id=user_id, user_name=user_name)
    if err == "NOT_FOUND":
        return error_response("TASK_NOT_FOUND", f"Task {id} not found.", 404)
    if err == "TASK_CLAIMED":
        return error_response("TASK_CLAIMED", f"Task {id} is already claimed by {task.assignee_name}.", 409)
    return {"task": task, "claimed": True}


@app.post("/api/v1/review-tasks/{id}/release")
def release_review_task(
    id: str,
    user_id: str = Header("reviewer_1", alias="X-User-ID"),
):
    success = review_task_mgr.release_task(task_id=id, user_id=user_id)
    if not success:
        return error_response("RELEASE_FAILED", f"Task {id} cannot be released by user {user_id}.", 400)
    return {"released": True}


class CompleteReviewRequest(BaseModel):
    corrections: List[ReviewCorrectionItem]


@app.post("/api/v1/review-tasks/{id}/complete")
def complete_review_task(
    id: str,
    req: CompleteReviewRequest,
    user_id: str = Header("reviewer_1", alias="X-User-ID"),
    tenant_id: str = Header("default_tenant", alias="X-Tenant-ID"),
):
    task = review_task_mgr.get_task(id)
    if not task:
        return error_response("TASK_NOT_FOUND", f"Task {id} not found.", 404)

    # Validate corrections using Module D rules (FR-REV-003)
    validation_errors = review_task_mgr.validate_corrections(req.corrections)
    if validation_errors:
        return error_response(
            "VALIDATION_FAILED",
            "Field validation failed on rule check.",
            400,
            details=validation_errors,
        )

    task.status = "completed"
    doc_id = task.document_id
    if doc_id in DOCUMENT_STORE:
        DOCUMENT_STORE[doc_id]["status"] = "reviewed"
        env = DOCUMENT_STORE[doc_id].get("envelope", {})
        fields = env.get("fields", {})
        for c in req.corrections:
            if c.field_name in fields:
                fields[c.field_name]["value"] = c.value
                fields[c.field_name]["validation_status"] = "passed"
                fields[c.field_name]["review_required"] = False

    audit_log.record_event(
        event_type="review.completed",
        tenant_id=tenant_id,
        actor_id=user_id,
        resource_type="review_task",
        resource_id=id,
        details={"document_id": doc_id, "corrections": [c.model_dump() for c in req.corrections]},
    )

    return {"status": "reviewed", "task_id": id, "document_id": doc_id}


# --- API-006, API-007, API-018: Schema Studio & Lifecycle Engine ---

from schema_lifecycle.manager import SchemaLifecycleManager
from schema_lifecycle.repository import InMemorySchemaRepository
from schema_lifecycle.serializer import schema_from_yaml, schema_to_dict

schema_repo = InMemorySchemaRepository()
schema_mgr = SchemaLifecycleManager(repository=schema_repo)


class CreateSchemaRequest(BaseModel):
    name: str
    document_type: str
    yaml_content: str


@app.get("/api/v1/schemas")
def list_schemas(tenant_id: str = Header("default_tenant", alias="X-Tenant-ID")):
    versions = schema_repo.list_versions(tenant_id=tenant_id, schema_id=None) if hasattr(schema_repo, "list_versions") else []
    return {"schemas": [v.model_dump() if hasattr(v, "model_dump") else str(v) for v in versions]}


@app.post("/api/v1/schemas")
def create_schema(
    req: CreateSchemaRequest,
    tenant_id: str = Header("default_tenant", alias="X-Tenant-ID"),
    author_id: str = Header("author_1", alias="X-User-ID"),
):
    try:
        schema = schema_from_yaml(req.yaml_content)
        schema.tenant_id = tenant_id
        schema.created_by = author_id
        saved = schema_mgr.create_schema(tenant_id=tenant_id, definition=schema, author=author_id)
        return {"schema_id": saved.schema_id, "status": saved.status.value, "version": saved.version}
    except Exception as e:
        return error_response("INVALID_SCHEMA", str(e), 400)


@app.post("/api/v1/schemas/{id}/approve")
def approve_schema(
    id: str,
    version: int = Query(1),
    tenant_id: str = Header("default_tenant", alias="X-Tenant-ID"),
    approver_id: str = Header("approver_1", alias="X-User-ID"),
):
    try:
        snapshot = schema_repo.get_snapshot(tenant_id=tenant_id, schema_id=id, version=version)
        if not snapshot:
            return error_response("SCHEMA_NOT_FOUND", f"Schema {id} v{version} not found.", 404)
        if snapshot.definition.created_by == approver_id:
            return error_response("SCHEMA_SELF_APPROVAL", "Author cannot approve their own schema.", 403)
        approved = schema_mgr.internal_promote_to_approved(tenant_id=tenant_id, schema_id=id, version=version, approver=approver_id)
        return {"schema_id": id, "status": approved.status.value, "version": approved.version}
    except Exception as e:
        return error_response("APPROVAL_FAILED", str(e), 400)


@app.post("/api/v1/schemas/{id}/rollback")
def rollback_schema(
    id: str,
    target_version: int = Query(...),
    tenant_id: str = Header("default_tenant", alias="X-Tenant-ID"),
    actor_id: str = Header("admin_1", alias="X-User-ID"),
):
    try:
        rb = schema_mgr.rollback(tenant_id=tenant_id, schema_id=id, target_version=target_version, actor=actor_id)
        return {"schema_id": id, "status": rb.status.value, "version": rb.version}
    except Exception as e:
        return error_response("ROLLBACK_FAILED", str(e), 400)


# --- API-008: Webhooks & Audit Events ---

class RegisterWebhookRequest(BaseModel):
    target_url: str
    secret: str
    events: List[str]


@app.post("/api/v1/webhooks")
def register_webhook(
    req: RegisterWebhookRequest,
    tenant_id: str = Header("default_tenant", alias="X-Tenant-ID"),
):
    sub = webhook_mgr.register_subscription(
        tenant_id=tenant_id,
        target_url=req.target_url,
        secret=req.secret,
        events=req.events,
    )
    return {"subscription_id": sub.id, "registered": True}


@app.get("/api/v1/audit-events")
def list_audit_events(
    event_type: Optional[str] = Query(None),
    resource_id: Optional[str] = Query(None),
    tenant_id: str = Header("default_tenant", alias="X-Tenant-ID"),
):
    events = audit_log.list_events(tenant_id=tenant_id, event_type=event_type, resource_id=resource_id)
    return {
        "events": [e.model_dump() for e in events],
        "integrity_verified": audit_log.verify_integrity(),
    }


# --- API-022, GR-015: Emergency Kill Switch ---

@app.post("/api/v1/admin/auto-approval/pause")
def pause_auto_approval(actor_id: str = Header("admin_1", alias="X-User-ID"), tenant_id: str = Header("default_tenant", alias="X-Tenant-ID")):
    global KILL_SWITCH_AUTO_APPROVAL_PAUSED
    KILL_SWITCH_AUTO_APPROVAL_PAUSED = True
    audit_log.record_event(
        event_type="admin.auto_approval.paused",
        tenant_id=tenant_id,
        actor_id=actor_id,
        resource_type="system",
        resource_id="kill_switch",
    )
    return {"auto_approval_paused": True, "message": "Auto-approval paused. All documents route to human review."}


@app.post("/api/v1/admin/resume")
def resume_auto_approval(actor_id: str = Header("admin_1", alias="X-User-ID"), tenant_id: str = Header("default_tenant", alias="X-Tenant-ID")):
    global KILL_SWITCH_AUTO_APPROVAL_PAUSED
    KILL_SWITCH_AUTO_APPROVAL_PAUSED = False
    audit_log.record_event(
        event_type="admin.auto_approval.resumed",
        tenant_id=tenant_id,
        actor_id=actor_id,
        resource_type="system",
        resource_id="kill_switch",
    )
    return {"auto_approval_paused": False, "message": "Normal auto-approval resumed."}
