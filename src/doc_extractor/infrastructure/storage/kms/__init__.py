"""
KMS and HSM abstractions package (Team Member 3, Phase 4B).
"""

from doc_extractor.infrastructure.storage.kms.aws_kms import AwsKmsProvider
from doc_extractor.infrastructure.storage.kms.base import DataKey, KmsProvider
from doc_extractor.infrastructure.storage.kms.errors import (
    KmsAuthenticationError,
    KmsConfigurationError,
    KmsError,
    KmsKeyNotFoundError,
    KmsUnavailableError,
)
from doc_extractor.infrastructure.storage.kms.fake_kms import FakeKmsProvider

__all__ = [
    "AwsKmsProvider",
    "DataKey",
    "FakeKmsProvider",
    "KmsAuthenticationError",
    "KmsConfigurationError",
    "KmsError",
    "KmsKeyNotFoundError",
    "KmsProvider",
    "KmsUnavailableError",
]
