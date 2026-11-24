"""
Typed exceptions for audit checkpoints, signing, and external immutable storage (Team Member 3, Phase 4C).

Adheres to:
- PRD SEC-008: Independently verifiable audit checkpoints and tamper-resistant storage.
- Fixed reason codes: Error details carry machine-readable tokens and never echo sensitive payloads,
  raw key material, or credentials.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from doc_extractor.exceptions import StorageError


class AuditCheckpointError(StorageError):
    """Base class for all audit checkpoint and verification errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        fixed_details = {"reason": message}
        if details:
            fixed_details.update({k: v for k, v in details.items() if k != "reason"})
        super().__init__(message=f"Audit checkpoint error: {message}.", details=fixed_details)


class CheckpointSigningError(AuditCheckpointError):
    """Raised when asymmetric signing of a checkpoint fails."""
    pass


class CheckpointVerificationError(AuditCheckpointError):
    """Raised when checkpoint verification fails or is malformed."""
    pass


class CheckpointSignatureInvalidError(CheckpointVerificationError):
    """Raised when a checkpoint's cryptographic signature does not verify."""
    pass


class CheckpointSinkError(AuditCheckpointError):
    """Base class for external checkpoint sink errors."""
    pass


class CheckpointSinkUnavailableError(CheckpointSinkError):
    """Raised when external immutable storage is unreachable or down."""
    pass


class CheckpointConflictError(CheckpointSinkError):
    """Raised when an external sink detects conflicting checkpoint data at an existing sequence."""
    pass


class CheckpointConfigurationError(AuditCheckpointError):
    """Raised when checkpoint signing or sink configuration is invalid or missing required security settings."""

    def __init__(self, reason: str, setting: Optional[str] = None) -> None:
        details: Dict[str, Any] = {"reason": reason}
        if setting:
            details["setting"] = setting
        super().__init__(message=reason, details=details)


class ObjectLockNotSupportedError(CheckpointConfigurationError):
    """Raised when immutable storage is required but the S3 destination does not support Object Lock."""
    pass
