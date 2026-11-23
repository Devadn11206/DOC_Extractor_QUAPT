"""
RS256 JWT authentication for user and service sessions (PRD SEC-001, SEC-002, SEC-003, SEC-006).

- Tokens are RS256 only. The verifier pins ``algorithms=["RS256"]``, so ``none`` and HMAC-with-the-public-key
  confusion are rejected before any claim is read.
- Lifetime: an access token must expire within ``max_lifetime_seconds`` (default 3600, SEC-002). Longer tokens are refused.
- Tenant: the tenant comes only from the verified ``tenant_id`` claim (SEC-006). A missing tenant fails closed.
- Roles: every role must be one of the seven canonical tenant roles (SEC-003). An unknown role refuses the token.
- MFA (SEC-001): administrators and schema approvers are refused unless the token's ``amr`` claim includes ``mfa``.
- Expired tokens raise ``TokenExpiredError``; every other verification failure raises ``InvalidTokenError``. Neither
  error echoes the token or the failing claim value.

Scope of this module: access-token verification and issuance. Refresh-token rotation (one-time-use, SEC-002) and the
identity service that holds the signing key belong to the identity service, not to the API process.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional, Set, Union

import jwt

from doc_extractor.api.exceptions import AuthenticationRequiredError, InvalidTokenError, TokenExpiredError
from doc_extractor.api.ports.authentication import ApiPrincipal, AuthenticationProvider, PrincipalType
from schema_lifecycle.roles import Role

logger = logging.getLogger("doc_extractor.auth.jwt")

ALGORITHM = "RS256"
DEFAULT_MAX_LIFETIME_SECONDS = 3600
# The seven canonical tenant roles (PRD SEC-003). Defined once in the schema lifecycle package; this set must match it.
CANONICAL_ROLES: frozenset = frozenset(role.value for role in Role)
MFA_REQUIRED_ROLES: frozenset = frozenset({Role.ADMIN.value, Role.SCHEMA_APPROVER.value})

_DOC_READ = "documents:read"
_DOC_WRITE = "documents:write"
_REVIEW_READ = "review_tasks:read"
_REVIEW_CLAIM = "review_tasks:claim"
# Least-privilege role-to-scope matrix (SEC-003). A token's explicit ``scope`` claim can only narrow this, never widen it.
ROLE_SCOPES: Dict[str, frozenset] = {
    Role.ANALYST.value: frozenset({_DOC_READ, _DOC_WRITE}),
    Role.REVIEWER.value: frozenset({_DOC_READ, _REVIEW_READ, _REVIEW_CLAIM}),
    Role.SCHEMA_AUTHOR.value: frozenset({_DOC_READ}),
    Role.SCHEMA_APPROVER.value: frozenset({_DOC_READ}),
    Role.INTEGRATOR.value: frozenset({_DOC_READ, _DOC_WRITE}),
    Role.AUDITOR.value: frozenset({_DOC_READ, _REVIEW_READ}),
    Role.ADMIN.value: frozenset({_DOC_READ, _DOC_WRITE, _REVIEW_READ, _REVIEW_CLAIM}),
}
_MFA_AMR_VALUE = "mfa"

KeyMaterial = Union[str, bytes]


class JwtAuthenticationProvider(AuthenticationProvider):
    """Verifies RS256 access tokens against one public key and returns an authoritative ApiPrincipal."""

    def __init__(
        self,
        public_key_pem: KeyMaterial,
        issuer: str,
        audience: str,
        *,
        leeway_seconds: int = 0,
        max_lifetime_seconds: int = DEFAULT_MAX_LIFETIME_SECONDS,
    ) -> None:
        if not issuer or not audience:
            raise ValueError("The JWT issuer and audience are required.")
        if not 0 < max_lifetime_seconds <= DEFAULT_MAX_LIFETIME_SECONDS:
            raise ValueError("Access tokens must live at most 3600 seconds (SEC-002).")
        if leeway_seconds < 0 or leeway_seconds > 60:
            raise ValueError("Clock-skew leeway must be between 0 and 60 seconds.")
        # Parse the key now: a malformed, non-RSA or weak key is a configuration error, not a per-request failure.
        self._public_key = _load_rsa_public_key(public_key_pem)
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway_seconds
        self._max_lifetime = max_lifetime_seconds

    async def authenticate(self, credentials: Any, **kwargs: Any) -> Optional[ApiPrincipal]:
        token = _bearer_token(credentials)
        if token is None:
            raise AuthenticationRequiredError(
                message="A bearer access token is required.", code="AUTHENTICATION_REQUIRED"
            )
        claims = self._verify(token)
        principal = _principal_from_claims(claims)
        privileged = set(principal.roles) & MFA_REQUIRED_ROLES
        if privileged and not principal.mfa_verified:
            raise AuthenticationRequiredError(
                message="Administrators and schema approvers must sign in with MFA.",
                code="MFA_REQUIRED",
            )
        return principal

    def _verify(self, token: str) -> Dict[str, Any]:
        try:
            claims = jwt.decode(
                token,
                self._public_key,
                algorithms=[ALGORITHM],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError() from exc
        except jwt.PyJWTError as exc:
            # Type only: the library text can quote claim values from the token.
            logger.info("JWT rejected: %s", type(exc).__name__)
            raise InvalidTokenError(message="Invalid access token.", code="INVALID_CREDENTIALS") from exc
        lifetime = int(claims["exp"]) - int(claims["iat"])
        if lifetime <= 0 or lifetime > self._max_lifetime:
            raise InvalidTokenError(message="Access token lifetime is not permitted.", code="INVALID_CREDENTIALS")
        return claims


def _load_rsa_public_key(key: KeyMaterial) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    data = key.encode("utf-8") if isinstance(key, str) else key
    try:
        loaded = load_pem_public_key(data)
    except (ValueError, TypeError) as exc:
        raise ValueError("The JWT verification key must be a PEM public key.") from exc
    if not isinstance(loaded, RSAPublicKey) or loaded.key_size < 2048:
        raise ValueError("The JWT verification key must be an RSA key of at least 2048 bits (SEC-002).")
    return data


def _bearer_token(credentials: Any) -> Optional[str]:
    if not credentials or not isinstance(credentials, str):
        return None
    cleaned = credentials.strip()
    if cleaned.lower().startswith("bearer "):
        cleaned = cleaned[7:].strip()
    return cleaned or None


def _principal_from_claims(claims: Dict[str, Any]) -> ApiPrincipal:
    tenant_id = claims.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise InvalidTokenError(message="Access token carries no tenant.", code="INVALID_CREDENTIALS")
    roles = claims.get("roles", [])
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise InvalidTokenError(message="Access token roles are malformed.", code="INVALID_CREDENTIALS")
    unknown = set(roles) - CANONICAL_ROLES
    if unknown:
        # Name no role value: it came from the token.
        raise InvalidTokenError(message="Access token carries an unknown role.", code="INVALID_CREDENTIALS")
    scope_text = claims.get("scope", "")
    if not isinstance(scope_text, str):
        raise InvalidTokenError(message="Access token scope is malformed.", code="INVALID_CREDENTIALS")
    amr = claims.get("amr", [])
    amr_values = amr if isinstance(amr, list) else []
    role_scopes: Set[str] = set()
    for role in roles:
        role_scopes |= ROLE_SCOPES.get(role, frozenset())
    explicit = set(scope_text.split())
    # An explicit scope claim narrows the role-derived scopes; it never adds to them.
    granted = role_scopes & explicit if explicit else role_scopes
    try:
        return ApiPrincipal(
            principal_id=str(claims["sub"]),
            tenant_id=tenant_id,
            principal_type=PrincipalType.USER,
            roles=set(roles),
            scopes=granted,
            is_active=True,
            mfa_verified=_MFA_AMR_VALUE in amr_values,
        )
    except ValueError as exc:
        raise InvalidTokenError(message="Access token claims are invalid.", code="INVALID_CREDENTIALS") from exc


def issue_access_token(
    private_key_pem: KeyMaterial,
    *,
    issuer: str,
    audience: str,
    subject: str,
    tenant_id: str,
    roles: Iterable[str],
    scopes: Iterable[str] = (),
    mfa_verified: bool = False,
    lifetime_seconds: int = DEFAULT_MAX_LIFETIME_SECONDS,
    now: Optional[datetime] = None,
) -> str:
    """
    Sign an RS256 access token. Used by the identity service and by tests. Refuses roles outside the canonical seven
    and lifetimes above one hour, so a token this function produces is one the verifier accepts.
    """
    role_set: Set[str] = set(roles)
    if not role_set <= CANONICAL_ROLES:
        raise ValueError("Only the seven canonical tenant roles may be issued (SEC-003).")
    if not 0 < lifetime_seconds <= DEFAULT_MAX_LIFETIME_SECONDS:
        raise ValueError("Access tokens must live at most 3600 seconds (SEC-002).")
    issued_at = now or datetime.now(timezone.utc)
    payload: Dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "iat": issued_at,
        "exp": issued_at + timedelta(seconds=lifetime_seconds),
        "tenant_id": tenant_id,
        "roles": sorted(role_set),
        "scope": " ".join(sorted(set(scopes))),
        "amr": ["pwd", "mfa"] if mfa_verified else ["pwd"],
    }
    return jwt.encode(payload, private_key_pem, algorithm=ALGORITHM)
