"""
Phase 1 / Task 5: confidence integrity, unknown-document routing and review gating.

Covers:
- No synthetic confidence: scores come only from measured signals; otherwise None (unavailable)
  with explicit provenance; unavailable/uncalibrated scores cannot authorize auto-approval.
- OCR confidence and its provenance are preserved end to end.
- UNKNOWN never silently becomes DOC-01; only Module B's thresholded classification or an explicit
  supported override binds a schema.
- The canonical envelope yields needs_review for every blocking condition and keeps existing
  behaviour for valid, fully grounded documents.
"""

import io
import json
from pathlib import Path
import re
import sys
import types
from typing import Any, Dict, List
from unittest.mock import MagicMock

import numpy as np
import pytest

from doc_extractor import app
from doc_extractor.contracts_b import (
    BoundingBox,
    CheckboxMark,
    ConfidenceProvenance,
    LayoutBlock,
    LayoutElementType,
    ModuleAPageRef,
    MVPDocumentType,
    PageLayoutOutput,
)
from doc_extractor.contracts_c import EvidenceSnippet, ExtractedField, FlagItem
from doc_extractor.extractor import SchemaGuidedExtractor, find_evidence_for_value
from doc_extractor.field_assembly import (
    DEFAULT_FIELD_CONFIDENCE_THRESHOLD,
    assemble_canonical_envelope,
    assemble_field_entry,
)
from doc_extractor.loader_b import LoadedPageImage
from doc_extractor.parsing.adapter import LocalOCRParserAdapter
from doc_extractor.parsing.base import RawOCRToken
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.routing import RoutedExtractor, heuristic_classify
from doc_extractor.validation_rules import calculate_calibrated_confidence, calibrate_field_confidence
from schema_lifecycle.testing.contracts import FieldPrediction, GroundTruthField
from schema_lifecycle.testing.metrics import compute_field_metrics

from tests.test_canonical_module_c_orchestration import (  # noqa: F401  (fixtures + helpers)
    CASA_VLM_OUTPUT,
    TENANT,
    VLM_ONLY_TABLE,
    FakeVLM,
    _casa_ocr_tokens,
    _isolate_environment,
    _module_a,
    _png_bytes,
    _run_app,
    _tiff_bytes,
    _vlm_pipeline,
    _write_png,
    casa_run,
    temp_storage,
)

SCH_012_STATUSES = {"processing", "auto_approved", "needs_review", "reviewed", "rejected", "failed"}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _block(text: str, conf, y: int = 50) -> LayoutBlock:
    bb = BoundingBox(x0=10, y0=y, x1=300, y1=y + 20)
    return LayoutBlock(
        block_id=f"b{y}", element_type=LayoutElementType.PARAGRAPH, text=text,
        ocr_confidence=conf, bbox_preprocessed=bb, bbox_original=bb, reading_order_index=0,
    )


def _page(*blocks: LayoutBlock, checkboxes=None) -> PageLayoutOutput:
    return PageLayoutOutput(
        page_number=1, markdown_content="\n".join(b.text for b in blocks),
        blocks=list(blocks), checkboxes=checkboxes or [],
    )


def _good_field(value: Any = "X") -> ExtractedField:
    return ExtractedField(
        value=value, confidence=0.97, confidence_provenance=ConfidenceProvenance.OCR,
        review_required=False, validation="passed",
        evidence=EvidenceSnippet(page=1, bbox=(0.0, 0.0, 10.0, 10.0), text=str(value)),
    )


def _envelope(fields, flags=(), document_type="DOC-01", payload_status="processing"):
    return assemble_canonical_envelope(
        document_id="doc", document_type=document_type, schema_id=document_type,
        schema_version="1.0.0", classification_confidence=0.9, page_count=1,
        fields=fields, flags=list(flags), run_id="run", parser_model_name="parser",
        extractor_model_name="extractor", payload_status=payload_status,
    )


def _tokens_unrelated(img) -> List[RawOCRToken]:
    return [RawOCRToken(text="Lorem ipsum dolor sit amet", bbox_preprocessed=(50, 50, 400, 100), confidence=0.9)]


def _module_b_with(storage, token_fn) -> ModuleBPipeline:
    return ModuleBPipeline(
        storage=storage, parser=LocalOCRParserAdapter(raw_extractor=token_fn), tenant_id=TENANT
    )


def _text_extractor(output: Dict[str, Any]) -> SchemaGuidedExtractor:
    """Text/Ollama-style extractor returning a fixed payload (grounded via Module B OCR)."""
    return SchemaGuidedExtractor(
        model_name="ollama/qwen2.5:7b-instruct", use_llm=True, allow_fallback=False,
        llm_caller=lambda payload, schema_cls: (output, "req-test"),
    )


def _ocr_grounded_payload(storage, extra: List[RawOCRToken]):
    handoff = _module_a(storage).process_document(io.BytesIO(_png_bytes()), "casa.png")
    pipe_b = _module_b_with(storage, lambda img: _casa_ocr_tokens(img) + extra)
    payload = pipe_b.process_handoff(handoff)
    assert payload.classification.document_type == MVPDocumentType.DOC_01_CASA
    return payload


NAME_TOKEN = RawOCRToken(text="Applicant Name: RUPESH KUMAR", bbox_preprocessed=(50, 310, 400, 350), confidence=0.97)
PAN_TOKEN = RawOCRToken(text="PAN: ABCDE1234F", bbox_preprocessed=(50, 360, 300, 400), confidence=0.96)


# ============================================================================
# 1. Confidence integrity
# ============================================================================

def test_calibration_never_invents_a_score():
    assert calibrate_field_confidence() == (None, ConfidenceProvenance.UNAVAILABLE)
    assert calibrate_field_confidence(ocr_confidence=0.9, has_evidence=False) == (None, ConfidenceProvenance.UNAVAILABLE)
    assert calculate_calibrated_confidence(validation_passed=False) is None  # penalty cannot create a score


def test_calibration_uses_only_measured_signals_and_names_the_source():
    assert calibrate_field_confidence(ocr_confidence=0.9) == (0.9, ConfidenceProvenance.OCR)
    assert calibrate_field_confidence(model_confidence=0.8) == (0.8, ConfidenceProvenance.MODEL)
    assert calibrate_field_confidence(ocr_confidence=0.9, model_confidence=0.8) == (0.72, ConfidenceProvenance.MODEL)
    assert calibrate_field_confidence(ocr_confidence=0.9, validation_passed=False)[0] == 0.45
    assert calibrate_field_confidence(ocr_confidence=0.9, is_handwritten=True)[0] == 0.77


def test_default_model_confidence_is_no_longer_one():
    import inspect

    assert inspect.signature(calibrate_field_confidence).parameters["model_confidence"].default is None
    assert inspect.signature(calculate_calibrated_confidence).parameters["model_confidence"].default is None
    assert inspect.signature(assemble_field_entry).parameters["model_confidence"].default is None


def test_ocr_confidence_and_provenance_are_preserved():
    page = _page(_block("Applicant Name: RUPESH KUMAR", conf=0.97))
    field = assemble_field_entry("applicant.full_name", "RUPESH KUMAR", [page], [], required=True)

    assert field.confidence == 0.97  # exactly the measured OCR value, not 1.0, not rescaled
    assert field.confidence_provenance == ConfidenceProvenance.OCR
    assert field.review_required is False
    assert field.evidence is not None


@pytest.mark.parametrize("ocr,expect_review", [(0.97, False), (0.80, False), (0.60, True)])
def test_confidence_tracks_the_evidence_not_a_constant(ocr, expect_review):
    page = _page(_block("City: BENGALURU", conf=ocr))
    field = assemble_field_entry("address.city", "BENGALURU", [page], [])
    assert field.confidence == ocr
    assert field.review_required is expect_review


def test_unavailable_ocr_confidence_yields_none_and_review():
    page = _page(_block("City: BENGALURU", conf=None))  # e.g. native PDF text: no optical score
    field = assemble_field_entry("address.city", "BENGALURU", [page], [])

    assert field.value == "BENGALURU" and field.evidence is not None
    assert field.confidence is None
    assert field.confidence_provenance == ConfidenceProvenance.UNAVAILABLE
    assert field.review_required is True


def test_model_grounded_hint_without_confidence_is_unavailable_not_authorizing():
    hint = {"page": 1, "bbox": (1.0, 2.0, 30.0, 40.0), "text": "RUPESH", "ocr_confidence": None}
    field = assemble_field_entry("applicant.full_name", "RUPESH", [], [], evidence_hint=hint)
    assert field.confidence is None
    assert field.confidence_provenance == ConfidenceProvenance.UNAVAILABLE
    assert field.review_required is True


def test_trustworthy_model_confidence_is_honoured_when_supplied():
    hint = {"page": 1, "bbox": (1.0, 2.0, 30.0, 40.0), "text": "RUPESH", "ocr_confidence": None}
    field = assemble_field_entry(
        "applicant.full_name", "RUPESH", [], [], evidence_hint=hint, model_confidence=0.9
    )
    assert field.confidence == 0.9
    assert field.confidence_provenance == ConfidenceProvenance.MODEL
    assert field.review_required is False


def test_null_values_have_no_confidence():
    field = assemble_field_entry("applicant.pan_card_number", None, [_page()], [])
    assert field.confidence is None
    assert field.confidence_provenance == ConfidenceProvenance.UNAVAILABLE


def test_existing_threshold_default_and_per_field_override_are_reused():
    assert DEFAULT_FIELD_CONFIDENCE_THRESHOLD == 0.75
    page = _page(_block("City: BENGALURU", conf=0.80))
    assert assemble_field_entry("c", "BENGALURU", [page], []).review_required is False
    strict = assemble_field_entry("c", "BENGALURU", [page], [], confidence_threshold=0.90)
    assert strict.review_required is True


def test_uncertain_checkbox_keeps_real_confidence_and_is_flagged_not_rescored():
    bb = BoundingBox(x0=10, y0=200, x1=30, y1=220)
    cb = CheckboxMark(mark_id="cb_cheque_book", state="[x]", ocr_confidence=0.91,
                      bbox_preprocessed=bb, bbox_original=bb, is_uncertain=True)
    page = _page(checkboxes=[cb])

    evidence = find_evidence_for_value(True, [page], field_name="account_details.opt_in_services.cheque_book")
    assert evidence["ocr_confidence"] == 0.91  # was replaced by an invented 0.5 before
    assert evidence["uncertain"] is True

    field = assemble_field_entry(
        "account_details.opt_in_services.cheque_book", True, [page], [], strict_boolean_evidence=False
    )
    assert field.confidence == 0.91
    assert field.review_required is True  # uncertain extraction state


def test_extracted_field_contract_keeps_score_and_provenance_consistent():
    assert ExtractedField(value="x", confidence=0.9).confidence_provenance == ConfidenceProvenance.UNCALIBRATED
    assert ExtractedField(value=None).confidence is None
    assert ExtractedField(value=None).confidence_provenance == ConfidenceProvenance.UNAVAILABLE
    with pytest.raises(ValueError):
        ExtractedField(value="x", confidence=None, confidence_provenance=ConfidenceProvenance.OCR)
    with pytest.raises(ValueError):
        ExtractedField(value="x", confidence=0.9, confidence_provenance=ConfidenceProvenance.UNAVAILABLE)


def test_no_synthetic_confidence_literals_remain_in_pipeline_sources():
    src_root = Path(app.__file__).parent
    scanned = [
        "field_assembly.py", "validation_rules.py", "pipeline_c.py", "extractor.py",
        "vlm_grounding.py", "app.py", "parsing/qwen_vl_adapter.py", "parsing/tables.py",
        "routed_pages.py", "adapters.py",
    ]
    assignment = re.compile(
        r"""(?<![\w])(?:ocr_|model_|mean_ocr_|calibrated_)?confidence\s*=\s*(?:1\.0|1|0\.\d+)\b"""
        r"""|["']\w*confidence["']\s*:\s*(?:1\.0|1|0\.\d+)\b"""
        r"""|\boptical_conf\s*=\s*(?:1\.0|0\.\d+)\b"""
    )
    offenders = []
    for name in scanned:
        for lineno, line in enumerate((src_root / name).read_text(encoding="utf-8").splitlines(), 1):
            if assignment.search(line) and "confidence_threshold" not in line:
                offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert offenders == []


def _stub_torch_and_transformers(monkeypatch):
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.dtype = object
    torch.bfloat16 = object()
    transformers = types.ModuleType("transformers")
    transformers.AutoProcessor = MagicMock()
    transformers.Qwen2_5_VLForConditionalGeneration = MagicMock()
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.delitem(sys.modules, "doc_extractor.parsing.qwen_vl_adapter", raising=False)


def test_qwen_vl_parser_adapter_reports_no_invented_ocr_confidence(monkeypatch):
    _stub_torch_and_transformers(monkeypatch)
    from doc_extractor.parsing.qwen_vl_adapter import QwenVLParserAdapter

    page_ref = ModuleAPageRef(
        page_number=1, original_artifact_id="o", preprocessed_artifact_id="p",
        original_storage_uri="t/o.png", preprocessed_storage_uri="t/p.png",
        original_width_px=1000, original_height_px=1400,
        preprocessed_width_px=1000, preprocessed_height_px=1400,
        dpi=200, applied_rotation_deg=0, applied_deskew_deg=0.0, metadata_storage_uri="t/m.json",
    )
    loaded = LoadedPageImage(
        page_number=1, image_bgr=np.full((1400, 1000, 3), 255, dtype=np.uint8),
        width_px=1000, height_px=1400, dpi=200, file_path=Path("p.png"), page_ref=page_ref,
    )
    adapter = object.__new__(QwenVLParserAdapter)
    layout = adapter._build_page_layout("[100, 100, 150, 400] BANK NAME: ACME\n[200, 100, 250, 300] [x] Cheque book", loaded)

    assert layout.blocks and all(b.ocr_confidence is None for b in layout.blocks)
    assert layout.mean_ocr_confidence is None
    assert layout.checkboxes and all(c.ocr_confidence is None for c in layout.checkboxes)
    monkeypatch.delitem(sys.modules, "doc_extractor.parsing.qwen_vl_adapter", raising=False)


def test_benchmark_metrics_tolerate_unavailable_confidence_without_inventing_one():
    gt = [GroundTruthField(field_path="applicant.pan", expected_value="ABCDE1234F")]
    unscored = [FieldPrediction(field_path="applicant.pan", predicted_value="ABCDE1234F", confidence=None)]
    scored = [FieldPrediction(field_path="applicant.pan", predicted_value="ABCDE1234F", confidence=0.9)]

    assert compute_field_metrics("applicant.pan", gt, unscored).true_positives == 1
    assert compute_field_metrics("applicant.pan", gt, scored).true_positives == 1


# ============================================================================
# 2. Unknown document routing
# ============================================================================

def test_filename_never_binds_a_schema_or_invents_confidence():
    for name in ("bank_account_form.pdf", "insurance_proposal.pdf", "casa_kyc.png", "scan0001.png"):
        result = heuristic_classify(name)
        assert result["document_type"] is None
        assert result["confidence"] is None


def test_module_b_type_takes_precedence_and_keeps_its_real_confidence():
    result = heuristic_classify("random_name.pdf", "DOC-02", 0.91)
    assert result["document_type"] == "DOC-02" and result["confidence"] == 0.91
    assert heuristic_classify("account_form.pdf", "DOC-03", 0.9)["document_type"] == "DOC-03"


def test_module_c_never_runs_a_schema_extractor_on_unknown(temp_storage):
    handoff = _module_a(temp_storage).process_document(io.BytesIO(_png_bytes()), "x.png")
    payload = _module_b_with(temp_storage, _tokens_unrelated).process_handoff(handoff)
    assert payload.classification.document_type == MVPDocumentType.UNKNOWN

    extractor = MagicMock()
    extractor.model_name = "test-model"
    extractor.prompt_version = "1.0.0"
    env = ModuleCPipeline(extractor=extractor).process(payload)

    extractor.extract_with_provenance.assert_not_called()
    assert env.document_metadata.status == "needs_review"
    assert env.document_metadata.document_type == "UNKNOWN"
    assert env.fields == {}
    assert [f.code for f in env.flags] == ["CLASSIFICATION_UNCERTAIN"]


def _wire(monkeypatch, storage, token_fn):
    monkeypatch.setattr(
        app, "get_pipelines", lambda settings=None: (_module_a(storage), _module_b_with(storage, token_fn))
    )
    app._ROUTED = RoutedExtractor(table=VLM_ONLY_TABLE, allow_fallback=False)


def _named_png(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(_png_bytes())
    return str(path)


def test_unidentified_document_never_becomes_doc01_in_the_app(temp_storage, tmp_path, monkeypatch):
    fake = FakeVLM().install(monkeypatch)
    _wire(monkeypatch, temp_storage, _tokens_unrelated)
    # A filename full of CASA keywords must not bind DOC-01 either (v1.5 default: dynamic fields, no fixed schema).
    (_, status, json_text, md, _, _), _ = _run_app(
        _named_png(tmp_path, "bank_account_form.png"), "Structured", app.AUTO, app.AUTO, "English"
    )
    envelope = json.loads(json_text)

    assert envelope["document_metadata"]["document_type"] == "generic"        # never DOC-01
    assert envelope["document_metadata"]["schema_id"] == "generic"
    assert envelope["document_metadata"]["status"] == "needs_review"
    # No schema field list reached a model: the extraction prompt is the generic, label-driven one.
    assert not any("applicant.full_name" in c["system_prompt"] for c in fake.calls)
    assert status.startswith("Status: NEEDS_REVIEW")


def test_vlm_suggested_type_is_advisory_and_does_not_bind(temp_storage, tmp_path, monkeypatch):
    prompts: List[str] = []

    def generate(self_extractor, image_input, system_prompt, user_text, max_new_tokens, max_pixels=None):
        prompts.append(system_prompt)
        if "You classify document images" in system_prompt:
            return json.dumps({"category": "structured", "document_type": "DOC-01", "subtype": None,
                               "language": "en", "confidence": 0.99})
        return json.dumps({"title": {"value": "Some Form", "bbox": [100, 100, 500, 150]}})

    monkeypatch.setattr(SchemaGuidedExtractor, "_generate_from_image", generate)
    _wire(monkeypatch, temp_storage, _tokens_unrelated)

    (_, _, json_text, md, _, _), _ = _run_app(
        _named_png(tmp_path, "scan.png"), app.AUTO, app.AUTO, app.AUTO, app.AUTO
    )
    envelope = json.loads(json_text)

    assert envelope["document_metadata"]["document_type"] == "generic"        # the suggestion did not bind
    assert "(suggested DOC-01)" in md                                           # surfaced to the user, not applied
    assert not any("applicant.full_name" in p for p in prompts)                # no DOC-01 schema prompt ran


def test_explicit_supported_override_processes_an_unknown_document(temp_storage, tmp_path, monkeypatch):
    fake = FakeVLM().install(monkeypatch)
    _wire(monkeypatch, temp_storage, _tokens_unrelated)
    overrides: List[Any] = []
    original = ModuleBPipeline.apply_reviewer_override

    def spy(self, payload, request, *a, **k):
        overrides.append(request)
        return original(self, payload, request, *a, **k)

    monkeypatch.setattr(ModuleBPipeline, "apply_reviewer_override", spy)

    (_, _, json_text, _, _, _), _ = _run_app(
        _named_png(tmp_path, "scan.png"), "Structured", "DOC-01", app.AUTO, "English"
    )
    envelope = json.loads(json_text)

    assert len(overrides) == 1
    assert overrides[0].new_document_type == MVPDocumentType.DOC_01_CASA
    assert overrides[0].reviewer_id == app.OVERRIDE_REVIEWER_ID and overrides[0].override_reason
    assert envelope["document_metadata"]["document_type"] == "DOC-01"
    assert envelope["fields"]["applicant.full_name"]["value"] == "RUPESH KUMAR"
    assert len(fake.calls) == 1


def test_unsupported_override_is_not_silently_replaced_by_doc01(temp_storage, tmp_path, monkeypatch):
    FakeVLM().install(monkeypatch)
    _wire(monkeypatch, temp_storage, _tokens_unrelated)
    (_, _, json_text, _, _, _), _ = _run_app(
        _named_png(tmp_path, "scan.png"), "Structured", "DOC-07", app.AUTO, "English"
    )
    assert json.loads(json_text)["document_metadata"]["document_type"] == "generic"


def test_module_b_identified_type_is_a_hint_not_a_schema_binding(temp_storage, tmp_path, monkeypatch):
    FakeVLM().install(monkeypatch)
    _wire(monkeypatch, temp_storage, _casa_ocr_tokens)
    overrides: List[Any] = []
    monkeypatch.setattr(ModuleBPipeline, "apply_reviewer_override", lambda *a, **k: overrides.append(1))

    (_, _, json_text, md, _, _), _ = _run_app(
        _named_png(tmp_path, "whatever.png"), "Structured", app.AUTO, app.AUTO, "English"
    )
    assert overrides == []                                              # nothing was overridden
    assert json.loads(json_text)["document_metadata"]["document_type"] == "generic"   # v1.5: schema only by user pick
    assert "(suggested DOC-01)" in md                                    # Module B's content-based type, as a hint


def _doc02_tokens(img):
    return [
        RawOCRToken(text="LIFE INSURANCE PROPOSAL FORM FOR INDIVIDUALS", bbox_preprocessed=(50, 50, 500, 100), confidence=0.98),
        RawOCRToken(text="Sum Insured INR 5000000", bbox_preprocessed=(50, 110, 350, 150), confidence=0.97),
        RawOCRToken(text="Policy Term 20 Years", bbox_preprocessed=(50, 160, 300, 200), confidence=0.96),
        RawOCRToken(text="Medical History Questionnaire", bbox_preprocessed=(50, 210, 400, 250), confidence=0.95),
        RawOCRToken(text="Nominee Name and Share", bbox_preprocessed=(50, 260, 350, 300), confidence=0.94),
    ]


def _doc03_tokens(img):
    return [
        RawOCRToken(text="HOSPITAL DISCHARGE SUMMARY", bbox_preprocessed=(50, 50, 450, 100), confidence=0.98),
        RawOCRToken(text="Date of Admission: 10-Oct-2026", bbox_preprocessed=(50, 110, 400, 150), confidence=0.97),
        RawOCRToken(text="Date of Discharge: 15-Oct-2026", bbox_preprocessed=(50, 160, 400, 200), confidence=0.96),
        RawOCRToken(text="Final Diagnosis: Acute Bronchitis", bbox_preprocessed=(50, 210, 450, 250), confidence=0.95),
    ]


@pytest.mark.parametrize("tokens,expected", [
    (_casa_ocr_tokens, "DOC-01"),
    (_doc02_tokens, "DOC-02"),
    (_doc03_tokens, "DOC-03"),
])
def test_supported_document_routing_is_preserved(temp_storage, tmp_path, monkeypatch, tokens, expected):
    FakeVLM().install(monkeypatch)
    _wire(monkeypatch, temp_storage, tokens)
    handoff = _module_a(temp_storage).process_document(io.BytesIO(_png_bytes()), "doc.png")
    assert _module_b_with(temp_storage, tokens).process_handoff(handoff).classification.document_type.value == expected
    (_, _, _, md, _, _), _ = _run_app(
        _named_png(tmp_path, "doc.png"), "Structured", app.AUTO, app.AUTO, "English"
    )
    assert f"(suggested {expected})" in md


# ============================================================================
# 3. Review gating in the canonical envelope
# ============================================================================

def test_baseline_fully_grounded_document_is_auto_approved():
    env = _envelope({"a": _good_field("A"), "b": _good_field("B")})
    assert env.document_metadata.status == "auto_approved"
    assert env.document_metadata.status in SCH_012_STATUSES


@pytest.mark.parametrize("label,kwargs", [
    ("classification UNKNOWN", dict(document_type="UNKNOWN")),
    ("upstream payload needs review", dict(payload_status="needs_review")),
    ("EVIDENCE_MISSING flag", dict(flags=[FlagItem(code="EVIDENCE_MISSING", fields=["a"])])),
    ("PARTIAL_PAGE_COVERAGE flag", dict(flags=[FlagItem(code="PARTIAL_PAGE_COVERAGE")])),
    ("business rule flag", dict(flags=[FlagItem(code="ARITHMETIC_MISMATCH")])),
])
def test_envelope_requires_review_for_blocking_conditions(label, kwargs):
    env = _envelope({"a": _good_field("A")}, **kwargs)
    assert env.document_metadata.status == "needs_review", label


@pytest.mark.parametrize("label,field", [
    ("required value missing", ExtractedField(value=None, review_required=True, validation="failed")),
    ("business validation failed", ExtractedField(
        value="X", confidence=0.97, confidence_provenance=ConfidenceProvenance.OCR, validation="failed")),
    ("uncertain extraction state", ExtractedField(
        value="X", confidence=0.97, confidence_provenance=ConfidenceProvenance.OCR, review_required=True)),
    ("confidence unavailable", ExtractedField(value="X", confidence=None, validation="passed")),
    ("confidence merely uncalibrated", ExtractedField(value="X", confidence=0.99, validation="passed")),
])
def test_envelope_requires_review_for_blocking_field_states(label, field):
    env = _envelope({"a": _good_field("A"), "b": field})
    assert env.document_metadata.status == "needs_review", label


def test_failed_payload_stays_failed():
    assert _envelope({"a": _good_field()}, payload_status="failed").document_metadata.status == "failed"


# ---- end to end through ModuleCPipeline (OCR-grounded, existing behaviour preserved) -------------

def test_valid_fully_grounded_document_is_still_auto_approved(temp_storage):
    payload = _ocr_grounded_payload(temp_storage, [NAME_TOKEN, PAN_TOKEN])
    extractor = _text_extractor({"applicant": {"full_name": "RUPESH KUMAR", "pan_card_number": "ABCDE1234F"}})

    env = ModuleCPipeline(extractor=extractor).process(payload)

    assert env.document_metadata.status == "auto_approved"
    assert env.flags == []
    name = env.fields["applicant.full_name"]
    assert name.confidence == 0.97 and name.confidence_provenance == ConfidenceProvenance.OCR
    for field in env.fields.values():
        if field.value is not None:
            assert field.confidence is not None
            assert field.confidence_provenance == ConfidenceProvenance.OCR


def test_ungrounded_value_raises_evidence_missing_and_blocks_approval(temp_storage):
    payload = _ocr_grounded_payload(temp_storage, [NAME_TOKEN])
    extractor = _text_extractor({
        "applicant": {"full_name": "RUPESH KUMAR"},
        "account_details": {"account_type": "Savings"},  # appears nowhere on the page
    })
    env = ModuleCPipeline(extractor=extractor).process(payload)

    assert any(f.code == "EVIDENCE_MISSING" and "account_details.account_type" in f.fields for f in env.flags)
    assert env.fields["account_details.account_type"].value is None
    assert env.document_metadata.status == "needs_review"


def test_failed_business_validation_forces_review(temp_storage):
    bad_pan = RawOCRToken(text="PAN: NOT-A-PAN", bbox_preprocessed=(50, 360, 300, 400), confidence=0.96)
    payload = _ocr_grounded_payload(temp_storage, [NAME_TOKEN, bad_pan])
    extractor = _text_extractor({"applicant": {"full_name": "RUPESH KUMAR", "pan_card_number": "NOT-A-PAN"}})

    env = ModuleCPipeline(extractor=extractor).process(payload)

    assert env.fields["applicant.pan_card_number"].validation == "failed"
    assert env.document_metadata.status == "needs_review"


def test_missing_required_field_forces_review(temp_storage):
    payload = _ocr_grounded_payload(temp_storage, [PAN_TOKEN])
    env = ModuleCPipeline(extractor=_text_extractor({"applicant": {"pan_card_number": "ABCDE1234F"}})).process(payload)
    assert env.fields["applicant.full_name"].value is None
    assert env.document_metadata.status == "needs_review"


def test_vlm_read_document_cannot_auto_approve_without_a_confidence_signal(casa_run, monkeypatch):
    FakeVLM().install(monkeypatch)
    env = _vlm_pipeline().process(casa_run["payload"], page_images=casa_run["page_images"])

    populated = [f for f in env.fields.values() if f.value is not None]
    assert populated
    assert all(f.confidence is None for f in populated)
    assert all(f.confidence_provenance == ConfidenceProvenance.UNAVAILABLE for f in populated)
    assert all(f.review_required for f in populated)
    assert env.document_metadata.status == "needs_review"
    assert env.fields["account_details.currency"].confidence is None  # derived field claims no more


def test_incomplete_page_coverage_forces_review_end_to_end(temp_storage, monkeypatch):
    handoff = _module_a(temp_storage).process_document(io.BytesIO(_tiff_bytes(2)), "casa.tiff")
    pipe_b = _module_b_with(temp_storage, _casa_ocr_tokens)
    payload, refs = pipe_b.process_handoff(handoff), pipe_b.load_page_images(handoff)
    failing_page_image = str(refs[1].image_path)

    def generate(self_extractor, image_input, system_prompt, user_text, max_new_tokens):
        if str(image_input) == failing_page_image:
            raise RuntimeError("CUDA out of memory")
        return json.dumps(CASA_VLM_OUTPUT)

    monkeypatch.setattr(SchemaGuidedExtractor, "_generate_from_image", generate)

    env = _vlm_pipeline().process(payload, page_images=refs)

    assert any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)
    assert env.document_metadata.status == "needs_review"


def test_currency_is_not_invented_without_an_amount(casa_run):
    env = ModuleCPipeline(extractor=SchemaGuidedExtractor(use_llm=False)).process(casa_run["payload"])
    currency = env.fields["account_details.currency"]
    if env.fields["account_details.initial_deposit_amount"].value is None:
        assert currency.value is None and currency.confidence is None
    else:
        amount = env.fields["account_details.initial_deposit_amount"]
        assert currency.confidence == amount.confidence
        assert currency.confidence_provenance == amount.confidence_provenance
