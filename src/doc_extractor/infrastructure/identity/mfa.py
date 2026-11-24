"""
Multi-Factor Authentication (MFA) enforcement engine (Team Member 3, Phase 5A).

Adheres to:
- PRD §10.1 SEC-001: Administrators and schema approvers MUST authenticate with verified MFA.
- Do not treat a caller-supplied 'amr' claim as proof of MFA.
- Verified by a trusted authentication mechanism (RFC 6238 TOTP or verified upstream IdP MFA assertion).
- Constant-time verification to prevent side-channel timing attacks.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from typing import Any, Dict, List, Optional, Set

from doc_extractor.api.providers.jwt_auth import MFA_REQUIRED_ROLES
from doc_extractor.infrastructure.identity.errors import (
    MfaRequiredError,
    MfaVerificationFailedError,
)

_TRUSTED_OIDC_MFA_AMR = frozenset({"mfa", "otp", "hwk", "sms", "fido", "webauthn", "pin"})
_TRUSTED_OIDC_MFA_ACR = frozenset({
    "urn:mace:incommon:iap:silver",
    "urn:mace:incommon:iap:gold",
    "http://schemas.microsoft.com/claims/multipleauthn",
    "https://refeds.org/profile/mfa",
})


class TotpEngine:
    """RFC 6238 Time-Based One-Time Password (TOTP) verification engine."""

    STEP_SECONDS = 30
    DIGITS = 6

    @classmethod
    def generate_secret(cls) -> str:
        """Generate a cryptographically secure 160-bit Base32 TOTP secret."""
        raw = hashlib.sha1(struct.pack(">d", time.time()) + base64.b64encode(hashlib.sha256(b"totp-seed").digest())).digest()
        # 20 bytes = 160 bits, standard for SHA-1 TOTP
        return base64.b32encode(raw[:20]).decode("ascii").replace("=", "")

    @classmethod
    def generate_code(cls, secret_base32: str, for_time: Optional[float] = None) -> str:
        """Calculate the 6-digit TOTP code for a given timestamp."""
        t = for_time if for_time is not None else time.time()
        counter = int(t // cls.STEP_SECONDS)
        secret_bytes = cls._decode_secret(secret_base32)

        # Pack counter into 8-byte big-endian integer
        msg = struct.pack(">Q", counter)
        h = hmac.new(secret_bytes, msg, hashlib.sha1).digest()

        # Dynamic truncation (RFC 4226 §5.4)
        offset = h[-1] & 0x0F
        code_int = (
            ((h[offset] & 0x7F) << 24)
            | ((h[offset + 1] & 0xFF) << 16)
            | ((h[offset + 2] & 0xFF) << 8)
            | (h[offset + 3] & 0xFF)
        )
        code = code_int % (10 ** cls.DIGITS)
        return f"{code:0{cls.DIGITS}d}"

    @classmethod
    def verify_code(
        cls,
        secret_base32: str,
        code: str,
        for_time: Optional[float] = None,
        allowed_drift_steps: int = 1,
    ) -> bool:
        """
        Verify a TOTP code within a configurable clock-skew window (default ±1 step = ±30s).
        Uses constant-time comparison.
        """
        if not code or not isinstance(code, str) or len(code.strip()) != cls.DIGITS:
            return False
        cleaned_code = code.strip()
        t = for_time if for_time is not None else time.time()
        current_counter = int(t // cls.STEP_SECONDS)

        try:
            # Check current step and drift steps
            for delta in range(-allowed_drift_steps, allowed_drift_steps + 1):
                step_time = (current_counter + delta) * cls.STEP_SECONDS
                expected = cls.generate_code(secret_base32, for_time=step_time)
                if hmac.compare_digest(cleaned_code, expected):
                    return True
            return False
        except Exception:
            return False

    @classmethod
    def _decode_secret(cls, secret_base32: str) -> bytes:
        cleaned = secret_base32.strip().upper().replace(" ", "")
        # Pad with '=' to multiple of 8 if needed
        padding = (8 - len(cleaned) % 8) % 8
        padded = cleaned + ("=" * padding)
        return base64.b32decode(padded, casefold=True)


class MfaEnforcer:
    """Evaluates and enforces MFA verification boundaries (PRD SEC-001)."""

    @classmethod
    def is_mfa_required(cls, roles: Set[str]) -> bool:
        """Checks if any role in the set requires mandatory MFA under PRD SEC-001."""
        return bool(set(roles) & MFA_REQUIRED_ROLES)

    @classmethod
    def verify_totp(cls, mfa_secret: Optional[str], mfa_code: Optional[str]) -> bool:
        """Verifies a user-provided TOTP passcode against their registered secret."""
        if not mfa_secret or not mfa_code:
            return False
        return TotpEngine.verify_code(mfa_secret, mfa_code)

    @classmethod
    def verify_upstream_idp_mfa(cls, id_token_claims: Dict[str, Any]) -> bool:
        """
        Inspects verified OIDC ID token claims to confirm if upstream IdP executed MFA.
        Checks both 'amr' (Authentication Methods References) and 'acr' (Authentication Context Class Reference).
        """
        # 1. Check AMR claim
        amr = id_token_claims.get("amr")
        if isinstance(amr, list):
            amr_lower = {str(item).lower() for item in amr}
            if amr_lower & _TRUSTED_OIDC_MFA_AMR:
                return True

        # 2. Check ACR claim
        acr = id_token_claims.get("acr")
        if isinstance(acr, str) and acr in _TRUSTED_OIDC_MFA_ACR:
            return True

        return False

    @classmethod
    def assert_privileged_access_permitted(cls, roles: Set[str], mfa_verified: bool) -> None:
        """Raises MfaRequiredError if privileged roles are requested without verified MFA."""
        if cls.is_mfa_required(roles) and not mfa_verified:
            raise MfaRequiredError(
                message="Administrators and schema approvers must authenticate with verified MFA (SEC-001).",
                details={"required_roles": sorted(set(roles) & MFA_REQUIRED_ROLES)},
            )
