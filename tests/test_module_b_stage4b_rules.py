"""
Tests for Module B Stage 4B: Rule-Based Document Classification (DOC-01, DOC-02, DOC-03).

Validates:
- RuleBasedClassifierAdapter metadata, operational check, and RULE_HEURISTIC provenance.
- Clear, deterministic classification for DOC-01 (CASA Account Opening Form).
- Clear, deterministic classification for DOC-02 (Insurance Application Form).
- Clear, deterministic classification for DOC-03 (Hospital Discharge Summary).
- Safe text normalization (case insensitivity, whitespace variations, punctuation tolerance).
- Negative signal deterrence and resistance to confusing shared terms (e.g. "PAN Card", "Aadhaar", "Date of Birth").
- Handling of ambiguous/conflicting evidence routing to UNKNOWN with review required.
- Empty text / blank pages / unreadable evidence routing to UNKNOWN.
- Sub-threshold scores routing to UNKNOWN with review required and no schema bound.
- End-to-end integration with DocumentClassificationService and SchemaRegistry.
- Provenance tracking (confidence_provenance is RULE_HEURISTIC, not MODEL).
- Deterministic repeatability (identical corpus produces identical score and reasons).
"""

from pathlib import Path
from typing import List
import numpy as np
import pytest

from doc_extractor.classification.base import (
    ClassificationEngineError,
    DocumentClassificationService,
    InMemorySchemaRegistry,
)
from doc_extractor.classification.rules import RuleBasedClassifierAdapter
from doc_extractor.contracts_b import (
    BoundingBox,
    ClassificationOutput,
    ConfidenceProvenance,
    KeyValuePair,
    LayoutBlock,
    LayoutElementType,
    ModuleAPageRef,
    ModuleBInputPayload,
    MVPClassificationDomain,
    MVPDocumentType,
    PageLayoutOutput,
)
from doc_extractor.loader_b import LoadedPageImage, ModuleBLoadResult


def _build_dummy_load_result(tenant_id: str = "tenant-001", doc_id: str = "doc-test-123") -> ModuleBLoadResult:
    page_ref = ModuleAPageRef(
        page_number=1,
        original_artifact_id="art_orig_1",
        preprocessed_artifact_id="art_prep_1",
        original_storage_uri="t1/doc1/pages/page_0001.png",
        preprocessed_storage_uri="t1/doc1/preprocessed/page_0001_preprocessed.png",
        original_width_px=1000,
        original_height_px=1400,
        preprocessed_width_px=1000,
        preprocessed_height_px=1400,
        dpi=300,
        applied_rotation_deg=0,
        applied_deskew_deg=0.0,
        metadata_storage_uri="t1/doc1/meta.json",
    )
    img = np.ones((1400, 1000, 3), dtype=np.uint8) * 255
    loaded_page = LoadedPageImage(
        page_number=1,
        image_bgr=img,
        width_px=1000,
        height_px=1400,
        dpi=300,
        file_path=Path("dummy.png"),
        page_ref=page_ref,
    )
    payload = ModuleBInputPayload(
        contract_version="1.0.0",
        run_id="run-a-1",
        document_id=doc_id,
        tenant_id=tenant_id,
        status="processing",
        internal_state="ready_for_extraction",
        claimed_filename="test.pdf",
        file_hash_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        file_size_bytes=1024,
        page_count=1,
        mime_type="application/pdf",
        pages=[page_ref],
        created_at="2026-10-29T10:00:00Z",
        completed_at="2026-10-29T10:00:01Z",
    )
    return ModuleBLoadResult(
        payload=payload,
        pages=[loaded_page],
        tenant_id=tenant_id,
        document_id=doc_id,
        run_id="run-b-1",
    )


def _build_page_layout(markdown: str, blocks: List[str] = None) -> PageLayoutOutput:
    bbox = BoundingBox(x0=10, y0=10, x1=100, y1=100)
    layout_blocks = []
    if blocks:
        for idx, text in enumerate(blocks):
            layout_blocks.append(
                LayoutBlock(
                    block_id=f"b_{idx}",
                    element_type=LayoutElementType.PARAGRAPH,
                    text=text,
                    ocr_confidence=0.95,
                    bbox_preprocessed=bbox,
                    bbox_original=bbox,
                    reading_order_index=idx,
                )
            )
    return PageLayoutOutput(
        page_number=1,
        markdown_content=markdown,
        blocks=layout_blocks,
        checkboxes=[],
        key_value_pairs=[],
        tables=[],
        character_grids=[],
        mean_ocr_confidence=0.95,
    )


# ----------------------------------------------------------------------------
# 1. Metadata and Capabilities
# ----------------------------------------------------------------------------

def test_rule_classifier_metadata():
    adapter = RuleBasedClassifierAdapter()
    meta = adapter.get_metadata()
    assert meta.classifier_name == "rule-based-classifier-adapter"
    assert meta.classifier_version == "1.0.0"
    assert meta.confidence_provenance == ConfidenceProvenance.RULE_HEURISTIC
    assert meta.calibrated_probabilities is False
    assert adapter.is_operational() is True


# ----------------------------------------------------------------------------
# 2. DOC-01: CASA Account Opening Form Classification
# ----------------------------------------------------------------------------

def test_classify_doc01_casa_account_opening():
    adapter = RuleBasedClassifierAdapter()
    load_result = _build_dummy_load_result()

    markdown = """
    # HDFC BANK LIMITED
    ## SAVINGS / CURRENT ACCOUNT OPENING APPLICATION FORM
    **Customer Information Form (CIF)**
    
    Please open a Savings Account with the following details:
    Customer ID / CIF No: 987654321
    Initial Deposit Amount: INR 10,000
    Mode of Operation: Either or Survivor
    Services Opted: Cheque Book, Debit Card, Internet Banking
    Applicant Details:
    PAN Card Number: ABCDE1234F
    Aadhaar Number: 1234-5678-9012
    """
    page = _build_page_layout(markdown)

    result = adapter.classify(load_result, [page])
    assert result.domain == MVPClassificationDomain.BFSI
    assert result.document_type == MVPDocumentType.DOC_01_CASA
    assert result.confidence_provenance == ConfidenceProvenance.RULE_HEURISTIC
    assert result.classification_confidence >= 0.85
    assert any("Title: Savings/Current Account Opening" in r or "Title: Customer Information Form" in r for r in result.classification_reasons)


# ----------------------------------------------------------------------------
# 3. DOC-02: Insurance Application Form Classification
# ----------------------------------------------------------------------------

def test_classify_doc02_insurance_application():
    adapter = RuleBasedClassifierAdapter()
    load_result = _build_dummy_load_result()

    markdown = """
    # MAX LIFE INSURANCE COMPANY
    ## LIFE INSURANCE PROPOSAL FORM FOR INDIVIDUALS
    
    Proposer Details: Rajesh Kumar
    Life to be Assured: Rajesh Kumar
    Sum Insured: INR 50,00,000
    Policy Term: 20 Years
    Premium Paying Term: 10 Years
    Premium Payment Frequency: Annual
    Nominee Name: Priya Kumar (Relationship: Spouse)
    
    ### Medical History Questionnaire
    Do you have any pre-existing diseases or conditions? [ ] Yes [x] No
    """
    page = _build_page_layout(markdown)

    result = adapter.classify(load_result, [page])
    assert result.domain == MVPClassificationDomain.BFSI
    assert result.document_type == MVPDocumentType.DOC_02_INSURANCE_APP
    assert result.confidence_provenance == ConfidenceProvenance.RULE_HEURISTIC
    assert result.classification_confidence >= 0.85
    assert any("Title: Insurance Proposal/Application" in r for r in result.classification_reasons)


# ----------------------------------------------------------------------------
# 4. DOC-03: Hospital Discharge Summary Classification
# ----------------------------------------------------------------------------

def test_classify_doc03_discharge_summary():
    adapter = RuleBasedClassifierAdapter()
    load_result = _build_dummy_load_result()

    markdown = """
    # APOLLO HOSPITALS ENTERPRISE LTD
    ## INPATIENT DISCHARGE SUMMARY
    
    Patient Name: Suresh Babu
    IP No / Inpatient Number: IP-2026-90812
    Date of Admission: 15-Oct-2026
    Date of Discharge: 22-Oct-2026
    Consultant Doctor: Dr. K. Venkatraman (Cardiology)
    
    Chief Complaints: Acute chest discomfort and shortness of breath.
    Final Diagnosis: Unstable Angina, Hypertension.
    Treatment Given: Managed conservatively with antiplatelets and statins.
    Medications on Discharge:
    1. Tab Atorvastatin 20mg OD
    2. Tab Aspirin 75mg OD
    Follow-up Advice: Review in cardiology OPD after 10 days.
    """
    page = _build_page_layout(markdown)

    result = adapter.classify(load_result, [page])
    assert result.domain == MVPClassificationDomain.HEALTHCARE
    assert result.document_type == MVPDocumentType.DOC_03_DISCHARGE_SUMMARY
    assert result.confidence_provenance == ConfidenceProvenance.RULE_HEURISTIC
    assert result.classification_confidence >= 0.85
    assert any("Title: Discharge Summary" in r for r in result.classification_reasons)


# ----------------------------------------------------------------------------
# 5. Discrimination Against Shared Generic Terms
# ----------------------------------------------------------------------------

def test_generic_kyc_alone_does_not_misclassify_as_doc01():
    """Generic terms like PAN and Aadhaar on an otherwise ambiguous document must not exceed threshold."""
    adapter = RuleBasedClassifierAdapter()
    load_result = _build_dummy_load_result()

    # Document only contains generic identity numbers, no bank titles or CIF or deposit
    markdown = """
    Identity Verification Slip
    Name: Amit Sharma
    PAN Card: ABCDE1234F
    Aadhaar Number: 9876 5432 1098
    Date: 2026-10-29
    """
    page = _build_page_layout(markdown)

    result = adapter.classify(load_result, [page])
    # Total score should be well below 20.0 (only 8.0 + 8.0 = 16.0)
    assert result.document_type == MVPDocumentType.UNKNOWN
    assert result.domain == MVPClassificationDomain.UNKNOWN
    assert result.classification_confidence < 0.85
    assert any("Insufficient evidence" in r for r in result.classification_reasons)


# ----------------------------------------------------------------------------
# 6. Ambiguous / Conflicting Signals Route to UNKNOWN
# ----------------------------------------------------------------------------

def test_conflicting_bank_and_hospital_signals_routes_to_unknown():
    """A document containing conflicting strong headers from multiple types must route to UNKNOWN."""
    adapter = RuleBasedClassifierAdapter()
    load_result = _build_dummy_load_result()

    markdown = """
    Account Opening Form and Hospital Discharge Summary
    CASA Application with Date of Admission and Inpatient Number.
    Initial Deposit: INR 5000.
    Final Diagnosis: Fever.
    """
    page = _build_page_layout(markdown)

    result = adapter.classify(load_result, [page])
    # The negative signals cancel out scores or narrow the margin, forcing UNKNOWN
    assert result.document_type == MVPDocumentType.UNKNOWN
    assert result.domain == MVPClassificationDomain.UNKNOWN


# ----------------------------------------------------------------------------
# 7. Empty or Insufficient Text Handling
# ----------------------------------------------------------------------------

def test_empty_parsed_pages_returns_unknown():
    adapter = RuleBasedClassifierAdapter()
    load_result = _build_dummy_load_result()
    page = _build_page_layout("")

    result = adapter.classify(load_result, [page])
    assert result.document_type == MVPDocumentType.UNKNOWN
    assert result.domain == MVPClassificationDomain.UNKNOWN
    assert result.classification_confidence == 0.0
    assert any("No text or layout blocks found" in r for r in result.classification_reasons)


# ----------------------------------------------------------------------------
# 8. Deterministic Repeatability
# ----------------------------------------------------------------------------

def test_deterministic_scoring_repeatability():
    """Identical input corpus must yield identical score, confidence, and reasons."""
    adapter = RuleBasedClassifierAdapter()
    load_result = _build_dummy_load_result()
    page = _build_page_layout("Discharge Summary Date of Admission Date of Discharge Final Diagnosis")

    run_1 = adapter.classify(load_result, [page])
    run_2 = adapter.classify(load_result, [page])

    assert run_1.document_type == run_2.document_type
    assert run_1.domain == run_2.domain
    assert run_1.classification_confidence == run_2.classification_confidence
    assert run_1.classification_reasons == run_2.classification_reasons


# ----------------------------------------------------------------------------
# 9. Integration with DocumentClassificationService and SchemaRegistry
# ----------------------------------------------------------------------------

def test_service_integration_doc01_approved_schema_binding():
    adapter = RuleBasedClassifierAdapter()
    registry = InMemorySchemaRegistry()
    service = DocumentClassificationService(classifier=adapter, schema_registry=registry, confidence_threshold=0.85)

    load_result = _build_dummy_load_result()
    markdown = """
    BANK ACCOUNT OPENING FORM
    CASA Application
    Customer Information Form CIF Number
    Initial Deposit Amount Mode of Operation
    Cheque Book and Debit Card
    """
    page = _build_page_layout(markdown)

    out = service.classify_and_bind(load_result, [page])
    assert out.domain == MVPClassificationDomain.BFSI
    assert out.document_type == MVPDocumentType.DOC_01_CASA
    assert out.schema_id == "DOC-01"
    assert out.schema_version == "1.0.0"
    assert out.review_required is False
    assert out.confidence_provenance == ConfidenceProvenance.RULE_HEURISTIC
    assert out.classifier_identity == "rule-based-classifier-adapter"
    assert len(out.classification_reasons) > 0


def test_service_integration_low_confidence_forces_review_and_no_schema():
    """When rule score produces confidence < 0.85, service forces UNKNOWN, schema=None, review_required=True."""
    # Raise threshold high to trigger fallback
    adapter = RuleBasedClassifierAdapter()
    registry = InMemorySchemaRegistry()
    service = DocumentClassificationService(classifier=adapter, schema_registry=registry, confidence_threshold=0.99)

    load_result = _build_dummy_load_result()
    markdown = "Discharge Summary Hospital Inpatient Report"
    page = _build_page_layout(markdown)

    out = service.classify_and_bind(load_result, [page])
    assert out.domain == MVPClassificationDomain.UNKNOWN
    assert out.document_type == MVPDocumentType.UNKNOWN
    assert out.schema_id is None
    assert out.schema_version is None
    assert out.review_required is True
    assert any("below threshold" in r for r in out.classification_reasons)

