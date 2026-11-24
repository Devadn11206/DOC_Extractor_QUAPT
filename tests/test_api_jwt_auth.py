"""
RS256 JWT authentication (PRD SEC-001, SEC-002, SEC-003). Every forged, expired, mis-scoped or over-privileged token
must be refused; a well-formed token of a canonical role must be accepted with its tenant and roles intact.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from doc_extractor.api.exceptions import AuthenticationRequiredError, InvalidTokenError, TokenExpiredError
from doc_extractor.api.providers.jwt_auth import (
    CANONICAL_ROLES,
    JwtAuthenticationProvider,
    issue_access_token,
)
from schema_lifecycle.roles import Role

ISSUER = "https://identity.example.internal"
AUDIENCE = "doc-extractor-api"


def _pem_pair(key_size: int = 2048):
    private = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private_pem, public_pem


PRIVATE_PEM, PUBLIC_PEM = _pem_pair()
OTHER_PRIVATE_PEM, _ = _pem_pair()


@pytest.fixture
def provider():
    return JwtAuthenticationProvider(PUBLIC_PEM, issuer=ISSUER, audience=AUDIENCE)


def _token(**overrides):
    kwargs = dict(
        issuer=ISSUER,
        audience=AUDIENCE,
        subject="user_42",
        tenant_id="tenant_a",
        roles=["analyst"],
        scopes=["documents:read"],
        mfa_verified=False,
    )
    kwargs.update(overrides)
    private_pem = kwargs.pop("private_pem", PRIVATE_PEM)
    return issue_access_token(private_pem, **kwargs)


def _authenticate(provider, token):
    return asyncio.run(provider.authenticate(f"Bearer {token}"))


def test_valid_token_yields_principal_with_tenant_roles_and_scopes(provider):
    principal = _authenticate(provider, _token())
    assert principal.principal_id == "user_42"
    assert principal.tenant_id == "tenant_a"
    assert principal.roles == {"analyst"}
    assert principal.scopes == {"documents:read"}
    assert principal.mfa_verified is False


def test_expired_token_is_refused_as_expired(provider):
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    with pytest.raises(TokenExpiredError):
        _authenticate(provider, _token(now=past))


def test_token_signed_by_another_key_is_refused(provider):
    with pytest.raises(InvalidTokenError):
        _authenticate(provider, _token(private_pem=OTHER_PRIVATE_PEM))


def test_unsigned_none_algorithm_token_is_refused(provider):
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "tenant_id": "tenant_a", "roles": ["admin"],
               "iat": int(datetime.now(timezone.utc).timestamp()), "exp": int(datetime.now(timezone.utc).timestamp()) + 60}
    unsigned = jwt.encode(payload, key=None, algorithm="none")
    with pytest.raises(InvalidTokenError):
        _authenticate(provider, unsigned)


def test_hmac_token_keyed_with_the_public_key_is_refused(provider):
    """Algorithm confusion: an attacker signs HS256 using the public PEM as the HMAC secret. Built by hand, because
    PyJWT itself refuses to HMAC-sign with a PEM key."""
    import base64
    import hashlib
    import hmac
    import json

    def b64(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    now = int(datetime.now(timezone.utc).timestamp())
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"iss": ISSUER, "aud": AUDIENCE, "sub": "x", "tenant_id": "tenant_a",
                              "roles": ["admin"], "iat": now, "exp": now + 60, "amr": ["mfa"]}).encode())
    signing_input = header + b"." + payload
    signature = b64(hmac.new(PUBLIC_PEM, signing_input, hashlib.sha256).digest())
    with pytest.raises(InvalidTokenError):
        _authenticate(provider, (signing_input + b"." + signature).decode())


def test_wrong_issuer_or_audience_is_refused(provider):
    with pytest.raises(InvalidTokenError):
        _authenticate(provider, _token(issuer="https://evil.example"))
    with pytest.raises(InvalidTokenError):
        _authenticate(provider, _token(audience="another-service"))


def test_lifetime_longer_than_one_hour_is_refused_at_issue_and_at_verify(provider):
    with pytest.raises(ValueError):
        _token(lifetime_seconds=3601)
    # A token minted outside the helper with a 2-hour lifetime must still be refused by the verifier.
    now = int(datetime.now(timezone.utc).timestamp())
    long_lived = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "u", "tenant_id": "tenant_a", "roles": ["analyst"],
         "iat": now, "exp": now + 7200},
        PRIVATE_PEM, algorithm="RS256",
    )
    with pytest.raises(InvalidTokenError):
        _authenticate(provider, long_lived)


def test_token_without_tenant_is_refused(provider):
    now = int(datetime.now(timezone.utc).timestamp())
    no_tenant = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "u", "roles": ["analyst"], "iat": now, "exp": now + 60},
        PRIVATE_PEM, algorithm="RS256",
    )
    with pytest.raises(InvalidTokenError):
        _authenticate(provider, no_tenant)


def test_unknown_role_is_refused_and_not_echoed(provider):
    with pytest.raises(InvalidTokenError) as exc_info:
        _authenticate(provider, issue_token_with_roles(["super_root"]))
    assert "super_root" not in str(exc_info.value.message)


def issue_token_with_roles(roles):
    """Mint a token with arbitrary role names, bypassing the issuer's own guard, as a hostile issuer would."""
    now = int(datetime.now(timezone.utc).timestamp())
    return jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "u", "tenant_id": "tenant_a", "roles": roles,
         "iat": now, "exp": now + 60},
        PRIVATE_PEM, algorithm="RS256",
    )


def test_issuer_refuses_non_canonical_roles():
    with pytest.raises(ValueError):
        _token(roles=["super_root"])


def test_privileged_roles_require_mfa(provider):
    for role in ("admin", "schema_approver"):
        with pytest.raises(AuthenticationRequiredError) as exc_info:
            _authenticate(provider, _token(roles=[role], mfa_verified=False))
        assert exc_info.value.code == "MFA_REQUIRED"
        principal = _authenticate(provider, _token(roles=[role], mfa_verified=True))
        assert principal.mfa_verified is True
        assert principal.roles == {role}


def test_non_privileged_roles_do_not_need_mfa(provider):
    assert _authenticate(provider, _token(roles=["reviewer"], mfa_verified=False)).roles == {"reviewer"}


def test_missing_or_blank_credentials_are_refused(provider):
    for credentials in (None, "", "   ", "Bearer    "):
        with pytest.raises(AuthenticationRequiredError):
            asyncio.run(provider.authenticate(credentials))


def test_the_seven_canonical_roles_are_exactly_the_prd_roles():
    assert CANONICAL_ROLES == {
        "analyst", "reviewer", "schema_author", "schema_approver", "integrator", "auditor", "admin",
    }
    assert {role.value for role in Role} == CANONICAL_ROLES


def test_verification_key_must_be_an_rsa_key_of_at_least_2048_bits():
    _, weak_pub = _pem_pair(key_size=1024)
    with pytest.raises(ValueError):
        JwtAuthenticationProvider(weak_pub, issuer=ISSUER, audience=AUDIENCE)
    with pytest.raises(ValueError):
        JwtAuthenticationProvider(b"not a key", issuer=ISSUER, audience=AUDIENCE)


def test_lifetime_and_leeway_bounds_are_configuration_errors():
    with pytest.raises(ValueError):
        JwtAuthenticationProvider(PUBLIC_PEM, issuer=ISSUER, audience=AUDIENCE, max_lifetime_seconds=7200)
    with pytest.raises(ValueError):
        JwtAuthenticationProvider(PUBLIC_PEM, issuer=ISSUER, audience=AUDIENCE, leeway_seconds=120)
