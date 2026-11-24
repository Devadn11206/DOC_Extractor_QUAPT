"""
Tests for internal identity, token lifecycle, MFA enforcement, OIDC federation, and API-key compatibility (Team Member 3, Phase 5A).

Adheres to:
- PRD §10.1 SEC-001 (MFA enforcement for admin and schema_approver).
- PRD §10.1 SEC-002 (Token lifecycle, RS256, 1 hr max lifetime, rotating refresh tokens with replay detection).
- PRD §10.1 SEC-003 (The seven canonical tenant roles).
- PRD §10.1 SEC-006 (Strict tenant boundary isolation).
- PRD §7.2 API-011 & SEC-012 (No credential or secret leakage in errors or logs).
- PRD §10.1 SEC-021 (Failed login monitoring with credential fingerprinting).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from doc_extractor.api.contracts.records import ApiKeyRecord
from doc_extractor.api.exceptions import (
    AuthenticationRequiredError,
    InvalidTokenError,
    TokenExpiredError,
)
from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
from doc_extractor.api.providers.api_key_auth import ApiKeyAuthenticationProvider
from doc_extractor.api.providers.jwt_auth import (
    CANONICAL_ROLES,
    DEFAULT_MAX_LIFETIME_SECONDS,
    JwtAuthenticationProvider,
    issue_access_token,
)
from doc_extractor.api.token_core import create_api_key_record, generate_api_secret, hash_api_token
from doc_extractor.infrastructure.identity.errors import (
    CrossTenantIdentityError,
    IdentityConfigurationError,
    InvalidCredentialsError,
    MfaRequiredError,
    MfaVerificationFailedError,
    OidcValidationError,
    TokenReplayError,
    TokenRevokedError,
)
from doc_extractor.infrastructure.identity.mfa import MfaEnforcer, TotpEngine
from doc_extractor.infrastructure.identity.models import (
    OidcConfiguration,
    RefreshTokenRecord,
    UserRecord,
    hash_password,
    verify_password,
)
from doc_extractor.infrastructure.identity.oidc import OidcClient, OidcRegistry
from doc_extractor.infrastructure.identity.service import InternalIdentityService
from doc_extractor.infrastructure.identity.tokens import (
    KeyRing,
    RefreshTokenRepository,
    TokenManager,
    TokenRevocationStore,
)

ISSUER = "https://identity.example.internal"
AUDIENCE = "doc-extractor-api"


def run(coro):
    return asyncio.run(coro)


def _generate_rsa_pair(key_size: int = 2048):
    private = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    public = private.public_key()
    priv_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    pub_pem = public.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public, priv_pem, pub_pem


# ==============================================================================
# 1. Password Hashing & User Record Validation
# ==============================================================================

class TestUserAndCredentialModels:
    def test_password_hash_and_verify(self):
        pw = "SuperSecurePass123!"
        hashed = hash_password(pw)
        assert hashed.startswith("pbkdf2:sha256:100000$")
        assert verify_password(pw, hashed) is True
        assert verify_password("WrongPass", hashed) is False
        assert verify_password("", hashed) is False
        assert verify_password(pw, "invalid_hash_format") is False

    def test_user_record_enforces_path_safe_tenant_and_canonical_roles(self):
        with pytest.raises(IdentityConfigurationError):
            UserRecord(user_id="u1", tenant_id="../evil", email="user@corp.com")

        with pytest.raises(IdentityConfigurationError):
            UserRecord(user_id="u1", tenant_id="tenant_a", email="invalid_email")

        with pytest.raises(IdentityConfigurationError):
            UserRecord(user_id="u1", tenant_id="tenant_a", email="user@corp.com", roles={"super_root"})

        user = UserRecord(
            user_id="usr_01",
            tenant_id="tenant_alpha",
            email="admin@alpha.com",
            roles={"admin"},
        )
        principal = user.to_principal(mfa_verified=True)
        assert principal.principal_id == "usr_01"
        assert principal.tenant_id == "tenant_alpha"
        assert "admin" in principal.roles
        assert principal.mfa_verified is True


# ==============================================================================
# 2. KeyRing, Key Rotation & JWKS
# ==============================================================================

class TestKeyRingAndRotation:
    def test_key_ring_creation_and_jwks_export(self):
        priv, pub, _, _ = _generate_rsa_pair()
        key_ring = KeyRing(active_private_key=priv, active_kid="v1")

        assert key_ring.active_kid == "v1"
        assert key_ring.get_public_key("v1") == pub

        jwks = key_ring.to_jwks()
        assert "keys" in jwks
        assert len(jwks["keys"]) == 1
        jwk = jwks["keys"][0]
        assert jwk["kty"] == "RSA"
        assert jwk["kid"] == "v1"
        assert jwk["alg"] == "RS256"
        assert jwk["use"] == "sig"
        assert "n" in jwk and "e" in jwk

    def test_key_ring_rotation_maintains_verification_archive(self):
        priv1, pub1, _, _ = _generate_rsa_pair()
        priv2, pub2, _, _ = _generate_rsa_pair()

        key_ring = KeyRing(active_private_key=priv1, active_kid="v1")
        key_ring.rotate_key(priv2, new_kid="v2")

        assert key_ring.active_kid == "v2"
        assert key_ring.get_public_key("v2") == pub2
        assert key_ring.get_public_key("v1") == pub1

        jwks = key_ring.to_jwks()
        assert len(jwks["keys"]) == 2
        kids = {k["kid"] for k in jwks["keys"]}
        assert kids == {"v1", "v2"}


# ==============================================================================
# 3. RS256 JWT Token Lifecycle, Validation, and Revocation
# ==============================================================================

class TestJwtTokenLifecycle:
    @pytest.fixture
    def token_manager(self):
        priv, _, _, _ = _generate_rsa_pair()
        key_ring = KeyRing(active_private_key=priv, active_kid="v1")
        return TokenManager(key_ring=key_ring, issuer=ISSUER, audience=AUDIENCE)

    def test_issue_and_verify_access_token_with_kid(self, token_manager):
        user = UserRecord(
            user_id="usr_analyst",
            tenant_id="tenant_a",
            email="analyst@tenant.a",
            roles={"analyst"},
        )
        pair = token_manager.issue_token_pair(user)
        assert pair.access_token is not None
        assert pair.token_type == "Bearer"
        assert pair.expires_in == DEFAULT_MAX_LIFETIME_SECONDS

        principal = token_manager.verify_access_token(pair.access_token)
        assert principal.principal_id == "usr_analyst"
        assert principal.tenant_id == "tenant_a"
        assert principal.roles == {"analyst"}
        assert principal.mfa_verified is False

    def test_key_rotation_token_verification(self, token_manager):
        user = UserRecord(
            user_id="usr_rot",
            tenant_id="tenant_a",
            email="rot@tenant.a",
            roles={"reviewer"},
        )
        # Issue with v1
        token_v1 = token_manager.issue_token_pair(user).access_token

        # Rotate key to v2
        new_priv, _, _, _ = _generate_rsa_pair()
        token_manager.key_ring.rotate_key(new_priv, new_kid="v2")

        # Issue with v2
        token_v2 = token_manager.issue_token_pair(user).access_token

        # Both v1 and v2 verify successfully
        p1 = token_manager.verify_access_token(token_v1)
        p2 = token_manager.verify_access_token(token_v2)
        assert p1.principal_id == "usr_rot"
        assert p2.principal_id == "usr_rot"

    def test_expired_token_rejected(self, token_manager):
        user = UserRecord(
            user_id="usr_exp",
            tenant_id="tenant_a",
            email="exp@tenant.a",
            roles={"analyst"},
        )
        past = datetime.now(timezone.utc) - timedelta(hours=2)
        token = token_manager.issue_token_pair(user, now=past).access_token

        with pytest.raises(TokenExpiredError):
            token_manager.verify_access_token(token)

    def test_token_revocation_by_jti(self, token_manager):
        user = UserRecord(
            user_id="usr_rev",
            tenant_id="tenant_a",
            email="rev@tenant.a",
            roles={"analyst"},
        )
        token = token_manager.issue_token_pair(user).access_token

        # Verifies initially
        assert token_manager.verify_access_token(token).principal_id == "usr_rev"

        # Revoke access token
        token_manager.revoke_access_token(token)

        # Fails verification now
        with pytest.raises(TokenRevokedError):
            token_manager.verify_access_token(token)

    def test_user_session_invalidation(self, token_manager):
        user = UserRecord(
            user_id="usr_sess",
            tenant_id="tenant_a",
            email="sess@tenant.a",
            roles={"analyst"},
        )
        token = token_manager.issue_token_pair(user).access_token
        assert token_manager.verify_access_token(token).principal_id == "usr_sess"

        # Sign out user
        token_manager.revoke_user_sessions("usr_sess", "tenant_a")

        with pytest.raises(TokenRevokedError):
            token_manager.verify_access_token(token)


# ==============================================================================
# 4. Rotating Refresh Tokens & Replay Attack Defense
# ==============================================================================

class TestRotatingRefreshTokens:
    @pytest.fixture
    def token_manager(self):
        priv, _, _, _ = _generate_rsa_pair()
        key_ring = KeyRing(active_private_key=priv, active_kid="v1")
        return TokenManager(key_ring=key_ring, issuer=ISSUER, audience=AUDIENCE)

    def test_refresh_token_rotation_success(self, token_manager):
        user = UserRecord(
            user_id="usr_refresh",
            tenant_id="tenant_ref",
            email="ref@tenant.com",
            roles={"analyst"},
        )
        initial_pair = token_manager.issue_token_pair(user)
        rt1 = initial_pair.refresh_token

        # Rotate 1: rt1 -> rt2
        pair2 = token_manager.rotate_refresh_token(rt1)
        rt2 = pair2.refresh_token
        assert rt2 != rt1
        assert token_manager.verify_access_token(pair2.access_token).principal_id == "usr_refresh"

        # Rotate 2: rt2 -> rt3
        pair3 = token_manager.rotate_refresh_token(rt2)
        rt3 = pair3.refresh_token
        assert rt3 != rt2
        assert token_manager.verify_access_token(pair3.access_token).principal_id == "usr_refresh"

    def test_replay_attack_detection_revokes_entire_family(self, token_manager):
        """
        Security critical test:
        1. Client holds rt1, rotates it to get rt2.
        2. Attacker (or client) re-submits rt1 (which was already used).
        3. TokenManager detects replay, immediately invalidates rt1, rt2, and whole family.
        4. Neither rt1 nor rt2 can be used thereafter.
        """
        user = UserRecord(
            user_id="usr_victim",
            tenant_id="tenant_victim",
            email="victim@tenant.com",
            roles={"analyst"},
        )
        initial_pair = token_manager.issue_token_pair(user)
        rt1 = initial_pair.refresh_token

        # Legitimate rotation rt1 -> rt2
        pair2 = token_manager.rotate_refresh_token(rt1)
        rt2 = pair2.refresh_token

        # Attacker attempts replay of rt1!
        with pytest.raises(TokenReplayError) as exc_info:
            token_manager.rotate_refresh_token(rt1)
        assert exc_info.value.code == "TOKEN_REPLAY_DETECTED"

        # Now rt2 (the legitimate successor) is also invalidated because family was revoked!
        with pytest.raises(TokenRevokedError):
            token_manager.rotate_refresh_token(rt2)


# ==============================================================================
# 5. Multi-Factor Authentication (MFA) Boundary Enforcement (PRD SEC-001)
# ==============================================================================

class TestMfaEnforcement:
    def test_totp_code_generation_and_drift_verification(self):
        secret = TotpEngine.generate_secret()
        now = time.time()
        code = TotpEngine.generate_code(secret, for_time=now)

        # Immediate verification
        assert TotpEngine.verify_code(secret, code, for_time=now) is True

        # Within ±1 step (30 seconds) drift
        assert TotpEngine.verify_code(secret, code, for_time=now + 25) is True
        assert TotpEngine.verify_code(secret, code, for_time=now - 25) is True

        # Beyond allowed drift (> 60 seconds) fails
        assert TotpEngine.verify_code(secret, code, for_time=now + 90) is False

        # Invalid code
        assert TotpEngine.verify_code(secret, "000000", for_time=now) is False
        assert TotpEngine.verify_code(secret, "abc", for_time=now) is False

    def test_privileged_roles_require_mfa_at_issue_and_verify(self):
        priv, pub, _, _ = _generate_rsa_pair()
        key_ring = KeyRing(active_private_key=priv)
        tm = TokenManager(key_ring=key_ring, issuer=ISSUER, audience=AUDIENCE)

        for role in ("admin", "schema_approver"):
            user = UserRecord(user_id=f"u_{role}", tenant_id="t1", email="u@t.com", roles={role})

            # Issuing without MFA verified raises MfaRequiredError
            with pytest.raises(MfaRequiredError):
                tm.issue_token_pair(user, mfa_verified=False)

            # Issuing with MFA verified succeeds
            pair = tm.issue_token_pair(user, mfa_verified=True)
            principal = tm.verify_access_token(pair.access_token)
            assert principal.mfa_verified is True
            assert role in principal.roles

    def test_caller_asserted_amr_claim_is_rejected_without_mfa(self):
        """An attacker mints a token with roles=['admin'] but amr=['pwd']. Verifier fails closed."""
        priv, pub, priv_pem, pub_pem = _generate_rsa_pair()
        provider = JwtAuthenticationProvider(public_key_pem=pub_pem, issuer=ISSUER, audience=AUDIENCE)

        # Admin token without MFA amr
        untrusted_token = issue_access_token(
            priv_pem,
            issuer=ISSUER,
            audience=AUDIENCE,
            subject="attacker",
            tenant_id="tenant_a",
            roles=["admin"],
            mfa_verified=False,
        )

        with pytest.raises(AuthenticationRequiredError) as exc_info:
            run(provider.authenticate(f"Bearer {untrusted_token}"))
        assert exc_info.value.code == "MFA_REQUIRED"


# ==============================================================================
# 6. Customer OIDC Federation & Cross-Tenant Boundary Protection
# ==============================================================================

class TestOidcFederationAndTenantIsolation:
    @pytest.fixture
    def oidc_setup(self):
        idp_priv, idp_pub, idp_priv_pem, idp_pub_pem = _generate_rsa_pair()
        idp_key_ring = KeyRing(idp_priv, active_kid="idp-k1")
        idp_jwks = idp_key_ring.to_jwks()

        config = OidcConfiguration(
            issuer="https://idp.customer-corp.com",
            client_id="doc-extractor-client-id",
            authorization_endpoint="https://idp.customer-corp.com/oauth2/authorize",
            token_endpoint="https://idp.customer-corp.com/oauth2/token",
            jwks_uri="https://idp.customer-corp.com/.well-known/jwks.json",
            tenant_id="tenant_corp",
            default_roles={"analyst"},
        )
        client = OidcClient(config, cached_jwks=idp_jwks)
        return client, idp_priv, idp_jwks

    def test_oidc_authorization_url_generation(self, oidc_setup):
        client, _, _ = oidc_setup
        url = client.generate_authorization_url(
            state="state_xyz_123",
            nonce="nonce_abc_456",
            redirect_uri="https://api.corp.internal/callback",
        )
        assert url.startswith("https://idp.customer-corp.com/oauth2/authorize?")
        assert "client_id=doc-extractor-client-id" in url
        assert "state=state_xyz_123" in url
        assert "nonce=nonce_abc_456" in url

    def test_oidc_state_validation_protects_against_csrf(self, oidc_setup):
        client, _, _ = oidc_setup
        with pytest.raises(OidcValidationError):
            client.validate_callback_parameters(
                received_state="attacker_state",
                expected_state="legit_state",
                code="auth_code_123",
            )

    def test_valid_oidc_id_token_validation_and_principal_mapping(self, oidc_setup):
        client, idp_priv, _ = oidc_setup
        now = datetime.now(timezone.utc)
        claims = {
            "iss": "https://idp.customer-corp.com",
            "aud": "doc-extractor-client-id",
            "sub": "user_1001",
            "email": "employee@customer-corp.com",
            "nonce": "nonce_safe_789",
            "roles": ["reviewer"],
            "amr": ["pwd", "mfa"],
            "iat": now,
            "exp": now + timedelta(minutes=10),
        }
        id_token = jwt.encode(claims, idp_priv, algorithm="RS256", headers={"kid": "idp-k1"})

        verified_claims = client.validate_id_token(id_token, expected_nonce="nonce_safe_789")
        assert verified_claims["sub"] == "user_1001"

        principal = client.map_claims_to_principal(verified_claims)
        assert principal.principal_id == "oidc_user_1001"
        assert principal.tenant_id == "tenant_corp"  # Bound to configured tenant
        assert principal.roles == {"reviewer"}
        assert principal.mfa_verified is True

    def test_cross_tenant_isolation_rejects_unconfigured_or_mismatched_tenant(self, oidc_setup):
        """OIDC IdP for tenant_corp CANNOT authenticate for tenant_other."""
        client, idp_priv, _ = oidc_setup
        registry = OidcRegistry()
        registry.register_provider(client.config, cached_jwks=client._cached_jwks)

        # Requesting provider for another tenant fails
        with pytest.raises(CrossTenantIdentityError):
            registry.assert_tenant_provider("tenant_other")


# ==============================================================================
# 7. Internal Identity Service End-to-End Coordination
# ==============================================================================

class TestInternalIdentityService:
    @pytest.fixture
    def service(self):
        priv, _, _, _ = _generate_rsa_pair()
        key_ring = KeyRing(active_private_key=priv)
        token_manager = TokenManager(key_ring=key_ring, issuer=ISSUER, audience=AUDIENCE)
        return InternalIdentityService(token_manager=token_manager)

    def test_user_password_auth_flow(self, service):
        service.create_user("tenant_abc", "alice@abc.com", password="SecretPassword1!", roles={"analyst"})

        # Successful auth
        pair = service.authenticate_password("tenant_abc", "alice@abc.com", "SecretPassword1!")
        assert pair.access_token is not None
        assert pair.refresh_token is not None

        # Verify access token
        principal = service.verify_token(pair.access_token)
        assert principal.principal_id.startswith("usr_")
        assert principal.tenant_id == "tenant_abc"
        assert principal.roles == {"analyst"}

    def test_mfa_enrollment_and_login_enforcement(self, service):
        user = service.create_user("tenant_mfa", "bob@mfa.com", password="Password123!", roles={"admin"})

        # Initial login as admin without MFA raises MfaRequiredError
        with pytest.raises(MfaRequiredError):
            service.authenticate_password("tenant_mfa", "bob@mfa.com", "Password123!")

        # Enroll MFA
        totp_secret = service.enroll_mfa("tenant_mfa", user.user_id)
        assert totp_secret is not None

        # Confirm enrollment with valid code
        code = TotpEngine.generate_code(totp_secret)
        assert service.confirm_mfa("tenant_mfa", user.user_id, code) is True

        # Now login with TOTP code succeeds!
        pair = service.authenticate_password("tenant_mfa", "bob@mfa.com", "Password123!", mfa_code=code)
        principal = service.verify_token(pair.access_token)
        assert principal.mfa_verified is True
        assert "admin" in principal.roles

    def test_service_as_authentication_provider(self, service):
        service.create_user("tenant_prov", "claire@prov.com", password="Password!", roles={"analyst"})
        pair = service.authenticate_password("tenant_prov", "claire@prov.com", "Password!")

        provider = service.as_authentication_provider()
        principal = run(provider.authenticate(f"Bearer {pair.access_token}"))
        assert principal is not None
        assert principal.tenant_id == "tenant_prov"


# ==============================================================================
# 8. API Key Authentication Provider Compatibility
# ==============================================================================

class TestApiKeyProviderPreservation:
    """Verifies that API Key repository and provider contracts remain 100% compatible."""

    def test_api_key_authentication_success_and_revocation(self):
        repo = MagicMock()
        gen_token = generate_api_secret()
        record = create_api_key_record(
            token=gen_token,
            tenant_id="tenant_api",
            name="test-key",
            scopes=["documents:read"],
            roles=["integrator"],
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )

        repo.get_by_key_hash = AsyncMock(return_value=record)
        repo.record_last_used = AsyncMock(return_value=True)

        provider = ApiKeyAuthenticationProvider(repository=repo)
        raw_token = gen_token.raw_secret

        # Authenticate
        principal = run(provider.authenticate(f"Bearer {raw_token}"))
        assert principal is not None
        assert principal.tenant_id == "tenant_api"
        assert principal.principal_type == PrincipalType.API_KEY
        assert principal.mfa_verified is False  # SEC-001 Invariant

        # Revoked key fails closed
        record.revoked_at = datetime.now(timezone.utc)
        record.is_active = False
        with pytest.raises(InvalidTokenError):
            run(provider.authenticate(f"Bearer {raw_token}"))


# ==============================================================================
# 9. Security Boundary, Cross-Tenant Isolation & Anti-Leakage
# ==============================================================================

class TestSecurityBoundariesAndAntiLeakage:
    def test_cross_tenant_password_authentication_fails(self):
        priv, _, _, _ = _generate_rsa_pair()
        key_ring = KeyRing(active_private_key=priv)
        tm = TokenManager(key_ring=key_ring, issuer=ISSUER, audience=AUDIENCE)
        service = InternalIdentityService(token_manager=tm)

        service.create_user("tenant_alpha", "user@corp.com", password="PassWord123!")

        # Attempting to authenticate under tenant_beta fails
        with pytest.raises(InvalidCredentialsError):
            service.authenticate_password("tenant_beta", "user@corp.com", "PassWord123!")

    def test_oidc_rejects_hs256_symmetric_confusion(self):
        import base64
        import hashlib
        import hmac

        idp_priv, idp_pub, _, idp_pub_pem = _generate_rsa_pair()
        idp_jwks = KeyRing(idp_priv).to_jwks()
        config = OidcConfiguration(
            issuer="https://idp.target.com",
            client_id="target-client",
            authorization_endpoint="https://idp.target.com/auth",
            token_endpoint="https://idp.target.com/token",
            jwks_uri="https://idp.target.com/jwks",
            tenant_id="tenant_target",
        )
        client = OidcClient(config, cached_jwks=idp_jwks)

        # Attacker constructs an HS256 token by hand using the public PEM as HMAC secret
        def b64(data: bytes) -> bytes:
            return base64.urlsafe_b64encode(data).rstrip(b"=")

        now = int(datetime.now(timezone.utc).timestamp())
        header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        payload = b64(json.dumps({
            "iss": "https://idp.target.com",
            "aud": "target-client",
            "sub": "user_hs256",
            "nonce": "n123",
            "iat": now,
            "exp": now + 300,
        }).encode())
        signing_input = header + b"." + payload
        signature = b64(hmac.new(idp_pub_pem, signing_input, hashlib.sha256).digest())
        hs256_token = (signing_input + b"." + signature).decode()

        with pytest.raises(OidcValidationError) as exc:
            client.validate_id_token(hs256_token, expected_nonce="n123")
        assert "algorithm" in str(exc.value.message).lower()

    def test_error_messages_do_not_leak_secrets(self):
        priv, _, _, _ = _generate_rsa_pair()
        key_ring = KeyRing(active_private_key=priv)
        tm = TokenManager(key_ring=key_ring, issuer=ISSUER, audience=AUDIENCE)
        service = InternalIdentityService(token_manager=tm)

        raw_secret_pw = "SuperTopSecretPassword999!"
        service.create_user("tenant_secret", "secret@corp.com", password=raw_secret_pw)

        try:
            service.authenticate_password("tenant_secret", "secret@corp.com", "WrongPasswordAttempt")
        except InvalidCredentialsError as exc:
            err_text = f"{exc.message} {exc.details}"
            assert raw_secret_pw not in err_text
            assert "WrongPasswordAttempt" not in err_text

    def test_sign_out_user_invalidates_all_active_tokens(self):
        priv, _, _, _ = _generate_rsa_pair()
        key_ring = KeyRing(active_private_key=priv)
        tm = TokenManager(key_ring=key_ring, issuer=ISSUER, audience=AUDIENCE)
        service = InternalIdentityService(token_manager=tm)

        service.create_user("tenant_signout", "user@signout.com", password="Password123!")
        pair1 = service.authenticate_password("tenant_signout", "user@signout.com", "Password123!")
        pair2 = service.authenticate_password("tenant_signout", "user@signout.com", "Password123!")

        assert service.verify_token(pair1.access_token).principal_id.startswith("usr_")
        assert service.verify_token(pair2.access_token).principal_id.startswith("usr_")

        user = service.get_user_by_email("tenant_signout", "user@signout.com")
        assert user is not None

        # Sign out user across all sessions
        service.sign_out_user("tenant_signout", user.user_id)

        # Both access tokens are now rejected
        with pytest.raises(TokenRevokedError):
            service.verify_token(pair1.access_token)
        with pytest.raises(TokenRevokedError):
            service.verify_token(pair2.access_token)

        # Both refresh tokens are now rejected
        with pytest.raises(TokenRevokedError):
            service.refresh_tokens(pair1.refresh_token)
        with pytest.raises(TokenRevokedError):
            service.refresh_tokens(pair2.refresh_token)
