"""
Phase 1 / Task 8A (D12): permanent DOC-03 (Hospital Discharge Summary, healthcare) integration test.

Converts the audit's DOC-03 acceptance probe into automated coverage. Real Module A artifacts
(a 3-page TIFF) flow through Module B classification, the real multimodal extractor, multipage
aggregation, CoordinateTransform, normalisation, field assembly and the canonical envelope. Only the
lowest-level model call is faked (page-aware), so no GPU/model is required.

Verifies: field extraction across pages, per-page evidence (page number + transformed box), date
normalisation/validation, repeating-group medications, chronology validation (FR-VAL-004),
required-field handling, the conservative review policy, and the OCR-grounded text path.
"""

import pytest

from doc_extractor.contracts_b import ConfidenceProvenance, MVPDocumentType
from doc_extractor.pipeline_c import ModuleCPipeline

from tests.test_canonical_module_c_orchestration import (  # noqa: F401  (fixtures + helpers)
    _expected_original_bbox,
    _isolate_environment,
    temp_storage,
)
from tests.test_confidence_integrity_and_review_gating import (  # noqa: F401
    _doc03_tokens,
    _text_extractor,
)
from tests.test_multipage_extraction import PageAwareVLM, _run, _vlm_pipeline

DOC03 = MVPDocumentType.DOC_03_DISCHARGE_SUMMARY

PAGES = {
    1: {"patient_name": {"value": "RAJESH GUPTA", "bbox": [100, 100, 500, 150]},
        "admission_date": {"value": "01/03/2025", "bbox": [100, 200, 500, 250]}},
    2: {"diagnosis": {"value": "Acute appendicitis", "bbox": [100, 300, 500, 350]},
        "medications.0.medicine_name": {"value": "Paracetamol", "bbox": [100, 400, 300, 450]},
        "medications.0.dosage": {"value": "500mg", "bbox": [320, 400, 400, 450]},
        "medications.0.frequency": {"value": "TDS", "bbox": [420, 400, 500, 450]}},
    3: {"discharge_date": {"value": "05/03/2025", "bbox": [100, 500, 500, 550]},
        "medications.1.medicine_name": {"value": "Amoxicillin", "bbox": [100, 600, 300, 650]}},
}


def _process(temp_storage, monkeypatch, pages=PAGES):
    run = _run(temp_storage, 3, tokens=_doc03_tokens, expect=DOC03)
    vlm = PageAwareVLM(pages).bind(run.refs).install(monkeypatch)
    return run, vlm, _vlm_pipeline().process(run.payload, page_images=run.refs)


def test_doc03_fields_are_extracted_from_every_page_with_page_specific_evidence(temp_storage, monkeypatch):
    run, vlm, env = _process(temp_storage, monkeypatch)

    assert [c["page"] for c in vlm.calls] == [1, 2, 3]
    assert env.document_metadata.document_type == "DOC-03"
    assert env.document_metadata.schema_id == "DOC-03" and env.document_metadata.page_count == 3

    expected = {
        "patient_name": ("RAJESH GUPTA", 1, [100, 100, 500, 150]),
        "admission_date": ("2025-03-01", 1, [100, 200, 500, 250]),          # normalised to ISO 8601
        "diagnosis": ("Acute appendicitis", 2, [100, 300, 500, 350]),
        "discharge_date": ("2025-03-05", 3, [100, 500, 500, 550]),
        "medications.0.medicine_name": ("Paracetamol", 2, [100, 400, 300, 450]),
        "medications.0.dosage": ("500mg", 2, [320, 400, 400, 450]),
        "medications.0.frequency": ("TDS", 2, [420, 400, 500, 450]),
        "medications.1.medicine_name": ("Amoxicillin", 3, [100, 600, 300, 650]),
    }
    for path, (value, page, box) in expected.items():
        field = env.fields[path]
        assert field.value == value, path
        assert field.evidence.page == page, path
        assert field.evidence.bbox == pytest.approx(_expected_original_bbox(run.refs[page - 1], box)), path
        assert field.validation == "passed", path


def test_doc03_repeating_medications_keep_entry_order_across_pages(temp_storage, monkeypatch):
    _run_, _vlm, env = _process(temp_storage, monkeypatch)
    names = [env.fields[f"medications.{i}.medicine_name"].value for i in range(2)]
    assert names == ["Paracetamol", "Amoxicillin"]
    assert "medications.2.medicine_name" not in env.fields
    assert env.fields["medications.1.medicine_name"].evidence.page == 3


def test_doc03_vlm_fields_have_unavailable_confidence_so_the_document_needs_review(temp_storage, monkeypatch):
    _run_, _vlm, env = _process(temp_storage, monkeypatch)

    assert env.flags == []                                    # nothing wrong with the content ...
    for path, field in env.fields.items():
        if field.value is not None:
            assert field.confidence is None, path             # ... no synthetic score
            assert field.confidence_provenance == ConfidenceProvenance.UNAVAILABLE, path
            assert field.review_required is True, path
    assert env.document_metadata.status == "needs_review"     # conservative VLM policy unchanged


def test_doc03_provenance_records_every_page_processed(temp_storage, monkeypatch):
    _run_, _vlm, env = _process(temp_storage, monkeypatch)
    details = env.run.provenance.details
    assert details["pages_attempted"] == details["pages_processed"] == [1, 2, 3]
    assert details["pages_failed"] == [] and details["required_fields_missing"] == []


def test_doc03_invalid_date_fails_validation_and_chronology_violation_is_flagged(temp_storage, monkeypatch):
    pages = {
        1: {"patient_name": PAGES[1]["patient_name"],
            "admission_date": {"value": "10/03/2025", "bbox": [100, 200, 500, 250]}},
        3: {"discharge_date": {"value": "05/03/2025", "bbox": [100, 500, 500, 550]}},   # before admission
    }
    _run_, _vlm, env = _process(temp_storage, monkeypatch, pages)

    codes = {f.code for f in env.flags}
    assert any("CHRONOLOG" in c for c in codes), codes
    assert env.fields["admission_date"].review_required and env.fields["discharge_date"].review_required
    assert env.document_metadata.status == "needs_review"

    bad = {1: {"patient_name": PAGES[1]["patient_name"],
               "admission_date": {"value": "99/99/9999", "bbox": [100, 200, 500, 250]}}}
    _run2, _vlm2, env2 = _process(temp_storage, monkeypatch, bad)
    assert env2.fields["admission_date"].validation == "failed"
    assert env2.document_metadata.status == "needs_review"


def test_doc03_missing_required_patient_name_keeps_the_rest_and_forces_review(temp_storage, monkeypatch):
    pages = {k: {p: v for p, v in page.items() if p != "patient_name"} for k, page in PAGES.items()}
    _run_, _vlm, env = _process(temp_storage, monkeypatch, pages)

    assert env.fields["patient_name"].value is None and env.fields["patient_name"].validation == "failed"
    assert [f.fields for f in env.flags if f.code == "REQUIRED_FIELD_MISSING"] == [["patient_name"]]
    assert env.fields["diagnosis"].value == "Acute appendicitis"
    assert env.document_metadata.status == "needs_review"


def test_doc03_failed_page_is_reported_and_blocks_approval(temp_storage, monkeypatch):
    pages = dict(PAGES)
    pages[2] = RuntimeError("model timeout")
    _run_, _vlm, env = _process(temp_storage, monkeypatch, pages)

    assert any(f.code == "PARTIAL_PAGE_COVERAGE" and "page 2" in f.message for f in env.flags)
    assert env.fields["discharge_date"].value == "2025-03-05"
    assert env.fields["diagnosis"].value is None
    assert env.run.provenance.details["pages_failed"][0]["page"] == 2
    assert env.document_metadata.status == "needs_review"


def test_doc03_ocr_grounded_text_path_uses_measured_ocr_confidence(temp_storage):
    """Text-model path: evidence comes from Module B OCR, so confidence is measured (OCR)."""
    from doc_extractor.parsing.base import RawOCRToken

    name = RawOCRToken(text="Patient Name: RAJESH GUPTA", bbox_preprocessed=(50, 260, 400, 300), confidence=0.96)
    payload = _ocr_grounded_payload_doc03(temp_storage, [name])
    extractor = _text_extractor({"patient_name": "RAJESH GUPTA", "diagnosis": "Acute Bronchitis"})

    env = ModuleCPipeline(extractor=extractor).process(payload)

    for path, conf in (("patient_name", 0.96), ("diagnosis", 0.95)):
        field = env.fields[path]
        assert field.confidence == pytest.approx(conf), path
        assert field.confidence_provenance == ConfidenceProvenance.OCR, path
        assert field.evidence is not None and field.evidence.page == 1, path
    assert env.document_metadata.document_type == "DOC-03"


def _ocr_grounded_payload_doc03(storage, extra):
    import io

    from doc_extractor.parsing.adapter import LocalOCRParserAdapter
    from doc_extractor.pipeline_b import ModuleBPipeline
    from tests.test_canonical_module_c_orchestration import TENANT, _module_a, _png_bytes

    handoff = _module_a(storage).process_document(io.BytesIO(_png_bytes()), "discharge.png")
    pipe_b = ModuleBPipeline(
        storage=storage,
        parser=LocalOCRParserAdapter(raw_extractor=lambda img: _doc03_tokens(img) + extra),
        tenant_id=TENANT,
    )
    payload = pipe_b.process_handoff(handoff)
    assert payload.classification.document_type == DOC03
    return payload
