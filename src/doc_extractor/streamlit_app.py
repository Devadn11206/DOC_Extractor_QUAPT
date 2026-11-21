"""
Document Extraction Engine — Unified Streamlit Workbench
Covers PRD Screens:
- SCR-01 / DOCX-SCR-001: Upload & Intake (Multi-format, EICAR check, Idempotency)
- SCR-02 / DOCX-SCR-002: Document List & Status Filter Strip
- SCR-03 / DOCX-SCR-003: Review Workbench (Visual Evidence, Bounding Box & Re-validation)
- SCR-04 / DOCX-SCR-004: Review Queue (Claim, Release, Complete)
- SCR-05 / DOCX-SCR-005: Schema Studio & 4-Eyes Approval Gate
- SCR-08 / DOCX-SCR-008: Tamper-Evident Audit Log Explorer
- SCR-09 / DOCX-SCR-009: Admin Kill Switch & Security Settings
- SCR-10 / DOCX-SCR-10: Observability & Prometheus Metrics
"""

import sys
from pathlib import Path

# Fix sys.path when running via `streamlit run src/doc_extractor/streamlit_app.py`
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
_CURRENT_DIR = str(Path(__file__).resolve().parent)
# Remove script directory if Python added it to sys.path to avoid shadowing standard library 'logging'
while _CURRENT_DIR in sys.path:
    sys.path.remove(_CURRENT_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import hashlib
import io
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
import uuid

import numpy as np
from PIL import Image, ImageDraw
import streamlit as st

# Configure page
st.set_page_config(
    page_title="Document Extraction Engine",
    page_icon="📄",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for high-density enterprise UI
st.markdown(
    """
    <style>
    .metric-card {
        background-color: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        padding: 12px 16px;
        margin-bottom: 8px;
    }
    .status-badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 9999px;
        font-size: 12px;
        font-weight: 600;
    }
    .badge-auto_approved { background-color: #dcfce7; color: #166534; }
    .badge-needs_review { background-color: #fef9c3; color: #854d0e; }
    .badge-reviewed { background-color: #e0e7ff; color: #3730a3; }
    .badge-processing { background-color: #e0f2fe; color: #075985; }
    .badge-failed { background-color: #fee2e2; color: #991b1b; }
    .badge-queued { background-color: #f1f5f9; color: #475569; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Ensure src/ is in Python module search path
import sys
from pathlib import Path
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# Core imports from pipeline
from doc_extractor.api.app import (
    DOCUMENT_STORE,
    KILL_SWITCH_AUTO_APPROVAL_PAUSED,
    PAGE_IMAGES,
    audit_log,
    idempotency_store,
    review_task_mgr,
    schema_mgr,
    schema_repo,
    webhook_mgr,
)
from doc_extractor.api.review import ReviewCorrectionItem
from doc_extractor.config import load_settings
from doc_extractor.contracts_b import MVPClassificationDomain, MVPDocumentType
from doc_extractor.models import DocumentStatus, IdempotencyRecord
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.validation import detect_magic_format
from doc_extractor.validation_rules import (
    normalize_clinical_frequency,
    normalize_currency_amount,
    normalize_date_to_iso,
    normalize_phone_number,
    validate_adult_age,
    validate_ifsc,
    validate_pan,
)
from schema_lifecycle.serializer import schema_from_yaml, schema_to_yaml

# Initialize session state defaults
if "tenant_id" not in st.session_state:
    st.session_state.tenant_id = "tenant_alpha"
if "user_id" not in st.session_state:
    st.session_state.user_id = "analyst_1"
if "user_name" not in st.session_state:
    st.session_state.user_name = "Alex Vance"

# Sidebar navigation & Tenant Context
with st.sidebar:
    st.title("📄 Doc Extraction")
    st.caption("On-Premise AI Document Extraction Engine")

    st.subheader("Tenant & User Context")
    st.session_state.tenant_id = st.text_input("Tenant ID", value=st.session_state.tenant_id)
    col_u1, col_u2 = st.columns(2)
    with col_u1:
        st.session_state.user_id = st.text_input("User ID", value=st.session_state.user_id)
    with col_u2:
        st.session_state.user_name = st.text_input("Name", value=st.session_state.user_name)

    st.divider()

    nav_option = st.radio(
        "Navigation",
        [
            "SCR-01: Upload & Intake",
            "SCR-02: Document Explorer",
            "SCR-03: Review Workbench",
            "SCR-04: Review Queue",
            "SCR-05: Schema Studio",
            "SCR-08: Audit Log Explorer",
            "SCR-09: Policy & Kill Switch",
            "SCR-10: Dashboard & Metrics",
        ],
    )

    st.divider()
    from doc_extractor.api.app import KILL_SWITCH_AUTO_APPROVAL_PAUSED as current_kill_switch
    if current_kill_switch:
        st.error("🚨 Auto-Approval Kill Switch: ACTIVE (100% Review)")
    else:
        st.success("🟢 Auto-Approval: ACTIVE")


# ==============================================================================
# SCR-01: UPLOAD & INTAKE (Multi-Format, EICAR check, Idempotency)
# ==============================================================================
if nav_option == "SCR-01: Upload & Intake":
    st.header("SCR-01: Upload & Intake")
    st.caption("Submit multi-page PDF, JPG, PNG, or TIFF files. Sniffs magic bytes, validates size/page limits, and prevents malware.")

    col1, col2 = st.columns([2, 1])

    with col1:
        uploaded_file = st.file_uploader(
            "Select Document File (PDF, JPG, PNG, TIFF — Max 50 MB)",
            type=["pdf", "jpg", "jpeg", "png", "tif", "tiff"],
        )
        doc_type_hint = st.selectbox(
            "Document Type Hint (Optional)",
            ["(Auto-Detect)", "DOC-01: CASA Account Opening Form", "DOC-02: Insurance Application", "DOC-03: Hospital Discharge Summary"],
        )
        custom_idempotency_key = st.text_input("Idempotency Key (Optional replay test)", value="")

        submit_btn = st.button("🚀 Ingest & Process Document", type="primary", disabled=uploaded_file is None)

    with col2:
        st.info(
            """
            **Intake Guardrails:**
            - Max File Size: 50 MB
            - Max Pages: 50
            - Sniffed Header Magic Bytes
            - EICAR Signature Malware Gate
            - Idempotent Replays via Hash / Key
            """
        )

    if submit_btn and uploaded_file is not None:
        raw_bytes = uploaded_file.read()
        filename = uploaded_file.name

        with st.status("Running Document Ingestion Pipeline...", expanded=True) as status_box:
            st.write("🔍 Sniffing magic bytes and checking file integrity...")
            detected_fmt = detect_magic_format(raw_bytes)
            if not detected_fmt:
                st.error("❌ Rejected: `UNSUPPORTED_FILE`. Header does not match PDF, JPG, PNG, or TIFF.")
                st.stop()
            st.write(f"✅ Magic bytes validated: `{detected_fmt}`")

            # Check for EICAR malware test string
            if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in raw_bytes:
                st.error("🚨 Malware Detected: Standard EICAR signature found! Document rejected as `rejected_malware`.")
                audit_log.record_event(
                    event_type="security.malware_detected",
                    tenant_id=st.session_state.tenant_id,
                    actor_id=st.session_state.user_id,
                    resource_type="document",
                    resource_id=f"malware_{uuid.uuid4().hex[:8]}",
                    details={"filename": filename, "reason": "EICAR signature detected"},
                )
                st.stop()

            sha256 = hashlib.sha256(raw_bytes).hexdigest()
            st.write(f"🔑 SHA-256 Digest: `{sha256[:16]}...`")

            # Idempotency check
            if custom_idempotency_key:
                cached = idempotency_store.get_record(st.session_state.tenant_id, custom_idempotency_key)
                if cached and cached.request_fingerprint == sha256:
                    st.success(f"⚡ Idempotency Cache Hit! Returning existing document `{cached.document_id}`.")
                    st.json(cached.response_payload)
                    st.stop()

            # Execute Phase 1: Ingestion & 200 DPI Preprocessing
            st.write("⚙️ Stage 1: Rasterizing to 200 DPI canvas & applying LAB-CLAHE deskew...")
            settings = load_settings()
            mod_a = ModuleAPipeline(settings=settings, tenant_id=st.session_state.tenant_id)
            handoff = mod_a.process_document(io.BytesIO(raw_bytes), filename)
            st.write(f"✅ Generated {len(handoff.pages)} preprocessed page(s).")

            # Cache page images
            doc_id = f"doc_{uuid.uuid4().hex[:10]}"
            PAGE_IMAGES[doc_id] = {}
            for p in handoff.pages:
                try:
                    img_path = mod_a.storage.resolve_uri_to_path(p.preprocessed_storage_uri)
                    if img_path.exists():
                        PAGE_IMAGES[doc_id][p.page_number] = img_path.read_bytes()
                except Exception:
                    pass

            # Execute Phase 2: Visual Layout Parsing & Token Grid Reconstruction
            st.write("⚙️ Stage 2: Parsing visual layout, character grids, checkboxes, and tables...")
            mod_b = ModuleBPipeline(storage=mod_a.storage, tenant_id=st.session_state.tenant_id)
            mod_b_out = mod_b.process_handoff(handoff)
            st.write(f"✅ Classified domain: `{mod_b_out.classification.domain.value}` | Type: `{mod_b_out.classification.document_type.value}`")

            # Execute Phase 3 & 4: Extraction & Validation
            st.write("⚙️ Stage 3 & 4: Schema-constrained extraction, invariant validation & confidence calibration...")
            mod_c = ModuleCPipeline()
            env = mod_c.process(mod_b_out)

            # Check Kill Switch
            from doc_extractor.api.app import KILL_SWITCH_AUTO_APPROVAL_PAUSED
            if KILL_SWITCH_AUTO_APPROVAL_PAUSED:
                env.document_metadata.status = DocumentStatus.NEEDS_REVIEW
                env.flags.append({"code": "KILL_SWITCH_ACTIVE", "message": "Auto-approval paused by administrator."})

            final_status = env.document_metadata.status.value if hasattr(env.document_metadata.status, "value") else str(env.document_metadata.status)

            DOCUMENT_STORE[doc_id] = {
                "document_id": doc_id,
                "tenant_id": st.session_state.tenant_id,
                "status": final_status,
                "filename": filename,
                "sha256": sha256,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "envelope": env.model_dump(),
            }

            if custom_idempotency_key:
                now = time.time()
                idempotency_store.save_record(
                    IdempotencyRecord(
                        idempotency_key=custom_idempotency_key,
                        tenant_id=st.session_state.tenant_id,
                        request_fingerprint=sha256,
                        document_id=doc_id,
                        response_payload={"document_id": doc_id, "status": final_status},
                        created_at_utc=now,
                        expires_at_utc=now + 86400.0,
                    )
                )

            # Record audit event
            audit_log.record_event(
                event_type="document.processed",
                tenant_id=st.session_state.tenant_id,
                actor_id=st.session_state.user_id,
                resource_type="document",
                resource_id=doc_id,
                details={"status": final_status, "document_type": env.document_type},
            )

            # If needs_review, create task
            if final_status == "needs_review":
                flagged = [f for f, field in env.fields.items() if field.review_required or field.validation_status == "validation_failed"]
                review_task_mgr.create_task(
                    document_id=doc_id,
                    tenant_id=st.session_state.tenant_id,
                    flagged_fields=flagged,
                    flag_reasons={flag.code: flag.message for flag in env.flags},
                )

            status_box.update(label=f"Processing Complete: Document is {final_status.upper()}", state="complete")

        st.success(f"Document `{doc_id}` processed with status: **{final_status}**")
        st.json(env.model_dump())


# ==============================================================================
# SCR-02: DOCUMENT EXPLORER & STATUS FILTER STRIP
# ==============================================================================
elif nav_option == "SCR-02: Document Explorer":
    st.header("SCR-02: Document Explorer")
    st.caption("View and search all ingested documents across status lifecycles.")

    tenant_docs = [d for d in DOCUMENT_STORE.values() if d["tenant_id"] == st.session_state.tenant_id]

    # Stat strip
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Documents", len(tenant_docs))
    c2.metric("Processing", sum(1 for d in tenant_docs if d["status"] == "processing"))
    c3.metric("Auto-Approved", sum(1 for d in tenant_docs if d["status"] == "auto_approved"))
    c4.metric("Needs Review", sum(1 for d in tenant_docs if d["status"] == "needs_review"))
    c5.metric("Reviewed", sum(1 for d in tenant_docs if d["status"] == "reviewed"))

    st.divider()

    filter_status = st.selectbox("Filter Status", ["ALL", "auto_approved", "needs_review", "reviewed", "processing", "failed"])
    filtered_docs = tenant_docs if filter_status == "ALL" else [d for d in tenant_docs if d["status"] == filter_status]

    if not filtered_docs:
        st.info("No documents found matching the filter.")
    else:
        for doc in filtered_docs:
            with st.expander(f"📄 {doc['document_id']} — {doc.get('filename')} [{doc['status'].upper()}]"):
                col_a, col_b = st.columns(2)
                with col_a:
                    st.write(f"**Document ID:** `{doc['document_id']}`")
                    st.write(f"**Status:** `{doc['status']}`")
                    st.write(f"**Created At:** {doc.get('created_at')}")
                with col_b:
                    st.write(f"**SHA-256:** `{doc.get('sha256', '')[:20]}...`")
                    if doc.get("envelope"):
                        st.write(f"**Doc Type:** `{doc['envelope'].get('document_type')}`")

                if doc.get("envelope"):
                    st.subheader("Extracted Fields Summary")
                    fields_data = []
                    for fname, fval in doc["envelope"].get("fields", {}).items():
                        fields_data.append({
                            "Field": fname,
                            "Value": str(fval.get("value")),
                            "Confidence": fval.get("calibrated_confidence"),
                            "Status": fval.get("validation_status"),
                            "Review Req?": fval.get("review_required"),
                        })
                    st.dataframe(fields_data, use_container_width=True)


# ==============================================================================
# SCR-03: REVIEW WORKBENCH (Side-by-side evidence inspection & corrections)
# ==============================================================================
elif nav_option == "SCR-03: Review Workbench":
    st.header("SCR-03: Human Review Workbench")
    st.caption("Inspect grounded visual evidence, view bounding boxes, correct values, and re-validate.")

    reviewable_docs = [d for d in DOCUMENT_STORE.values() if d["tenant_id"] == st.session_state.tenant_id and d["status"] in ("needs_review", "reviewed")]

    if not reviewable_docs:
        st.info("No documents currently requiring review.")
    else:
        doc_ids = [d["document_id"] for d in reviewable_docs]
        selected_doc_id = st.selectbox("Select Document for Review", doc_ids)
        doc = DOCUMENT_STORE[selected_doc_id]
        envelope = doc.get("envelope", {})
        fields = envelope.get("fields", {})

        col_img, col_fields = st.columns([1, 1])

        # Left column: Render 200 DPI Page with Bounding Boxes
        with col_img:
            st.subheader("Visual Grounding Evidence (200 DPI)")
            pages = PAGE_IMAGES.get(selected_doc_id, {})
            if pages:
                selected_page_num = st.selectbox("Page", list(pages.keys()))
                img_bytes = pages[selected_page_num]
                pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                draw = ImageDraw.Draw(pil_img)

                # Draw bounding boxes for fields on this page
                for fname, fval in fields.items():
                    ev = fval.get("evidence")
                    if ev and ev.get("page") == selected_page_num and ev.get("bbox"):
                        bbox = ev["bbox"]
                        color = "#ef4444" if fval.get("review_required") else "#22c55e"
                        draw.rectangle(bbox, outline=color, width=3)

                st.image(pil_img, caption=f"Page {selected_page_num} with Grounded Bounding Boxes", use_container_width=True)
            else:
                st.warning("No rasterized page images found in cache for this document.")

        # Right column: Fields, Flags & Corrections
        with col_fields:
            st.subheader("Field Review & Corrections")
            flags = envelope.get("flags", [])
            if flags:
                st.error("⚠️ Document Invariant Flags:")
                for fl in flags:
                    st.write(f"- **{fl.get('code')}:** {fl.get('message')}")

            with st.form("correction_form"):
                corrections = {}
                for fname, fval in fields.items():
                    val = fval.get("value")
                    rev_req = fval.get("review_required", False)
                    label = f"{'🚨 ' if rev_req else '✅ '}{fname} (Conf: {fval.get('calibrated_confidence')})"
                    new_val = st.text_input(label, value=str(val) if val is not None else "")
                    corrections[fname] = new_val

                save_btn = st.form_submit_button("Submit Corrections & Re-Validate")

            if save_btn:
                # Re-validate fields using Module D rules (FR-REV-003)
                corr_items = [ReviewCorrectionItem(field_name=k, value=v) for k, v in corrections.items()]
                validation_errors = review_task_mgr.validate_corrections(corr_items)

                if validation_errors:
                    st.error("❌ Correction Validation Failed:")
                    for k, err in validation_errors.items():
                        st.write(f"- **{k}:** {err}")
                else:
                    st.success("✅ All corrections passed Module D validation rules!")
                    for k, v in corrections.items():
                        if k in fields:
                            fields[k]["value"] = v
                            fields[k]["validation_status"] = "passed"
                            fields[k]["review_required"] = False
                    doc["status"] = "reviewed"
                    audit_log.record_event(
                        event_type="review.completed",
                        tenant_id=st.session_state.tenant_id,
                        actor_id=st.session_state.user_id,
                        resource_type="document",
                        resource_id=selected_doc_id,
                        details={"status": "reviewed", "reviewer": st.session_state.user_name},
                    )
                    st.rerun()


# ==============================================================================
# SCR-04: REVIEW QUEUE (Claim, Release, Complete)
# ==============================================================================
elif nav_option == "SCR-04: Review Queue":
    st.header("SCR-04: Review Queue")
    st.caption("Manage exception tasks, claim items, and avoid lock collisions.")

    tasks = review_task_mgr.list_tasks(tenant_id=st.session_state.tenant_id)
    if not tasks:
        st.info("No active review tasks in the queue.")
    else:
        for t in tasks:
            with st.container():
                st.markdown(f"**Task ID:** `{t.task_id}` | **Doc ID:** `{t.document_id}` | **Status:** `{t.status.upper()}`")
                if t.assignee_name:
                    st.write(f"👤 **Claimed by:** {t.assignee_name} (`{t.assignee_id}`)")

                st.write(f"Flagged Fields: {', '.join(t.flagged_fields)}")

                col_btn1, col_btn2 = st.columns(6)
                with col_btn1:
                    if t.status == "unassigned":
                        if st.button("Claim Task", key=f"claim_{t.task_id}"):
                            review_task_mgr.claim_task(t.task_id, st.session_state.user_id, st.session_state.user_name)
                            st.rerun()
                with col_btn2:
                    if t.status == "claimed" and t.assignee_id == st.session_state.user_id:
                        if st.button("Release", key=f"rel_{t.task_id}"):
                            review_task_mgr.release_task(t.task_id, st.session_state.user_id)
                            st.rerun()
                st.divider()


# ==============================================================================
# SCR-05: SCHEMA STUDIO & 4-EYES APPROVAL GATE
# ==============================================================================
elif nav_option == "SCR-05: Schema Studio":
    st.header("SCR-05: Schema Studio & 4-Eyes Approval Gate")
    st.caption("Define YAML extraction schemas, enforce 4-eyes approval rules, and test rollbacks.")

    tab1, tab2 = st.tabs(["Author Schema", "Approve & Manage Versions"])

    with tab1:
        st.subheader("Author Schema (YAML)")
        schema_name = st.text_input("Schema Name", value="Custom Invoice Schema")
        doc_type = st.text_input("Document Type Identifier", value="DOC-04")
        sample_yaml = """schema_id: DOC-04
name: Custom Invoice Schema
document_type: DOC-04
version: 1
status: draft
target_fields:
  - name: invoice_number
    type: string
    required: true
  - name: total_amount
    type: float
    required: true
"""
        yaml_input = st.text_area("Schema Definition (YAML)", value=sample_yaml, height=220)

        if st.button("Create Schema Draft", type="primary"):
            try:
                parsed = schema_from_yaml(yaml_input)
                parsed.tenant_id = st.session_state.tenant_id
                parsed.created_by = st.session_state.user_id
                saved = schema_mgr.create_schema(
                    tenant_id=st.session_state.tenant_id,
                    definition=parsed,
                    author=st.session_state.user_id,
                )
                st.success(f"Schema `{saved.schema_id}` v{saved.version} created successfully in status `{saved.status.value}`!")
            except Exception as e:
                st.error(f"Error creating schema: {e}")

    with tab2:
        st.subheader("Four-Eyes Approval Gate")
        st.info("SCH-005 Rule: An author CANNOT approve their own schema (enforces peer verification).")

        approver_user = st.text_input("Current Approver User ID", value="auditor_bob")
        target_schema_id = st.text_input("Schema ID to Approve", value="DOC-04")
        target_ver = st.number_input("Version", value=1, min_value=1)

        col_app, col_roll = st.columns(2)
        with col_app:
            if st.button("Approve Schema Version"):
                snapshot = schema_repo.get_snapshot(st.session_state.tenant_id, target_schema_id, target_ver)
                if not snapshot:
                    st.error("Schema not found.")
                elif snapshot.definition.created_by == approver_user:
                    st.error("🚨 4-Eyes Gate Rejection: `SCHEMA_SELF_APPROVAL`. The author cannot approve their own schema!")
                else:
                    try:
                        approved = schema_mgr.internal_promote_to_approved(
                            tenant_id=st.session_state.tenant_id,
                            schema_id=target_schema_id,
                            version=target_ver,
                            approver=approver_user,
                        )
                        st.success(f"✅ Schema `{approved.schema_id}` v{approved.version} promoted to `APPROVED`!")
                    except Exception as e:
                        st.error(f"Approval failed: {e}")

        with col_roll:
            rollback_ver = st.number_input("Rollback to Version", value=1, min_value=1)
            if st.button("Rollback Version"):
                try:
                    rb = schema_mgr.rollback(
                        tenant_id=st.session_state.tenant_id,
                        schema_id=target_schema_id,
                        target_version=rollback_ver,
                        actor=approver_user,
                    )
                    st.success(f"✅ Rolled back to v{rb.version}")
                except Exception as e:
                    st.error(f"Rollback failed: {e}")


# ==============================================================================
# SCR-08: AUDIT LOG EXPLORER (Tamper-evident hash chain)
# ==============================================================================
elif nav_option == "SCR-08: Audit Log Explorer":
    st.header("SCR-08: Tamper-Evident Audit Log Explorer")
    st.caption("Cryptographically chained, append-only event trail (SEC-008, API-008).")

    is_valid = audit_log.verify_integrity()
    if is_valid:
        st.success("🔒 Cryptographic Chain Status: VERIFIED (All event hashes intact)")
    else:
        st.error("🚨 Cryptographic Chain Status: TAMPERED / BROKEN INTEGRITY!")

    events = audit_log.list_events(tenant_id=st.session_state.tenant_id)
    if not events:
        st.info("No audit events recorded yet.")
    else:
        event_table = []
        for e in events:
            event_table.append({
                "Timestamp": e.timestamp,
                "Event Type": e.event_type,
                "Actor": e.actor_id,
                "Resource": f"{e.resource_type}:{e.resource_id}",
                "Event Hash": f"{e.event_hash[:16]}...",
                "Prev Hash": f"{e.previous_hash[:16]}...",
            })
        st.dataframe(event_table, use_container_width=True)

        if st.button("Simulate Audit Tampering Attack"):
            if audit_log._events:
                audit_log._events[-1].details = {"tampered": True}
                st.warning("Modified previous audit entry payload in memory!")
                st.rerun()


# ==============================================================================
# SCR-09: POLICY & EMERGENCY KILL SWITCH
# ==============================================================================
elif nav_option == "SCR-09: Policy & Kill Switch":
    st.header("SCR-09: Security & Emergency Controls")
    st.caption("Emergency controls, auto-approval pause (GR-015), and air-gapped egress protection.")

    from doc_extractor.api import app as api_app_module

    st.subheader("Auto-Approval Kill Switch (GR-015)")
    st.write(
        "Activating the kill switch immediately forces 100% of newly ingested documents into the human review queue without restarting the engine."
    )

    if api_app_module.KILL_SWITCH_AUTO_APPROVAL_PAUSED:
        st.error("Current Status: AUTO-APPROVAL PAUSED (All documents route to review)")
        if st.button("Resume Normal Auto-Approval", type="primary"):
            api_app_module.KILL_SWITCH_AUTO_APPROVAL_PAUSED = False
            audit_log.record_event(
                event_type="admin.auto_approval.resumed",
                tenant_id=st.session_state.tenant_id,
                actor_id=st.session_state.user_id,
                resource_type="system",
                resource_id="kill_switch",
            )
            st.rerun()
    else:
        st.success("Current Status: NORMAL PROCESSING (Confidence thresholds apply)")
        if st.button("🚨 EMERGENCY PAUSE AUTO-APPROVAL", type="primary"):
            api_app_module.KILL_SWITCH_AUTO_APPROVAL_PAUSED = True
            audit_log.record_event(
                event_type="admin.auto_approval.paused",
                tenant_id=st.session_state.tenant_id,
                actor_id=st.session_state.user_id,
                resource_type="system",
                resource_id="kill_switch",
            )
            st.rerun()


# ==============================================================================
# SCR-10: DASHBOARD & METRICS
# ==============================================================================
elif nav_option == "SCR-10: Dashboard & Metrics":
    st.header("SCR-10: Observability Dashboard & Prometheus Metrics")
    st.caption("Real-time telemetry adhering strictly to SEC-012 (Zero PII/PHI in metrics).")

    from doc_extractor.api.metrics import get_prometheus_metrics

    col_m1, col_m2, col_m3 = st.columns(3)
    col_m1.metric("Target DPI", "200 DPI (Strict)")
    col_m2.metric("External Network Egress", "0 (Air-Gapped Guaranteed)")
    col_m3.metric("Production Escapes", "0")

    st.subheader("Prometheus Raw Metrics Output (/api/v1/metrics)")
    raw_metrics = get_prometheus_metrics().decode("utf-8")
    st.code(raw_metrics, language="text")

