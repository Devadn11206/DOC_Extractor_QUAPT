"""Routing table, category rules, fallback and OCR adapter wiring (FR-RTE-001..006)."""
import pytest

from doc_extractor.contracts_c import ExecutionMode, ExecutionProvenance
from doc_extractor.routing import (
    DEFAULT_ROUTING_TABLE,
    DocumentCategory,
    RoutedExtractor,
    heuristic_classify,
    select_model,
    select_route,
)

PROV = ExecutionProvenance(mode=ExecutionMode.GENUINE_LLM, model_name="m", provider="p", fallback_occurred=False)


class FakeAdapter:
    kind = "fake"

    def __init__(self, label, fail=False, grounded=True):
        self.model_label, self.fail, self.grounded = label, fail, grounded

    def extract(self, image_path, prompt):
        if self.fail:
            raise RuntimeError("boom")
        return {"a": {"value": "x", "bbox": [0, 0, 1, 1]}}, ExecutionProvenance(**PROV.model_dump()), {"grounded": self.grounded}


def test_default_routes_per_category():
    # all categories run on the grounded Qwen3-VL default until GLM-OCR returns boxes
    for cat in DocumentCategory:
        assert select_route(cat, "en") == DEFAULT_ROUTING_TABLE["default_model"]
    assert DEFAULT_ROUTING_TABLE["alt_engines"]["ocr_text"]["adapter"] == "ocr_text"


def test_language_row_overrides_default():
    table = {"routes": {"handwritten": {"default": "A", "hi": "B"}}, "default_model": "D"}
    assert select_route(DocumentCategory.HANDWRITTEN, "hi", table) == "B"
    assert select_route(DocumentCategory.HANDWRITTEN, "en", table) == "A"
    assert select_model(DocumentCategory.HANDWRITTEN, "en", table) == "A"


def test_unknown_category_falls_back_to_default_model():
    assert select_route(DocumentCategory.UNSTRUCTURED, "en", {"routes": {}, "default_model": "D"}) == "D"


def test_heuristic_classify_filename_hints():
    assert heuristic_classify("rx_patient.png")["category"] == "handwritten"
    assert heuristic_classify("brochure.pdf")["category"] == "unstructured"
    assert heuristic_classify("casa_form.pdf")["document_type"] == "DOC-01"


def test_filled_by_hand_forces_handwritten():
    r = RoutedExtractor(table={"default_model": "D", "routes": {}})

    class Router:
        def classify_page_image(self, _):
            return {"category": "structured", "filled_by": "hand", "language": "en"}

    r.get_router = lambda: Router()
    out = r.classify("x.png", "f.png")
    assert out["category"] == "handwritten" and out["category_before_rule"] == "structured"


def test_classify_falls_back_to_heuristic_on_failure():
    r = RoutedExtractor(table={"default_model": "D", "routes": {}})
    r.get_router = lambda: (_ for _ in ()).throw(RuntimeError("no model"))
    out = r.classify("x.png", "prescription.png")
    assert out["category"] == "handwritten" and "fallback_reason" in out


def test_routed_model_failure_falls_back_to_default_and_records_it():
    table = {"default_model": "D", "routes": {"handwritten": {"default": "ROUTED"}}}
    r = RoutedExtractor(table=table)
    r.get_extractor = lambda name, device=None: type("E", (), {"_build_system_prompt": lambda s, c: "p"})()
    r.get_adapter = lambda route: FakeAdapter(route, fail=(route == "ROUTED"))
    raw, prov, info = r.extract("x.png", DocumentCategory.HANDWRITTEN)
    assert info["fallback_occurred"] and info["model_used"] == "D"
    assert prov.fallback_occurred and "ROUTED" in prov.fallback_reason


def test_both_routes_failing_raises():
    r = RoutedExtractor(table={"default_model": "D", "routes": {"handwritten": {"default": "R"}}})
    r.get_extractor = lambda name, device=None: object()
    r.get_adapter = lambda route: FakeAdapter(route, fail=True)
    with pytest.raises(RuntimeError):
        r.extract("x.png", DocumentCategory.HANDWRITTEN)


def test_ocr_adapter_reports_ungrounded_and_both_models(monkeypatch):
    from doc_extractor.adapters import OCRTextAdapter

    a = OCRTextAdapter()
    monkeypatch.setattr(a, "read_text", lambda p: "NAME: RUPESH")
    monkeypatch.setattr(a, "text_to_fields", lambda t, s: {"name": {"value": "RUPESH", "bbox": None}})
    raw, prov, meta = a.extract("x.png", "prompt")
    assert meta["grounded"] is False
    assert meta["models"]["ocr"] == "zai-org/GLM-OCR"
    assert meta["models"]["text_llm"] == "Qwen/Qwen3-4B-Instruct-2507"
