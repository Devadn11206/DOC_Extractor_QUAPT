"""
DocExtract AI — Enterprise Multimodal Document Extraction Workbench (Gradio).

High-performance, reactive interface for testing, debugging, and inspecting
end-to-end document extraction with visual bounding-box evidence grounding.
"""

import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

import cv2
import gradio as gr
import numpy as np

# Ensure src is on Python path
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_SRC = CURRENT_DIR.parent.parent
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from doc_extractor.config import Settings, load_settings
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.validation import detect_magic_format


# ---------------------------------------------------------------------------
# Global Pipeline Singletons (Avoid re-initialization overhead per run)
# ---------------------------------------------------------------------------
_SETTINGS: Optional[Settings] = None
_MODULE_A: Optional[ModuleAPipeline] = None
_MODULE_B: Optional[ModuleBPipeline] = None
_MODULE_C: Optional[ModuleCPipeline] = None


def get_pipelines() -> Tuple[ModuleAPipeline, ModuleBPipeline, ModuleCPipeline]:
    """Retrieves or initializes cached pipeline singletons."""
    global _SETTINGS, _MODULE_A, _MODULE_B, _MODULE_C
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    if _MODULE_A is None:
        _MODULE_A = ModuleAPipeline(settings=_SETTINGS, tenant_id="tenant_workbench")
    if _MODULE_B is None:
        _MODULE_B = ModuleBPipeline(storage=_MODULE_A.storage, tenant_id="tenant_workbench")
    if _MODULE_C is None:
        _MODULE_C = ModuleCPipeline()
    return _MODULE_A, _MODULE_B, _MODULE_C


# ---------------------------------------------------------------------------
# UI Helpers & Formatting
# ---------------------------------------------------------------------------
STATUS_THEMES = {
    "auto_approved": {"bg": "#059669", "fg": "#ffffff", "icon": "✓ AUTO-APPROVED"},
    "needs_review": {"bg": "#d97706", "fg": "#ffffff", "icon": "⚠ NEEDS REVIEW"},
    "rejected": {"bg": "#dc2626", "fg": "#ffffff", "icon": "✕ REJECTED"},
    "failed": {"bg": "#dc2626", "fg": "#ffffff", "icon": "✕ FAILED"},
}


def create_status_pill(status: str, latency_ms: float, avg_conf: float, flag_count: int, total_fields: int) -> str:
    theme = STATUS_THEMES.get(status.lower(), {"bg": "#475569", "fg": "#ffffff", "icon": status.upper()})
    return f"""
    <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; font-family: ui-sans-serif, system-ui, sans-serif;">
        <div style="background-color: {theme['bg']}; color: {theme['fg']}; padding: 6px 14px; border-radius: 9999px; font-weight: 700; font-size: 0.875rem; letter-spacing: 0.05em; display: inline-flex; align-items: center; gap: 6px; box-shadow: 0 1px 2px rgba(0,0,0,0.1);">
            {theme['icon']}
        </div>
        <div style="background-color: #f1f5f9; color: #0f172a; padding: 6px 12px; border-radius: 8px; font-size: 0.875rem; font-weight: 600; border: 1px solid #e2e8f0;">
            ⚡ Latency: <span style="color: #2563eb;">{latency_ms:.1f} ms</span>
        </div>
        <div style="background-color: #f1f5f9; color: #0f172a; padding: 6px 12px; border-radius: 8px; font-size: 0.875rem; font-weight: 600; border: 1px solid #e2e8f0;">
            🎯 Mean Conf: <span style="color: {'#16a34a' if avg_conf >= 0.85 else '#d97706'};">{avg_conf * 100:.1f}%</span>
        </div>
        <div style="background-color: #f1f5f9; color: #0f172a; padding: 6px 12px; border-radius: 8px; font-size: 0.875rem; font-weight: 600; border: 1px solid #e2e8f0;">
            🚩 Flags: <span style="color: {'#dc2626' if flag_count > 0 else '#16a34a'};">{flag_count} / {total_fields} fields</span>
        </div>
    </div>
    """


def categorize_field(field_path: str) -> str:
    path_low = field_path.lower()
    if any(k in path_low for k in ["applicant", "party", "proposer", "patient", "name", "dob", "gender", "relation", "age"]):
        return "primary_party"
    if any(k in path_low for k in ["pan", "ifsc", "account", "sum_assured", "deposit", "policy", "currency", "bill", "admission"]):
        return "financial_statutory"
    if any(k in path_low for k in ["opt_in", "checkbox", "tobacco", "smoker", "facility", "cheque", "card", "banking"]):
        return "checkboxes_facilities"
    if any(k in path_low for k in ["item", "diagnosis", "medication", "procedure", "nominee", "list"]):
        return "line_items"
    return "other"


def build_fields_dataframe(fields_dict: Dict[str, Any]) -> List[List[Any]]:
    rows = []
    for f_name, f_info in fields_dict.items():
        val = f_info.get("value")
        val_str = "None" if val is None else str(val)
        conf = f_info.get("confidence", 0.0)
        conf_str = f"{conf * 100:.1f}%" if conf is not None else "N/A"
        val_stat = f_info.get("validation", "N/A")
        rev = "Yes" if f_info.get("review_required") else "No"
        
        ev = f_info.get("evidence")
        if ev and isinstance(ev, dict):
            page_num = ev.get("page", 1)
            bbox = ev.get("bbox")
            bbox_str = f"P{page_num} {bbox}" if bbox else "No BBox"
            text_snip = ev.get("text", "")[:40]
        else:
            bbox_str = "—"
            text_snip = "—"

        rows.append([f_name, val_str, conf_str, val_stat, rev, bbox_str, text_snip])
    return rows


def build_accordion_group_markdown(group_fields: Dict[str, Any]) -> str:
    if not group_fields:
        return "*No fields detected in this group.*"
    
    html = ['<div style="display: flex; flex-direction: column; gap: 8px; font-family: ui-sans-serif, system-ui, sans-serif;">']
    for f_name, f_info in group_fields.items():
        val = f_info.get("value")
        val_display = '<span style="color: #94a3b8; font-style: italic;">null</span>' if val is None else f'<strong style="color: #0f172a;">{val}</strong>'
        conf = f_info.get("confidence", 0.0) or 0.0
        rev = f_info.get("review_required", False)
        val_stat = f_info.get("validation", "passed")
        
        conf_badge = f'<span style="background: {"#dcfce7; color: #15803d" if conf >= 0.85 else "#fef3c7; color: #b45309"}; padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 700;">{conf*100:.1f}%</span>'
        
        stat_badge = ""
        if val_stat == "failed":
            stat_badge = '<span style="background: #fee2e2; color: #b91c1c; padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 700;">Validation Failed</span>'
        elif rev:
            stat_badge = '<span style="background: #fef3c7; color: #b45309; padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 700;">Review Needed</span>'
        else:
            stat_badge = '<span style="background: #dbeafe; color: #1d4ed8; padding: 2px 8px; border-radius: 9999px; font-size: 0.75rem; font-weight: 700;">Valid</span>'

        ev = f_info.get("evidence")
        ev_snip = f'<div style="font-size: 0.75rem; color: #64748b; margin-top: 2px;">📍 Evidence: <code style="background: #f1f5f9; padding: 1px 4px; border-radius: 4px;">{ev.get("text", "")[:60]}</code> (P{ev.get("page", 1)})</div>' if ev else ''

        html.append(f"""
        <div style="background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 14px;">
            <div style="display: flex; justify-content: space-between; align-items: baseline; gap: 8px;">
                <span style="font-weight: 600; color: #334155; font-size: 0.875rem;">{f_name}</span>
                <div style="display: flex; gap: 6px; align-items: center;">
                    {conf_badge}
                    {stat_badge}
                </div>
            </div>
            <div style="font-size: 0.95rem; margin-top: 4px;">{val_display}</div>
            {ev_snip}
        </div>
        """)
    html.append('</div>')
    return "\n".join(html)


# ---------------------------------------------------------------------------
# Core Processing Function
# ---------------------------------------------------------------------------
def process_uploaded_document(
    file_obj: Any,
    schema_choice: str,
    progress: gr.Progress = gr.Progress(track_tqdm=True),
) -> Tuple[
    Any,               # Annotated image for page 1
    float,             # Slider max pages
    float,             # Slider current value
    str,               # Status HTML
    List[List[Any]],   # Structured Inspector table
    str,               # Accordion Group 1
    str,               # Accordion Group 2
    str,               # Accordion Group 3
    str,               # Accordion Group 4
    Dict[str, Any],    # Canonical JSON
    str,               # Layout Markdown
    str,               # Flags log
    str,               # Export JSON path
    str,               # Export CSV path
    Dict[str, Any],    # Cached document state
]:
    """
    Executes the 3-module pipeline and prepares all reactive Gradio outputs.
    """
    if file_obj is None:
        empty_state = {"pages": {}, "annotations_by_page": {}, "envelope": {}}
        return (
            None, gr.update(minimum=1, maximum=2, value=1),
            "<div style='color: #64748b;'>Upload a document to begin.</div>",
            [], "*No data*", "*No data*", "*No data*", "*No data*",
            {}, "", "No flags.", None, None, empty_state
        )

    t0 = time.perf_counter()
    file_path = Path(file_obj.name if hasattr(file_obj, "name") else str(file_obj))
    file_bytes = file_path.read_bytes()
    filename = file_path.name

    schema_hint = None
    if "CASA" in schema_choice:
        schema_hint = "DOC-01"
    elif "Insurance" in schema_choice:
        schema_hint = "DOC-02"
    elif "Medical" in schema_choice or "Discharge" in schema_choice:
        schema_hint = "DOC-03"

    mod_a, mod_b, mod_c = get_pipelines()

    # Stage 1: Ingestion & 200 DPI Rendering
    progress(0.15, desc="Stage 1: Ingesting & Rendering 200 DPI native pages...")
    stream = io.BytesIO(file_bytes)
    handoff = mod_a.process_document(stream, filename)

    # Decode RGB pages for rendering
    pages_rgb: Dict[int, np.ndarray] = {}
    for p in handoff.pages:
        try:
            img_path = mod_a.storage.resolve_uri_to_path(p.original_storage_uri)
            if not img_path.exists():
                img_path = mod_a.storage.resolve_uri_to_path(p.preprocessed_storage_uri)
            if img_path.exists():
                bgr_img = cv2.imread(str(img_path))
                if bgr_img is not None:
                    pages_rgb[p.page_number] = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2RGB)
        except Exception:
            pass

    if not pages_rgb:
        # Fallback if image decode had issue
        pages_rgb[1] = np.full((800, 600, 3), 255, dtype=np.uint8)

    # Stage 2: Visual Layout Parsing & Classification
    progress(0.50, desc="Stage 2: Parsing visual OCR layout & classifying document...")
    mod_b_out = mod_b.process_handoff(handoff)

    # Stage 3: Schema-Guided Extraction & Provenance Grounding
    progress(0.85, desc="Stage 3: Extracting schema fields with visual evidence grounding...")
    envelope = mod_c.process(mod_b_out, force_schema_id=schema_hint)
    envelope_dict = envelope.model_dump()

    latency_ms = (time.perf_counter() - t0) * 1000.0

    # Build Gradio native annotations by page: ((x0, y0, x1, y1), label)
    annotations_by_page: Dict[int, List[Tuple[Tuple[int, int, int, int], str]]] = {
        p_num: [] for p_num in pages_rgb.keys()
    }

    fields_dict = envelope_dict.get("fields", {})
    for f_name, f_data in fields_dict.items():
        ev = f_data.get("evidence")
        if ev and isinstance(ev, dict) and ev.get("bbox"):
            page_num = ev.get("page", 1)
            bbox = ev.get("bbox")
            conf = f_data.get("confidence", 0.0) or 0.0
            val_stat = f_data.get("validation", "passed")
            rev = f_data.get("review_required", False)

            # Assign color category tag
            if val_stat == "failed":
                tag = f"[Failed] {f_name}"
            elif rev or conf < 0.85:
                tag = f"[Review] {f_name}"
            else:
                tag = f"[Valid] {f_name}"

            # Convert to integer pixel coordinates
            x0, y0, x1, y1 = [int(round(coord)) for coord in bbox]
            if page_num in annotations_by_page:
                annotations_by_page[page_num].append(((x0, y0, x1, y1), tag))

    # Field group bucketing
    group_party: Dict[str, Any] = {}
    group_fin: Dict[str, Any] = {}
    group_cb: Dict[str, Any] = {}
    group_lines: Dict[str, Any] = {}

    for f_name, f_data in fields_dict.items():
        cat = categorize_field(f_name)
        if cat == "primary_party":
            group_party[f_name] = f_data
        elif cat == "financial_statutory":
            group_fin[f_name] = f_data
        elif cat == "checkboxes_facilities":
            group_cb[f_name] = f_data
        else:
            group_lines[f_name] = f_data

    html_party = build_accordion_group_markdown(group_party)
    html_fin = build_accordion_group_markdown(group_fin)
    html_cb = build_accordion_group_markdown(group_cb)
    html_lines = build_accordion_group_markdown(group_lines)

    table_rows = build_fields_dataframe(fields_dict)

    # Status Pill Metrics
    doc_meta = envelope_dict.get("document_metadata", {})
    status_str = doc_meta.get("status", "needs_review")
    if hasattr(status_str, "value"):
        status_str = status_str.value

    all_confs = [f.get("confidence", 0.0) for f in fields_dict.values() if f.get("confidence") is not None]
    avg_conf = (sum(all_confs) / len(all_confs)) if all_confs else 0.0
    flags_list = envelope_dict.get("flags", [])
    flagged_fields_count = sum(1 for f in fields_dict.values() if f.get("review_required"))

    status_html = create_status_pill(
        status=str(status_str),
        latency_ms=latency_ms,
        avg_conf=avg_conf,
        flag_count=flagged_fields_count,
        total_fields=len(fields_dict),
    )

    # Layout Markdown & Flags
    layout_md = mod_b_out.combined_markdown
    flags_formatted = json.dumps(flags_list, indent=2) if flags_list else "No active flags. All invariants passed."

    # Prepare Export Files
    tmp_dir = Path(tempfile.gettempdir()) / "docextract_exports"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    json_export_path = str(tmp_dir / f"{handoff.document_id}_canonical.json")
    with open(json_export_path, "w", encoding="utf-8") as jf:
        json.dump(envelope_dict, jf, indent=2)

    csv_export_path = str(tmp_dir / f"{handoff.document_id}_fields.csv")
    with open(csv_export_path, "w", newline="", encoding="utf-8") as cf:
        writer = csv.writer(cf)
        writer.writerow(["Field Name", "Extracted Value", "Confidence", "Validation", "Review Required", "Bounding Box", "Supporting Text"])
        writer.writerows(table_rows)

    num_pages = len(pages_rgb)
    doc_state = {
        "pages": pages_rgb,
        "annotations_by_page": annotations_by_page,
        "envelope": envelope_dict,
    }

    # Default to page 1
    initial_annotated = (pages_rgb.get(1, next(iter(pages_rgb.values()))), annotations_by_page.get(1, []))
    progress(1.0, desc="Extraction completed successfully!")

    slider_update = gr.update(minimum=1, maximum=max(2, num_pages), value=1)
    return (
        initial_annotated,
        slider_update,
        status_html,
        table_rows,
        html_party,
        html_fin,
        html_cb,
        html_lines,
        envelope_dict,
        layout_md,
        flags_formatted,
        json_export_path,
        csv_export_path,
        doc_state,
    )


def switch_page(page_idx: float, doc_state: Dict[str, Any]) -> Any:
    """Updates the gr.AnnotatedImage view when the page slider moves."""
    if not doc_state or "pages" not in doc_state:
        return None
    p_num = int(page_idx)
    pages = doc_state.get("pages", {})
    annots = doc_state.get("annotations_by_page", {})
    if p_num in pages:
        return (pages[p_num], annots.get(p_num, []))
    return None


# ---------------------------------------------------------------------------
# Gradio UI Layout
# ---------------------------------------------------------------------------
CUSTOM_CSS = """
.gradio-container {
    max-width: 98% !important;
}
.header-banner {
    padding: 16px 20px;
    background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
    border-radius: 12px;
    margin-bottom: 16px;
    color: white;
}
.header-title {
    font-size: 1.5rem;
    font-weight: 800;
    margin: 0;
    letter-spacing: -0.02em;
}
.header-subtitle {
    font-size: 0.875rem;
    color: #94a3b8;
    margin: 4px 0 0 0;
}
"""

with gr.Blocks(title="DocExtract AI Workbench") as demo:
    # State storage for multi-page inspection
    doc_state = gr.State({})

    with gr.Row(elem_classes=["header-banner"]):
        with gr.Column():
            gr.Markdown(
                """
                <div class="header-title">⚡ DocExtract AI — Enterprise Multimodal Document Extraction Workbench</div>
                <div class="header-subtitle">Zero-reexecution Gradio interface with native 200 DPI visual grounding, invariant validation, and canonical envelope export.</div>
                """
            )

    with gr.Row():
        # LEFT PANEL: Controls & Interactive Document Canvas
        with gr.Column(scale=5):
            with gr.Group():
                file_input = gr.File(
                    label="Upload Document",
                    file_types=[".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"],
                    type="filepath",
                )
                with gr.Row():
                    schema_dropdown = gr.Dropdown(
                        label="Document Schema Target",
                        choices=[
                            "Generic / Auto-Discovery",
                            "DOC-01: Indian CASA Account Opening Form",
                            "DOC-02: Retail Insurance Application",
                            "DOC-03: Clinical Hospital Discharge Summary",
                        ],
                        value="Generic / Auto-Discovery",
                        scale=3,
                    )
                    run_btn = gr.Button("Extract Document", variant="primary", scale=2)

                page_slider = gr.Slider(
                    minimum=1,
                    maximum=5,
                    step=1,
                    value=1,
                    label="Document Page Navigation",
                    visible=True,
                )

            gr.Markdown("### 🖼️ Visual Evidence Canvas (200 DPI Native)")
            annotated_image = gr.AnnotatedImage(
                label="Visual Bounding-Box Grounding",
                color_map={
                    "[Valid]": "#16a34a",
                    "[Review]": "#d97706",
                    "[Failed]": "#dc2626",
                },
                height=720,
            )

        # RIGHT PANEL: Extracted Data, Tabs & Exports
        with gr.Column(scale=5):
            status_box = gr.HTML("<div style='color: #64748b;'>Upload a document and click 'Extract Document' to begin.</div>")

            with gr.Tabs():
                with gr.Tab("📋 Structured Inspector"):
                    with gr.Accordion("👤 Primary Party / Applicant Details", open=True):
                        html_party = gr.HTML("*No extraction run yet.*")
                    with gr.Accordion("💳 Financial & Statutory Data", open=True):
                        html_fin = gr.HTML("*No extraction run yet.*")
                    with gr.Accordion("☑️ Checkboxes & Facilities", open=True):
                        html_cb = gr.HTML("*No extraction run yet.*")
                    with gr.Accordion("📑 Line Items & Clinical Notes", open=True):
                        html_lines = gr.HTML("*No extraction run yet.*")

                with gr.Tab("📊 Tabular View"):
                    fields_table = gr.Dataframe(
                        headers=["Field Name", "Extracted Value", "Confidence", "Validation", "Review Required", "Bounding Box", "Supporting Text"],
                        datatype=["str", "str", "str", "str", "str", "str", "str"],
                        interactive=False,
                        wrap=True,
                    )

                with gr.Tab("📄 Canonical JSON Envelope"):
                    json_viewer = gr.JSON(label="Section 6.3 Canonical Envelope")

                with gr.Tab("📝 Module B Layout Markdown"):
                    markdown_viewer = gr.Markdown("*Markdown stream will appear here after parsing.*")

                with gr.Tab("🚩 Flags & Audit Log"):
                    flags_viewer = gr.Code(language="json", label="Active Processing Flags")

            with gr.Row():
                json_export_btn = gr.DownloadButton("📥 Download JSON Envelope", variant="secondary")
                csv_export_btn = gr.DownloadButton("📥 Download Table CSV", variant="secondary")

    # Reactive Bindings
    run_btn.click(
        fn=process_uploaded_document,
        inputs=[file_input, schema_dropdown],
        outputs=[
            annotated_image,
            page_slider,
            status_box,
            fields_table,
            html_party,
            html_fin,
            html_cb,
            html_lines,
            json_viewer,
            markdown_viewer,
            flags_viewer,
            json_export_btn,
            csv_export_btn,
            doc_state,
        ],
    )

    page_slider.change(
        fn=switch_page,
        inputs=[page_slider, doc_state],
        outputs=[annotated_image],
    )


# ---------------------------------------------------------------------------
# Direct Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("GRADIO_PORT", 7860))
    print(f"Starting DocExtract AI Gradio Workbench on http://localhost:{port}...")
    demo.launch(server_name="0.0.0.0", server_port=port, inbrowser=False, show_error=True, theme=gr.themes.Soft(primary_hue="blue", neutral_hue="slate"), css=CUSTOM_CSS)
