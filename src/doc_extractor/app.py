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
import tempfile
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

from doc_extractor.config import Settings, load_settings
from doc_extractor.contracts_b import MVPDocumentType
from doc_extractor.extractor import SchemaGuidedExtractor, SCHEMA_MAP
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline, ReviewerOverrideRequest
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.routing import (
    DEFAULT_MODEL,
    LANGUAGES,
    UNSTRUCTURED_SUBTYPES,
    DocumentCategory,
    RoutedExtractor,
    build_generic_prompt,
    heuristic_classify,
    select_model,
)
from doc_extractor.storage import LocalDiskStorage

AUTO = "Auto-Detect"
TENANT_ID = "test_tenant"
OVERRIDE_REVIEWER_ID = "extraction_ui_user"

try:
    import gradio as gr
except ImportError:
    gr = None


_SETTINGS: Optional[Settings] = None
_PIPELINES: Dict[str, Tuple[ModuleAPipeline, ModuleBPipeline]] = {}


def get_settings() -> Settings:
    """Application settings; validated once and shared by every module the app builds."""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


def configure_settings(settings: Settings) -> None:
    """Sets the application settings (e.g. a non-default OCR backend) before processing."""
    global _SETTINGS
    _SETTINGS = settings


def get_pipelines(settings: Optional[Settings] = None) -> Tuple[ModuleAPipeline, ModuleBPipeline]:
    """
    Builds (and caches per settings) Module A and Module B with the *same* Settings, so the
    configured storage path and OCR backend are used instead of silent defaults. A missing OCR
    runtime raises here with an actionable error.
    """
    settings = settings if settings is not None else get_settings()
    key = settings.model_dump_json()
    if key not in _PIPELINES:
        storage = LocalDiskStorage(settings.storage_base_dir, tenant_id=TENANT_ID)
        pipe_a = ModuleAPipeline(settings=settings, storage=storage, tenant_id=TENANT_ID)
        pipe_b = ModuleBPipeline(storage=storage, tenant_id=TENANT_ID, settings=settings)
        _PIPELINES[key] = (pipe_a, pipe_b)
    return _PIPELINES[key]


def get_pipeline():
    """Backward-compatible (pipe_a, pipe_b, pipe_c) accessor; pipe_c is the canonical ModuleCPipeline."""
    pipe_a, pipe_b = get_pipelines()
    return pipe_a, pipe_b, ModuleCPipeline(extractor=SchemaGuidedExtractor(use_llm=False))


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
        color = colors[idx % len(colors)]

        # Draw rectangle
        draw.rectangle([x0, y0, x1, y1], outline=color, width=3)

        # Draw label header
        label = f"{field_name.split('.')[-1]}: {str(val)[:20]}"
        text_bbox = draw.textbbox((x0, max(0, y0 - 18)), label)
        draw.rectangle(text_bbox, fill=color)
        draw.text((x0 + 2, max(0, y0 - 18)), label, fill="#000000")

    return img_draw


_ROUTED: Optional[RoutedExtractor] = None


def get_routed_extractor() -> RoutedExtractor:
    """One RoutedExtractor per process so model weights load once."""
    global _ROUTED
    if _ROUTED is None:
        # allow_fallback=False: a failed VLM run must surface (or use the routing fallback in
        # ModuleCPipeline), never silently degrade to regex heuristics.
        _ROUTED = RoutedExtractor(allow_fallback=False)
    return _ROUTED


def _classification_markdown(info: Dict[str, Any]) -> str:
    rows = [
        ("Category", info.get("category"), info.get("category_source")),
        ("Document type", info.get("document_type"), info.get("type_source")),
        ("Sub-type", info.get("subtype") or "-", info.get("subtype_source", "-")),
        ("Language", info.get("language"), info.get("language_source")),
        ("Suggested type", info.get("suggested_document_type") or "-", "advisory only; select it to apply"),
        (
            "Confidence",
            f"{float(info['confidence']):.2f}" if info.get("confidence") is not None else "n/a",
            info.get("method", "-"),
        ),
        ("Model", info.get("model_used", "-"), "fallback used" if info.get("fallback_occurred") else "routed"),
    ]
    lines = ["| Field | Value | Source |", "| --- | --- | --- |"]
    lines += [f"| {k} | {v} | {src} |" for k, v, src in rows]
    return "\n".join(lines)


def _write_json_download(document_id: str, text: str) -> str:
    out = Path(tempfile.gettempdir()) / f"{document_id}.json"
    out.write_text(text, encoding="utf-8")
    return str(out)


def _resolve_original_image(pipe_a: ModuleAPipeline, payload_a) -> Optional[Path]:
    """Resolves the original (200 DPI) first-page image used as the annotation canvas."""
    if not payload_a.pages:
        return None
    path = pipe_a.storage.resolve_uri_to_path(payload_a.pages[0].original_storage_uri)
    return path if path.exists() else None


def _module_c_for(use_vlm: bool, category: DocumentCategory, language: str) -> ModuleCPipeline:
    """
    Builds the canonical Module C orchestrator for one request.

    VLM mode: the routed model's multimodal extractor (FR-RTE-001) with the default model as
    fallback (FR-RTE-003). CPU mode: the deterministic heuristic extractor. Extraction,
    normalization, evidence and the envelope all happen inside ModuleCPipeline.
    """
    if not use_vlm:
        return ModuleCPipeline(extractor=SchemaGuidedExtractor(use_llm=False))

    routed = get_routed_extractor()
    primary_name = select_model(category, language, routed.table)
    default_name = routed.table.get("default_model", DEFAULT_MODEL)
    fallback = routed.get_extractor(default_name) if default_name != primary_name else None
    return ModuleCPipeline(extractor=routed.get_extractor(primary_name), fallback_extractor=fallback)


def process_document(
    file_obj,
    category_choice: str,
    doc_type_choice: str,
    subtype_choice: str,
    language_choice: str,
    use_qwen_vl: bool,
    settings: Optional[Settings] = None,
) -> Tuple[Optional[Image.Image], str, str, str, Optional[str]]:
    """Returns (annotated_image, status_text, json_output, classification_markdown, json_file_path).

    Category, type, sub-type and language are either user-selected or auto-detected
    (FR-DEMO-002..005). The category picks the model via the routing table (FR-RTE-001).
    Extraction always runs through ModuleCPipeline; this function only wires Module A/B
    outputs (including the explicit page-image references) into it and renders the result.
    ``settings`` defaults to the application settings (see ``get_settings``).
    """
    if file_obj is None:
        return None, "No file uploaded", "{}", "", None

    file_path = file_obj.name if hasattr(file_obj, "name") else str(file_obj)
    claimed_name = Path(file_path).name

    try:
        settings = settings if settings is not None else get_settings()
        pipe_a, pipe_b = get_pipelines(settings)

        # 1. Ingestion & preprocessing (Module A)
        payload_a = pipe_a.process_document(file_path, claimed_filename=claimed_name)
        if payload_a.status == "rejected":
            err = json.dumps(payload_a.to_dict(), indent=2)
            return None, f"Rejected: {payload_a.error}", err, "", None

        # 2. Explicit Module A page-image references (the VLM reads these, never storage guesses)
        page_images = pipe_b.load_page_images(payload_a)
        orig_path = _resolve_original_image(pipe_a, payload_a)

        # 3. Layout parsing + rule-based classification (Module B, configured OCR backend)
        payload_b = pipe_b.process_handoff(payload_a)
        if payload_b.status == "failed":
            err = payload_b.error or {}
            msg = f"Module B failed: {err.get('code', 'UNKNOWN')}: {err.get('message', 'no detail')}"
            return None, msg, json.dumps({"error": err}, indent=2, default=str), "", None
        # Module B's content-based type is already subject to its confidence threshold: it is
        # UNKNOWN (needs_review, no schema bound) whenever classification is inconclusive.
        module_b_type = payload_b.classification.document_type.value
        rule_type = module_b_type if module_b_type != "UNKNOWN" else None
        rule_confidence = payload_b.classification.classification_confidence if rule_type else None

        # 4. Resolve category, type, sub-type, language: user choice wins, else auto-detect
        user_cat = bool(category_choice) and category_choice != AUTO
        user_type = bool(doc_type_choice) and doc_type_choice != AUTO
        user_lang = bool(language_choice) and language_choice != AUTO
        user_sub = bool(subtype_choice) and subtype_choice != AUTO

        classify_image = page_images[0].image_path if page_images else None
        auto: Dict[str, Any] = {}
        if not (user_cat and user_type and user_lang):
            if use_qwen_vl and classify_image is not None and Path(classify_image).exists():
                auto = get_routed_extractor().classify(
                    str(classify_image), claimed_name, rule_type, rule_confidence
                )
            else:
                auto = heuristic_classify(claimed_name, rule_type, rule_confidence)

        category = DocumentCategory(category_choice.lower() if user_cat else auto.get("category", "structured"))

        # A document-specific schema is bound only by (a) Module B's thresholded classification
        # or (b) an explicit, supported user override. Filename hints and the VLM classifier's
        # uncalibrated self-reported type may suggest a type but never bind one, and an
        # unidentified document is never defaulted to DOC-01.
        doc_type = doc_type_choice if user_type else rule_type
        if doc_type not in SCHEMA_MAP:
            doc_type = None
        if category != DocumentCategory.STRUCTURED and not user_type:
            doc_type = None
        suggested_type = auto.get("document_type") if auto.get("document_type") in SCHEMA_MAP else None
        if suggested_type == doc_type:
            suggested_type = None

        language = LANGUAGES[language_choice] if user_lang else str(auto.get("language") or "en")
        subtype = subtype_choice if user_sub else auto.get("subtype")
        if category != DocumentCategory.UNSTRUCTURED or subtype in (None, "null"):
            subtype = None

        info: Dict[str, Any] = {
            "category": category.value,
            "category_source": "user" if user_cat else "auto",
            "document_type": doc_type or ("UNKNOWN" if category == DocumentCategory.STRUCTURED else "generic"),
            "type_source": "user" if user_type else ("module_b" if doc_type else "unidentified"),
            "suggested_document_type": suggested_type,
            "subtype": subtype,
            "subtype_source": "user" if user_sub else "auto",
            "language": language,
            "language_source": "user" if user_lang else "auto",
            # Reported as measured, or None (unavailable): nothing is invented for display either.
            "confidence": None if user_cat else auto.get("confidence"),
            "method": "user" if user_cat else auto.get("method", "-"),
        }

        schema_bound = category == DocumentCategory.STRUCTURED and doc_type in SCHEMA_MAP
        unidentified = category == DocumentCategory.STRUCTURED and doc_type is None
        use_vlm = bool(use_qwen_vl and page_images)

        # CPU heuristic mode only supports the rule-based structured schemas
        if not use_vlm and not schema_bound and not unidentified:
            msg = "CPU heuristic mode only supports structured DOC-01 to DOC-03. Enable the VLM for this category."
            return None, msg, json.dumps({"error": msg}, indent=2), _classification_markdown(info), None

        # 5. Canonical extraction: ModuleCPipeline is the only extraction/envelope path
        # Unidentified docs run nothing (they are routed to review without a model call).
        module_c = _module_c_for(use_vlm and not unidentified, category, language)
        if unidentified:
            # Inconclusive classification: Module C routes UNKNOWN to needs_review without running
            # any schema-specific extraction (CLASSIFICATION_UNCERTAIN). A supported type must be
            # selected explicitly to process the document.
            env = module_c.process(payload_b)
        elif schema_bound:
            if user_type and payload_b.classification.document_type.value != doc_type:
                # Explicit user override via Module B's audited override path (FR-CLS-004):
                # resolves the approved schema and records who overrode the type and why.
                payload_b = pipe_b.apply_reviewer_override(
                    payload_b,
                    ReviewerOverrideRequest(
                        document_id=payload_b.document_id,
                        tenant_id=payload_b.tenant_id,
                        reviewer_id=OVERRIDE_REVIEWER_ID,
                        new_document_type=MVPDocumentType(doc_type),
                        override_reason="Document type explicitly selected in the extraction UI",
                    ),
                )
            env = module_c.process(payload_b, page_images=page_images if use_vlm else None)
        else:
            env = module_c.process_generic(
                payload_b,
                page_images,
                build_generic_prompt(category, subtype, language),
                document_type=doc_type or "generic",
                force_review=category == DocumentCategory.HANDWRITTEN,
            )

        provenance = env.run.provenance
        info["model_used"] = (provenance.model_name if provenance else None) or env.run.model_versions.get(
            "extractor", "-"
        )
        info["fallback_occurred"] = bool(provenance and provenance.fallback_occurred)

        annotated_img = None
        if orig_path is not None:
            with Image.open(orig_path) as pil_img:
                annotated_img = draw_bounding_boxes(pil_img, env.fields)

        status_msg = (
            f"Status: {env.document_metadata.status.upper()} | Category: {category.value} | "
            f"Type: {env.document_metadata.document_type} | Fields: {len(env.fields)} | "
            f"Flags: {len(env.flags)} | Model: {info['model_used']}"
        )
        json_text = json.dumps(env.model_dump(), indent=2, default=str)
        return (
            annotated_img,
            status_msg,
            json_text,
            _classification_markdown(info),
            _write_json_download(payload_a.document_id, json_text),
        )

    except Exception as e:
        return None, f"Error: {e}", json.dumps({"error": str(e)}, indent=2), "", None


def create_demo(settings: Optional[Settings] = None) -> "gr.Blocks":
    """Constructs Gradio UI layout (FR-DEMO-001..006). ``settings`` configures every module it runs."""
    if settings is not None:
        configure_settings(settings)
    with gr.Blocks(title="Document Extraction Engine") as demo:
        gr.Markdown(
            "# 📄 Document Extraction Engine\n"
            "Upload a document, pick its category and type (or leave on Auto-Detect), and get classified, "
            "grounded JSON. The category decides which model runs."
        )

        with gr.Row():
            with gr.Column(scale=1):
                input_file = gr.File(label="Upload Document (PDF, JPG, PNG, TIFF)", file_types=[".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"])
                category_dd = gr.Dropdown(
                    choices=[AUTO, "Structured", "Unstructured", "Handwritten"], value=AUTO,
                    label="Document Category",
                )
                doc_type_dd = gr.Dropdown(
                    choices=[AUTO, "DOC-01", "DOC-02", "DOC-03"], value=AUTO,
                    label="Document Type (structured schemas)",
                )
                subtype_dd = gr.Dropdown(
                    choices=[AUTO] + UNSTRUCTURED_SUBTYPES, value=AUTO,
                    label="Unstructured Sub-type (brochure, pamphlet, menu card, ...)",
                )
                language_dd = gr.Dropdown(
                    choices=[AUTO] + list(LANGUAGES), value=AUTO, label="Language",
                )
                use_vlm_checkbox = gr.Checkbox(
                    value=True,
                    label="🤖 Use Qwen3-VL-8B (uncheck for CPU heuristic fallback, structured DOC-01 to DOC-03 only)",
                )
                submit_btn = gr.Button("Extract Document", variant="primary")

            with gr.Column(scale=2):
                status_box = gr.Textbox(label="Status & Quality Gate", interactive=False)
                classification_md = gr.Markdown(label="Classification Result")
                with gr.Tab("Visual Grounding"):
                    output_image = gr.Image(label="Annotated Document Page (with Grounded Bounding Boxes)", type="pil")
                with gr.Tab("Canonical JSON Output"):
                    output_json = gr.Code(label="Extracted JSON Envelope", language="json")
                    json_file = gr.File(label="Download JSON")

        submit_btn.click(
            fn=process_document,
            inputs=[input_file, category_dd, doc_type_dd, subtype_dd, language_dd, use_vlm_checkbox],
            outputs=[output_image, status_box, output_json, classification_md, json_file],
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

