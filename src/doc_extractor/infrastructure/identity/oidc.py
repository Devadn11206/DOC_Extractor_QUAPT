"""
Customer OIDC federation and SSO provider integration (Team Member 3, Phase 5A).

Adheres to:
- PRD §10.1 SEC-001 (MFA detection from upstream IdP assertions).
- PRD §10.1 SEC-002 (OIDC token exchange and signature validation).
- PRD §10.1 SEC-003 (Mapping external claims to the seven canonical tenant roles).
- PRD §10.1 SEC-006 (Strict tenant boundary isolation: external IdPs are strictly bound to one tenant).
- PRD §7.2 API-011 & SEC-012 (Sanitized logging without token or secret exposure).

SAML 2.0 Note:
SAML 2.0 Web SSO is an enterprise requirement for legacy corporate identity providers (ADFS, PingFederate).
In accordance with Phase 5A scope and minimal dependencies, SAML 2.0 is designed as an external enterprise
bridge mapping into the OidcProvider abstraction; native SAML XML parsing is deferred to enterprise bridge deployments.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
from doc_extractor.api.providers.jwt_auth import CANONICAL_ROLES, ROLE_SCOPES
from doc_extractor.infrastructure.identity.errors import (
    CrossTenantIdentityError,
    IdentityConfigurationError,
    OidcDiscoveryError,
    OidcValidationError,
)
from doc_extractor.infrastructure.identity.mfa import MfaEnforcer
from doc_extractor.infrastructure.identity.models import OidcConfiguration
from doc_extractor.tenancy import require_path_safe_tenant_id

logger = logging.getLogger("doc_extractor.identity.oidc")


@dataclass(frozen=True)
class OidcTokenResponse:
    """OAuth2 / OIDC token exchange response."""

    id_token: str
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    refresh_token: Optional[str] = None


class OidcClient:
    """
    Client for customer OIDC federation bound to a specific tenant.
    Enforces strict issuer validation, audience verification, state/nonce protection,
    and safe claim-to-role mapping.
    """

    def __init__(
        self,
        config: OidcConfiguration,
        *,
        http_transport: Optional[Callable[[str, Dict[str, str], Dict[str, Any]], Dict[str, Any]]] = None,
        cached_jwks: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._config = config
        self._http_transport = http_transport
        self._cached_jwks = cached_jwks or {}
        # kid -> RSAPublicKey
        self._parsed_keys: Dict[str, RSAPublicKey] = {}
        if cached_jwks:
            self._load_jwks(cached_jwks)

    @property
    def config(self) -> OidcConfiguration:
        return self._config

    @property
    def tenant_id(self) -> str:
        return self._config.tenant_id

    def generate_authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        redirect_uri: str,
        scopes: Optional[List[str]] = None,
    ) -> str:
        """
        Generate the authorization redirect URL with cryptographic state and nonce.
        """
        if not state or not nonce or not redirect_uri:
            raise OidcValidationError("State, nonce, and redirect_uri are required for OIDC authorization.")

        query_scopes = " ".join(scopes or ["openid", "email", "profile"])
        params = {
            "client_id": self._config.client_id,
            "response_type": "code",
            "scope": query_scopes,
            "redirect_uri": redirect_uri,
            "state": state,
            "nonce": nonce,
        }
        encoded_query = urllib.parse.urlencode(params)
        sep = "&" if "?" in self._config.authorization_endpoint else "?"
        return f"{self._config.authorization_endpoint}{sep}{encoded_query}"

    def validate_callback_parameters(
        self,
        *,
        received_state: str,
        expected_state: str,
        code: str,
    ) -> None:
        """Validate state against CSRF attacks."""
        if not received_state or not expected_state:
            raise OidcValidationError("Missing OIDC state parameter.")
        if received_state != expected_state:
            raise OidcValidationError("OIDC state mismatch: potential CSRF attack detected.")
        if not code or not code.strip():
            raise OidcValidationError("Missing authorization code in OIDC callback.")

    def validate_id_token(
        self,
        id_token: str,
        *,
        expected_nonce: str,
        jwks: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Validate an OIDC ID token:
        1. Decode header to extract 'kid'.
        2. Look up matching RSA public key from JWKS.
        3. Verify signature, issuer, audience, and expiration.
        4. Validate nonce claim against replay.
        """
        if not id_token or not isinstance(id_token, str):
            raise OidcValidationError("Invalid or missing ID token.")

        if jwks:
            self._load_jwks(jwks)

        try:
            unverified_header = jwt.get_unverified_header(id_token)
            alg = unverified_header.get("alg", "RS256")
            if alg != "RS256":
                raise OidcValidationError(f"Unsupported OIDC token algorithm: {alg}. Only RS256 is accepted.")
            kid = unverified_header.get("kid")
            public_key = self._get_key(kid)

            claims = jwt.decode(
                id_token,
                public_key,
                algorithms=["RS256"],
                audience=self._config.client_id,
                issuer=self._config.issuer,
                leeway=self._config.allowed_clock_skew_seconds,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            logger.warning("OIDC ID token verification failed: %s", type(exc).__name__)
            raise OidcValidationError("OIDC ID token signature or claim validation failed.") from exc

        # Validate nonce to prevent replay attacks
        received_nonce = claims.get("nonce")
        if not received_nonce or received_nonce != expected_nonce:
            raise OidcValidationError("OIDC token nonce mismatch: potential replay attack.")

        return claims

    def map_claims_to_principal(self, claims: Dict[str, Any]) -> ApiPrincipal:
        """
        Map verified OIDC claims to an authoritative ApiPrincipal:
        - Tenant ID is ALWAYS the provider's bound tenant_id (prevents cross-tenant injection).
        - Roles are mapped from configured claim or fallback to default_roles.
        - Upstream MFA is detected from 'amr' or 'acr' claims.
        """
        sub = str(claims.get("sub", "")).strip()
        if not sub:
            raise OidcValidationError("OIDC claims missing subject identifier (sub).")

        email = str(claims.get("email", "")).strip()

        # Role resolution
        raw_roles = claims.get(self._config.role_claim_name)
        assigned_roles: Set[str] = set()
        if isinstance(raw_roles, list):
            for r in raw_roles:
                if isinstance(r, str) and r in CANONICAL_ROLES:
                    assigned_roles.add(r)
        elif isinstance(raw_roles, str) and raw_roles in CANONICAL_ROLES:
            assigned_roles.add(raw_roles)

        # Fallback to configured default roles if IdP does not provide valid canonical roles
        if not assigned_roles:
            assigned_roles = set(self._config.default_roles)

        # MFA detection from upstream IdP
        mfa_verified = MfaEnforcer.verify_upstream_idp_mfa(claims)

        # Scope calculation under least-privilege matrix
        role_scopes: Set[str] = set()
        for role in assigned_roles:
            role_scopes |= ROLE_SCOPES.get(role, frozenset())

        return ApiPrincipal(
            principal_id=f"oidc_{sub}",
            tenant_id=self.tenant_id,  # Authoritative tenant binding
            principal_type=PrincipalType.USER,
            roles=assigned_roles,
            scopes=role_scopes,
            is_active=True,
            mfa_verified=mfa_verified,
            metadata={"email": email, "idp_issuer": self._config.issuer},
        )

    def _load_jwks(self, jwks: Dict[str, Any]) -> None:
        """Parse RSA public keys from an RFC 7517 JWKS dictionary."""
        from jwt.algorithms import RSAAlgorithm

        keys = jwks.get("keys", [])
        for k in keys:
            if k.get("kty") == "RSA":
                kid = k.get("kid")
                try:
                    pub_key = RSAAlgorithm.from_jwk(json.dumps(k))
                    if isinstance(pub_key, RSAPublicKey):
                        if kid:
                            self._parsed_keys[kid] = pub_key
                        # Also store as default if only one key
                        if len(keys) == 1:
                            self._parsed_keys["__default__"] = pub_key
                except Exception as exc:
                    logger.debug("Failed to parse JWK key: %s", exc)

    def _get_key(self, kid: Optional[str]) -> RSAPublicKey:
        if kid and kid in self._parsed_keys:
            return self._parsed_keys[kid]
        if "__default__" in self._parsed_keys:
            return self._parsed_keys["__default__"]
        if self._parsed_keys:
            return next(iter(self._parsed_keys.values()))
        raise OidcValidationError("No matching public verification key found in IdP JWKS.")


class OidcRegistry:
    """Registry maintaining per-tenant OIDC configurations and clients."""

    def __init__(self) -> None:
        # tenant_id -> OidcClient
        self._providers: Dict[str, OidcClient] = {}

    def register_provider(self, config: OidcConfiguration, **kwargs: Any) -> OidcClient:
        require_path_safe_tenant_id(config.tenant_id)
        client = OidcClient(config, **kwargs)
        self._providers[config.tenant_id] = client
        logger.info("Registered OIDC SSO provider for tenant '%s' (issuer: %s)", config.tenant_id, config.issuer)
        return client

    def get_provider_for_tenant(self, tenant_id: str) -> Optional[OidcClient]:
        require_path_safe_tenant_id(tenant_id)
        return self._providers.get(tenant_id)

    def assert_tenant_provider(self, tenant_id: str) -> OidcClient:
        provider = self.get_provider_for_tenant(tenant_id)
        if provider is None:
            raise CrossTenantIdentityError(
                message=f"No OIDC identity provider configured for tenant '{tenant_id}'.",
                details={"reason": "unconfigured_tenant_idp"},
            )
        return provider
