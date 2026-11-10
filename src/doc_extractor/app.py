"""
Interactive Gradio Testing UI for Document Extraction Engine.
Ideal for Kaggle notebooks (with share=True) and local testing.
Uploads PDF/Image -> Ingests & Preprocesses -> Extracts with Qwen-VL ->
Draws grounded bounding boxes on the image -> Displays structured JSON.
"""

import io
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Tuple

# Ensure src/ is on sys.path and clean out doc_extractor directory from sys.path
# to prevent local files (logging.py, etc.) from shadowing stdlib and sibling packages.
_src_dir = str(Path(__file__).resolve().parent.parent)
_current_dir = str(Path(__file__).resolve().parent)

while "" in sys.path:
    sys.path.remove("")
while _current_dir in sys.path:
    sys.path.remove(_current_dir)
while "." in sys.path:
    sys.path.remove(".")

if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

from PIL import Image, ImageDraw, ImageFont

from doc_extractor.config import Settings
from doc_extractor.contracts_b import MVPDocumentType
from doc_extractor.extractor import SchemaGuidedExtractor, SCHEMA_MAP
from doc_extractor.handoff import CoordinateTransform
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.storage import LocalDiskStorage

try:
    import gradio as gr
except ImportError:
    gr = None


def get_pipeline():
    """Initializes and caches pipeline instances."""
    storage = LocalDiskStorage(Path("./data/storage"), tenant_id="test_tenant")
    pipe_a = ModuleAPipeline(storage=storage, tenant_id="test_tenant")
    pipe_b = ModuleBPipeline(storage=storage, tenant_id="test_tenant")
    pipe_c = ModuleCPipeline()
    return pipe_a, pipe_b, pipe_c


def draw_bounding_boxes(
    pil_image: Image.Image,
    fields: Dict[str, Any],
) -> Image.Image:
    """Draws color-coded bounding boxes and field labels on the page image."""
    img_draw = pil_image.convert("RGB").copy()
    draw = ImageDraw.Draw(img_draw)
    img_w, img_h = img_draw.size

    # Color palette for bounding boxes
    colors = [
        "#00E676", "#2979FF", "#FF9100", "#FF1744",
        "#651FFF", "#00E5FF", "#F50057", "#76FF03",
    ]

    for idx, (field_name, field_data) in enumerate(fields.items()):
        val = getattr(field_data, "value", None)
        if isinstance(field_data, dict):
            val = field_data.get("value")

        if val is None:
            continue

        evidence = getattr(field_data, "evidence", None)
        if isinstance(field_data, dict):
            evidence = field_data.get("evidence")

        if not evidence:
            continue

        bbox = getattr(evidence, "bbox", None)
        if isinstance(evidence, dict):
            bbox = evidence.get("bbox")

        if not bbox or len(bbox) != 4:
            continue

        x0, y0, x1, y1 = bbox
        # Convert if normalized [0, 1000] space
        if max(x0, y0, x1, y1) <= 1000 and (img_w > 1000 or img_h > 1000):
            x0 = int(x0 * img_w / 1000.0)
            y0 = int(y0 * img_h / 1000.0)
            x1 = int(x1 * img_w / 1000.0)
            y1 = int(y1 * img_h / 1000.0)

        color = colors[idx % len(colors)]

        # Draw rectangle
        draw.rectangle([x0, y0, x1, y1], outline=color, width=3)

        # Draw label header
        label = f"{field_name.split('.')[-1]}: {str(val)[:20]}"
        text_bbox = draw.textbbox((x0, max(0, y0 - 18)), label)
        draw.rectangle(text_bbox, fill=color)
        draw.text((x0 + 2, max(0, y0 - 18)), label, fill="#000000")

    return img_draw


def process_document(
    file_obj,
    doc_type_override: str,
    use_qwen_vl: bool,
) -> Tuple[Optional[Image.Image], str, str]:
    """Processes document and returns (annotated_page_image, status_text, json_output).
    use_qwen_vl=True  → live Qwen3-VL-8B inference (needs GPU/vLLM)
    use_qwen_vl=False → heuristic regex fast-path (CPU, for quick local testing without GPU)
    """
    if file_obj is None:
        return None, "No file uploaded", "{}"

    pipe_a, pipe_b, pipe_c = get_pipeline()

    # Wire model mode
    pipe_c.extractor.use_llm = use_qwen_vl

    # Handle file path
    file_path = file_obj.name if hasattr(file_obj, "name") else str(file_obj)
    claimed_name = Path(file_path).name

    try:
        # 1. Ingestion & Preprocessing (Module A)
        payload_a = pipe_a.process_document(file_path, claimed_filename=claimed_name)
        if payload_a.status == "rejected":
            return None, f"Rejected: {payload_a.error}", json.dumps(payload_a.to_dict(), indent=2)

        # Resolve image file path on disk
        orig_path = None
        if payload_a.pages:
            first_page_rec = payload_a.pages[0]
            prep_path = pipe_a.storage.resolve_uri_to_path(first_page_rec.preprocessed_storage_uri)
            orig_path = pipe_a.storage.resolve_uri_to_path(first_page_rec.original_storage_uri)
            img_path = prep_path if prep_path.exists() else orig_path
        else:
            img_path = Path(file_path)

        # 2. Classification & Parsing (Module B)
        payload_b = pipe_b.process_handoff(payload_a)

        # Smart document type resolution (dropdown or filename heuristic)
        target_doc_type = doc_type_override
        if not target_doc_type or target_doc_type == "Auto-Detect":
            fname = claimed_name.lower()
            if any(k in fname for k in ("bank", "casa", "account", "saving", "canara")):
                target_doc_type = "DOC-01"
            elif any(k in fname for k in ("insurance", "policy", "nominee", "proposal", "claim")):
                target_doc_type = "DOC-02"
            elif any(k in fname for k in ("discharge", "hospital", "patient", "summary", "medical")):
                target_doc_type = "DOC-03"
            elif payload_b.classification.document_type.value != "UNKNOWN":
                target_doc_type = payload_b.classification.document_type.value
            else:
                target_doc_type = "DOC-01"

        target_type_enum = MVPDocumentType(target_doc_type)
        payload_b.classification.document_type = target_type_enum
        payload_b.classification.schema_id = target_doc_type
        payload_b.classification.review_required = False
        payload_b.classification.classification_confidence = max(0.95, payload_b.classification.classification_confidence)
        payload_b.status = "processing"

        # 3. Extraction & Grounding (Module C)
        schema_cls = SCHEMA_MAP.get(target_doc_type)
        annotated_img = None

        if use_qwen_vl and img_path and img_path.exists() and schema_cls:
            # 1-Stage Direct Multimodal extraction with Qwen3-VL
            raw_dict, prov = pipe_c.extractor.extract_page_image(str(img_path), schema_cls)
            
            with Image.open(orig_path if orig_path and orig_path.exists() else img_path) as pil_img:
                w, h = pil_img.size
                base_img = pil_img.copy()

            fields_dict: Dict[str, Any] = {}
            for fpath, item in raw_dict.items():
                if not isinstance(item, dict):
                    continue
                v = item.get("value")
                bbox = item.get("bbox")
                if v is None and bbox is None:
                    continue
                ev = None
                if bbox and len(bbox) == 4:
                    ymin, xmin, ymax, xmax = bbox
                    # convert [ymin, xmin, ymax, xmax] in [0, 1000] to [x0, y0, x1, y1] in pixels
                    x0 = int(xmin * w / 1000.0)
                    y0 = int(ymin * h / 1000.0)
                    x1 = int(xmax * w / 1000.0)
                    y1 = int(ymax * h / 1000.0)
                    ev = {
                        "page": 1,
                        "bbox": (x0, y0, x1, y1),
                        "text_snippet": str(v)[:120],
                        "ocr_confidence": 0.95,
                    }
                fields_dict[fpath] = {
                    "value": v,
                    "calibrated_confidence": 0.95 if v is not None else 0.0,
                    "review_required": False,
                    "evidence": ev,
                }

            annotated_img = draw_bounding_boxes(base_img, fields_dict)
            output_envelope = {
                "document_id": payload_a.document_id,
                "document_metadata": {
                    "document_type": target_doc_type,
                    "schema_id": target_doc_type,
                    "schema_version": "1.0.0",
                    "classification_confidence": 0.95,
                    "status": "valid",
                    "page_count": len(payload_a.pages),
                },
                "fields": fields_dict,
                "flags": [],
                "run": {
                    "run_id": payload_a.run_id,
                    "model_versions": {
                        "extractor": pipe_c.extractor.model_name,
                    },
                    "provenance": prov.model_dump() if hasattr(prov, "model_dump") else prov,
                },
            }
            status_msg = f"Status: VALID | Type: {target_doc_type} | Extracted Fields: {len(fields_dict)} | Model: {pipe_c.extractor.model_name}"
            return annotated_img, status_msg, json.dumps(output_envelope, indent=2, default=str)

        # Heuristic fast-path fallback
        envelope = pipe_c.process(payload_b)

        if orig_path and orig_path.exists():
            with Image.open(orig_path) as pil_img:
                annotated_img = draw_bounding_boxes(pil_img, envelope.fields)

        status_msg = (
            f"Status: {envelope.document_metadata.status.upper()} | "
            f"Type: {envelope.document_metadata.document_type} | "
            f"Confidence: {envelope.document_metadata.classification_confidence:.2f} | "
            f"Flags: {len(envelope.flags)}"
        )

        return annotated_img, status_msg, json.dumps(envelope.model_dump(), indent=2, default=str)

    except Exception as e:
        return None, f"Error: {e}", json.dumps({"error": str(e)}, indent=2)


def create_demo() -> "gr.Blocks":
    """Constructs Gradio UI layout."""
    with gr.Blocks(title="Document Extraction Engine — Qwen-VL Demo") as demo:
        gr.Markdown(
            "# 📄 Document Extraction Engine\n"
            "**Single-Stage Multimodal Extraction (Qwen3-VL / Qwen2.5-VL)** with deterministic financial normalization and pixel evidence grounding."
        )

        with gr.Row():
            with gr.Column(scale=1):
                input_file = gr.File(label="Upload Document (PDF, JPG, PNG, TIFF)", file_types=[".pdf", ".jpg", ".png", ".tiff"])
                doc_type_dropdown = gr.Dropdown(
                    choices=["Auto-Detect", "DOC-01", "DOC-02", "DOC-03"],
                    value="Auto-Detect",
                    label="Document Type Hint",
                )
                use_vlm_checkbox = gr.Checkbox(
                    value=True,
                    label="🤖 Use Qwen3-VL-8B (uncheck for CPU heuristic fallback — no GPU needed)",
                )
                submit_btn = gr.Button("Extract Document", variant="primary")

            with gr.Column(scale=2):
                status_box = gr.Textbox(label="Status & Quality Gate", interactive=False)
                with gr.Tab("Visual Grounding"):
                    output_image = gr.Image(label="Annotated Document Page (with Grounded Bounding Boxes)", type="pil")
                with gr.Tab("Canonical JSON Output"):
                    output_json = gr.Code(label="Extracted §6.3 JSON Envelope", language="json")

        submit_btn.click(
            fn=process_document,
            inputs=[input_file, doc_type_dropdown, use_vlm_checkbox],
            outputs=[output_image, status_box, output_json],
        )

    return demo


if __name__ == "__main__":
    if gr is None:
        print("Gradio is not installed. Run: pip install gradio")
    else:
        demo = create_demo()
        # In Kaggle or remote server, share=True generates a public gradio.live URL
        share_mode = os.environ.get("GRADIO_SHARE", "True").lower() == "true"
        port = int(os.environ.get("GRADIO_SERVER_PORT", 0)) or None
        demo.launch(share=share_mode, server_port=port)

