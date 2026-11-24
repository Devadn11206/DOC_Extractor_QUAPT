"""
Internal identity, token lifecycle, MFA, and customer OIDC federation (Team Member 3, Phase 5A).

Adheres to:
- PRD §10.1 SEC-001 (MFA enforcement for admin and schema_approver).
- PRD §10.1 SEC-002 (RS256 JWT, <= 1 hr max lifetime, rotating refresh tokens with replay detection).
- PRD §10.1 SEC-003 (The seven canonical tenant roles).
- PRD §10.1 SEC-006 (Strict tenant boundary isolation).
"""

from __future__ import annotations

from doc_extractor.infrastructure.identity.errors import (
    CrossTenantIdentityError,
    IdentityConfigurationError,
    IdentityError,
    InvalidCredentialsError,
    MfaRequiredError,
    MfaVerificationFailedError,
    OidcDiscoveryError,
    OidcValidationError,
    TokenReplayError,
    TokenRevokedError,
)
from doc_extractor.infrastructure.identity.mfa import MfaEnforcer, TotpEngine
from doc_extractor.infrastructure.identity.models import (
    OidcConfiguration,
    RefreshTokenRecord,
    TokenPair,
    UserRecord,
    hash_password,
    verify_password,
)
from doc_extractor.infrastructure.identity.oidc import OidcClient, OidcRegistry, OidcTokenResponse
from doc_extractor.infrastructure.identity.service import InternalIdentityService
from doc_extractor.infrastructure.identity.tokens import (
    KeyRing,
    RefreshTokenRepository,
    TokenManager,
    TokenRevocationStore,
)

__all__ = [
    # Errors
    "IdentityError",
    "InvalidCredentialsError",
    "TokenReplayError",
    "TokenRevokedError",
    "MfaRequiredError",
    "MfaVerificationFailedError",
    "OidcDiscoveryError",
    "OidcValidationError",
    "CrossTenantIdentityError",
    "IdentityConfigurationError",
    # Models & Helpers
    "UserRecord",
    "RefreshTokenRecord",
    "TokenPair",
    "OidcConfiguration",
    "hash_password",
    "verify_password",
    # MFA
    "TotpEngine",
    "MfaEnforcer",
    # Tokens & Key Management
    "KeyRing",
    "TokenRevocationStore",
    "RefreshTokenRepository",
    "TokenManager",
    # OIDC
    "OidcClient",
    "OidcRegistry",
    "OidcTokenResponse",
    # Service
    "InternalIdentityService",
]
