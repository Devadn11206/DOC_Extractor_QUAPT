"""
Audit checkpoints, asymmetric signing, and immutable storage infrastructure (Team Member 3, Phase 4C).
"""

from doc_extractor.infrastructure.audit.errors import (
    AuditCheckpointError,
    CheckpointConfigurationError,
    CheckpointConflictError,
    CheckpointSignatureInvalidError,
    CheckpointSigningError,
    CheckpointSinkError,
    CheckpointSinkUnavailableError,
    CheckpointVerificationError,
    ObjectLockNotSupportedError,
)
from doc_extractor.infrastructure.audit.kms_signer import AwsKmsAuditSigner
from doc_extractor.infrastructure.audit.models import (
    CHECKPOINT_FORMAT_VERSION,
    AuditCheckpoint,
    CheckpointVerificationReport,
    CheckpointVerificationStatus,
)
from doc_extractor.infrastructure.audit.service import AuditCheckpointService
from doc_extractor.infrastructure.audit.signer import (
    AuditCheckpointSigner,
    AuditCheckpointVerifier,
    FakeAuditSigner,
    IndependentCheckpointVerifier,
)
from doc_extractor.infrastructure.audit.sink import (
    AuditCheckpointSink,
    EphemeralCheckpointSink,
    S3ObjectLockCheckpointSink,
)
from doc_extractor.infrastructure.audit.verification import AuditChainVerifier

__all__ = [
    "CHECKPOINT_FORMAT_VERSION",
    "AuditChainVerifier",
    "AuditCheckpoint",
    "AuditCheckpointError",
    "AuditCheckpointService",
    "AuditCheckpointSigner",
    "AuditCheckpointSink",
    "AuditCheckpointVerifier",
    "AwsKmsAuditSigner",
    "CheckpointConfigurationError",
    "CheckpointConflictError",
    "CheckpointSignatureInvalidError",
    "CheckpointSigningError",
    "CheckpointSinkError",
    "CheckpointSinkUnavailableError",
    "CheckpointVerificationError",
    "CheckpointVerificationReport",
    "CheckpointVerificationStatus",
    "EphemeralCheckpointSink",
    "FakeAuditSigner",
    "IndependentCheckpointVerifier",
    "ObjectLockNotSupportedError",
    "S3ObjectLockCheckpointSink",
]
