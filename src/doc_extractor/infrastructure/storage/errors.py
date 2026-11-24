"""
Typed errors for the object storage adapter (Team Member 3, Phase 4A).

Adheres to:
- PRD ARC-003: Durable object storage under tenant and document ID boundaries.
- PRD SEC-005 / SEC-006: Tenant isolation; cross-tenant access prohibited.
- PRD §7.2 API-010 / API-011: Fixed error messages and machine-readable reasons;
  never disclose document contents, credentials, or internal secret keys in errors.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from doc_extractor.exceptions import (
    ConfigurationError,
    CrossTenantStorageAccessError,
    InvalidTenantIdentifierError,
    StorageError,
    TenantStorageUnavailableError,
)

__all__ = [
    "CrossTenantStorageAccessError",
    "InvalidTenantIdentifierError",
    "ObjectNotFoundError",
    "ObjectStorageConfigurationError",
    "ObjectStorageError",
    "ObjectStorageUnavailableError",
]


def _details(reason: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    details: Dict[str, Any] = {"reason": reason}
    if extra:
        details.update(extra)
    return details


class ObjectStorageError(StorageError):
    """Base class for all operational errors originating from the object storage adapter."""

    default_message = "An object storage operation failed."
    default_reason = "object_storage_failed"
    default_code = "OBJECT_STORAGE_FAILED"

    def __init__(self, reason: Optional[str] = None, *, details: Optional[Dict[str, Any]] = None) -> None:
        merged = _details(reason or self.default_reason, details)
        super().__init__(message=self.default_message, code=self.default_code, details=merged)


class ObjectStorageUnavailableError(ObjectStorageError, TenantStorageUnavailableError):
    """
    The remote object storage backend is unreachable, connection timed out, or endpoint refused.
    Inherits from TenantStorageUnavailableError so existing callers catching TenantStorageUnavailableError
    handle remote storage outages identically without special casing.
    """

    default_message = "The object storage service is unavailable."
    default_reason = "storage_unavailable"
    default_code = "OBJECT_STORAGE_UNAVAILABLE"

    def __init__(self, reason: Optional[str] = None, *, details: Optional[Dict[str, Any]] = None) -> None:
        # Pass reason to TenantStorageUnavailableError so details={"reason": reason} is populated
        TenantStorageUnavailableError.__init__(self, reason=reason or self.default_reason)
        # Update message and code from this class
        self.message = self.default_message
        self.code = self.default_code
        if details:
            self.details.update(details)


class ObjectNotFoundError(ObjectStorageError):
    """An object was not found in object storage (e.g. 404 NoSuchKey)."""

    default_message = "The requested storage object was not found."
    default_reason = "object_not_found"
    default_code = "OBJECT_NOT_FOUND"


class ObjectStorageConfigurationError(ConfigurationError):
    """
    Object storage configuration is invalid, incomplete, or violates security policies.
    Never discloses secret access keys or credentials in error messages or details.
    """

    def __init__(self, reason: str, *, setting: Optional[str] = None) -> None:
        extra = {"setting": setting} if setting else None
        super().__init__(
            message="The object storage configuration is not valid.",
            details=_details(reason, extra),
        )
