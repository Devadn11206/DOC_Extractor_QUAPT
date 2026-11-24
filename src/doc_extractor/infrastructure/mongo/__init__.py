"""
MongoDB foundation (Team Member 3, Phase 2A): settings, one connection per process, a tenant-scoped repository base,
safe serialization and the central index registry, and the review-task and API-key repositories (``review_tasks.MongoReviewTaskRepository``,
``api_keys.MongoApiKeyRepository``, imported from their modules, not from here). The durable audit repository is ``audit.MongoAuditRepository`` (Phase 2B), imported from its module too.

Importing this package does not import the ``pymongo`` driver.
"""

from doc_extractor.infrastructure.mongo.connection import MongoConnection, MongoHealth, build_client_options
from doc_extractor.infrastructure.mongo.errors import (
    DuplicateKeyPersistenceError,
    IndexConflictError,
    PersistenceConfigurationError,
    PersistenceDataError,
    PersistenceError,
    PersistenceUnavailableError,
    TenantScopeError,
    TransactionUnavailableError,
    UnsafeQueryError,
    translate_driver_error,
)
from doc_extractor.infrastructure.mongo.indexes import (
    DEFAULT_REGISTRY,
    IndexBootstrapReport,
    IndexRegistry,
    IndexSpec,
    ensure_indexes,
)
from doc_extractor.infrastructure.mongo.repository import TenantScopedRepository
from doc_extractor.infrastructure.mongo.serialization import from_document, to_document
from doc_extractor.infrastructure.mongo.settings import MongoSettings

__all__ = [
    "DEFAULT_REGISTRY",
    "DuplicateKeyPersistenceError",
    "IndexBootstrapReport",
    "IndexConflictError",
    "IndexRegistry",
    "IndexSpec",
    "MongoConnection",
    "MongoHealth",
    "MongoSettings",
    "PersistenceConfigurationError",
    "PersistenceDataError",
    "PersistenceError",
    "PersistenceUnavailableError",
    "TenantScopedRepository",
    "TenantScopeError",
    "TransactionUnavailableError",
    "UnsafeQueryError",
    "build_client_options",
    "ensure_indexes",
    "from_document",
    "to_document",
    "translate_driver_error",
]
