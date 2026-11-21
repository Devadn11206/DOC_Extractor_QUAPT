"""
Document Extraction Engine — Standalone Interactive UI & Visual Grounding Workbench (`demo_app.py`).
Conforms to PRD Screen SCR-03 and Task Specifications.
"""

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# Ensure src/ is in sys.path
_SRC_DIR = str(Path(__file__).resolve().parent.parent.parent)
_CURRENT_DIR = str(Path(__file__).resolve().parent)
while _CURRENT_DIR in sys.path:
    sys.path.remove(_CURRENT_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from PIL import Image
import streamlit as st

from doc_extractor.ui.client import ExtractionClient, ExecutionResult
from doc_extractor.ui.visualizer import draw_bounding_boxes
from doc_extractor.validation_rules import (
    normalize_clinical_frequency,
    normalize_currency_amount,
    normalize_date_to_iso,
    normalize_phone_number,
    validate_ifsc,
    validate_pan,
)

# Page Configuration
st.set_page_config(
    page_title="Document Extraction & Visual Grounding",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Enterprise Modern Theme Styling
st.markdown(
    """
    <style>
    .kpi-container {
        display: flex;
        gap: 12px;
        margin-bottom: 16px;
    }
    .kpi-box {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        padding: 10px 14px;
        flex: 1;
        text-align: center;
    }
    .kpi-value {
        font-size: 20px;
        font-weight: 700;
        color: #0f172a;
    }
    .kpi-label {
        font-size: 11px;
        color: #64748b;
        text-transform: uppercase;
        font-weight: 600;
    }
    .chip {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 6px;
        font-size: 11px;
        font-weight: 600;
    }
    .chip-green { background-color: #dcfce7; color: #166534; }
    .chip-yellow { background-color: #fef9c3; color: #854d0e; }
    .chip-red { background-color: #fee2e2; color: #991b1b; }
    .chip-blue { background-color: #dbeafe; color: #1e40af; }
    .chip-gray { background-color: #f1f5f9; color: #475569; }
    </style>
    """,
    unsafe_allow_html=True,
)

# Session State Initialization
if "execution_result" not in st.session_state:
    st.session_state.execution_result = None
if "selected_page" not in st.session_state:
    st.session_state.selected_page = 1
if "selected_field" not in st.session_state:
    st.session_state.selected_field = None
if "edited_fields" not in st.session_state:
    st.session_state.edited_fields = {}


# ==============================================================================
# 1. SIDEBAR CONFIGURATION
# ==============================================================================
with st.sidebar:
    st.title("⚙️ Engine Settings")

    doc_schema_hint = st.selectbox(
        "Document Schema Target",
        [
            "Generic / Auto-Discovery",
            "DOC-01: CASA Account Opening Form",
            "DOC-02: Insurance Application Form",
            "DOC-03: Hospital Discharge Summary",
        ],
    )

    execution_mode = st.radio(
        "Execution Mode",
        ["Direct Python Mode", "FastAPI HTTP Mode"],
        help="Direct mode executes Module A/B/C pipelines directly in Python. FastAPI mode calls the HTTP REST API.",
    )

    api_endpoint = "http://localhost:8000"
    if execution_mode == "FastAPI HTTP Mode":
        api_endpoint = st.text_input("FastAPI Base URL", value="http://localhost:8000")

    confidence_threshold = st.slider(
        "Confidence Review Threshold (τ)",
        min_value=0.50,
        max_value=0.95,
        value=0.75,
        step=0.05,
        help="Fields with confidence below this threshold are flagged for human review.",
    )

    pii_masking = st.checkbox("PII Redaction / Masking", value=False, help="Mask PAN, Phone, and Account numbers in the display.")
    show_all_boxes = st.checkbox("Show All Layout Blocks", value=False, help="Display all detected OCR text blocks, tables, and checkboxes on the image canvas.")

    st.divider()
    st.caption("Document Extraction Engine — Phase 1 to 5")


# ==============================================================================
# 2. DOCUMENT UPLOAD & TOP BAR
# ==============================================================================
st.title("📄 Document Extraction & Visual Grounding")
st.caption("Upload documents (PDF, JPG, PNG, TIFF) to inspect structured extraction, field validation, and 200 DPI visual bounding box evidence.")

uploaded_file = st.file_uploader(
    "Choose a file to extract",
    type=["pdf", "jpg", "jpeg", "png", "tif", "tiff"],
    help="Supports PDF, JPG, PNG, TIFF up to 50 MB.",
)

if uploaded_file is not None:
    file_bytes = uploaded_file.getvalue()
    filename = uploaded_file.name

    col_btn, col_info = st.columns([1, 4])
    with col_btn:
        run_extraction = st.button("🚀 Extract & Ground", type="primary", use_container_width=True)

    with col_info:
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()
        st.write(f"📁 **File:** `{filename}` | **Size:** `{len(file_bytes)/1024:.1f} KB` | **SHA-256:** `{file_sha256[:16]}...`")

    if run_extraction:
        mode_param = "FastAPI" if "FastAPI" in execution_mode else "Direct"
        client = ExtractionClient(mode=mode_param, api_base_url=api_endpoint)

        with st.spinner("Processing document through multi-stage computer vision & LLM extraction..."):
            try:
                res = client.process_document(
                    file_bytes=file_bytes,
                    filename=filename,
                    document_type_hint=None if "Auto" in doc_schema_hint else doc_schema_hint.split(":")[0],
                )
                st.session_state.execution_result = res
                st.session_state.selected_page = 1
                st.session_state.selected_field = None
                st.session_state.edited_fields = {}
                st.success(f"Extraction completed in {res.execution_time_ms:.1f} ms with status: {res.status.upper()}")
            except Exception as exc:
                st.error(f"Execution Error: {exc}")


# ==============================================================================
# 3. TWO-COLUMN WORKBENCH (SCR-03: Visual Grounding & Schema Review)
# ==============================================================================
result: Optional[ExecutionResult] = st.session_state.execution_result

if result:
    envelope = result.envelope
    fields_dict: Dict[str, Any] = envelope.get("fields", {})
    flags: List[Dict[str, Any]] = envelope.get("flags", [])

    # Warning banners for invariant failures
    for fl in flags:
        st.warning(f"⚠️ **{fl.get('code', 'INVARIANT_FLAG')}**: {fl.get('message', '')}")

    st.divider()

    col_viewer, col_editor = st.columns([55, 45])

    # --------------------------------------------------------------------------
    # LEFT COLUMN (55%): VISUAL DOCUMENT VIEWER WITH BOUNDING BOX OVERLAYS
    # --------------------------------------------------------------------------
    with col_viewer:
        st.subheader("🖼️ Visual Evidence Canvas (200 DPI)")

        available_pages = sorted(list(result.pages.keys())) if result.pages else [1]
        if len(available_pages) > 1:
            page_nav_col1, page_nav_col2 = st.columns([1, 3])
            with page_nav_col1:
                cur_page = st.selectbox("Page", available_pages, index=available_pages.index(st.session_state.selected_page) if st.session_state.selected_page in available_pages else 0)
                st.session_state.selected_page = cur_page
        else:
            cur_page = 1
            st.session_state.selected_page = 1

        # Retrieve 200 DPI page image
        img_bytes = result.pages.get(cur_page)
        if img_bytes:
            # Build bounding box list for fields on this page
            page_boxes = []
            for fname, fentry in fields_dict.items():
                ev = fentry.get("evidence")
                if ev and ev.get("page") == cur_page and ev.get("bbox"):
                    conf = fentry.get("calibrated_confidence", 1.0)
                    rev_req = fentry.get("review_required", False)
                    page_boxes.append({
                        "field_name": fname,
                        "bbox": ev["bbox"],
                        "confidence": conf,
                        "status": fentry.get("validation_status", "passed"),
                        "review_required": rev_req or (conf < confidence_threshold),
                    })

            # Filter layout blocks for this page
            page_blocks = [b for b in result.all_blocks if b.get("page") == cur_page]

            # Render overlay using visualizer engine
            highlighted_img = draw_bounding_boxes(
                image=img_bytes,
                boxes=page_boxes,
                selected_field_name=st.session_state.selected_field,
                show_all_detected_boxes=show_all_boxes,
                all_blocks=page_blocks,
                draw_labels=True,
            )

            st.image(
                highlighted_img,
                caption=f"Page {cur_page} / {result.page_count} — 200 DPI Native Canvas",
                use_container_width=True,
            )
        else:
            st.info("No rendered image available for this page.")

    # --------------------------------------------------------------------------
    # RIGHT COLUMN (45%): EXTRACTED SCHEMA FIELDS, EDITING & REVIEW
    # --------------------------------------------------------------------------
    with col_editor:
        st.subheader("📋 Extracted Fields & Review")

        # KPI Header
        total_fields = len(fields_dict)
        flagged_count = sum(1 for f, v in fields_dict.items() if v.get("review_required") or v.get("calibrated_confidence", 1.0) < confidence_threshold)
        status_color = "chip-green" if result.status == "auto_approved" and flagged_count == 0 else "chip-red"

        st.markdown(
            f"""
            <div class="kpi-container">
                <div class="kpi-box">
                    <div class="kpi-label">Status</div>
                    <div><span class="chip {status_color}">{result.status.upper()}</span></div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-label">Total Fields</div>
                    <div class="kpi-value">{total_fields}</div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-label">Flagged Fields</div>
                    <div class="kpi-value">{flagged_count}</div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-label">Latency</div>
                    <div class="kpi-value">{result.execution_time_ms:.0f} ms</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.caption("Click any field row to highlight its bounding box on the left canvas.")

        # Group fields into clean categories
        groups: Dict[str, List[str]] = {
            "Applicant / Patient Information": [],
            "Policy / Account Details": [],
            "Services & Checkboxes": [],
            "General / Uncategorized": [],
        }

        for fname in fields_dict.keys():
            low = fname.lower()
            if any(term in low for term in ["applicant", "patient", "name", "dob", "pan", "phone"]):
                groups["Applicant / Patient Information"].append(fname)
            elif any(term in low for term in ["account", "policy", "premium", "deposit", "sum_assured", "hospital", "date"]):
                groups["Policy / Account Details"].append(fname)
            elif any(term in low for term in ["opt_in", "checkbox", "service", "medical"]):
                groups["Services & Checkboxes"].append(fname)
            else:
                groups["General / Uncategorized"].append(fname)

        for group_name, field_names in groups.items():
            if not field_names:
                continue

            with st.expander(f"🔹 {group_name} ({len(field_names)})", expanded=True):
                for fname in field_names:
                    fentry = fields_dict[fname]
                    raw_val = fentry.get("value")
                    conf = float(fentry.get("calibrated_confidence", 1.0))
                    val_status = fentry.get("validation_status", "passed")
                    null_reason = fentry.get("null_reason")
                    ev = fentry.get("evidence") or {}

                    # Masking logic if toggled
                    display_val = str(raw_val) if raw_val is not None else ""
                    if pii_masking and raw_val:
                        if any(term in fname.lower() for term in ["pan", "phone", "account"]):
                            display_val = display_val[:2] + "X" * max(0, len(display_val) - 4) + display_val[-2:] if len(display_val) > 4 else "XXXX"

                    # Confidence Chip Styling
                    if conf >= 0.85:
                        conf_class = "chip-green"
                    elif conf >= 0.75:
                        conf_class = "chip-yellow"
                    else:
                        conf_class = "chip-red"

                    f_col1, f_col2 = st.columns([3, 1])

                    with f_col1:
                        # Radio / select button to target this bounding box
                        is_active = (st.session_state.selected_field == fname)
                        btn_label = f"{'🎯 ' if is_active else ''}**{fname.split('.')[-1]}**"
                        if st.button(btn_label, key=f"sel_{fname}"):
                            st.session_state.selected_field = fname
                            if ev.get("page"):
                                st.session_state.selected_page = ev["page"]
                            st.rerun()

                        # Human correction editable text input
                        new_val = st.text_input(
                            f"Value for {fname}",
                            value=st.session_state.edited_fields.get(fname, display_val),
                            key=f"input_{fname}",
                            label_visibility="collapsed",
                        )
                        st.session_state.edited_fields[fname] = new_val

                    with f_col2:
                        st.markdown(
                            f"""
                            <div>
                                <span class="chip {conf_class}">{conf:.2f}</span>
                                <span class="chip chip-gray">{val_status}</span>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                        if null_reason:
                            st.markdown(f"<span class='chip chip-blue'>{null_reason}</span>", unsafe_allow_html=True)

                    # Show evidence snippet if present
                    if ev.get("text"):
                        with st.expander("Evidence Snippet", expanded=False):
                            st.code(ev.get("text"), language="text")
                            if ev.get("bbox"):
                                st.caption(f"Coordinates: `{ev.get('bbox')}` on page {ev.get('page')}")

        # Human-in-the-Loop Re-Validation & Completion
        st.divider()
        if st.button("💾 Apply Corrections & Re-Validate Rules", type="primary"):
            validation_errors = {}
            for k, v in st.session_state.edited_fields.items():
                if "pan" in k.lower():
                    _, err = validate_pan(v)
                    if err:
                        validation_errors[k] = f"Invalid PAN ({err})"
                elif "ifsc" in k.lower():
                    _, err = validate_ifsc(v)
                    if err:
                        validation_errors[k] = f"Invalid IFSC ({err})"
                elif "date" in k.lower() or "dob" in k.lower():
                    _, err = normalize_date_to_iso(v)
                    if err:
                        validation_errors[k] = f"Invalid Date ({err})"
                elif "phone" in k.lower() or "mobile" in k.lower():
                    _, err = normalize_phone_number(v)
                    if err:
                        validation_errors[k] = f"Invalid Phone ({err})"
                elif "frequency" in k.lower():
                    _, err = normalize_clinical_frequency(v)
                    if err:
                        validation_errors[k] = f"Unrecognized Frequency ({err})"
                elif "amount" in k.lower() or "deposit" in k.lower() or "premium" in k.lower():
                    _, _, err = normalize_currency_amount(v)
                    if err:
                        validation_errors[k] = f"Invalid Amount ({err})"

            if validation_errors:
                st.error("❌ Corrections Failed Invariant Validation:")
                for k, err in validation_errors.items():
                    st.write(f"- **{k}:** {err}")
            else:
                st.success("✅ Corrections Passed Module D Invariants! Document updated to REVIEWED.")
                for k, v in st.session_state.edited_fields.items():
                    if k in fields_dict:
                        fields_dict[k]["value"] = v
                        fields_dict[k]["validation_status"] = "passed"
                        fields_dict[k]["review_required"] = False
                result.status = "reviewed"
                st.rerun()


# ==============================================================================
# 4. RAW DATA & CANONICAL EXPORT DRAWER
# ==============================================================================
if result:
    st.divider()
    st.subheader("📦 Export & Raw Data Views")

    tab_table, tab_json, tab_md = st.tabs(["📊 Formatted Table View", "📄 Canonical JSON Envelope", "📝 Module B Layout Markdown"])

    with tab_table:
        table_rows = []
        for fname, fentry in fields_dict.items():
            ev = fentry.get("evidence") or {}
            table_rows.append({
                "Field Path": fname,
                "Extracted Value": fentry.get("value"),
                "Calibrated Confidence": fentry.get("calibrated_confidence"),
                "Validation Status": fentry.get("validation_status"),
                "Review Required": fentry.get("review_required"),
                "Page": ev.get("page"),
                "BBox": str(ev.get("bbox")),
                "Supporting Text": ev.get("text"),
            })
        st.dataframe(table_rows, use_container_width=True)

        # CSV Download Button
        csv_buffer = io.StringIO()
        csv_writer = csv.DictWriter(csv_buffer, fieldnames=list(table_rows[0].keys()) if table_rows else ["field"])
        csv_writer.writeheader()
        csv_writer.writerows(table_rows)

        st.download_button(
            "📥 Download Table (CSV)",
            data=csv_buffer.getvalue(),
            file_name=f"{result.document_id}_extracted.csv",
            mime="text/csv",
        )

    with tab_json:
        envelope_json = json.dumps(envelope, indent=2)
        st.code(envelope_json, language="json")

        st.download_button(
            "📥 Download Canonical Envelope (JSON)",
            data=envelope_json,
            file_name=f"{result.document_id}_envelope.json",
            mime="application/json",
        )

    with tab_md:
        st.markdown(result.layout_markdown)
        st.download_button(
            "📥 Download Layout (Markdown)",
            data=result.layout_markdown,
            file_name=f"{result.document_id}_layout.md",
            mime="text/markdown",
        )
