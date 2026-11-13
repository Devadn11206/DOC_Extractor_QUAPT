"""
Review UI for document extraction (CASA, insurance application, discharge summary).

Upload a form image. The UI shows:
  - the detected document type and confidence (FR-CLS-001), or a reviewer override (FR-CLS-004),
  - the document status and flags,
  - a field table with the routing decision (ok or REVIEW),
  - the page image, where selecting a field draws its evidence box (FR-REV-001),
  - the full output in the PRD format (section 6.3).

Anything marked REVIEW must be checked by a person (FR-CNF-003, GR-008, GR-009).

Run in Colab or Kaggle with make_app(reader).launch(share=True), see notebooks/casa_ui_colab.ipynb.
"""

from __future__ import annotations

from typing import Any, List, Optional

import gradio as gr
from PIL import Image, ImageDraw

from doc_extractor.document_router import TYPE_KEYWORDS, route_document

TABLE_HEADERS = ["field", "value", "null_reason", "routing", "error", "evidence_source"]
AUTO = "Auto-detect"
TYPE_CHOICES = [AUTO] + list(TYPE_KEYWORDS.keys())


def result_rows(fields: dict) -> List[List[Any]]:
    """Flattens the fields of one extraction into table rows."""
    rows = []
    for name, field in fields.items():
        evidence = field.get("evidence") or {}
        rows.append([
            name,
            "" if field.get("value") is None else str(field.get("value")),
            field.get("null_reason") or "",
            "REVIEW" if field.get("review_required") else "ok",
            field.get("error") or "",
            evidence.get("source", ""),
        ])
    return rows


def nominee_rows(nominees: list) -> List[List[Any]]:
    """Insurance nominees (repeating group, FR-EXT-005) as table rows, in page order."""
    rows = []
    for i, n in enumerate(nominees, start=1):
        rows.append([
            f"nominee {i}",
            f"{n.get('name') or ''} | {n.get('relationship') or ''} | age {n.get('age') or ''} | share {n.get('share_percentage') or ''}%",
            "" if n.get("name") else "absent",
            "REVIEW" if not n.get("name") else "ok",
            "",
            "",
        ])
    return rows


def highlight(image: Image.Image, bbox: Optional[List[int]]) -> Image.Image:
    """Copy of the page with the evidence box drawn in red (FR-REV-001). bbox is in page pixels, top-left origin."""
    page = image.convert("RGB").copy()
    if bbox and len(bbox) == 4:
        draw = ImageDraw.Draw(page)
        draw.rectangle([bbox[0], bbox[1], bbox[2], bbox[3]], outline=(220, 0, 0), width=4)
    return page


def show_field(name: Optional[str], state: Optional[dict]):
    """Returns the page with the selected field's evidence box, and a note describing the evidence."""
    if not state or not name or not state.get("fields"):
        return (state or {}).get("image"), "Select a field to see its evidence on the page."
    field = state["fields"].get(name, {})
    evidence = field.get("evidence") or {}
    snippet = evidence.get("text") or "no snippet"
    bbox = evidence.get("bbox")
    if not bbox:
        return (state["image"],
                f"**{name}**: no box on the page (source: {evidence.get('source', 'none')}). Snippet: {snippet}")
    return highlight(state["image"], bbox), f"**{name}**: evidence box shown. Snippet: {snippet}"


def make_app(reader: Any) -> gr.Blocks:
    """Builds the UI. reader is a QwenVLCasaReader or PaddleVLCasaReader (loads on first use)."""

    def extract(image, choice):
        if image is None:
            return ("Upload a form image first.", [], None, None, None, gr.update(choices=[], value=None), "")
        override = None if choice in (None, AUTO) else choice
        routed = route_document(image, reader, override=override)
        cls = routed["classification"]
        extraction = routed["extraction"]
        flags = ", ".join(f["code"] for f in routed["flags"]) or "none"
        origin = "reviewer override" if cls.get("overridden") else "auto-detected"
        summary = (
            f"**Document type:** {cls['doc_type']} ({cls.get('document_id') or 'no id'}, "
            f"{cls.get('domain') or 'unknown domain'}) - {origin}, confidence {cls['confidence']}  \n"
            f"**Status:** {routed['status']}  \n"
            f"**Flags:** {flags}"
        )
        if extraction is None:
            return summary, [], None, image.convert("RGB"), None, gr.update(choices=[], value=None), \
                "No extraction for this document type (see flags)."
        fields = extraction.get("fields", {})
        envelope = extraction.get("prd_envelope") or {"document_type": cls["doc_type"], "fields": fields}
        state = {"image": image.convert("RGB"), "fields": fields}
        names = list(fields.keys())
        rows = result_rows(fields) + nominee_rows(extraction.get("nominees") or [])
        return (summary, rows, envelope, image.convert("RGB"), state,
                gr.update(choices=names, value=None), "")

    with gr.Blocks(title="Document extraction") as demo:
        gr.Markdown(
            "# Document extraction (CASA, insurance application, discharge summary)\n"
            "Upload a form. The type is detected automatically; you can override it. "
            "Fields marked **REVIEW** must be checked by a person before use."
        )
        state = gr.State(None)
        with gr.Row():
            with gr.Column():
                image = gr.Image(type="pil", label="Document image")
                choice = gr.Dropdown(label="Document type", choices=TYPE_CHOICES, value=AUTO)
                button = gr.Button("Extract", variant="primary")
                summary = gr.Markdown()
                table = gr.Dataframe(headers=TABLE_HEADERS, label="Fields", interactive=False)
            with gr.Column():
                pick = gr.Dropdown(label="Select a field to show its evidence on the page", choices=[])
                viewer = gr.Image(label="Page with evidence box", type="pil")
                note = gr.Markdown()
        envelope = gr.JSON(label="PRD envelope (section 6.3)")
        button.click(extract, inputs=[image, choice],
                     outputs=[summary, table, envelope, viewer, state, pick, note])
        pick.change(show_field, inputs=[pick, state], outputs=[viewer, note])
    return demo
