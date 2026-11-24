"""
Identity, session, and federation data models (Team Member 3, Phase 5A).

Adheres to:
- PRD §10.1 SEC-001 (MFA requirements for admin and schema_approver).
- PRD §10.1 SEC-002 (Token lifecycle, RS256, 1 hour max access token, rotating refresh tokens).
- PRD §10.1 SEC-003 (The seven canonical tenant roles).
- PRD §10.1 SEC-006 (Strict tenant boundary isolation).
- PRD §7.2 API-011 & SEC-012 (Credentials and secrets never exposed in logs or models).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
from doc_extractor.api.providers.jwt_auth import CANONICAL_ROLES, MFA_REQUIRED_ROLES, ROLE_SCOPES
from doc_extractor.infrastructure.identity.errors import IdentityConfigurationError
from doc_extractor.tenancy import require_path_safe_tenant_id


def hash_password(password: str, salt: Optional[str] = None) -> str:
    """Hash a password using salted PBKDF2-HMAC-SHA256."""
    if not password:
        raise ValueError("Password cannot be empty.")
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return f"pbkdf2:sha256:100000${salt}${hashed.hex()}"


def verify_password(plain_password: str, stored_hash: str) -> bool:
    """Verify a plain password against the stored salted PBKDF2 hash using constant-time comparison."""
    if not plain_password or not stored_hash or not stored_hash.startswith("pbkdf2:sha256:"):
        return False
    try:
        parts = stored_hash.split("$")
        if len(parts) != 3:
            return False
        iterations = int(parts[0].split(":")[2])
        salt = parts[1]
        expected_hex = parts[2]
        computed = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), iterations)
        return hmac.compare_digest(computed.hex(), expected_hex)
    except Exception:
        return False


@dataclass
class UserRecord:
    """Internal user account record bound to an authoritative tenant."""

    user_id: str
    tenant_id: str
    email: str
    hashed_password: Optional[str] = None
    roles: Set[str] = field(default_factory=lambda: {"analyst"})
    scopes: Set[str] = field(default_factory=set)
    mfa_enabled: bool = False
    mfa_secret: Optional[str] = None  # Base32 TOTP secret (sensitive, masked in logs)
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    external_idp_id: Optional[str] = None
    external_subject: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.user_id or not self.user_id.strip():
            raise IdentityConfigurationError("User ID must be non-empty.")
        try:
            require_path_safe_tenant_id(self.tenant_id)
        except Exception as exc:
            raise IdentityConfigurationError(f"Invalid tenant ID: {exc}") from exc
        if not self.email or "@" not in self.email:
            raise IdentityConfigurationError("User email must be valid.")
        unknown_roles = set(self.roles) - CANONICAL_ROLES
        if unknown_roles:
            raise IdentityConfigurationError(f"User contains non-canonical roles: {sorted(unknown_roles)}")

    def verify_password(self, password: str) -> bool:
        if not self.hashed_password:
            return False
        return verify_password(password, self.hashed_password)

    def to_principal(self, *, mfa_verified: bool = False) -> ApiPrincipal:
        """Derive an authoritative ApiPrincipal from this user record."""
        # Calculate granted scopes under least-privilege matrix
        role_scopes: Set[str] = set()
        for role in self.roles:
            role_scopes |= ROLE_SCOPES.get(role, frozenset())
        granted_scopes = role_scopes & self.scopes if self.scopes else role_scopes

        return ApiPrincipal(
            principal_id=self.user_id,
            tenant_id=self.tenant_id,
            principal_type=PrincipalType.USER,
            roles=set(self.roles),
            scopes=granted_scopes,
            is_active=self.is_active,
            mfa_verified=mfa_verified,
            metadata={"email": self.email},
        )


@dataclass
class RefreshTokenRecord:
    """
    Cryptographic state of a rotating, one-time-use refresh token.
    Stores the token hash (never the raw secret) and family group for replay detection.
    """

    token_id: str
    token_hash: str
    family_id: str
    user_id: str
    tenant_id: str
    roles: Set[str]
    scopes: Set[str]
    mfa_verified: bool
    expires_at: datetime
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    revoked: bool = False
    used: bool = False
    replaced_by: Optional[str] = None

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        check_time = now or datetime.now(timezone.utc)
        return check_time >= self.expires_at


@dataclass(frozen=True)
class TokenPair:
    """OAuth2 / OIDC token pair response."""

    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "Bearer"
    expires_in: int = 3600
    mfa_required: bool = False
    mfa_token: Optional[str] = None  # Temporary exchange token when step-up MFA is pending


@dataclass(frozen=True)
class OidcConfiguration:
    """OIDC Customer SSO configuration bound to an authoritative tenant."""

    issuer: str
    client_id: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    tenant_id: str
    client_secret: Optional[str] = None
    userinfo_endpoint: Optional[str] = None
    default_roles: Set[str] = field(default_factory=lambda: {"analyst"})
    role_claim_name: str = "roles"
    allowed_clock_skew_seconds: int = 60

    def __post_init__(self) -> None:
        if not self.issuer or not self.issuer.startswith("https://"):
            # Localhost exception for testing
            if not self.issuer.startswith("http://localhost") and not self.issuer.startswith("http://127.0.0.1"):
                raise IdentityConfigurationError("OIDC issuer must be an HTTPS URL.")
        if not self.client_id:
            raise IdentityConfigurationError("OIDC client_id is required.")
        try:
            require_path_safe_tenant_id(self.tenant_id)
        except Exception as exc:
            raise IdentityConfigurationError(f"Invalid tenant ID: {exc}") from exc
        unknown_roles = set(self.default_roles) - CANONICAL_ROLES
        if unknown_roles:
            raise IdentityConfigurationError(f"Default roles contain non-canonical roles: {sorted(unknown_roles)}")
