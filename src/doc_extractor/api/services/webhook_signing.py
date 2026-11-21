"""
Webhook payload signing and verification, HMAC-SHA256 with a per-subscription secret (PRD v1.5 section 7.1), Phase 4. PROVISIONAL.

A reusable, stateless component. The caller supplies the subscription's secret on every call; this module stores no secret,
looks none up, generates none and logs nothing (it has no logger at all, so no secret or signature material can reach a log).
How the secret is created, stored (M5-D09 is Open: ``WebhookSubscriptionRecord.secret_hash`` cannot sign) and rotated is outside
this module, as is judging its strength: it refuses only a secret that is unusable (see ``WebhookSigningError``).

PROVISIONAL WIRE CONTRACT. The PRD says only "signed (HMAC-SHA256 with a per-subscription secret)"; it names no header, no
encoding and no signed content, so NO COMPATIBILITY with any external implementation is assumed. A consumer must follow this:

- signed content: the EXACT request body bytes (``contracts/webhook_events.serialize_event``), nothing else: no timestamp, no
  header, no delivery identifier is mixed in;
- signature: ``HMAC-SHA256(key = the secret's UTF-8 bytes, message = the body bytes)``;
- header: ``X-Webhook-Signature: sha256=<64 lowercase hexadecimal characters>``. The ``sha256=`` prefix names the scheme so that a
  later scheme can coexist; the name of the header is itself provisional;
- verification: recompute over the RAW body bytes before parsing them, compare in constant time, refuse anything else.

KNOWN LIMITATION: because the signature binds only the body, a captured delivery can be REPLAYED to the endpoint with a valid
signature. The body carries ``event_id`` and ``occurred_at`` (PRD: consumers de-duplicate by ``event_id``), but a freshness check
needs a signed delivery timestamp, which changes the signed content: that is a wire-format decision for the owner, not made here.
"""

import hashlib
import hmac
import re
from enum import Enum
from typing import Any

SIGNATURE_HEADER = "X-Webhook-Signature"          # provisional
SIGNATURE_SCHEME = "sha256"
MAX_SECRET_BYTES = 1024                           # a defensive bound on the key, not a strength policy

_SIGNATURE_VALUE = re.compile(r"sha256=([0-9a-f]{64})")


class WebhookSigningError(Exception):
    """
    An unusable input. ``INVALID_SECRET``: not ``bytes`` or ``str``, empty, blank (whitespace-only text, as ``str`` or as UTF-8
    ``bytes``), not encodable, or longer than ``MAX_SECRET_BYTES``. ``INVALID_PAYLOAD``: not ``bytes`` or empty. The message is
    fixed: neither the secret nor the payload is ever echoed.
    """

    INVALID_SECRET = "INVALID_SECRET"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    _MESSAGES = {
        INVALID_SECRET: "The webhook secret is not usable.",
        INVALID_PAYLOAD: "The webhook payload must be non-empty bytes.",
    }

    def __init__(self, code: str) -> None:
        self.code = code if code in self._MESSAGES else self.INVALID_SECRET
        super().__init__(self._MESSAGES[self.code])


class SignatureVerification(str, Enum):
    VALID = "valid"
    MISMATCH = "mismatch"        # well formed, but not the signature of this payload under this secret (tampering, wrong secret)
    MALFORMED = "malformed"      # not in the ``sha256=<64 lowercase hex>`` form; no comparison was attempted


def _key(secret: Any) -> bytes:
    """
    THE one path from a secret to its key bytes (signing and verification both use it, so they cannot disagree). A ``str`` is its
    UTF-8 bytes; ``bytes`` are used as given. The same two checks then judge the KEY BYTES, whichever type they came from: a
    secret is refused when it is empty or too long, or BLANK (the bytes decode as UTF-8 text that is empty after ``str.strip()``).
    So a ``str`` and its UTF-8 bytes are always accepted or refused together, while arbitrary binary secrets (bytes that are not
    UTF-8 text, or not blank text; NUL bytes included) are valid as they are.
    """
    if isinstance(secret, str):
        try:
            key = secret.encode("utf-8")
        except UnicodeEncodeError:
            raise WebhookSigningError(WebhookSigningError.INVALID_SECRET) from None
    elif isinstance(secret, bytes):
        key = secret
    else:
        raise WebhookSigningError(WebhookSigningError.INVALID_SECRET)
    if not key or len(key) > MAX_SECRET_BYTES:
        raise WebhookSigningError(WebhookSigningError.INVALID_SECRET)
    try:
        text = key.decode("utf-8")
    except UnicodeDecodeError:
        return key                                       # arbitrary binary: there is no text that could be blank
    if not text.strip():
        raise WebhookSigningError(WebhookSigningError.INVALID_SECRET)
    return key


def _body(payload: Any) -> bytes:
    if not isinstance(payload, bytes) or not payload:
        raise WebhookSigningError(WebhookSigningError.INVALID_PAYLOAD)
    return payload


def _digest(secret: Any, payload: Any) -> bytes:
    return hmac.new(_key(secret), _body(payload), hashlib.sha256).digest()


def compute_signature(secret: Any, payload: Any) -> str:
    """The signature as 64 lowercase hex characters: ``HMAC-SHA256(secret, payload)``. Deterministic."""
    return _digest(secret, payload).hex()


def sign_payload(secret: Any, payload: Any) -> str:
    """The ``X-Webhook-Signature`` header value for ``payload`` (the exact bytes that will be sent): ``sha256=<hex>``."""
    return f"{SIGNATURE_SCHEME}={compute_signature(secret, payload)}"


def verify_signature(secret: Any, payload: Any, signature_header: Any) -> SignatureVerification:
    """
    Verify a received header value against the RAW body bytes. A malformed header is ``MALFORMED`` (no comparison is made); a
    well-formed one is compared with ``hmac.compare_digest`` on the two digests (constant time for equal-length inputs; the length
    is public). An unusable secret or payload raises ``WebhookSigningError`` before the header is looked at.
    """
    expected = _digest(secret, payload)
    if not isinstance(signature_header, str):
        return SignatureVerification.MALFORMED
    match = _SIGNATURE_VALUE.fullmatch(signature_header)
    if match is None:
        return SignatureVerification.MALFORMED
    provided = bytes.fromhex(match.group(1))
    return SignatureVerification.VALID if hmac.compare_digest(expected, provided) else SignatureVerification.MISMATCH
