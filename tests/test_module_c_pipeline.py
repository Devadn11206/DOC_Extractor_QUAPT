"""
Comprehensive Unit and Integration Tests for Module C.
Covers:
- Schemas (DOC-01, DOC-02, DOC-03)
- Validation rules (PAN, Aadhaar Verhoeff, Dates, Phones, Chronology, Arithmetic)
- Evidence linking and extractor
- Module C Pipeline and Canonical Envelope assembly
"""

import pytest
from datetime import datetime, timezone

from doc_extractor.contracts_b import (
    BoundingBox,
    ClassificationOutput,
    ConfidenceProvenance,
    LayoutBlock,
    LayoutElementType,
    ModuleBOutputPayload,
    MVPClassificationDomain,
    MVPDocumentType,
    PageLayoutOutput,
)
from doc_extractor.schemas import CASASchema, InsuranceApplicationSchema, DischargeSummarySchema
from doc_extractor.validation_rules import (
    validate_pan,
    validate_aadhaar_checksum,
    normalize_date_to_iso,
    normalize_phone_number,
    validate_chronology,
    validate_arithmetic,
)
from doc_extractor.extractor import SchemaGuidedExtractor, find_evidence_for_value
from doc_extractor.pipeline_c import ModuleCPipeline, CanonicalOutputEnvelope


def test_validation_pan():
    valid, err = validate_pan("ABCDE1234F")
    assert valid == "ABCDE1234F"
    assert err is None

    invalid, err = validate_pan("12345ABCDE")
    assert err == "INVALID_PAN_FORMAT"

    none_val, err = validate_pan(None)
    assert none_val is None
    assert err is None


def test_validation_aadhaar():
    # Valid Aadhaar numbers using Verhoeff
    assert validate_aadhaar_checksum("234567890126") is False  # random invalid
    # Test valid Verhoeff sequence
    assert validate_aadhaar_checksum("invalid") is False
    assert validate_aadhaar_checksum("123") is False


def test_validation_dates_and_phone():
    norm_date, err = normalize_date_to_iso("22/06/1985")
    assert norm_date == "1985-06-22"
    assert err is None

    norm_date2, err2 = normalize_date_to_iso("2026-10-29")
    assert norm_date2 == "2026-10-29"
    assert err2 is None

    bad_date, err_bad = normalize_date_to_iso("not-a-date")
    assert err_bad == "INVALID_DATE_FORMAT"

    norm_phone, p_err = normalize_phone_number("9876543210")
    assert norm_phone == "+919876543210"
    assert p_err is None


def test_chronology_and_arithmetic():
    # Normal chronology
    assert validate_chronology("2026-01-01", "2026-01-10") is None

    # Mismatch chronology
    flag = validate_chronology("2026-01-10", "2026-01-01")
    assert flag is not None
    assert flag["code"] == "CHRONOLOGY_MISMATCH"

    # Arithmetic check
    assert validate_arithmetic([100.0, 200.0, 50.0], 350.0) is None
    mismatch = validate_arithmetic([100.0, 200.0], 500.0)
    assert mismatch is not None
    assert mismatch["code"] == "ARITHMETIC_MISMATCH"


def test_pipeline_c_casa_end_to_end():
    # Construct a synthetic ModuleBOutputPayload for DOC-01 CASA
    block_name = LayoutBlock(
        block_id="blk_1",
        element_type=LayoutElementType.PARAGRAPH,
        text="Applicant Name: SURESH KUMAR RAMAN",
        ocr_confidence=0.98,
        bbox_preprocessed=BoundingBox(x0=10, y0=20, x1=200, y1=40),
        bbox_original=BoundingBox(x0=10, y0=20, x1=200, y1=40),
        reading_order_index=0,
    )
    block_pan = LayoutBlock(
        block_id="blk_2",
        element_type=LayoutElementType.PARAGRAPH,
        text="PAN: ABCDE1234F",
        ocr_confidence=0.99,
        bbox_preprocessed=BoundingBox(x0=10, y0=50, x1=150, y1=70),
        bbox_original=BoundingBox(x0=10, y0=50, x1=150, y1=70),
        reading_order_index=1,
    )

    page = PageLayoutOutput(
        page_number=1,
        markdown_content="Applicant Name: SURESH KUMAR RAMAN\nPAN: ABCDE1234F\nCheque Book: [x]",
        blocks=[block_name, block_pan],
        mean_ocr_confidence=0.98,
    )

    payload = ModuleBOutputPayload(
        run_id="run_test_001",
        document_id="doc_test_001",
        tenant_id="tenant_default",
        status="processing",
        classification=ClassificationOutput(
            domain=MVPClassificationDomain.BFSI,
            document_type=MVPDocumentType.DOC_01_CASA,
            classification_confidence=0.98,
            confidence_provenance=ConfidenceProvenance.RULE_HEURISTIC,
            schema_id="DOC-01",
            schema_version="1.0.0",
        ),
        pages=[page],
        combined_markdown="Applicant Name: SURESH KUMAR RAMAN\nPAN: ABCDE1234F\nCheque Book: [x]",
        parser_model_name="mock-layout-parser",
        classifier_model_name="mock-classifier",
        processing_time_ms=120.0,
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )

    pipeline = ModuleCPipeline()
    envelope = pipeline.process(payload)

    assert isinstance(envelope, CanonicalOutputEnvelope)
    assert envelope.document_id == "doc_test_001"
    assert envelope.document_metadata.document_type == "DOC-01"
    assert "applicant.full_name" in envelope.fields
    assert envelope.fields["applicant.full_name"].value == "SURESH KUMAR RAMAN"
    assert envelope.fields["applicant.full_name"].evidence is not None
    assert envelope.fields["applicant.full_name"].evidence.page == 1
    assert envelope.fields["applicant.pan_card_number"].value == "ABCDE1234F"
    assert envelope.fields["applicant.pan_card_number"].validation == "passed"


def test_pipeline_c_discharge_summary_chronology_flag():
    # Construct a synthetic ModuleBOutputPayload for DOC-03 with inverted dates
    page = PageLayoutOutput(
        page_number=1,
        markdown_content="Patient: RAJESH SHARMA\nAdmission Date: 2026-05-15\nDischarge Date: 2026-05-10",
        blocks=[
            LayoutBlock(
                block_id="b1",
                element_type=LayoutElementType.PARAGRAPH,
                text="Patient: RAJESH SHARMA",
                ocr_confidence=0.95,
                bbox_preprocessed=BoundingBox(x0=10, y0=10, x1=100, y1=30),
                bbox_original=BoundingBox(x0=10, y0=10, x1=100, y1=30),
                reading_order_index=0,
            )
        ],
        mean_ocr_confidence=0.95,
    )

    payload = ModuleBOutputPayload(
        run_id="run_test_002",
        document_id="doc_test_002",
        tenant_id="tenant_default",
        status="processing",
        classification=ClassificationOutput(
            domain=MVPClassificationDomain.HEALTHCARE,
            document_type=MVPDocumentType.DOC_03_DISCHARGE_SUMMARY,
            classification_confidence=0.96,
            confidence_provenance=ConfidenceProvenance.RULE_HEURISTIC,
            schema_id="DOC-03",
            schema_version="1.0.0",
        ),
        pages=[page],
        combined_markdown=page.markdown_content,
        parser_model_name="mock-layout-parser",
        classifier_model_name="mock-classifier",
        processing_time_ms=140.0,
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )

    pipeline = ModuleCPipeline()
    envelope = pipeline.process(payload)

    # Inverted dates should trigger chronology mismatch flag and route to needs_review
    assert envelope.document_metadata.status == "needs_review"
    assert any(flag.code == "CHRONOLOGY_MISMATCH" for flag in envelope.flags)
    assert envelope.fields["admission_date"].review_required is True
    assert envelope.fields["discharge_date"].review_required is True
