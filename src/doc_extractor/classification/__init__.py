"""
Module B Document Classification and Schema Binding Package.

Exports the core interfaces and services for document classification,
confidence provenance, schema binding, and routing invariant enforcement.
"""

from doc_extractor.classification.base import (
    ApprovedSchemaRef,
    ClassificationEngineError,
    ClassifierEngine,
    ClassifierMetadata,
    ClassifierRawResult,
    DocumentClassificationService,
    InMemorySchemaRegistry,
    SchemaRegistry,
    SchemaResolutionError,
)
from doc_extractor.classification.rules import (
    RuleBasedClassifierAdapter,
    RuleSignal,
)

__all__ = [
    "ApprovedSchemaRef",
    "ClassificationEngineError",
    "ClassifierEngine",
    "ClassifierMetadata",
    "ClassifierRawResult",
    "DocumentClassificationService",
    "InMemorySchemaRegistry",
    "RuleBasedClassifierAdapter",
    "RuleSignal",
    "SchemaRegistry",
    "SchemaResolutionError",
]

