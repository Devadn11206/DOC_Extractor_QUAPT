"""
Typed exceptions for internal identity, JWT lifecycle, OIDC federation, and MFA (Team Member 3, Phase 5A).

Adheres to:
- PRD §10.1 SEC-001 (MFA enforcement), SEC-002 (Token lifecycle), SEC-003 (Role permissions).
- PRD §7.2 API-010 (Canonical error envelope), API-011 & SEC-012 (No sensitive leaks).
- Fixed error reason tokens; never echoes raw tokens, passwords, secrets, or PII.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from doc_extractor.api.exceptions import AuthenticationRequiredError
from doc_extractor.exceptions import ConfigurationError


class IdentityError(AuthenticationRequiredError):
    """Base class for all internal identity and authentication errors."""

    def __init__(
        self,
        message: str = "Identity authentication error.",
        code: str = "AUTHENTICATION_REQUIRED",
        status_code: int = 401,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code=code, details=details)
        self.status_code = status_code


class InvalidCredentialsError(IdentityError):
    """Raised when supplied credentials (password, secret, or code) are invalid."""

    def __init__(
        self,
        message: str = "Invalid credentials.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code="INVALID_CREDENTIALS", status_code=401, details=details)


class TokenReplayError(IdentityError):
    """Raised when an already-used one-time refresh token is presented (replay attack detected)."""

    def __init__(
        self,
        message: str = "Token replay detected: refresh token has already been consumed.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code="TOKEN_REPLAY_DETECTED", status_code=401, details=details)


class TokenRevokedError(IdentityError):
    """Raised when an access or refresh token has been explicitly revoked."""

    def __init__(
        self,
        message: str = "Token has been revoked.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code="TOKEN_REVOKED", status_code=401, details=details)


class MfaRequiredError(IdentityError):
    """Raised when a privileged role (admin, schema_approver) attempts access without verified MFA."""

    def __init__(
        self,
        message: str = "Multi-factor authentication is required for privileged operations (SEC-001).",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code="MFA_REQUIRED", status_code=401, details=details)


class MfaVerificationFailedError(IdentityError):
    """Raised when an MFA one-time passcode or assertion fails verification."""

    def __init__(
        self,
        message: str = "MFA code verification failed.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code="MFA_VERIFICATION_FAILED", status_code=401, details=details)


class OidcDiscoveryError(IdentityError):
    """Raised when OIDC metadata discovery or JWKS retrieval fails."""

    def __init__(
        self,
        message: str = "Failed to discover OIDC provider configuration or JWKS keys.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code="OIDC_DISCOVERY_ERROR", status_code=502, details=details)


class OidcValidationError(IdentityError):
    """Raised when an OIDC ID token, state, nonce, or signature fails validation."""

    def __init__(
        self,
        message: str = "OIDC token validation failed.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code="OIDC_VALIDATION_ERROR", status_code=401, details=details)


class CrossTenantIdentityError(IdentityError):
    """Raised when an identity or assertion from one tenant attempts to access another tenant."""

    def __init__(
        self,
        message: str = "Cross-tenant identity assertion is strictly prohibited.",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=message, code="TENANT_ACCESS_ERROR", status_code=403, details=details)


class IdentityConfigurationError(ConfigurationError):
    """Raised when identity provider or key management configuration is invalid."""

    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message=f"Identity configuration error: {message}.", details=details)
