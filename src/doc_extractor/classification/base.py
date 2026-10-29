"""
Pluggable Document Classifier and Schema Registry Interfaces.

Establishes:
- ClassifierEngine interface for domain and document-type classification (FR-CLS-001, ARC-002).
- SchemaRegistry interface for resolving approved extraction schemas (FR-CLS-003, SCH-001..007).
- Fail-safe uncertainty and routing policy enforcement (FR-CLS-002, GR-004).
- Distinguishes model-evaluated confidence from heuristic or uncalibrated scores.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from doc_extractor.contracts_b import (
    ClassificationOutput,
    ConfidenceProvenance,
    MVPClassificationDomain,
    MVPDocumentType,
    PageLayoutOutput,
)
from doc_extractor.exceptions import DocumentExtractorError, DocumentProcessingError
from doc_extractor.loader_b import LoadedPageImage, ModuleBLoadResult


class ClassificationEngineError(DocumentProcessingError):
    """Raised when the classifier engine encounters an internal failure."""
    def __init__(
        self,
        message: str,
        classifier_name: str,
        code: str = "CLASSIFIER_ENGINE_ERROR",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        merged = dict(details or {})
        merged["classifier_name"] = classifier_name
        super().__init__(message=message, code=code, details=merged)


class SchemaResolutionError(DocumentProcessingError):
    """Raised when an approved schema cannot be resolved or is invalid."""
    def __init__(
        self,
        message: str,
        document_type: str,
        code: str = "SCHEMA_RESOLUTION_ERROR",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        merged = dict(details or {})
        merged["document_type"] = document_type
        super().__init__(message=message, code=code, details=merged)


@dataclass(frozen=True)
class ApprovedSchemaRef:
    """Immutable reference to an approved schema binding (FR-CLS-003)."""
    schema_id: str
    schema_version: str
    document_type: MVPDocumentType
    is_active: bool = True


@dataclass(frozen=True)
class ClassifierMetadata:
    """Identity and capabilities metadata of a classification engine."""
    classifier_name: str
    classifier_version: str
    supported_domains: Tuple[MVPClassificationDomain, ...]
    supported_types: Tuple[MVPDocumentType, ...]
    is_operational: bool
    confidence_provenance: ConfidenceProvenance
    calibrated_probabilities: bool = False


@dataclass(frozen=True)
class ClassifierRawResult:
    """Raw document classification prediction prior to schema resolution and policy binding."""
    domain: MVPClassificationDomain
    document_type: MVPDocumentType
    classification_confidence: float
    confidence_provenance: ConfidenceProvenance
    classifier_identity: str
    classification_reasons: Tuple[str, ...] = ()


class ClassifierEngine(ABC):
    """
    Abstract Base Class for document classifiers (ARC-002).
    
    Accepts loaded page images and/or parsed page layouts and classifies
    the document into a domain and document type.
    """

    @abstractmethod
    def get_metadata(self) -> ClassifierMetadata:
        """Returns classifier identity and operational capabilities."""
        pass

    @abstractmethod
    def is_operational(self) -> bool:
        """Returns True if local classifier model/rules are ready."""
        pass

    @abstractmethod
    def classify(
        self,
        load_result: ModuleBLoadResult,
        parsed_pages: List[PageLayoutOutput],
    ) -> ClassifierRawResult:
        """
        Classifies the document into domain and document type.
        
        Guarantees:
        - Never guesses or forces a match when evidence is below threshold.
        - Returns MVPDocumentType.UNKNOWN with review_required=True when uncertain (FR-CLS-002).
        - Explicitly indicates confidence provenance (MODEL, HEURISTIC, or UNCALIBRATED).
        - Raises ClassificationEngineError on system/driver failures (fail closed).
        """
        pass


class SchemaRegistry(ABC):
    """
    Abstract interface for managing and resolving approved extraction schemas (FR-CLS-003).
    """

    @abstractmethod
    def resolve_approved_schema(
        self,
        document_type: MVPDocumentType,
        tenant_id: Optional[str] = None,
    ) -> ApprovedSchemaRef:
        """
        Resolves the approved schema reference for a recognized document type.
        
        Raises:
            SchemaResolutionError: If document_type is UNKNOWN, or no approved schema exists.
        """
        pass

    @abstractmethod
    def is_schema_approved(
        self,
        schema_id: str,
        schema_version: str,
        tenant_id: Optional[str] = None,
    ) -> bool:
        """Checks if a specific schema version is approved for production extraction."""
        pass


class InMemorySchemaRegistry(SchemaRegistry):
    """
    Reference/test-double implementation of SchemaRegistry for local development.
    """

    def __init__(self, schemas: Optional[Dict[MVPDocumentType, ApprovedSchemaRef]] = None) -> None:
        # Default MVP registry with standard approved versions
        default_schemas = {
            MVPDocumentType.DOC_01_CASA: ApprovedSchemaRef(
                schema_id="DOC-01",
                schema_version="1.0.0",
                document_type=MVPDocumentType.DOC_01_CASA,
                is_active=True,
            ),
            MVPDocumentType.DOC_02_INSURANCE_APP: ApprovedSchemaRef(
                schema_id="DOC-02",
                schema_version="1.0.0",
                document_type=MVPDocumentType.DOC_02_INSURANCE_APP,
                is_active=True,
            ),
            MVPDocumentType.DOC_03_DISCHARGE_SUMMARY: ApprovedSchemaRef(
                schema_id="DOC-03",
                schema_version="1.0.0",
                document_type=MVPDocumentType.DOC_03_DISCHARGE_SUMMARY,
                is_active=True,
            ),
        }
        self._schemas: Dict[MVPDocumentType, ApprovedSchemaRef] = schemas if schemas is not None else default_schemas

    def resolve_approved_schema(
        self,
        document_type: MVPDocumentType,
        tenant_id: Optional[str] = None,
    ) -> ApprovedSchemaRef:
        if document_type == MVPDocumentType.UNKNOWN:
            raise SchemaResolutionError(
                message="Cannot resolve schema for UNKNOWN document type (FR-CLS-002).",
                document_type=document_type.value,
                code="UNKNOWN_DOC_TYPE_NO_SCHEMA",
            )

        ref = self._schemas.get(document_type)
        if ref is None or not ref.is_active:
            raise SchemaResolutionError(
                message=f"No approved schema active for document type '{document_type.value}'.",
                document_type=document_type.value,
                code="NO_APPROVED_SCHEMA",
            )
        return ref

    def is_schema_approved(
        self,
        schema_id: str,
        schema_version: str,
        tenant_id: Optional[str] = None,
    ) -> bool:
        for ref in self._schemas.values():
            if ref.schema_id == schema_id and ref.schema_version == schema_version and ref.is_active:
                return True
        return False


class DocumentClassificationService:
    """
    Coordinates classification and approved schema binding (Stage 4A foundation).
    
    Enforces PRD rules:
    - FR-CLS-001: Classification into domain & type with confidence.
    - FR-CLS-002: Confidence < threshold or UNKNOWN -> review_required=True, schema=None (GR-004).
    - FR-CLS-003: Binding of approved schema version on document.
    """

    DEFAULT_CONFIDENCE_THRESHOLD = 0.85  # PRD benchmark target M4 >= 98%

    def __init__(
        self,
        classifier: ClassifierEngine,
        schema_registry: SchemaRegistry,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        self.classifier = classifier
        self.schema_registry = schema_registry
        self.confidence_threshold = confidence_threshold

    def classify_and_bind(
        self,
        load_result: ModuleBLoadResult,
        parsed_pages: List[PageLayoutOutput],
    ) -> ClassificationOutput:
        """
        Executes classification and binds approved schema according to policy.
        """
        if not self.classifier.is_operational():
            meta = self.classifier.get_metadata()
            raise ClassificationEngineError(
                message=f"Classifier engine '{meta.classifier_name}' is not operational.",
                classifier_name=meta.classifier_name,
                code="CLASSIFIER_UNAVAILABLE",
            )

        # 1. Execute classification
        raw_output = self.classifier.classify(load_result, parsed_pages)

        # 2. Check confidence threshold policy (FR-CLS-002, GR-004)
        if (
            raw_output.document_type == MVPDocumentType.UNKNOWN
            or raw_output.classification_confidence < self.confidence_threshold
        ):
            # Must fall back to UNKNOWN and require review; no extraction schema bound
            reasons = list(raw_output.classification_reasons)
            if raw_output.document_type != MVPDocumentType.UNKNOWN and raw_output.classification_confidence < self.confidence_threshold:
                reasons.append(
                    f"Confidence {raw_output.classification_confidence:.3f} below threshold {self.confidence_threshold:.3f}; routed to UNKNOWN review."
                )
            return ClassificationOutput(
                domain=MVPClassificationDomain.UNKNOWN,
                document_type=MVPDocumentType.UNKNOWN,
                classification_confidence=raw_output.classification_confidence,
                confidence_provenance=raw_output.confidence_provenance,
                schema_id=None,
                schema_version=None,
                review_required=True,
                classifier_identity=raw_output.classifier_identity,
                classification_reasons=reasons,
            )

        # 3. Resolve approved schema for recognized document type (FR-CLS-003)
        try:
            schema_ref = self.schema_registry.resolve_approved_schema(
                document_type=raw_output.document_type,
                tenant_id=load_result.tenant_id,
            )
            return ClassificationOutput(
                domain=raw_output.domain,
                document_type=raw_output.document_type,
                classification_confidence=raw_output.classification_confidence,
                confidence_provenance=raw_output.confidence_provenance,
                schema_id=schema_ref.schema_id,
                schema_version=schema_ref.schema_version,
                review_required=False,
                classifier_identity=raw_output.classifier_identity,
                classification_reasons=list(raw_output.classification_reasons),
            )
        except SchemaResolutionError:
            # If recognized type has no active approved schema, route to review (GR-004)
            reasons = list(raw_output.classification_reasons)
            reasons.append(f"No approved active schema found for {raw_output.document_type.value}; routing to review.")
            return ClassificationOutput(
                domain=raw_output.domain,
                document_type=raw_output.document_type,
                classification_confidence=raw_output.classification_confidence,
                confidence_provenance=raw_output.confidence_provenance,
                schema_id=None,
                schema_version=None,
                review_required=True,
                classifier_identity=raw_output.classifier_identity,
                classification_reasons=reasons,
            )
