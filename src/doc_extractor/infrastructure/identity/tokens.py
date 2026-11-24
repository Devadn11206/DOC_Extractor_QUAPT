"""
JWT and rotating refresh token lifecycle engine (Team Member 3, Phase 5A).

Adheres to:
- PRD §10.1 SEC-001 (MFA enforcement for admin and schema_approver).
- PRD §10.1 SEC-002:
  - RS256-only access token signing and verification.
  - Maximum access token lifetime of 3600 seconds (1 hour).
  - Rotating, one-time-use refresh tokens.
  - Replay attack detection with immediate family revocation.
- Key rotation with 'kid' header and RFC 7517 JWKS export.
- Token revocation list by 'jti' and principal revocation timestamp.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

from doc_extractor.api.exceptions import TokenExpiredError
from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
from doc_extractor.api.providers.jwt_auth import (
    ALGORITHM,
    CANONICAL_ROLES,
    DEFAULT_MAX_LIFETIME_SECONDS,
    MFA_REQUIRED_ROLES,
    ROLE_SCOPES,
    _bearer_token,
    _load_rsa_public_key,
)
from doc_extractor.infrastructure.identity.errors import (
    IdentityConfigurationError,
    InvalidCredentialsError,
    MfaRequiredError,
    TokenReplayError,
    TokenRevokedError,
)
from doc_extractor.infrastructure.identity.mfa import MfaEnforcer
from doc_extractor.infrastructure.identity.models import RefreshTokenRecord, TokenPair, UserRecord
from doc_extractor.tenancy import require_path_safe_tenant_id

logger = logging.getLogger("doc_extractor.identity.tokens")

_DEFAULT_REFRESH_TOKEN_LIFETIME_DAYS = 7


def _int_to_base64url(n: int) -> str:
    """Encode an integer into an unpadded base64url string (RFC 7517)."""
    byte_length = (n.bit_length() + 7) // 8
    raw_bytes = n.to_bytes(byte_length, "big")
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")


class KeyRing:
    """
    Manages active RSA signing keys and archived verification public keys.
    Supports key rotation, 'kid' tracking, and RFC 7517 JWKS export.
    """

    def __init__(
        self,
        active_private_key: RSAPrivateKey,
        active_kid: str = "v1",
        archived_public_keys: Optional[Dict[str, RSAPublicKey]] = None,
    ) -> None:
        if not isinstance(active_private_key, RSAPrivateKey):
            raise IdentityConfigurationError("Active private key must be an RSAPrivateKey.")
        if active_private_key.key_size < 2048:
            raise IdentityConfigurationError("RSA keys must be at least 2048 bits (SEC-002).")
        if not active_kid or not active_kid.strip():
            raise IdentityConfigurationError("Active kid must be a non-empty string.")

        self._active_kid = active_kid
        self._active_private_key = active_private_key
        self._active_public_key = active_private_key.public_key()
        self._verification_keys: Dict[str, RSAPublicKey] = {active_kid: self._active_public_key}
        if archived_public_keys:
            self._verification_keys.update(archived_public_keys)

    @property
    def active_kid(self) -> str:
        return self._active_kid

    @property
    def active_private_key(self) -> RSAPrivateKey:
        return self._active_private_key

    @property
    def active_public_key(self) -> RSAPublicKey:
        return self._active_public_key

    def rotate_key(self, new_private_key: RSAPrivateKey, new_kid: str) -> None:
        """Rotate the active signing key; the old public key is retained for verification."""
        if not isinstance(new_private_key, RSAPrivateKey) or new_private_key.key_size < 2048:
            raise IdentityConfigurationError("New RSA key must be at least 2048 bits.")
        if not new_kid or not new_kid.strip():
            raise IdentityConfigurationError("New kid must be non-empty.")
        if new_kid in self._verification_keys and new_kid != self._active_kid:
            raise IdentityConfigurationError(f"Key id '{new_kid}' already exists in key ring.")

        self._active_kid = new_kid
        self._active_private_key = new_private_key
        self._active_public_key = new_private_key.public_key()
        self._verification_keys[new_kid] = self._active_public_key
        logger.info("Rotated identity signing key to active kid '%s'", new_kid)

    def get_public_key(self, kid: Optional[str] = None) -> RSAPublicKey:
        """Look up public key by kid; defaults to active key if kid is None."""
        if kid is None:
            return self._active_public_key
        key = self._verification_keys.get(kid)
        if key is None:
            raise InvalidCredentialsError(
                message="Unknown signing key identifier.",
                details={"reason": "unknown_kid"},
            )
        return key

    def to_jwks(self) -> Dict[str, List[Dict[str, Any]]]:
        """Export all verification public keys as an RFC 7517 JWKS dictionary."""
        keys: List[Dict[str, Any]] = []
        for kid, pub_key in self._verification_keys.items():
            public_numbers = pub_key.public_numbers()
            keys.append({
                "kty": "RSA",
                "use": "sig",
                "alg": ALGORITHM,
                "kid": kid,
                "n": _int_to_base64url(public_numbers.n),
                "e": _int_to_base64url(public_numbers.e),
            })
        return {"keys": keys}

    @classmethod
    def generate(cls, kid: str = "v1", key_size: int = 2048) -> "KeyRing":
        """Generate a fresh in-memory RSA KeyRing."""
        private = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
        return cls(active_private_key=private, active_kid=kid)


class TokenRevocationStore:
    """In-memory revocation store tracking revoked JTIs and user session invalidation."""

    def __init__(self) -> None:
        # jti -> expiry timestamp
        self._revoked_jtis: Dict[str, datetime] = {}
        # (user_id, tenant_id) -> revoked_at timestamp
        self._user_revocations: Dict[Tuple[str, str], datetime] = {}

    def revoke_jti(self, jti: str, expires_at: datetime) -> None:
        self._revoked_jtis[jti] = expires_at

    def is_jti_revoked(self, jti: str) -> bool:
        expires_at = self._revoked_jtis.get(jti)
        if expires_at is None:
            return False
        if datetime.now(timezone.utc) >= expires_at:
            # Clean up expired entry
            self._revoked_jtis.pop(jti, None)
            return False
        return True

    def revoke_user_sessions(self, user_id: str, tenant_id: str, revoked_at: Optional[datetime] = None) -> None:
        at = revoked_at or datetime.now(timezone.utc)
        self._user_revocations[(user_id, tenant_id)] = at

    def is_user_revoked(self, user_id: str, tenant_id: str, issued_at: datetime) -> bool:
        revoked_at = self._user_revocations.get((user_id, tenant_id))
        if revoked_at is None:
            return False
        return issued_at <= revoked_at


class RefreshTokenRepository:
    """In-memory store for rotating refresh tokens supporting family replay detection."""

    def __init__(self) -> None:
        # token_hash -> RefreshTokenRecord
        self._tokens_by_hash: Dict[str, RefreshTokenRecord] = {}
        # token_id -> token_hash
        self._id_to_hash: Dict[str, str] = {}
        # family_id -> set of token_ids
        self._family_tokens: Dict[str, Set[str]] = {}

    def save_token(self, record: RefreshTokenRecord) -> None:
        self._tokens_by_hash[record.token_hash] = record
        self._id_to_hash[record.token_id] = record.token_hash
        if record.family_id not in self._family_tokens:
            self._family_tokens[record.family_id] = set()
        self._family_tokens[record.family_id].add(record.token_id)

    def get_by_hash(self, token_hash: str) -> Optional[RefreshTokenRecord]:
        return self._tokens_by_hash.get(token_hash)

    def mark_used(self, token_id: str, replaced_by: str) -> bool:
        token_hash = self._id_to_hash.get(token_id)
        if token_hash and token_hash in self._tokens_by_hash:
            record = self._tokens_by_hash[token_hash]
            if record.used or record.revoked:
                return False
            record.used = True
            record.replaced_by = replaced_by
            return True
        return False

    def revoke_family(self, family_id: str) -> int:
        """Revoke all tokens in a family (triggered upon replay detection)."""
        token_ids = self._family_tokens.get(family_id, set())
        revoked_count = 0
        for tid in token_ids:
            thash = self._id_to_hash.get(tid)
            if thash and thash in self._tokens_by_hash:
                self._tokens_by_hash[thash].revoked = True
                revoked_count += 1
        logger.warning(
            "Family '%s' revoked due to token replay attack (%d tokens invalidated)",
            family_id,
            revoked_count,
        )
        return revoked_count

    def revoke_user_tokens(self, user_id: str, tenant_id: str) -> int:
        revoked = 0
        for record in self._tokens_by_hash.values():
            if record.user_id == user_id and record.tenant_id == tenant_id and not record.revoked:
                record.revoked = True
                revoked += 1
        return revoked


class TokenManager:
    """
    Coordinates access token signing, verification, and rotating refresh tokens.
    Enforces PRD SEC-001 (MFA), SEC-002 (RS256, 1 hr max lifetime, replay detection),
    and SEC-003 (Canonical roles).
    """

    def __init__(
        self,
        key_ring: KeyRing,
        issuer: str,
        audience: str,
        *,
        revocation_store: Optional[TokenRevocationStore] = None,
        refresh_repo: Optional[RefreshTokenRepository] = None,
        max_access_token_lifetime: int = DEFAULT_MAX_LIFETIME_SECONDS,
        clock_skew_leeway_seconds: int = 0,
    ) -> None:
        if not issuer or not audience:
            raise IdentityConfigurationError("Issuer and audience are required.")
        if not 0 < max_access_token_lifetime <= DEFAULT_MAX_LIFETIME_SECONDS:
            raise IdentityConfigurationError("Max access token lifetime must be between 1 and 3600 seconds (SEC-002).")

        self._key_ring = key_ring
        self._issuer = issuer
        self._audience = audience
        self._max_lifetime = max_access_token_lifetime
        self._leeway = clock_skew_leeway_seconds
        self._revocation_store = revocation_store or TokenRevocationStore()
        self._refresh_repo = refresh_repo or RefreshTokenRepository()

    @property
    def key_ring(self) -> KeyRing:
        return self._key_ring

    def issue_token_pair(
        self,
        user: UserRecord,
        *,
        mfa_verified: bool = False,
        scopes: Optional[Set[str]] = None,
        family_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> TokenPair:
        """
        Issue a new RS256 access token and rotating refresh token.
        Enforces SEC-001 (MFA required for admin/schema_approver).
        """
        # Validate roles
        roles = set(user.roles)
        MfaEnforcer.assert_privileged_access_permitted(roles, mfa_verified)

        current_time = now or datetime.now(timezone.utc)
        effective_scopes = scopes if scopes is not None else user.scopes
        jti = str(uuid.uuid4())

        # Build RS256 JWT payload
        claims: Dict[str, Any] = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": user.user_id,
            "jti": jti,
            "tenant_id": user.tenant_id,
            "roles": sorted(roles),
            "scope": " ".join(sorted(effective_scopes)),
            "iat": current_time,
            "exp": current_time + timedelta(seconds=self._max_lifetime),
            "amr": ["pwd", "mfa"] if mfa_verified else ["pwd"],
        }

        # Sign JWT with active private key and embed 'kid' in header
        headers = {"kid": self._key_ring.active_kid}
        access_token = jwt.encode(
            claims,
            self._key_ring.active_private_key,
            algorithm=ALGORITHM,
            headers=headers,
        )

        # Generate rotating refresh token (256 bits entropy)
        raw_refresh_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_refresh_token.encode("utf-8")).hexdigest()
        token_family = family_id or str(uuid.uuid4())
        token_id = f"rt_{uuid.uuid4().hex[:16]}"

        refresh_record = RefreshTokenRecord(
            token_id=token_id,
            token_hash=token_hash,
            family_id=token_family,
            user_id=user.user_id,
            tenant_id=user.tenant_id,
            roles=roles,
            scopes=effective_scopes,
            mfa_verified=mfa_verified,
            expires_at=current_time + timedelta(days=_DEFAULT_REFRESH_TOKEN_LIFETIME_DAYS),
            created_at=current_time,
        )
        self._refresh_repo.save_token(refresh_record)

        return TokenPair(
            access_token=access_token,
            refresh_token=raw_refresh_token,
            token_type="Bearer",
            expires_in=self._max_lifetime,
            mfa_required=False,
        )

    def rotate_refresh_token(
        self,
        raw_refresh_token: str,
        user_resolver: Optional[Any] = None,
        now: Optional[datetime] = None,
    ) -> TokenPair:
        """
        Consume a single-use refresh token, generate a replacement token in the same family,
        and issue a new access token.
        Detects replay attacks and revokes the whole family on reuse.
        """
        if not raw_refresh_token or not isinstance(raw_refresh_token, str):
            raise InvalidCredentialsError("Refresh token is required.")

        current_time = now or datetime.now(timezone.utc)
        token_hash = hashlib.sha256(raw_refresh_token.strip().encode("utf-8")).hexdigest()
        record = self._refresh_repo.get_by_hash(token_hash)

        if record is None:
            raise InvalidCredentialsError("Invalid or unknown refresh token.")

        if record.is_expired(current_time):
            raise InvalidCredentialsError("Refresh token has expired.")

        if record.revoked:
            raise TokenRevokedError("Refresh token has been revoked.")

        # --- REPLAY DETECTION ---
        if record.used:
            # Token was already consumed! Replay attack detected!
            self._refresh_repo.revoke_family(record.family_id)
            self._revocation_store.revoke_user_sessions(record.user_id, record.tenant_id, current_time)
            from doc_extractor.alerts import credential_fingerprint, get_default_monitor
            get_default_monitor().record_failed_login(credential_fingerprint(raw_refresh_token))
            raise TokenReplayError(
                message="Refresh token replay detected. Session terminated for security.",
                details={"reason": "refresh_token_already_used", "family_id": record.family_id},
            )

        # Token is valid and unused: rotate it
        next_raw_token = secrets.token_urlsafe(32)
        next_token_hash = hashlib.sha256(next_raw_token.encode("utf-8")).hexdigest()
        next_token_id = f"rt_{uuid.uuid4().hex[:16]}"

        # Mark current record used atomically (protects against concurrent consumption)
        consumed = self._refresh_repo.mark_used(record.token_id, replaced_by=next_token_id)
        if consumed is False:
            self._refresh_repo.revoke_family(record.family_id)
            self._revocation_store.revoke_user_sessions(record.user_id, record.tenant_id, current_time)
            from doc_extractor.alerts import credential_fingerprint, get_default_monitor
            get_default_monitor().record_failed_login(credential_fingerprint(raw_refresh_token))
            raise TokenReplayError(
                message="Refresh token replay detected. Session terminated for security.",
                details={"reason": "refresh_token_already_used", "family_id": record.family_id},
            )

        # Save new refresh token in same family
        next_record = RefreshTokenRecord(
            token_id=next_token_id,
            token_hash=next_token_hash,
            family_id=record.family_id,
            user_id=record.user_id,
            tenant_id=record.tenant_id,
            roles=record.roles,
            scopes=record.scopes,
            mfa_verified=record.mfa_verified,
            expires_at=current_time + timedelta(days=_DEFAULT_REFRESH_TOKEN_LIFETIME_DAYS),
            created_at=current_time,
        )
        self._refresh_repo.save_token(next_record)

        # Issue new access token
        jti = str(uuid.uuid4())
        claims: Dict[str, Any] = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": record.user_id,
            "jti": jti,
            "tenant_id": record.tenant_id,
            "roles": sorted(record.roles),
            "scope": " ".join(sorted(record.scopes)),
            "iat": current_time,
            "exp": current_time + timedelta(seconds=self._max_lifetime),
            "amr": ["pwd", "mfa"] if record.mfa_verified else ["pwd"],
        }
        headers = {"kid": self._key_ring.active_kid}
        access_token = jwt.encode(
            claims,
            self._key_ring.active_private_key,
            algorithm=ALGORITHM,
            headers=headers,
        )

        return TokenPair(
            access_token=access_token,
            refresh_token=next_raw_token,
            token_type="Bearer",
            expires_in=self._max_lifetime,
        )

    def verify_access_token(self, token_or_credentials: Any) -> ApiPrincipal:
        """
        Verify an RS256 access token:
        1. Extract token from Bearer prefix.
        2. Inspect 'kid' header and look up public key in KeyRing.
        3. Verify signature, lifetime, issuer, audience, and tenant.
        4. Check token revocation list (JTI) and user session invalidation.
        5. Verify canonical roles and MFA requirements.
        6. Return authoritative ApiPrincipal.
        """
        token = _bearer_token(token_or_credentials)
        if token is None:
            raise InvalidCredentialsError("A bearer access token is required.")

        try:
            # Read unverified header for kid lookup
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")
            public_key = self._key_ring.get_public_key(kid)

            claims = jwt.decode(
                token,
                public_key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError() from exc
        except jwt.PyJWTError as exc:
            logger.info("JWT rejected: %s", type(exc).__name__)
            raise InvalidCredentialsError("Invalid access token.") from exc

        # Validate maximum lifetime (SEC-002: <= 3600 seconds)
        lifetime = int(claims["exp"]) - int(claims["iat"])
        if lifetime <= 0 or lifetime > self._max_lifetime:
            raise InvalidCredentialsError("Access token lifetime is not permitted.")

        # Check token revocation (JTI)
        jti = claims.get("jti")
        if jti and self._revocation_store.is_jti_revoked(jti):
            raise TokenRevokedError("Access token has been revoked.")

        # Check tenant isolation
        tenant_id = claims.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise InvalidCredentialsError("Access token carries no tenant.")
        require_path_safe_tenant_id(tenant_id)

        # Check user revocation timestamp
        sub = str(claims["sub"])
        iat_dt = datetime.fromtimestamp(int(claims["iat"]), tz=timezone.utc)
        if self._revocation_store.is_user_revoked(sub, tenant_id, iat_dt):
            raise TokenRevokedError("User session has been revoked.")

        # Validate canonical roles (SEC-003)
        roles = claims.get("roles", [])
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            raise InvalidCredentialsError("Access token roles are malformed.")
        unknown = set(roles) - CANONICAL_ROLES
        if unknown:
            raise InvalidCredentialsError("Access token carries an unknown role.")

        # Validate MFA (SEC-001)
        amr = claims.get("amr", [])
        amr_values = amr if isinstance(amr, list) else []
        mfa_verified = "mfa" in amr_values

        privileged = set(roles) & MFA_REQUIRED_ROLES
        if privileged and not mfa_verified:
            raise MfaRequiredError(
                "Administrators and schema approvers must sign in with verified MFA (SEC-001)."
            )

        # Calculate scopes under least-privilege matrix
        scope_text = claims.get("scope", "")
        role_scopes: Set[str] = set()
        for role in roles:
            role_scopes |= ROLE_SCOPES.get(role, frozenset())
        explicit = set(scope_text.split()) if isinstance(scope_text, str) and scope_text else set()
        granted = role_scopes & explicit if explicit else role_scopes

        return ApiPrincipal(
            principal_id=sub,
            tenant_id=tenant_id,
            principal_type=PrincipalType.USER,
            roles=set(roles),
            scopes=granted,
            is_active=True,
            mfa_verified=mfa_verified,
        )

    def revoke_access_token(self, token_or_credentials: Any) -> None:
        """Add the token's JTI to the revocation list until its expiration."""
        token = _bearer_token(token_or_credentials)
        if not token:
            return
        try:
            unverified_claims = jwt.decode(token, options={"verify_signature": False})
            jti = unverified_claims.get("jti")
            exp = unverified_claims.get("exp")
            if jti and exp:
                exp_dt = datetime.fromtimestamp(int(exp), tz=timezone.utc)
                self._revocation_store.revoke_jti(jti, exp_dt)
        except Exception:
            pass

    def revoke_user_sessions(self, user_id: str, tenant_id: str) -> None:
        """Revoke all active refresh tokens and access tokens for a user in a tenant."""
        now = datetime.now(timezone.utc)
        self._revocation_store.revoke_user_sessions(user_id, tenant_id, now)
        self._refresh_repo.revoke_user_tokens(user_id, tenant_id)
        logger.info("Revoked all sessions for user '%s' in tenant '%s'", user_id, tenant_id)
