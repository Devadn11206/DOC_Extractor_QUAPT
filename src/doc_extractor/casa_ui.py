"""
Review UI for DOC-01 (CASA account opening) extraction.

Upload a form image. The UI shows:
  - the document status and flags,
  - a field table with the routing decision (ok or REVIEW),
  - the page image, where selecting a field draws its evidence box (PRD FR-REV-001),
  - the full output in the PRD format (section 6.3).

Anything marked REVIEW must be checked by a person (PRD FR-CNF-003, GR-008, GR-009).

Run in Colab or Kaggle with make_app(reader).launch(share=True), see notebooks/casa_ui_colab.ipynb.
"""

from __future__ import annotations

from typing import Any, List, Optional

import gradio as gr
from PIL import Image, ImageDraw

from doc_extractor.casa_reader import read_casa_form

TABLE_HEADERS = ["field", "value", "null_reason", "routing", "error", "evidence_source"]


def result_rows(result: dict) -> List[List[Any]]:
    """Flattens the fields of one result into table rows."""
    rows = []
    for name, field in result.get("fields", {}).items():
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


def highlight(image: Image.Image, bbox: Optional[List[int]]) -> Image.Image:
    """Copy of the page with the evidence box drawn in red (FR-REV-001). bbox is in page pixels, top-left origin."""
    page = image.convert("RGB").copy()
    if bbox and len(bbox) == 4:
        draw = ImageDraw.Draw(page)
        draw.rectangle([bbox[0], bbox[1], bbox[2], bbox[3]], outline=(220, 0, 0), width=4)
    return page


def show_field(name: Optional[str], state: Optional[dict]):
    """Returns the page with the selected field's evidence box, and a note describing the evidence."""
    if not state or not name:
        return None, "Select a field to see its evidence on the page."
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

    def extract(image):
        if image is None:
            return "Upload a form image first.", [], None, None, None, gr.update(choices=[], value=None), ""
        result = read_casa_form(image, reader)
        flags = ", ".join(f["code"] for f in result.get("flags", [])) or "none"
        summary = (
            f"**Document status:** {result['status']}  \n"
            f"**Model time:** {result['timing']['model_seconds']} s  \n"
            f"**Flags:** {flags}"
        )
        page = image.convert("RGB")
        state = {"image": page, "fields": result.get("fields", {})}
        names = list(result.get("fields", {}).keys())
        return (summary, result_rows(result), result.get("prd_envelope"), page, state,
                gr.update(choices=names, value=None), "")

    with gr.Blocks(title="CASA form extraction") as demo:
        gr.Markdown(
            "# CASA account opening form extraction\n"
            "Upload a form. Fields marked **REVIEW** must be checked by a person before use."
        )
        state = gr.State(None)
        with gr.Row():
            with gr.Column():
                image = gr.Image(type="pil", label="Form image")
                button = gr.Button("Extract", variant="primary")
                summary = gr.Markdown()
                table = gr.Dataframe(headers=TABLE_HEADERS, label="Fields", interactive=False)
            with gr.Column():
                pick = gr.Dropdown(label="Select a field to show its evidence on the page", choices=[])
                viewer = gr.Image(label="Page with evidence box", type="pil")
                note = gr.Markdown()
        envelope = gr.JSON(label="PRD envelope (section 6.3)")
        button.click(extract, inputs=image,
                     outputs=[summary, table, envelope, viewer, state, pick, note])
        pick.change(show_field, inputs=[pick, state], outputs=[viewer, note])
    return demo
