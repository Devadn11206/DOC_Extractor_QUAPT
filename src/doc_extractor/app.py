"""
Interactive Gradio Testing UI for Document Extraction Engine (PRD v1.5).
Ideal for Kaggle notebooks (with share=True) and local testing.

Uploads PDF/Image -> Module A (200 DPI pages) -> Module B (classification hand-off) -> per page:
classify once, route to the specialist for the category (VLM, or OCR + text LLM), read the page ->
ModuleCPipeline (the canonical processing and envelope layer) -> grounded boxes on every page + JSON.

The app does the per-page routing and model calls (so it can stream live progress and turn pages
upright); ModuleCPipeline.process_routed_pages then normalizes, grounds, validates, gates and assembles
the envelope from those results. No model is run twice and no envelope is built here.
"""

import io
import json
import os

# Reduce CUDA fragmentation on small GPUs (must be set before torch initialises CUDA)
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
from pathlib import Path
import sys
import tempfile
import time
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
from doc_extractor.contracts_c import CanonicalExtractionEnvelope, FlagItem
from doc_extractor.extractor import SchemaGuidedExtractor, SCHEMA_MAP
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline, ReviewerOverrideRequest
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.routed_pages import RoutedPageResult
from doc_extractor.routing import (
    DEFAULT_MODEL,
    LANGUAGES,
    UNSTRUCTURED_SUBTYPES,
    DocumentCategory,
    RoutedExtractor,
    build_generic_prompt,
    heuristic_classify,
)
from doc_extractor.storage import LocalDiskStorage

AUTO = "Auto-Detect"
TENANT_ID = "test_tenant"
OVERRIDE_REVIEWER_ID = "extraction_ui_user"
ENGINE_CHOICES = {
    "Follow routing table": None,
    "GLM-OCR + Qwen3-4B-Instruct text (experimental)": "ocr_text",
    "GLM-OCR + Qwen2.5-7B text (experimental, comparison)": "ocr_text_7b",
}

try:
    import gradio as gr
except ImportError:
    gr = None


_SETTINGS: Optional[Settings] = None
_PIPELINES: Dict[str, Tuple[ModuleAPipeline, ModuleBPipeline]] = {}
_MODULE_C: Optional[ModuleCPipeline] = None


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
    Builds (and caches per settings) Module A and Module B with the *same* Settings, so the configured
    storage path and OCR backend are used instead of silent defaults. The default backend is the explicit
    no-OCR pass-through, so this needs no Tesseract; a configured OCR backend whose runtime is missing
    raises here with an actionable error (there is no fallback to another engine).
    """
    settings = settings if settings is not None else get_settings()
    key = settings.model_dump_json()
    if key not in _PIPELINES:
        storage = LocalDiskStorage(settings.storage_base_dir, tenant_id=TENANT_ID)
        pipe_a = ModuleAPipeline(settings=settings, storage=storage, tenant_id=TENANT_ID)
        pipe_b = ModuleBPipeline(storage=storage, tenant_id=TENANT_ID, settings=settings)
        _PIPELINES[key] = (pipe_a, pipe_b)
    return _PIPELINES[key]


def get_module_c() -> ModuleCPipeline:
    """The canonical Module C orchestrator. It reads nothing itself here: routed page results go in."""
    global _MODULE_C
    if _MODULE_C is None:
        _MODULE_C = ModuleCPipeline(extractor=SchemaGuidedExtractor(use_llm=False))
    return _MODULE_C


def get_pipeline():
    """Backward-compatible (pipe_a, pipe_b, pipe_c) accessor; pipe_c is the canonical ModuleCPipeline."""
    pipe_a, pipe_b = get_pipelines()
    return pipe_a, pipe_b, get_module_c()


def draw_bounding_boxes(
    pil_image: Image.Image,
    fields: Dict[str, Any],
    page: Optional[int] = None,
) -> Image.Image:
    """
    Draws color-coded boxes and labels from each field's EVIDENCE: ``(x0, y0, x1, y1)`` pixels of the
    200-DPI original page (PRD FR-EXT-011), exactly as ModuleCPipeline mapped them - nothing is rescaled
    or reordered here. With ``page`` only the evidence located on that page is drawn (the primary
    evidence and any additional evidence of the same value on other pages).
    """
    img_draw = pil_image.convert("RGB").copy()
    draw = ImageDraw.Draw(img_draw)

    # Color palette for bounding boxes
    colors = [
        "#00E676", "#2979FF", "#FF9100", "#FF1744",
        "#651FFF", "#00E5FF", "#F50057", "#76FF03",
    ]

    for idx, (field_name, field_data) in enumerate(fields.items()):
        val = field_data.get("value") if isinstance(field_data, dict) else getattr(field_data, "value", None)
        if val is None:
            continue

        evidences = []
        primary = field_data.get("evidence") if isinstance(field_data, dict) else getattr(field_data, "evidence", None)
        if primary:
            evidences.append(primary)
        extra = (field_data.get("additional_evidence") if isinstance(field_data, dict)
                 else getattr(field_data, "additional_evidence", None)) or []
        evidences.extend(extra)

        color = colors[idx % len(colors)]
        for evidence in evidences:
            ev_page = evidence.get("page") if isinstance(evidence, dict) else getattr(evidence, "page", None)
            if page is not None and ev_page != page:
                continue
            bbox = evidence.get("bbox") if isinstance(evidence, dict) else getattr(evidence, "bbox", None)
            if not bbox or len(bbox) != 4:
                continue
            x0, y0, x1, y1 = bbox

            draw.rectangle([x0, y0, x1, y1], outline=color, width=3)
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
        # allow_fallback=False: a failed model run must surface (or use the routing fallback), never
        # silently degrade to regex heuristics.
        _ROUTED = RoutedExtractor(allow_fallback=False)
    return _ROUTED


# Every page is read by default. DOC_MAX_PAGES=N is only an optional limit for quick test runs (0 = no limit).
MAX_PAGES = int(os.environ.get("DOC_MAX_PAGES", "0"))


def _fmt_confidence(value: Any) -> str:
    """Confidence as measured, or n/a when there is none (nothing is invented for display either)."""
    return f"{float(value):.2f}" if value is not None else "n/a"


def _classification_markdown(pages_info: List[Dict[str, Any]]) -> str:
    """One row per page: what it was classified as and which model read it."""
    lines = [
        "| Page | Category | Type | Sub-type | Language | Confidence | Model | Source |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for i in pages_info:
        lines.append(
            f"| {i.get('page', 1)} | {i.get('category')} | {i.get('document_type')}"
            f"{' (suggested ' + str(i['suggested_document_type']) + ')' if i.get('suggested_document_type') else ''} | {i.get('subtype') or '-'} | "
            f"{i.get('language')} | {_fmt_confidence(i.get('confidence'))} | {i.get('model_used', '-')}"
            f"{' (fallback)' if i.get('fallback_occurred') else ''} | {i.get('category_source')}/{i.get('method', '-')} |"
        )
    return "\n".join(lines)


def _write_json_download(document_id: str, text: str) -> str:
    out = Path(tempfile.gettempdir()) / f"{document_id}.json"
    out.write_text(text, encoding="utf-8")
    return str(out)


def _resolve_page(
    img_path: Path,
    filename: str,
    rule_type: Optional[str],
    rule_confidence: Optional[float],
    choices: Dict[str, str],
    page_no: int,
) -> Dict[str, Any]:
    """
    Category, type, sub-type and language for one page: user choice wins, else auto-detect (FR-CLS-005).

    A document-specific schema is bound ONLY by an explicit user choice of a supported type (v1.5 reads
    dynamic fields by default). A filename never binds a type or supplies a confidence, and the VLM
    router's self-reported type is advisory only (``suggested_document_type``).
    """
    user_cat = bool(choices["category"]) and choices["category"] != AUTO
    user_type = bool(choices["doc_type"]) and choices["doc_type"] != AUTO
    user_lang = bool(choices["language"]) and choices["language"] != AUTO
    user_sub = bool(choices["subtype"]) and choices["subtype"] != AUTO

    auto: Dict[str, Any] = {}
    if not (user_cat and user_type and user_lang):
        hint_type = rule_type if page_no == 1 else None
        hint_conf = rule_confidence if page_no == 1 else None
        if img_path and img_path.exists():
            auto = get_routed_extractor().classify(str(img_path), filename, hint_type, hint_conf)
        else:
            auto = heuristic_classify(filename, hint_type, hint_conf)

    category = DocumentCategory(choices["category"].lower() if user_cat else auto.get("category", "structured"))
    suggested = auto.get("document_type") or (rule_type if page_no == 1 else None)
    doc_type = choices["doc_type"] if user_type else None
    if doc_type not in SCHEMA_MAP or category != DocumentCategory.STRUCTURED:
        doc_type = None          # a fixed schema applies to the structured category only
    if suggested == doc_type:
        suggested = None
    language = LANGUAGES[choices["language"]] if user_lang else str(auto.get("language") or "en")
    subtype = choices["subtype"] if user_sub else auto.get("subtype")
    if category != DocumentCategory.UNSTRUCTURED or subtype in (None, "null"):
        subtype = None

    return {
        "page": page_no,
        "category": category.value,
        "category_source": "user" if user_cat else "auto",
        "document_type": doc_type or "generic",
        "schema_id": doc_type,
        "suggested_document_type": suggested,
        "type_source": "user" if doc_type else "auto",
        "subtype": subtype,
        "language": language,
        "language_source": "user" if user_lang else "auto",
        # as measured, or None (unavailable): a user's choice carries no model confidence
        "confidence": None if user_cat else auto.get("confidence"),
        "method": "user" if user_cat else auto.get("method", "-"),
        "rotation": _safe_rotation(auto.get("rotation")),
        "router_model": auto.get("router_model"),
        "filled_by": auto.get("filled_by"),
    }


_STEP_ICONS = {"pending": "⬜", "running": "⏳", "done": "✅", "failed": "❌"}


def _step(steps: List[Dict[str, Any]], key: str, label: Optional[str] = None,
          state: Optional[str] = None, detail: Optional[str] = None) -> None:
    """Add or update one row of the pipeline-steps panel."""
    row = next((r for r in steps if r["key"] == key), None)
    if row is None:
        row = {"key": key, "label": label or key, "state": "pending", "detail": "", "t0": None, "secs": None}
        steps.append(row)
    if label:
        row["label"] = label
    if state:
        if state == "running":
            row["t0"] = time.perf_counter()
        elif state in ("done", "failed") and row.get("t0"):
            row["secs"] = time.perf_counter() - row["t0"]
        row["state"] = state
    if detail is not None:
        row["detail"] = detail


def _render_steps(steps: List[Dict[str, Any]]) -> str:
    if not steps:
        return "Upload a document and click **Classify & Extract**."
    lines = ["| | Step | Result | Time |", "| --- | --- | --- | --- |"]
    for r in steps:
        t = f"{r['secs']:.1f}s" if r.get("secs") is not None else ("..." if r["state"] == "running" else "")
        lines.append(f"| {_STEP_ICONS[r['state']]} | {r['label']} | {r['detail']} | {t} |")
    return "\n".join(lines)


def _safe_rotation(value: Any) -> int:
    try:
        r = int(value) % 360
    except (TypeError, ValueError):
        return 0
    return r if r in (90, 180, 270) else 0


def _rotate_page(path: Path, clockwise_deg: int) -> Path:
    """Turn a page image upright (clockwise degrees) and save a copy next to the system temp dir."""
    out = Path(tempfile.gettempdir()) / f"upright_{clockwise_deg}_{path.name}"
    with Image.open(path) as im:
        im.rotate(-clockwise_deg, expand=True).save(out)
    return out


def _thumb(path: Path) -> Image.Image:
    with Image.open(path) as im:
        return im.convert("RGB").copy()


def process_document(
    file_obj,
    category_choice: str,
    doc_type_choice: str,
    subtype_choice: str,
    language_choice: str,
    engine_choice: str = "Follow routing table",
    settings: Optional[Settings] = None,
):
    """UI entry point: runs the pipeline and adds the live pipeline-steps panel to every update."""
    steps: List[Dict[str, Any]] = []
    for out in _process_core(file_obj, category_choice, doc_type_choice, subtype_choice,
                             language_choice, engine_choice, steps, settings):
        yield (*out, _render_steps(steps))


def _process_core(
    file_obj,
    category_choice: str,
    doc_type_choice: str,
    subtype_choice: str,
    language_choice: str,
    engine_choice: str,
    steps: List[Dict[str, Any]],
    settings: Optional[Settings] = None,
):
    """Reads every page of the document: classify once, route each page to the model for its category,
    then hand the page results to ModuleCPipeline for the canonical envelope (FR-CLS-005, FR-RTE-001,
    FR-ING-004).

    Yields (gallery_of_annotated_pages, status_text, json_output, classification_markdown, json_file_path).
    """
    if file_obj is None:
        yield None, "No file uploaded", "{}", "", None
        return

    file_path = file_obj.name if hasattr(file_obj, "name") else str(file_obj)
    claimed_name = Path(file_path).name
    choices = {"category": category_choice, "doc_type": doc_type_choice,
               "subtype": subtype_choice, "language": language_choice}
    split_gallery: List[Tuple[Image.Image, str]] = []

    try:
        settings = settings if settings is not None else get_settings()
        pipe_a, pipe_b = get_pipelines(settings)

        _step(steps, "upload", "Upload received", "done", claimed_name)
        _step(steps, "split", "Split into page images (200 DPI) and clean up", "running")
        yield None, "Splitting into page images...", "", "", None

        # 1. Ingestion & preprocessing (Module A): every page rendered at 200 DPI
        payload_a = pipe_a.process_document(file_path, claimed_filename=claimed_name)
        if payload_a.status == "rejected":
            err = json.dumps(payload_a.to_dict(), indent=2)
            yield None, f"Rejected: {payload_a.error}", err, "", None
            return

        # (page number, image the model reads, original 200-DPI image evidence boxes refer to)
        page_items: List[Tuple[int, Path, Optional[Path]]] = []
        for n, rec in enumerate(payload_a.pages, start=1):
            prep = pipe_a.storage.resolve_uri_to_path(rec.preprocessed_storage_uri)
            orig = pipe_a.storage.resolve_uri_to_path(rec.original_storage_uri)
            page_items.append((getattr(rec, "page_number", n), prep if prep.exists() else orig, orig))
        if not page_items:
            page_items = [(1, Path(file_path), None)]
        total_pages = len(page_items)
        if MAX_PAGES > 0:
            page_items = page_items[:MAX_PAGES]
        _step(steps, "split", state="done", detail=f"{total_pages} page image(s)" + (
            f", reading the first {len(page_items)}" if total_pages > len(page_items) else ""))
        split_gallery = [(_thumb(p), f"Page {n} (split)") for n, p, _ in page_items if p and Path(p).exists()]
        if split_gallery:
            pw, ph = split_gallery[0][0].size
            _step(steps, "split", detail=steps[1]["detail"] + f" · page 1 is {pw}×{ph} px ({pw * ph / 1e6:.1f} MP)")
        _step(steps, "cls", "Classify document from page 1 (router model, once)")
        for n, _, _ in page_items:
            _step(steps, f"ext{n}", f"Page {n}: extract fields")
        _step(steps, "merge", "Validate and build JSON")
        yield split_gallery or None, f"Split into {total_pages} page image(s).", "", "", None

        # 2. Module B hand-off: explicit page-image references (authoritative geometry) and the
        #    configured parser's classification hint (UNKNOWN / needs_review under the pass-through default)
        page_refs = {ref.page_number: ref for ref in pipe_b.load_page_images(payload_a)}
        payload_b = pipe_b.process_handoff(payload_a)
        if payload_b.status == "failed":
            err = payload_b.error or {}
            msg = f"Module B failed: {err.get('code', 'UNKNOWN')}: {err.get('message', 'no detail')}"
            yield split_gallery or None, msg, json.dumps({"error": err}, indent=2, default=str), "", None
            return
        module_b_type = payload_b.classification.document_type.value
        rule_type = module_b_type if module_b_type != "UNKNOWN" else None
        rule_confidence = payload_b.classification.classification_confidence if rule_type else None

        engine = ENGINE_CHOICES.get(engine_choice)
        routed = get_routed_extractor()

        # Classify ONCE from page 1 and apply to every page (mixed-category PDFs are rare)
        _step(steps, "cls", state="running", detail="detecting category, type and language")
        yield (split_gallery or None, "Classifying the document from page 1...", "", "", None)
        doc_info = _resolve_page(page_items[0][1], claimed_name, rule_type, rule_confidence, choices, 1)
        _step(steps, "cls", state="done",
              detail=f"{doc_info['category']} · {doc_info['document_type']} · {doc_info['language']} · {doc_info['category_source']}"
                     + (f" · filled by {doc_info['filled_by']}" if doc_info.get("filled_by") else "")
                     + (f" · {doc_info['router_model'].split('/')[-1]}" if doc_info.get("router_model") else ""))

        rotation = doc_info.get("rotation", 0)
        read_items = page_items
        if rotation:
            # Orientation fix: the router saw the page sideways/upside down, so the model reads an upright
            # copy. Boxes are mapped back to the stored page by ModuleCPipeline (RoutedPageResult.rotation).
            read_items = [(n, _rotate_page(p, rotation), o) for n, p, o in page_items]
            _step(steps, "cls", detail=steps[[r["key"] for r in steps].index("cls")]["detail"] + f" · rotated {rotation}°")

        category = DocumentCategory(doc_info["category"])
        schema_cls = SCHEMA_MAP.get(doc_info["schema_id"]) if doc_info["schema_id"] else None
        doc_type = doc_info["schema_id"]
        if doc_type and payload_b.classification.document_type.value != doc_type:
            # Explicit user choice: Module B's audited override path (FR-CLS-004) records who set the
            # type and why, and binds the approved schema Module C will read.
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

        pages_info: List[Dict[str, Any]] = []
        page_results: List[RoutedPageResult] = []
        routes: List[Dict[str, Any]] = []

        for page_no, img_path, _orig in read_items:
            info = dict(doc_info, page=page_no)
            pages_info.append(info)
            _step(steps, f"ext{page_no}", state="running", detail=f"routing to the {category.value} model")
            yield (split_gallery or None,
                   f"Page {page_no}/{len(read_items)}: classified as {category.value} ({info['language']}). Extracting...",
                   "", _classification_markdown(pages_info), None)
            try:
                if not (img_path and img_path.exists()):
                    raise FileNotFoundError(f"Page {page_no} image not found: {img_path}")
                # 3. Extraction with the model routed for this page's category (the only inference)
                raw_dict, prov, route = routed.extract(
                    str(img_path), category, info["language"], schema_cls, info["subtype"], engine=engine
                )
            except Exception as exc:  # one failing page must not discard the others (PARTIAL_PAGE_COVERAGE)
                page_results.append(RoutedPageResult(
                    page_number=page_no, rotation=rotation, error=f"{type(exc).__name__}: {exc}"))
                info["model_used"] = "-"
                _step(steps, f"ext{page_no}", state="failed", detail=str(exc)[:120])
                continue
            info["model_used"] = route["model_used"]
            info["fallback_occurred"] = route["fallback_occurred"]
            routes.append({"page": page_no, **route})
            page_results.append(RoutedPageResult(
                page_number=page_no, raw=raw_dict, provenance=prov, route=route, rotation=rotation))
            _step(steps, f"ext{page_no}", state="done",
                  detail=f"{route['model_used']}{' (fallback)' if route['fallback_occurred'] else ''} · {len(raw_dict)} fields")

        # 4. Canonical processing: normalization, evidence mapping, validation, gating, envelope
        _step(steps, "merge", state="running")
        refs = [page_refs[n] for n, _, _ in page_items if n in page_refs]
        env: CanonicalExtractionEnvelope = get_module_c().process_routed_pages(
            payload_b,
            page_results,
            refs,
            system_prompt=build_generic_prompt(category, doc_info["subtype"], doc_info["language"]),
            document_type=doc_type or "generic",
            schema_bound=schema_cls is not None,
            force_review=category == DocumentCategory.HANDWRITTEN,
        )
        if total_pages > len(page_items):
            env.flags.append(FlagItem(
                code="PAGES_NOT_PROCESSED",
                message=f"Read pages 1-{len(page_items)} of {total_pages} (demo cap DOC_MAX_PAGES={MAX_PAGES}).",
                fields=[],
            ))

        first = pages_info[0]
        specialists = sorted({p.get("model_used") for p in pages_info if p.get("model_used") not in (None, "-")})
        md = env.document_metadata
        md.document_category = first["category"]
        md.document_subtype = first["subtype"]
        md.language = first["language"]
        md.pages_processed = len(page_items)
        md.pages = [
            {k: p.get(k) for k in ("page", "category", "document_type", "subtype", "language", "model_used")}
            for p in pages_info
        ]
        env.run.routing = routes
        env.run.confidence_calibrated = False
        env.run.model_versions.update({
            k: v for k, v in {
                "router": first.get("router_model") or first.get("method"),
                "specialist": ", ".join(specialists),
                "normalizer": "deterministic-v1",
            }.items() if v
        })
        _step(steps, "merge", state="done", detail=f"{len(env.fields)} fields · {len(env.flags)} flag(s)")

        # Gallery: every page with the evidence located on it, drawn on the 200-DPI original (the frame
        # the evidence boxes are expressed in) and then turned upright like the page the model read.
        gallery: List[Tuple[Image.Image, str]] = []
        for page_no, img_path, orig_path in page_items:
            canvas_path = orig_path if orig_path and orig_path.exists() else img_path
            with Image.open(canvas_path) as pil_img:
                annotated = draw_bounding_boxes(pil_img, env.fields, page=page_no)
            if rotation:
                annotated = annotated.rotate(-rotation, expand=True)
            gallery.append((annotated, f"Page {page_no}: {category.value}"))

        json_text = json.dumps(env.model_dump(mode="json"), indent=2, default=str)
        withheld = any(not r.route.get("grounded", True) for r in page_results)
        status_msg = (
            f"Status: {env.document_metadata.status.upper()} | Pages read: {len(page_items)}/{total_pages} | "
            f"Fields: {len(env.fields)} | Flags: {len(env.flags)} | Model(s): {', '.join(specialists) or '-'}"
            + (" | No boxes on this route: values withheld (EVIDENCE_MISSING); unverified readings are in "
               "run.provenance.details" if withheld else "")
        )
        yield (gallery, status_msg, json_text, _classification_markdown(pages_info),
               _write_json_download(payload_a.document_id, json_text))
        return

    except Exception as e:
        for r in steps:
            if r["state"] == "running":
                _step(steps, r["key"], state="failed", detail=str(e)[:120])
        yield split_gallery or None, f"Error: {e}", json.dumps({"error": str(e)}, indent=2), "", None
        return


def create_demo(settings: Optional[Settings] = None) -> "gr.Blocks":
    """Constructs Gradio UI layout (FR-DEMO-001..006). ``settings`` configures every module it runs."""
    if settings is not None:
        configure_settings(settings)
    with gr.Blocks(title="Document Extraction Engine") as demo:
        gr.Markdown(
            "# 📄 Document Extraction Engine\n"
            "Upload a document. The engine first classifies it (structured, unstructured or handwritten), "
            "then routes it to the model for that category and returns grounded JSON."
        )

        with gr.Row():
            with gr.Column(scale=1):
                input_file = gr.File(label="Upload Document (PDF, JPG, PNG, TIFF)", file_types=[".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"])
                with gr.Accordion("Manual override (optional, leave on Auto-Detect to classify automatically)", open=False):
                    category_dd = gr.Dropdown(
                        choices=[AUTO, "Structured", "Unstructured", "Handwritten"], value=AUTO,
                        label="Document Category",
                    )
                    doc_type_dd = gr.Dropdown(
                        choices=[AUTO, "DOC-01", "DOC-02", "DOC-03"], value=AUTO,
                        label="Document Type (pick one to force its fixed schema; Auto = dynamic fields)",
                    )
                    subtype_dd = gr.Dropdown(
                        choices=[AUTO] + UNSTRUCTURED_SUBTYPES, value=AUTO,
                        label="Unstructured Sub-type (brochure, pamphlet, menu card, ...)",
                    )
                    language_dd = gr.Dropdown(
                        choices=[AUTO] + list(LANGUAGES), value=AUTO, label="Language",
                    )
                    engine_dd = gr.Dropdown(
                        choices=list(ENGINE_CHOICES), value="Follow routing table",
                        label="Engine (compare the default VLM with GLM-OCR + text LLM; the OCR route has no boxes)",
                    )
                submit_btn = gr.Button("Classify & Extract", variant="primary")

            with gr.Column(scale=2):
                status_box = gr.Textbox(label="Status & Quality Gate", interactive=False)
                gr.Markdown("### Pipeline steps")
                steps_md = gr.Markdown(_render_steps([]))
                classification_md = gr.Markdown(label="Classification Result")
                with gr.Tab("Visual Grounding"):
                    output_image = gr.Gallery(label="Annotated pages (grounded bounding boxes)", columns=1, height=700)
                with gr.Tab("Canonical JSON Output"):
                    output_json = gr.Code(label="Extracted JSON Envelope", language="json")
                    json_file = gr.File(label="Download JSON")

        submit_btn.click(
            fn=process_document,
            inputs=[input_file, category_dd, doc_type_dd, subtype_dd, language_dd, engine_dd],
            outputs=[output_image, status_box, output_json, classification_md, json_file, steps_md],
        )

    return demo


if __name__ == "__main__":
    if gr is None:
        print("Gradio is not installed. Run: pip install gradio")
    else:
        if os.environ.get("DOC_PRELOAD", "1") == "1":
            # Load the models before the UI starts so the first click is not slowed by model loading
            try:
                _r = get_routed_extractor()
                print("Preloading models (set DOC_PRELOAD=0 to skip)...")
                _r.get_extractor(_r.table.get("default_model", DEFAULT_MODEL))._load_model()
                _r.get_router()._load_model()
                print("Models loaded.")
            except Exception as _e:  # the app still starts; models load on first use
                print(f"Preload skipped: {_e}")
        demo = create_demo()
        # In Kaggle or remote server, share=True generates a public gradio.live URL
        share_mode = os.environ.get("GRADIO_SHARE", "True").lower() == "true"
        port = int(os.environ.get("GRADIO_SERVER_PORT", 0)) or None
        demo.launch(share=share_mode, server_port=port)

