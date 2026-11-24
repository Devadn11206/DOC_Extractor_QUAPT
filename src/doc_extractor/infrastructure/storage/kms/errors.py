"""
Typed errors for KMS and HSM key management operations (Team Member 3, Phase 4B).

Adheres to:
- PRD SEC-005: KMS-backed key management and rotation.
- PRD §7.2 API-010 / API-011: Sanitized error messages; never disclose plaintext DEKs,
  raw KEKs, customer secrets, or internal credential parameters.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from doc_extractor.exceptions import ConfigurationError, StorageError, TenantStorageUnavailableError


def _details(reason: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    details: Dict[str, Any] = {"reason": reason}
    if extra:
        details.update(extra)
    return details


class KmsError(StorageError):
    """Base class for all operational errors originating from KMS/HSM providers."""

    default_message = "A KMS key operation failed."
    default_reason = "kms_operation_failed"
    default_code = "KMS_FAILED"

    def __init__(self, reason: Optional[str] = None, *, details: Optional[Dict[str, Any]] = None) -> None:
        merged = _details(reason or self.default_reason, details)
        super().__init__(message=self.default_message, code=self.default_code, details=merged)


class KmsUnavailableError(KmsError, TenantStorageUnavailableError):
    """The KMS/HSM service is unreachable, connection timed out, or endpoint refused."""

    default_message = "The KMS key service is unavailable."
    default_reason = "kms_unavailable"
    default_code = "KMS_UNAVAILABLE"

    def __init__(self, reason: Optional[str] = None, *, details: Optional[Dict[str, Any]] = None) -> None:
        TenantStorageUnavailableError.__init__(self, reason=reason or self.default_reason)
        self.message = self.default_message
        self.code = self.default_code
        if details:
            self.details.update(details)


class KmsAuthenticationError(KmsError):
    """KMS access was denied or authentication credentials are invalid."""

    default_message = "KMS key access was denied."
    default_reason = "kms_access_denied"
    default_code = "KMS_ACCESS_DENIED"


class KmsKeyNotFoundError(KmsError):
    """The requested KEK or key version was not found, has been disabled, or is revoked."""

    default_message = "The requested KMS key was not found or is disabled."
    default_reason = "kms_key_not_found"
    default_code = "KMS_KEY_NOT_FOUND"


class KmsConfigurationError(ConfigurationError):
    """KMS configuration is invalid, incomplete, or violates environment security policies."""

    def __init__(self, reason: str, *, setting: Optional[str] = None) -> None:
        extra = {"setting": setting} if setting else None
        super().__init__(message="The KMS configuration is not valid.", details=_details(reason, extra))
