"""
Tests for Module B Stage 4A: Document Classification & Schema Binding Service Foundation.

Covers:
- ClassifierEngine interface contract and operational readiness checks.
- SchemaRegistry resolution and rejection of invalid / inactive / UNKNOWN schemas.
- DocumentClassificationService enforcing PRD invariants:
  - FR-CLS-001: High-confidence recognized document correctly binds schema (DOC-01, DOC-02, DOC-03).
  - FR-CLS-002 / GR-004: Low confidence (< 0.85 default) forces document_type=UNKNOWN,
    domain=Unknown, schema=None, review_required=True.
  - FR-CLS-002: Unknown document type forces domain=Unknown, schema=None, review_required=True.
  - FR-CLS-003: Binding of approved schema versions, rejection of inactive / missing schemas forces review.
  - ConfidenceProvenance preservation (MODEL vs RULE_HEURISTIC vs UNCALIBRATED).
  - ModuleBOutputEnvelope alignment with ClassificationOutput (review_required -> status="needs_review").
"""

from typing import List, Optional
import pytest

from doc_extractor.classification.base import (
    ApprovedSchemaRef,
    ClassificationEngineError,
    ClassifierEngine,
    ClassifierMetadata,
    ClassifierRawResult,
    DocumentClassificationService,
    InMemorySchemaRegistry,
    SchemaResolutionError,
)
import numpy as np
from doc_extractor.contracts_b import (
    ClassificationOutput,
    ConfidenceProvenance,
    ModuleAPageRef,
    ModuleBInputPayload,
    ModuleBOutputPayload,
    MVPClassificationDomain,
    MVPDocumentType,
    PageLayoutOutput,
)
from doc_extractor.loader_b import LoadedPageImage, ModuleBLoadResult


class MockClassifierEngine(ClassifierEngine):
    """Configurable mock classifier for testing interface and service behavior."""

    def __init__(
        self,
        domain: MVPClassificationDomain = MVPClassificationDomain.BFSI,
        document_type: MVPDocumentType = MVPDocumentType.DOC_01_CASA,
        confidence: float = 0.95,
        provenance: ConfidenceProvenance = ConfidenceProvenance.MODEL,
        operational: bool = True,
        classifier_name: str = "mock-classifier-v1",
    ) -> None:
        self._domain = domain
        self._document_type = document_type
        self._confidence = confidence
        self._provenance = provenance
        self._operational = operational
        self._classifier_name = classifier_name

    def classify(
        self,
        load_result: ModuleBLoadResult,
        parsed_pages: List[PageLayoutOutput],
    ) -> ClassifierRawResult:
        return ClassifierRawResult(
            domain=self._domain,
            document_type=self._document_type,
            classification_confidence=self._confidence,
            confidence_provenance=self._provenance,
            classifier_identity=self._classifier_name,
        )

    def is_operational(self) -> bool:
        return self._operational

    def get_metadata(self) -> ClassifierMetadata:
        return ClassifierMetadata(
            classifier_name=self._classifier_name,
            classifier_version="1.0.0",
            supported_domains=(MVPClassificationDomain.BFSI, MVPClassificationDomain.HEALTHCARE),
            supported_types=(MVPDocumentType.DOC_01_CASA, MVPDocumentType.DOC_03_DISCHARGE_SUMMARY),
            is_operational=self._operational,
            confidence_provenance=self._provenance,
            calibrated_probabilities=True,
        )


from pathlib import Path

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


def test_classifier_metadata_and_interface_conformance():
    """Validates ClassifierEngine metadata and operational reporting."""
    engine = MockClassifierEngine()
    meta = engine.get_metadata()
    assert meta.classifier_name == "mock-classifier-v1"
    assert meta.classifier_version == "1.0.0"
    assert meta.calibrated_probabilities is True
    assert engine.is_operational() is True


def test_schema_registry_default_mvp_bindings():
    """Validates InMemorySchemaRegistry default mappings for MVP documents."""
    registry = InMemorySchemaRegistry()
    casa_schema = registry.resolve_approved_schema(MVPDocumentType.DOC_01_CASA)
    assert casa_schema.schema_id == "DOC-01"
    assert casa_schema.schema_version == "1.0.0"
    assert casa_schema.is_active is True

    discharge_schema = registry.resolve_approved_schema(MVPDocumentType.DOC_03_DISCHARGE_SUMMARY)
    assert discharge_schema.schema_id == "DOC-03"
    assert discharge_schema.schema_version == "1.0.0"

    assert registry.is_schema_approved("DOC-01", "1.0.0") is True
    assert registry.is_schema_approved("DOC-01", "2.0.0") is False


def test_schema_registry_unknown_doc_type_raises_error():
    """Validates that SchemaRegistry immediately rejects UNKNOWN document type (FR-CLS-002)."""
    registry = InMemorySchemaRegistry()
    with pytest.raises(SchemaResolutionError) as exc_info:
        registry.resolve_approved_schema(MVPDocumentType.UNKNOWN)
    assert exc_info.value.code == "UNKNOWN_DOC_TYPE_NO_SCHEMA"


def test_schema_registry_inactive_schema_raises_error():
    """Validates that SchemaRegistry rejects schemas marked inactive."""
    inactive_schemas = {
        MVPDocumentType.DOC_01_CASA: ApprovedSchemaRef(
            schema_id="DOC-01",
            schema_version="0.9.0",
            document_type=MVPDocumentType.DOC_01_CASA,
            is_active=False,
        )
    }
    registry = InMemorySchemaRegistry(schemas=inactive_schemas)
    with pytest.raises(SchemaResolutionError) as exc_info:
        registry.resolve_approved_schema(MVPDocumentType.DOC_01_CASA)
    assert exc_info.value.code == "NO_APPROVED_SCHEMA"


def test_classification_service_successful_binding():
    """FR-CLS-001 & FR-CLS-003: High-confidence recognized document correctly binds approved schema."""
    engine = MockClassifierEngine(
        domain=MVPClassificationDomain.BFSI,
        document_type=MVPDocumentType.DOC_01_CASA,
        confidence=0.96,
        provenance=ConfidenceProvenance.MODEL,
    )
    registry = InMemorySchemaRegistry()
    service = DocumentClassificationService(classifier=engine, schema_registry=registry)

    load_result = _build_dummy_load_result()
    out = service.classify_and_bind(load_result, parsed_pages=[])

    assert out.domain == MVPClassificationDomain.BFSI
    assert out.document_type == MVPDocumentType.DOC_01_CASA
    assert out.classification_confidence == 0.96
    assert out.confidence_provenance == ConfidenceProvenance.MODEL
    assert out.schema_id == "DOC-01"
    assert out.schema_version == "1.0.0"
    assert out.review_required is False
    assert out.classifier_identity == "mock-classifier-v1"


def test_classification_service_below_threshold_forces_unknown_and_review():
    """FR-CLS-002 & GR-004: Low confidence (< 0.85) forces UNKNOWN, domain=Unknown, schema=None, review_required=True."""
    engine = MockClassifierEngine(
        domain=MVPClassificationDomain.BFSI,
        document_type=MVPDocumentType.DOC_01_CASA,
        confidence=0.72,  # Below 0.85 default threshold
        provenance=ConfidenceProvenance.RULE_HEURISTIC,
    )
    registry = InMemorySchemaRegistry()
    service = DocumentClassificationService(classifier=engine, schema_registry=registry, confidence_threshold=0.85)

    load_result = _build_dummy_load_result()
    out = service.classify_and_bind(load_result, parsed_pages=[])

    assert out.domain == MVPClassificationDomain.UNKNOWN
    assert out.document_type == MVPDocumentType.UNKNOWN
    assert out.classification_confidence == 0.72
    assert out.confidence_provenance == ConfidenceProvenance.RULE_HEURISTIC
    assert out.schema_id is None
    assert out.schema_version is None
    assert out.review_required is True


def test_classification_service_unknown_type_forces_review_and_no_schema():
    """FR-CLS-002: UNKNOWN document type from classifier forces schema=None and review_required=True."""
    engine = MockClassifierEngine(
        domain=MVPClassificationDomain.UNKNOWN,
        document_type=MVPDocumentType.UNKNOWN,
        confidence=0.99,  # Even if confidence is nominally high, UNKNOWN must route to review
        provenance=ConfidenceProvenance.UNCALIBRATED,
    )
    registry = InMemorySchemaRegistry()
    service = DocumentClassificationService(classifier=engine, schema_registry=registry)

    load_result = _build_dummy_load_result()
    out = service.classify_and_bind(load_result, parsed_pages=[])

    assert out.domain == MVPClassificationDomain.UNKNOWN
    assert out.document_type == MVPDocumentType.UNKNOWN
    assert out.schema_id is None
    assert out.schema_version is None
    assert out.review_required is True
    assert out.confidence_provenance == ConfidenceProvenance.UNCALIBRATED


def test_classification_service_missing_approved_schema_forces_review():
    """FR-CLS-003 & GR-004: Recognized document with missing/inactive schema routes to review without schema."""
    engine = MockClassifierEngine(
        domain=MVPClassificationDomain.HEALTHCARE,
        document_type=MVPDocumentType.DOC_03_DISCHARGE_SUMMARY,
        confidence=0.94,
    )
    # Registry has no schemas configured
    empty_registry = InMemorySchemaRegistry(schemas={})
    service = DocumentClassificationService(classifier=engine, schema_registry=empty_registry)

    load_result = _build_dummy_load_result()
    out = service.classify_and_bind(load_result, parsed_pages=[])

    # Recognizes type but cannot bind schema, so must trigger review
    assert out.domain == MVPClassificationDomain.HEALTHCARE
    assert out.document_type == MVPDocumentType.DOC_03_DISCHARGE_SUMMARY
    assert out.schema_id is None
    assert out.schema_version is None
    assert out.review_required is True


def test_classifier_unoperational_raises_classification_engine_error():
    """Validates that an unavailable/unhealthy classifier engine raises ClassificationEngineError."""
    engine = MockClassifierEngine(operational=False, classifier_name="offline-engine")
    registry = InMemorySchemaRegistry()
    service = DocumentClassificationService(classifier=engine, schema_registry=registry)

    load_result = _build_dummy_load_result()
    with pytest.raises(ClassificationEngineError) as exc_info:
        service.classify_and_bind(load_result, parsed_pages=[])
    assert exc_info.value.code == "CLASSIFIER_UNAVAILABLE"
    assert exc_info.value.details["classifier_name"] == "offline-engine"


def test_classification_output_envelope_status_alignment():
    """Validates that ModuleBOutputPayload status respects review_required flag from ClassificationOutput."""
    review_output = ClassificationOutput(
        domain=MVPClassificationDomain.UNKNOWN,
        document_type=MVPDocumentType.UNKNOWN,
        classification_confidence=0.5,
        confidence_provenance=ConfidenceProvenance.UNCALIBRATED,
        schema_id=None,
        schema_version=None,
        review_required=True,
    )
    # When review_required is True, status cannot be "processing" (SCH-012)
    with pytest.raises(ValueError, match="Expected 'needs_review'"):
        ModuleBOutputPayload(
            run_id="run-1",
            document_id="doc-test-123",
            tenant_id="tenant-1",
            status="processing",
            classification=review_output,
            pages=[],
            combined_markdown="",
            parser_model_name="mock-parser",
            classifier_model_name="mock-classifier",
            processing_time_ms=120.0,
            created_at="2026-10-29T10:00:00Z",
            completed_at="2026-10-29T10:00:01Z",
        )

    valid_review_payload = ModuleBOutputPayload(
        run_id="run-1",
        document_id="doc-test-123",
        tenant_id="tenant-1",
        status="needs_review",
        classification=review_output,
        pages=[],
        combined_markdown="",
        parser_model_name="mock-parser",
        classifier_model_name="mock-classifier",
        processing_time_ms=120.0,
        created_at="2026-10-29T10:00:00Z",
        completed_at="2026-10-29T10:00:01Z",
    )
    assert valid_review_payload.status == "needs_review"
