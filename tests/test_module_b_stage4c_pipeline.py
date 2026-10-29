"""
Comprehensive Integration Tests for Module B Stage 4C:
Pipeline Orchestration, Handoff Packaging, and Reviewer Overrides.

Covers:
- Successful synthetic pipelines for DOC-01, DOC-02, and DOC-03.
- Table, checkbox, and character-grid reconstruction integration during layout parsing.
- Page Markdown combination with explicit page separators (--- Page {N} ---).
- UNKNOWN and ambiguous document routing to 'needs_review' without schema binding.
- Handling missing/inactive schemas routing to 'needs_review'.
- Fail-closed error handling on corrupt input / parser errors (returning status='failed').
- Idempotency key replay returning cached terminal payload.
- Reviewer override foundation (FR-CLS-004):
  - Authorized override re-binding approved schema and creating a new run record.
  - Prior-run immutability (original payload unchanged).
  - Validation: rejects blank reviewer ID, missing reason, or tenant mismatches.
"""

import io
from pathlib import Path
import tempfile
from typing import Dict, List
import cv2
import numpy as np
import pytest
from PIL import Image

from doc_extractor.classification.base import (
    ApprovedSchemaRef,
    DocumentClassificationService,
    InMemorySchemaRegistry,
)
from doc_extractor.classification.rules import RuleBasedClassifierAdapter
from doc_extractor.contracts_b import (
    MODULE_B_CONTRACT_VERSION,
    BoundingBox,
    ModuleAPageRef,
    ModuleBInputPayload,
    ModuleBOutputPayload,
    MVPClassificationDomain,
    MVPDocumentType,
    PageLayoutOutput,
)
from doc_extractor.exceptions import DocumentProcessingError, StorageError
from doc_extractor.handoff import ModuleAHandoffPayload
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.loader_b import LoadedPageImage, ModuleBLoadResult, SecureModuleBLoader
from doc_extractor.parsing.adapter import LocalOCRParserAdapter
from doc_extractor.parsing.base import ParserEngineError, RawOCRToken
from doc_extractor.parsing.character_grids import CandidateCharacterGrid, RawCharacterObservation
from doc_extractor.parsing.checkboxes import CandidateCheckboxRegion
from doc_extractor.parsing.tables import CandidateTableCell, CandidateTableRegion
from doc_extractor.pipeline_b import ModuleBPipeline, ReviewerOverrideRequest
from doc_extractor.storage import LocalDiskStorage


@pytest.fixture
def temp_storage():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage = LocalDiskStorage(base_dir=tmpdir, tenant_id="tenant-test")
        yield storage


from doc_extractor.malware import MockMalwareScanner
from doc_extractor.pipeline import ModuleAPipeline

def _create_synthetic_handoff_files(storage: LocalDiskStorage, page_texts: List[str], tenant_id: str = "tenant-test") -> ModuleAHandoffPayload:
    """Uses ModuleAPipeline to create real synthetic multi-page files on disk and return valid ModuleAHandoffPayload."""
    pipeline_a = ModuleAPipeline(
        scanner=MockMalwareScanner(),
        storage=storage,
        tenant_id=tenant_id,
    )
    images = []
    for _ in page_texts:
        img = Image.new("RGB", (1000, 1400), color=(255, 255, 255))
        images.append(img)

    buf = io.BytesIO()
    if len(images) == 1:
        images[0].save(buf, format="PNG")
        filename = "test.png"
    else:
        images[0].save(buf, format="TIFF", save_all=True, append_images=images[1:])
        filename = "test.tiff"

    buf.seek(0)
    return pipeline_a.process_document(buf, filename)


# ----------------------------------------------------------------------------
# 1. Successful Pipeline Execution for DOC-01, DOC-02, DOC-03
# ----------------------------------------------------------------------------

def test_pipeline_success_doc01_casa(temp_storage):
    """Executes full pipeline on synthetic DOC-01 CASA form with OCR tokens."""
    def doc01_extractor(img: np.ndarray) -> List[RawOCRToken]:
        return [
            RawOCRToken(text="BANK ACCOUNT OPENING FORM", bbox_preprocessed=(50, 50, 400, 100), confidence=0.98),
            RawOCRToken(text="CASA Application", bbox_preprocessed=(50, 110, 300, 150), confidence=0.97),
            RawOCRToken(text="Customer Information Form CIF Number", bbox_preprocessed=(50, 160, 450, 200), confidence=0.96),
            RawOCRToken(text="Initial Deposit Amount", bbox_preprocessed=(50, 210, 300, 250), confidence=0.95),
            RawOCRToken(text="Cheque Book and Debit Card", bbox_preprocessed=(50, 260, 350, 300), confidence=0.94),
        ]

    parser = LocalOCRParserAdapter(raw_extractor=doc01_extractor)
    pipeline = ModuleBPipeline(storage=temp_storage, parser=parser, tenant_id="tenant-test")

    handoff = _create_synthetic_handoff_files(temp_storage, ["Page 1 placeholder"])
    output = pipeline.process_handoff(handoff)

    assert output.status == "processing"  # Bound and ready
    assert output.classification.domain == MVPClassificationDomain.BFSI
    assert output.classification.document_type == MVPDocumentType.DOC_01_CASA
    assert output.classification.schema_id == "DOC-01"
    assert output.classification.schema_version == "1.0.0"
    assert output.classification.review_required is False
    assert len(output.pages) == 1
    assert "--- Page 1 ---" in output.combined_markdown
    assert "BANK ACCOUNT OPENING FORM" in output.combined_markdown


def test_pipeline_success_doc02_insurance(temp_storage):
    """Executes full pipeline on synthetic DOC-02 Insurance proposal."""
    def doc02_extractor(img: np.ndarray) -> List[RawOCRToken]:
        return [
            RawOCRToken(text="LIFE INSURANCE PROPOSAL FORM FOR INDIVIDUALS", bbox_preprocessed=(50, 50, 500, 100), confidence=0.98),
            RawOCRToken(text="Sum Insured INR 5000000", bbox_preprocessed=(50, 110, 350, 150), confidence=0.97),
            RawOCRToken(text="Policy Term 20 Years", bbox_preprocessed=(50, 160, 300, 200), confidence=0.96),
            RawOCRToken(text="Medical History Questionnaire", bbox_preprocessed=(50, 210, 400, 250), confidence=0.95),
            RawOCRToken(text="Nominee Name and Share", bbox_preprocessed=(50, 260, 350, 300), confidence=0.94),
        ]

    parser = LocalOCRParserAdapter(raw_extractor=doc02_extractor)
    pipeline = ModuleBPipeline(storage=temp_storage, parser=parser, tenant_id="tenant-test")

    handoff = _create_synthetic_handoff_files(temp_storage, ["Page 1 placeholder"])
    output = pipeline.process_handoff(handoff)

    assert output.status == "processing"
    assert output.classification.domain == MVPClassificationDomain.BFSI
    assert output.classification.document_type == MVPDocumentType.DOC_02_INSURANCE_APP
    assert output.classification.schema_id == "DOC-02"
    assert output.classification.schema_version == "1.0.0"
    assert output.classification.review_required is False


def test_pipeline_success_doc03_discharge_summary(temp_storage):
    """Executes full pipeline on multi-page synthetic DOC-03 Discharge summary."""
    def doc03_extractor(img: np.ndarray) -> List[RawOCRToken]:
        return [
            RawOCRToken(text="HOSPITAL DISCHARGE SUMMARY", bbox_preprocessed=(50, 50, 450, 100), confidence=0.98),
            RawOCRToken(text="Date of Admission: 10-Oct-2026", bbox_preprocessed=(50, 110, 400, 150), confidence=0.97),
            RawOCRToken(text="Date of Discharge: 15-Oct-2026", bbox_preprocessed=(50, 160, 400, 200), confidence=0.96),
            RawOCRToken(text="Final Diagnosis: Acute Bronchitis", bbox_preprocessed=(50, 210, 450, 250), confidence=0.95),
        ]

    parser = LocalOCRParserAdapter(raw_extractor=doc03_extractor)
    pipeline = ModuleBPipeline(storage=temp_storage, parser=parser, tenant_id="tenant-test")

    handoff = _create_synthetic_handoff_files(temp_storage, ["Page 1 placeholder", "Page 2 placeholder"])
    output = pipeline.process_handoff(handoff)

    assert output.status == "processing"
    assert output.classification.domain == MVPClassificationDomain.HEALTHCARE
    assert output.classification.document_type == MVPDocumentType.DOC_03_DISCHARGE_SUMMARY
    assert output.classification.schema_id == "DOC-03"
    assert output.classification.schema_version == "1.0.0"
    assert len(output.pages) == 2
    assert "--- Page 1 ---" in output.combined_markdown
    assert "--- Page 2 ---" in output.combined_markdown


# ----------------------------------------------------------------------------
# 2. Table, Checkbox, and Character-Grid Integration
# ----------------------------------------------------------------------------

def test_pipeline_with_tables_checkboxes_and_grids(temp_storage):
    """Validates that candidate tables, checkboxes, and grids merge into layout outputs."""
    parser = LocalOCRParserAdapter(raw_extractor=lambda img: [
        RawOCRToken(text="BANK ACCOUNT OPENING FORM CASA Application", bbox_preprocessed=(50, 50, 400, 100), confidence=0.95),
        RawOCRToken(text="Customer Information Form CIF Number", bbox_preprocessed=(50, 110, 400, 150), confidence=0.95),
    ])
    pipeline = ModuleBPipeline(storage=temp_storage, parser=parser, tenant_id="tenant-test")
    handoff = _create_synthetic_handoff_files(temp_storage, ["Page 1"])

    # Provide candidate table
    cand_tbl = CandidateTableRegion(
        region_id="t1",
        cells=[
            CandidateTableCell(text="Account Type", bbox_preprocessed=(50, 200, 150, 240), is_header=True),
            CandidateTableCell(text="Savings", bbox_preprocessed=(50, 240, 150, 280)),
        ],
    )
    # Provide candidate checkbox
    cand_cb = CandidateCheckboxRegion(
        candidate_id="cb1",
        bbox_preprocessed=(200, 200, 230, 230),
        raw_state_hint="checked",
    )
    # Provide candidate character grid
    cand_grid = CandidateCharacterGrid(
        grid_id="g1",
        observations=[
            RawCharacterObservation(char="P", bbox_preprocessed=(300, 200, 320, 240)),
            RawCharacterObservation(char="A", bbox_preprocessed=(325, 200, 345, 240)),
            RawCharacterObservation(char="N", bbox_preprocessed=(350, 200, 370, 240)),
        ],
    )

    output = pipeline.process_handoff(
        handoff,
        candidate_tables_by_page={1: [cand_tbl]},
        candidate_checkboxes_by_page={1: [cand_cb]},
        candidate_grids_by_page={1: [cand_grid]},
    )

    p1 = output.pages[0]
    assert len(p1.tables) == 1
    assert "<table>" in p1.tables[0].html_content
    assert len(p1.checkboxes) == 1
    assert p1.checkboxes[0].state == "[x]"
    assert len(p1.character_grids) == 1
    assert p1.character_grids[0].reconstructed_text == "PAN"


# ----------------------------------------------------------------------------
# 3. UNKNOWN / Low Confidence Routing
# ----------------------------------------------------------------------------

def test_pipeline_unknown_and_low_confidence_routes_to_needs_review(temp_storage):
    """Empty or unrecognised documents route strictly to 'needs_review' with schema_id=None."""
    # Blank OCR extractor -> UNKNOWN
    parser = LocalOCRParserAdapter(raw_extractor=lambda img: [])
    pipeline = ModuleBPipeline(storage=temp_storage, parser=parser, tenant_id="tenant-test")

    handoff = _create_synthetic_handoff_files(temp_storage, ["Page 1"])
    output = pipeline.process_handoff(handoff)

    assert output.status == "needs_review"
    assert output.classification.domain == MVPClassificationDomain.UNKNOWN
    assert output.classification.document_type == MVPDocumentType.UNKNOWN
    assert output.classification.schema_id is None
    assert output.classification.schema_version is None
    assert output.classification.review_required is True


def test_pipeline_missing_approved_schema_routes_to_needs_review(temp_storage):
    """A recognized document type with no active schema routes to 'needs_review'."""
    parser = LocalOCRParserAdapter(raw_extractor=lambda img: [
        RawOCRToken(text="BANK ACCOUNT OPENING FORM CASA Application", bbox_preprocessed=(50, 50, 400, 100), confidence=0.98),
        RawOCRToken(text="Customer Information Form CIF Initial Deposit", bbox_preprocessed=(50, 110, 400, 150), confidence=0.98),
    ])
    # Empty schema registry
    empty_registry = InMemorySchemaRegistry(schemas={})
    pipeline = ModuleBPipeline(storage=temp_storage, parser=parser, schema_registry=empty_registry, tenant_id="tenant-test")

    handoff = _create_synthetic_handoff_files(temp_storage, ["Page 1"])
    output = pipeline.process_handoff(handoff)

    # Document type was recognized as DOC-01, but lack of schema forces review
    assert output.status == "needs_review"
    assert output.classification.document_type == MVPDocumentType.DOC_01_CASA
    assert output.classification.schema_id is None
    assert output.classification.review_required is True


# ----------------------------------------------------------------------------
# 4. Fail-Closed Error Handling
# ----------------------------------------------------------------------------

def test_pipeline_unoperational_parser_fails_closed(temp_storage):
    """When parser engine fails, pipeline returns status='failed' with error envelope."""
    parser = LocalOCRParserAdapter(is_operational=False)
    pipeline = ModuleBPipeline(storage=temp_storage, parser=parser, tenant_id="tenant-test")

    handoff = _create_synthetic_handoff_files(temp_storage, ["Page 1"])
    output = pipeline.process_handoff(handoff)

    assert output.status == "failed"
    assert output.error is not None
    assert output.error["code"] == "PARSER_UNAVAILABLE"


# ----------------------------------------------------------------------------
# 5. Idempotency Key Replay Support
# ----------------------------------------------------------------------------

def test_pipeline_idempotency_cache_hit(temp_storage):
    """Replaying identical request with idempotency_key returns cached result without re-parsing."""
    idempotency_store = LocalIdempotencyStore(base_dir=temp_storage.base_dir)
    call_count = 0

    def counting_extractor(img: np.ndarray) -> List[RawOCRToken]:
        nonlocal call_count
        call_count += 1
        return [
            RawOCRToken(text="HOSPITAL DISCHARGE SUMMARY Date of Admission", bbox_preprocessed=(50, 50, 400, 100), confidence=0.95),
            RawOCRToken(text="Final Diagnosis Date of Discharge", bbox_preprocessed=(50, 110, 400, 150), confidence=0.95),
        ]

    parser = LocalOCRParserAdapter(raw_extractor=counting_extractor)
    pipeline = ModuleBPipeline(
        storage=temp_storage,
        parser=parser,
        idempotency_store=idempotency_store,
        tenant_id="tenant-test",
    )
    handoff = _create_synthetic_handoff_files(temp_storage, ["Page 1"])

    # Run 1
    out_1 = pipeline.process_handoff(handoff, idempotency_key="idem-key-999")
    assert out_1.status == "processing"
    assert call_count == 1

    # Run 2 with same idempotency key
    out_2 = pipeline.process_handoff(handoff, idempotency_key="idem-key-999")
    assert out_2.run_id == out_1.run_id
    assert out_2.document_id == out_1.document_id
    assert call_count == 1  # OCR parser was not invoked a second time!


# ----------------------------------------------------------------------------
# 6. Workstream 3: Reviewer Override Foundation (FR-CLS-004)
# ----------------------------------------------------------------------------

def test_reviewer_override_success(temp_storage):
    """Reviewer overrides an UNKNOWN document to DOC-01 CASA form, emitting new run."""
    parser = LocalOCRParserAdapter(raw_extractor=lambda img: [])
    pipeline = ModuleBPipeline(storage=temp_storage, parser=parser, tenant_id="tenant-test")
    handoff = _create_synthetic_handoff_files(temp_storage, ["Page 1"])

    orig_payload = pipeline.process_handoff(handoff)
    assert orig_payload.status == "needs_review"
    assert orig_payload.classification.document_type == MVPDocumentType.UNKNOWN
    assert orig_payload.classification.schema_id is None

    # Reviewer inspects page and applies override
    req = ReviewerOverrideRequest(
        document_id=orig_payload.document_id,
        tenant_id="tenant-test",
        reviewer_id="reviewer_alice",
        new_document_type=MVPDocumentType.DOC_01_CASA,
        override_reason="Scanned form header was faint but clearly is CASA Account Opening Form",
    )

    overridden_payload = pipeline.apply_reviewer_override(orig_payload, req)

    # 1. New run has correct bound schema and status='processing'
    assert overridden_payload.status == "processing"
    assert overridden_payload.classification.document_type == MVPDocumentType.DOC_01_CASA
    assert overridden_payload.classification.domain == MVPClassificationDomain.BFSI
    assert overridden_payload.classification.schema_id == "DOC-01"
    assert overridden_payload.classification.schema_version == "1.0.0"
    assert overridden_payload.classification.review_required is False
    assert overridden_payload.run_id != orig_payload.run_id
    assert overridden_payload.extra_metadata["prior_run_id"] == orig_payload.run_id
    assert overridden_payload.extra_metadata["reviewer_id"] == "reviewer_alice"

    # 2. Prior run payload remains completely immutable
    assert orig_payload.status == "needs_review"
    assert orig_payload.classification.document_type == MVPDocumentType.UNKNOWN
    assert orig_payload.classification.schema_id is None


def test_reviewer_override_tenant_mismatch_raises_error(temp_storage):
    """Override request from wrong tenant fails with StorageError."""
    pipeline = ModuleBPipeline(storage=temp_storage, tenant_id="tenant-test")
    handoff = _create_synthetic_handoff_files(temp_storage, ["Page 1"])
    orig_payload = pipeline.process_handoff(handoff)

    req = ReviewerOverrideRequest(
        document_id=orig_payload.document_id,
        tenant_id="attacker-tenant",  # Mismatch
        reviewer_id="reviewer_alice",
        new_document_type=MVPDocumentType.DOC_01_CASA,
        override_reason="Legitimate reason",
    )

    with pytest.raises(StorageError, match="Tenant isolation violation"):
        pipeline.apply_reviewer_override(orig_payload, req)


def test_reviewer_override_missing_reviewer_or_reason_raises_error(temp_storage):
    """Override without reviewer ID or reason is rejected (FR-CLS-004 auditability)."""
    pipeline = ModuleBPipeline(storage=temp_storage, tenant_id="tenant-test")
    handoff = _create_synthetic_handoff_files(temp_storage, ["Page 1"])
    orig_payload = pipeline.process_handoff(handoff)

    # Missing reviewer
    req_no_rev = ReviewerOverrideRequest(
        document_id=orig_payload.document_id,
        tenant_id="tenant-test",
        reviewer_id="",
        new_document_type=MVPDocumentType.DOC_01_CASA,
        override_reason="Reason given",
    )
    with pytest.raises(DocumentProcessingError, match="Reviewer ID is required"):
        pipeline.apply_reviewer_override(orig_payload, req_no_rev)

    # Missing reason
    req_no_reason = ReviewerOverrideRequest(
        document_id=orig_payload.document_id,
        tenant_id="tenant-test",
        reviewer_id="reviewer_alice",
        new_document_type=MVPDocumentType.DOC_01_CASA,
        override_reason="   ",
    )
    with pytest.raises(DocumentProcessingError, match="Override reason is required"):
        pipeline.apply_reviewer_override(orig_payload, req_no_reason)
