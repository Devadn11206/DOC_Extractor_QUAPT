"""
Internal Identity Service (Team Member 3, Phase 5A).

Adheres to:
- PRD §10.1 SEC-001 (MFA enforcement for admin and schema_approver).
- PRD §10.1 SEC-002 (Token lifecycle, RS256, 1 hr max access token, rotating refresh tokens).
- PRD §10.1 SEC-003 (The seven canonical tenant roles).
- PRD §10.1 SEC-006 (Strict tenant boundary isolation).
- PRD §7.2 API-011 & SEC-012 (Sanitized logging and error handling).
- SEC-021 (Failed login monitoring with credential fingerprinting).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from doc_extractor.alerts import credential_fingerprint, get_default_monitor
from doc_extractor.api.ports.authentication import ApiPrincipal, AuthenticationProvider
from doc_extractor.api.providers.jwt_auth import (
    DEFAULT_MAX_LIFETIME_SECONDS,
    JwtAuthenticationProvider,
    MFA_REQUIRED_ROLES,
)
from doc_extractor.infrastructure.identity.errors import (
    CrossTenantIdentityError,
    IdentityConfigurationError,
    InvalidCredentialsError,
    MfaRequiredError,
    MfaVerificationFailedError,
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
)
from doc_extractor.infrastructure.identity.oidc import OidcClient, OidcRegistry
from doc_extractor.infrastructure.identity.tokens import KeyRing, TokenManager, TokenRevocationStore
from doc_extractor.tenancy import require_path_safe_tenant_id

logger = logging.getLogger("doc_extractor.identity.service")


class InternalIdentityService:
    """
    Product-owned identity abstraction managing internal user accounts, credentials,
    RS256 JWT tokens, rotating refresh tokens, MFA verification, and customer OIDC SSO.
    """

    def __init__(
        self,
        token_manager: TokenManager,
        *,
        oidc_registry: Optional[OidcRegistry] = None,
        user_repo: Optional[Any] = None,
    ) -> None:
        self._token_manager = token_manager
        self._oidc_registry = oidc_registry or OidcRegistry()
        self._user_repo = user_repo
        # Fallback local store if no persistent repository is injected
        # (tenant_id, user_id) -> UserRecord
        self._users_by_id: Dict[Tuple[str, str], UserRecord] = {}
        # (tenant_id, email) -> user_id
        self._users_by_email: Dict[Tuple[str, str], str] = {}

    @property
    def token_manager(self) -> TokenManager:
        return self._token_manager

    @property
    def oidc_registry(self) -> OidcRegistry:
        return self._oidc_registry

    # --------------------------------------------------------------------------
    # User Account Management
    # --------------------------------------------------------------------------

    def create_user(
        self,
        tenant_id: str,
        email: str,
        *,
        password: Optional[str] = None,
        roles: Optional[Set[str]] = None,
        scopes: Optional[Set[str]] = None,
        mfa_enabled: bool = False,
        mfa_secret: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> UserRecord:
        """Create and register an internal user account bound to a tenant."""
        require_path_safe_tenant_id(tenant_id)
        email_clean = email.strip().lower()

        if self._user_repo is not None:
            if self._user_repo.get_user_by_email(tenant_id, email_clean) is not None:
                raise IdentityConfigurationError(f"User with email '{email_clean}' already exists in tenant.")
        elif (tenant_id, email_clean) in self._users_by_email:
            raise IdentityConfigurationError(f"User with email '{email_clean}' already exists in tenant.")

        uid = user_id or f"usr_{uuid.uuid4().hex[:16]}"
        hashed_pw = hash_password(password) if password else None

        user = UserRecord(
            user_id=uid,
            tenant_id=tenant_id,
            email=email_clean,
            hashed_password=hashed_pw,
            roles=roles or {"analyst"},
            scopes=scopes or set(),
            mfa_enabled=mfa_enabled,
            mfa_secret=mfa_secret,
        )

        if self._user_repo is not None:
            self._user_repo.save_user(user)
        else:
            self._users_by_id[(tenant_id, uid)] = user
            self._users_by_email[(tenant_id, email_clean)] = uid
        logger.info("Created user '%s' in tenant '%s' with roles %s", uid, tenant_id, sorted(user.roles))
        return user

    def get_user(self, tenant_id: str, user_id: str) -> Optional[UserRecord]:
        require_path_safe_tenant_id(tenant_id)
        if self._user_repo is not None:
            return self._user_repo.get_user(tenant_id, user_id)
        return self._users_by_id.get((tenant_id, user_id))

    def get_user_by_email(self, tenant_id: str, email: str) -> Optional[UserRecord]:
        require_path_safe_tenant_id(tenant_id)
        email_clean = email.strip().lower()
        if self._user_repo is not None:
            return self._user_repo.get_user_by_email(tenant_id, email_clean)
        uid = self._users_by_email.get((tenant_id, email_clean))
        if not uid:
            return None
        return self._users_by_id.get((tenant_id, uid))

    def set_user_active(self, tenant_id: str, user_id: str, is_active: bool) -> bool:
        """Activate or deactivate a user account."""
        require_path_safe_tenant_id(tenant_id)
        if self._user_repo is not None:
            res = self._user_repo.set_active(tenant_id, user_id, is_active)
        else:
            user = self.get_user(tenant_id, user_id)
            if user is None:
                return False
            user.is_active = is_active
            res = True

        if not is_active:
            # Terminate active sessions upon deactivation
            self.sign_out_user(tenant_id, user_id)
        return res

    def enroll_mfa(self, tenant_id: str, user_id: str) -> str:
        """Enroll a user in TOTP MFA; returns the new Base32 secret."""
        user = self.get_user(tenant_id, user_id)
        if user is None:
            raise InvalidCredentialsError("User not found.")
        secret = TotpEngine.generate_secret()
        user.mfa_secret = secret
        user.mfa_enabled = False  # Pending confirmation with a valid code
        if self._user_repo is not None:
            self._user_repo.update_user(tenant_id, user_id, {"mfa_secret": secret, "mfa_enabled": False})
        return secret

    def confirm_mfa(self, tenant_id: str, user_id: str, code: str) -> bool:
        """Confirm MFA enrollment by validating the first passcode."""
        user = self.get_user(tenant_id, user_id)
        if user is None or not user.mfa_secret:
            raise InvalidCredentialsError("User or MFA secret not found.")
        if TotpEngine.verify_code(user.mfa_secret, code):
            user.mfa_enabled = True
            if self._user_repo is not None:
                self._user_repo.update_user(tenant_id, user_id, {"mfa_enabled": True})
            logger.info("Confirmed MFA enrollment for user '%s' in tenant '%s'", user_id, tenant_id)
            return True
        raise MfaVerificationFailedError("Invalid confirmation code.")

    # --------------------------------------------------------------------------
    # Authentication Lifecycle
    # --------------------------------------------------------------------------

    def authenticate_password(
        self,
        tenant_id: str,
        email: str,
        password: str,
        *,
        mfa_code: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> TokenPair:
        """
        Authenticate an internal user via password and optional/mandatory TOTP MFA.
        Enforces PRD SEC-001 (MFA required for admin/schema_approver).
        """
        require_path_safe_tenant_id(tenant_id)
        email_clean = email.strip().lower()
        user = self.get_user_by_email(tenant_id, email_clean)

        if user is None or not user.is_active:
            get_default_monitor().record_failed_login(credential_fingerprint(f"{tenant_id}:{email_clean}"))
            raise InvalidCredentialsError("Invalid email or password.")

        if not user.verify_password(password):
            get_default_monitor().record_failed_login(credential_fingerprint(f"{tenant_id}:{email_clean}"))
            raise InvalidCredentialsError("Invalid email or password.")

        # MFA evaluation
        privileged = MfaEnforcer.is_mfa_required(user.roles)
        mfa_required = privileged or user.mfa_enabled
        mfa_verified = False

        if mfa_required:
            if not mfa_code:
                # Privileged roles cannot obtain access tokens without MFA
                if privileged:
                    raise MfaRequiredError(
                        "Administrators and schema approvers must authenticate with verified MFA (SEC-001)."
                    )
                # Return challenge indicating MFA is required
                return TokenPair(
                    access_token="",
                    refresh_token=None,
                    mfa_required=True,
                    mfa_token=f"mfa_challenge_{user.user_id}",
                )

            # Verify TOTP code
            if not user.mfa_secret or not TotpEngine.verify_code(user.mfa_secret, mfa_code):
                get_default_monitor().record_failed_login(credential_fingerprint(f"{tenant_id}:{email_clean}:mfa"))
                raise MfaVerificationFailedError("Invalid MFA passcode.")
            mfa_verified = True

        # Issue token pair
        return self._token_manager.issue_token_pair(
            user,
            mfa_verified=mfa_verified,
            now=now,
        )

    def register_oidc_provider(self, config: OidcConfiguration, **kwargs: Any) -> OidcClient:
        """Register a customer OIDC SSO provider strictly bound to config.tenant_id."""
        return self._oidc_registry.register_provider(config, **kwargs)

    def authenticate_oidc(
        self,
        tenant_id: str,
        id_token: str,
        expected_nonce: str,
        *,
        jwks: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> TokenPair:
        """
        Authenticate an external customer via OIDC SSO:
        1. Validate ID token against the tenant's registered OIDC provider.
        2. Map verified claims into an authoritative ApiPrincipal.
        3. Enforce tenant isolation (IdP can only issue for its bound tenant).
        4. Enforce SEC-001: privileged roles require verified upstream MFA.
        5. Return internal RS256 token pair.
        """
        require_path_safe_tenant_id(tenant_id)
        oidc_client = self._oidc_registry.assert_tenant_provider(tenant_id)

        # Validate ID token
        claims = oidc_client.validate_id_token(id_token, expected_nonce=expected_nonce, jwks=jwks, now=now)

        # Map claims to principal
        principal = oidc_client.map_claims_to_principal(claims)

        # Verify tenant match
        if principal.tenant_id != tenant_id:
            raise CrossTenantIdentityError("OIDC token claims mismatch target tenant.")

        # Ensure privileged roles have MFA verified from upstream IdP
        MfaEnforcer.assert_privileged_access_permitted(principal.roles, principal.mfa_verified)

        # Resolve or upsert user record for this external subject
        user = self.get_user(tenant_id, principal.principal_id)
        if user is None:
            user = UserRecord(
                user_id=principal.principal_id,
                tenant_id=tenant_id,
                email=str(principal.metadata.get("email") or f"{principal.principal_id}@{tenant_id}.idp"),
                roles=principal.roles,
                scopes=principal.scopes,
                external_idp_id=oidc_client.config.issuer,
                external_subject=str(claims.get("sub")),
            )
            self._users_by_id[(tenant_id, principal.principal_id)] = user

        return self._token_manager.issue_token_pair(
            user,
            mfa_verified=principal.mfa_verified,
            now=now,
        )

    def refresh_tokens(self, raw_refresh_token: str, *, now: Optional[datetime] = None) -> TokenPair:
        """
        Exchange a single-use refresh token for a replacement refresh token and new access token.
        Detects replay attacks with immediate family revocation.
        """
        return self._token_manager.rotate_refresh_token(raw_refresh_token, now=now)

    def verify_token(self, token_or_credentials: Any) -> ApiPrincipal:
        """Verify an RS256 access token against KeyRing and revocation store."""
        return self._token_manager.verify_access_token(token_or_credentials)

    def revoke_token(self, token_or_credentials: Any) -> None:
        """Revoke a single access token by its JTI."""
        self._token_manager.revoke_access_token(token_or_credentials)

    def sign_out_user(self, tenant_id: str, user_id: str) -> None:
        """Sign out user: revokes all refresh tokens and invalidates active session tokens."""
        require_path_safe_tenant_id(tenant_id)
        self._token_manager.revoke_user_sessions(user_id, tenant_id)

    def get_jwks(self) -> Dict[str, Any]:
        """Export RFC 7517 JWKS dictionary for public verification."""
        return self._token_manager.key_ring.to_jwks()

    def as_authentication_provider(self) -> AuthenticationProvider:
        """Expose this identity service as an AuthenticationProvider for HTTP composition."""
        return _IdentityServiceProviderBridge(self)


class _IdentityServiceProviderBridge(AuthenticationProvider):
    """Bridges InternalIdentityService into the AuthenticationProvider protocol."""

    def __init__(self, service: InternalIdentityService) -> None:
        self._service = service

    async def authenticate(self, credentials: Any, **kwargs: Any) -> Optional[ApiPrincipal]:
        return self._service.verify_token(credentials)
