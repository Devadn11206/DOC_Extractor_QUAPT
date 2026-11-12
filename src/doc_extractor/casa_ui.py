"""
Simple review UI for DOC-01 (CASA account opening) extraction.

Upload a form image, get the PRD envelope (section 6.3) and a field table with the routing decision.
Anything marked REVIEW must be checked by a person (PRD FR-CNF-003, GR-008, GR-009).

Run in Colab or Kaggle with make_app(reader).launch(share=True), see notebooks/casa_ui_colab.ipynb.
"""

from __future__ import annotations

from typing import Any, List

import gradio as gr

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


def make_app(reader: Any) -> gr.Blocks:
    """Builds the UI. reader is a QwenVLCasaReader or PaddleVLCasaReader (loads on first use)."""

    def extract(image):
        if image is None:
            return "Upload a form image first.", [], None
        result = read_casa_form(image, reader)
        flags = ", ".join(f["code"] for f in result.get("flags", [])) or "none"
        summary = (
            f"**Document status:** {result['status']}  \n"
            f"**Model time:** {result['timing']['model_seconds']} s  \n"
            f"**Flags:** {flags}"
        )
        return summary, result_rows(result), result.get("prd_envelope")

    with gr.Blocks(title="CASA form extraction") as demo:
        gr.Markdown(
            "# CASA account opening form extraction\n"
            "Upload a form. Fields marked **REVIEW** must be checked by a person before use."
        )
        image = gr.Image(type="pil", label="Form image")
        button = gr.Button("Extract", variant="primary")
        summary = gr.Markdown()
        table = gr.Dataframe(headers=TABLE_HEADERS, label="Fields", interactive=False)
        envelope = gr.JSON(label="PRD envelope (section 6.3)")
        button.click(extract, inputs=image, outputs=[summary, table, envelope])
    return demo
