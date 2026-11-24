"""
Object storage infrastructure package (Team Member 3, Phase 4A).

Provides S3-compatible, tenant-isolated object storage adapter, configuration settings,
and typed storage exceptions.
"""

from doc_extractor.infrastructure.storage.adapter import S3Storage
from doc_extractor.infrastructure.storage.envelope import (
    compute_aad,
    decrypt_envelope,
    encrypt_envelope,
    is_envelope_encrypted,
    rewrap_envelope,
)
from doc_extractor.infrastructure.storage.errors import (
    CrossTenantStorageAccessError,
    InvalidTenantIdentifierError,
    ObjectNotFoundError,
    ObjectStorageConfigurationError,
    ObjectStorageError,
    ObjectStorageUnavailableError,
)
from doc_extractor.infrastructure.storage.kms import (
    AwsKmsProvider,
    DataKey,
    FakeKmsProvider,
    KmsAuthenticationError,
    KmsConfigurationError,
    KmsError,
    KmsKeyNotFoundError,
    KmsProvider,
    KmsUnavailableError,
)
from doc_extractor.infrastructure.storage.settings import S3StorageSettings

__all__ = [
    "AwsKmsProvider",
    "CrossTenantStorageAccessError",
    "DataKey",
    "FakeKmsProvider",
    "InvalidTenantIdentifierError",
    "KmsAuthenticationError",
    "KmsConfigurationError",
    "KmsError",
    "KmsKeyNotFoundError",
    "KmsProvider",
    "KmsUnavailableError",
    "ObjectNotFoundError",
    "ObjectStorageConfigurationError",
    "ObjectStorageError",
    "ObjectStorageUnavailableError",
    "S3Storage",
    "S3StorageSettings",
    "compute_aad",
    "decrypt_envelope",
    "encrypt_envelope",
    "is_envelope_encrypted",
    "rewrap_envelope",
]
