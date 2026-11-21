"""
Tests for webhook signing and verification (HMAC-SHA256, PRD v1.5 section 7.1), Phase 4, PROVISIONAL.

Verified: the signature against a published known-answer vector (RFC 4231) and an independent ``hmac`` computation; determinism;
verification of valid, tampered, wrong-secret and re-serialized payloads; the refusal of unusable secrets and payloads and of
malformed signature headers; that the comparison is ``hmac.compare_digest`` on the two digests (and only for a well-formed header);
that no secret or signature material reaches an error, a representation or a log; and signing of real serialized events.
Synthetic secrets only.
"""

import ast
import hashlib
import hmac
import inspect
import logging
import re

import pytest

from doc_extractor.api.contracts.webhook_events import document_completed, parse_event, serialize_event
from doc_extractor.api.services import webhook_signing as module
from doc_extractor.api.services.webhook_signing import (
    MAX_SECRET_BYTES,
    SIGNATURE_HEADER,
    SIGNATURE_SCHEME,
    SignatureVerification,
    WebhookSigningError,
    compute_signature,
    sign_payload,
    verify_signature,
)
from datetime import datetime, timezone

SECRET = "synthetic-subscription-secret-731"
BODY = b'{"data":{"document_id":"doc_1","status":"reviewed"},"event_id":"evt_00000000000000000000000000000001",' \
       b'"event_type":"document.completed","occurred_at":"2026-03-01T12:00:00.000000Z"}'
VALID = SignatureVerification.VALID
MISMATCH = SignatureVerification.MISMATCH
MALFORMED = SignatureVerification.MALFORMED


def _independent(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


# ============================================================================
# The signature
# ============================================================================

def test_the_signature_matches_the_rfc_4231_known_answer():
    """RFC 4231 test case 2: key "Jefe", data "what do ya want for nothing?"."""
    expected = "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    assert compute_signature(b"Jefe", b"what do ya want for nothing?") == expected
    assert compute_signature("Jefe", b"what do ya want for nothing?") == expected
    assert sign_payload("Jefe", b"what do ya want for nothing?") == f"sha256={expected}"


def test_the_signature_is_the_hmac_sha256_of_the_exact_bytes_under_the_secret():
    for secret, payload in ((SECRET, BODY), ("é-ключ-鍵", b"\x00\x01\xffbinary"), (b"\x00\xff" * 8, b"x"), ("k", b"p" * 100_000)):
        key = secret.encode("utf-8") if isinstance(secret, str) else secret
        assert compute_signature(secret, payload) == _independent(key, payload)


def test_the_header_value_is_the_scheme_a_separator_and_64_lowercase_hex_characters():
    value = sign_payload(SECRET, BODY)
    assert SIGNATURE_HEADER == "X-Webhook-Signature" and SIGNATURE_SCHEME == "sha256"
    assert re.fullmatch(r"sha256=[0-9a-f]{64}", value) and value == "sha256=" + _independent(SECRET.encode(), BODY)


def test_signing_is_deterministic():
    first = sign_payload(SECRET, BODY)
    assert all(sign_payload(SECRET, BODY) == first for _ in range(50))
    assert sign_payload(SECRET.encode("utf-8"), BODY) == first                      # a str secret is its UTF-8 bytes


def test_each_input_changes_the_signature():
    base = sign_payload(SECRET, BODY)
    assert sign_payload(SECRET + "x", BODY) != base and sign_payload("x" + SECRET, BODY) != base
    assert sign_payload(SECRET, BODY + b" ") != base and sign_payload(SECRET, BODY[:-1]) != base
    assert sign_payload(SECRET, BODY.replace(b"reviewed", b"Reviewed")) != base


def test_a_secret_is_used_exactly_as_given_whitespace_is_part_of_the_key():
    assert sign_payload(" s ", BODY) != sign_payload("s", BODY)
    assert sign_payload("s\n", BODY) != sign_payload("s", BODY)


# ============================================================================
# Verification
# ============================================================================

def test_a_valid_signature_verifies():
    assert verify_signature(SECRET, BODY, sign_payload(SECRET, BODY)) is VALID
    assert verify_signature(SECRET.encode(), BODY, sign_payload(SECRET, BODY)) is VALID


def test_tampering_is_a_mismatch():
    header = sign_payload(SECRET, BODY)
    flipped = bytearray(BODY)
    flipped[10] ^= 0x01
    for payload in (bytes(flipped), BODY + b" ", BODY[:-1], BODY.replace(b"reviewed", b"auto_approved"), b"{}", BODY[::-1]):
        assert verify_signature(SECRET, payload, header) is MISMATCH
    assert verify_signature(SECRET + "x", BODY, header) is MISMATCH
    assert verify_signature("other-secret", BODY, header) is MISMATCH


def test_a_signature_of_another_payload_or_with_one_hex_digit_changed_is_a_mismatch():
    other = sign_payload(SECRET, b"another body")
    assert verify_signature(SECRET, BODY, other) is MISMATCH
    genuine = sign_payload(SECRET, BODY)
    for index in (7, 20, len(genuine) - 1):
        digit = "0" if genuine[index] != "0" else "1"
        assert verify_signature(SECRET, BODY, genuine[:index] + digit + genuine[index + 1:]) is MISMATCH


def test_the_signature_is_over_the_exact_bytes_not_the_json_they_mean():
    import json

    reformatted = json.dumps(json.loads(BODY), indent=2).encode()
    assert json.loads(reformatted) == json.loads(BODY)
    assert verify_signature(SECRET, reformatted, sign_payload(SECRET, BODY)) is MISMATCH
    assert verify_signature(SECRET, BODY, sign_payload(SECRET, reformatted)) is MISMATCH


@pytest.mark.parametrize("header", [
    "", " ", "sha256=", "sha256", "=", "sha256=abc", "sha256=" + "a" * 63, "sha256=" + "a" * 65, "sha256=" + "A" * 64,
    "SHA256=" + "a" * 64, "sha1=" + "a" * 64, "sha256:" + "a" * 64, " sha256=" + "a" * 64, "sha256=" + "a" * 64 + " ",
    "sha256=" + "a" * 64 + "\n", "\nsha256=" + "a" * 64, "sha256=" + "g" * 64, "sha256=" + "٠" * 64, "sha256=" + "ａ" * 64,
    "sha256=" + "a" * 32 + " " + "a" * 31, "v1=" + "a" * 64, "sha256=sha256=" + "a" * 64, "x" * 1_000_000,
    None, 0, b"sha256=" + b"a" * 64, ["sha256=" + "a" * 64], {"sha256": "a" * 64}, True,
])
def test_a_malformed_signature_header_is_reported_malformed_and_never_compared(header, monkeypatch):
    calls = []
    monkeypatch.setattr(hmac, "compare_digest", lambda *a: calls.append(a) or False)
    assert verify_signature(SECRET, BODY, header) is MALFORMED
    assert calls == []


def test_a_header_with_the_right_shape_is_always_compared_never_skipped(monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    genuine = sign_payload(SECRET, BODY)
    assert verify_signature(SECRET, BODY, genuine) is VALID
    assert verify_signature(SECRET, BODY, "sha256=" + "0" * 64) is MISMATCH
    assert verify_signature(SECRET, BODY + b"x", genuine) is MISMATCH
    assert len(calls) == 3


# ============================================================================
# Constant-time comparison
# ============================================================================

def test_the_comparison_is_hmac_compare_digest_on_the_two_32_byte_digests(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    genuine = sign_payload(SECRET, BODY)
    assert verify_signature(SECRET, BODY, genuine) is VALID
    assert verify_signature(SECRET, BODY, "sha256=" + "f" * 64) is MISMATCH
    assert len(calls) == 2
    expected = hmac.new(SECRET.encode(), BODY, hashlib.sha256).digest()
    for left, right in calls:
        assert isinstance(left, bytes) and isinstance(right, bytes) and len(left) == len(right) == 32     # equal-length bytes
        assert left == expected                                                                            # the recomputed digest
    assert calls[0][1] == expected and calls[1][1] == bytes.fromhex("f" * 64)


def test_the_module_never_compares_signature_material_with_an_equality_operator():
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            assert not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops), ast.unparse(node)
    source = inspect.getsource(module)
    assert "compare_digest" in source and "hexdigest() ==" not in source and ".hexdigest()" not in source


# ============================================================================
# Invalid secrets and payloads
# ============================================================================

_BAD_SECRETS = [None, 0, 7, True, False, 1.5, [], ["s"], {}, b"", "", " ", "   ", "\n", "\t \n", bytearray(b"secret"),
                memoryview(b"secret"), "\ud800", "x" * (MAX_SECRET_BYTES + 1), b"x" * (MAX_SECRET_BYTES + 1), "é" * MAX_SECRET_BYTES]


@pytest.mark.parametrize("bad", _BAD_SECRETS)
def test_an_unusable_secret_is_refused_by_every_operation(bad):
    genuine = sign_payload(SECRET, BODY)
    for call in (lambda: compute_signature(bad, BODY), lambda: sign_payload(bad, BODY), lambda: verify_signature(bad, BODY, genuine)):
        with pytest.raises(WebhookSigningError) as caught:
            call()
        assert caught.value.code == "INVALID_SECRET" and str(caught.value) == "The webhook secret is not usable."


def test_a_secret_at_the_size_limit_is_usable():
    for secret in ("x" * MAX_SECRET_BYTES, b"x" * MAX_SECRET_BYTES, "k"):
        assert verify_signature(secret, BODY, sign_payload(secret, BODY)) is VALID


@pytest.mark.parametrize("bad", ["a string", None, 0, bytearray(b"body"), memoryview(b"body"), b"", [b"body"], {"a": 1}])
def test_a_payload_that_is_not_non_empty_bytes_is_refused(bad):
    genuine = sign_payload(SECRET, BODY)
    for call in (lambda: compute_signature(SECRET, bad), lambda: sign_payload(SECRET, bad), lambda: verify_signature(SECRET, bad, genuine)):
        with pytest.raises(WebhookSigningError) as caught:
            call()
        assert caught.value.code == "INVALID_PAYLOAD" and str(caught.value) == "The webhook payload must be non-empty bytes."


def test_an_unusable_secret_or_payload_is_refused_before_the_header_is_looked_at():
    with pytest.raises(WebhookSigningError):
        verify_signature("", BODY, "not a signature")
    with pytest.raises(WebhookSigningError):
        verify_signature(SECRET, b"", "not a signature")


def test_signing_errors_are_fixed_and_never_echo_the_secret_or_the_payload():
    hostile_secret = " " * 3                                       # unusable and sensitive-looking input must not appear
    for call in (lambda: sign_payload(hostile_secret, BODY), lambda: sign_payload(SECRET, "payload with " + SECRET)):
        with pytest.raises(WebhookSigningError) as caught:
            call()
        error = caught.value
        assert SECRET not in str(error) and SECRET not in repr(error) and error.__cause__ is None
    assert WebhookSigningError("anything").code == "INVALID_SECRET"
    for code, message in WebhookSigningError._MESSAGES.items():
        assert str(WebhookSigningError(code)) == message


# ============================================================================
# No leak, no state
# ============================================================================

def test_nothing_is_logged_by_any_operation(caplog):
    genuine = sign_payload(SECRET, BODY)
    with caplog.at_level(logging.DEBUG):
        assert verify_signature(SECRET, BODY, genuine) is VALID
        assert verify_signature(SECRET, BODY + b"x", genuine) is MISMATCH
        assert verify_signature(SECRET, BODY, "garbage") is MALFORMED
        for call in (lambda: sign_payload("", BODY), lambda: sign_payload(SECRET, b""), lambda: verify_signature(None, BODY, genuine)):
            with pytest.raises(WebhookSigningError):
                call()
    assert caplog.records == [] and SECRET not in caplog.text and genuine not in caplog.text


def test_the_module_has_no_logger_no_print_and_no_global_state():
    tree = ast.parse(inspect.getsource(module))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not {"logging", "os", "random", "secrets", "socket", "time", "datetime"} & imported
    assert not any(isinstance(node, ast.Name) and node.id in {"logger", "print", "global"} for node in ast.walk(tree))
    assert not any(isinstance(node, ast.Global) for node in ast.walk(tree))
    public_state = [
        name for name, value in vars(module).items()
        if not name.startswith("__") and isinstance(value, (list, dict, set, bytearray))
    ]
    assert public_state == []


def test_the_secret_is_not_retained_anywhere(monkeypatch):
    """The component keeps nothing between calls: a different secret in the next call gives an independent result."""
    first = sign_payload(SECRET, BODY)
    second = sign_payload("another-secret-value", BODY)
    assert first != second and sign_payload(SECRET, BODY) == first
    assert not any(SECRET in str(value) for value in vars(module).values())


# ============================================================================
# Signing real serialized events
# ============================================================================

def test_a_real_serialized_event_is_signed_verified_and_tampering_is_detected():
    event = document_completed(document_id="doc_1", status="reviewed", occurred_at=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc))
    body = serialize_event(event)
    header = sign_payload(SECRET, body)
    assert verify_signature(SECRET, body, header) is VALID
    assert parse_event(body) == event                                       # verify the raw bytes first, then parse
    tampered = body.replace(b"reviewed", b"auto_approved")
    assert parse_event(tampered).data.status == "auto_approved" and verify_signature(SECRET, tampered, header) is MISMATCH
    assert serialize_event(event) == body and sign_payload(SECRET, serialize_event(event)) == header   # deterministic end to end


# ============================================================================
# Hardening F4: byte secrets follow the same empty/blank rule as str secrets, through the one key path
# ============================================================================

_BLANK_BYTE_SECRETS = [
    b"", b" ", b"   ", b"\t", b"\n", b"\r\n", b" \t\r\n\x0b\x0c ",
    b"\x1c\x1d\x1e\x1f",                                                      # whitespace to str.strip() as well
    "\u00a0".encode(), "\u2003\u2003".encode(), "\u3000".encode(), "\u2028 \u2029".encode(),
]
_BINARY_SECRETS = [
    b"\x00", b"\x00\x00\x00", b"\x00\x01\xff", b"a\x00b", b"   \x00  ", b"\x00 \x00", b" \xff ", b"\xff", b"\xc3\x28",     # NUL and invalid UTF-8
    bytes(range(256))[:200], bytes(range(256))[::-1][:64], b"\x80" * 16, b"secret", b" s ", b"s\n",
]


@pytest.mark.parametrize("secret", _BLANK_BYTE_SECRETS)
def test_an_empty_or_whitespace_only_byte_secret_is_refused_by_every_operation(secret):
    genuine = sign_payload(SECRET, BODY)
    for call in (lambda: compute_signature(secret, BODY), lambda: sign_payload(secret, BODY), lambda: verify_signature(secret, BODY, genuine)):
        with pytest.raises(WebhookSigningError) as caught:
            call()
        assert caught.value.code == "INVALID_SECRET" and str(caught.value) == "The webhook secret is not usable."
        assert caught.value.__cause__ is None


@pytest.mark.parametrize("secret", _BINARY_SECRETS)
def test_arbitrary_binary_secrets_including_nul_bytes_are_valid_and_used_exactly_as_given(secret):
    signature = compute_signature(secret, BODY)
    assert signature == _independent(secret, BODY)
    header = sign_payload(secret, BODY)
    assert verify_signature(secret, BODY, header) is VALID
    assert verify_signature(secret + b"x", BODY, header) is MISMATCH and verify_signature(b"x" + secret, BODY, header) is MISMATCH


def test_a_str_and_its_utf8_bytes_are_accepted_or_refused_together_with_the_same_signature():
    candidates = [
        "", " ", "   ", "\t\n", "\u00a0", "\u2003", "\u3000", "\x1c", "\x1f", "\x00", "a\x00b", "\ufeff", "\x00\x00", "k", " s ", "s\n", "é", "é-ключ-鍵",
        "a b", "x" * MAX_SECRET_BYTES, "x" * (MAX_SECRET_BYTES + 1), "é" * 512, "é" * 513, "\ud800", "\ud800 ",
    ]
    for text in candidates:
        try:
            as_text = sign_payload(text, BODY)
        except WebhookSigningError as error:
            as_text = error.code
        try:
            as_bytes = sign_payload(text.encode("utf-8"), BODY)
        except UnicodeEncodeError:                                   # a lone surrogate has no UTF-8 bytes to compare
            assert as_text == "INVALID_SECRET"
            continue
        except WebhookSigningError as error:
            as_bytes = error.code
        assert as_text == as_bytes, repr(text)
        assert (as_text == "INVALID_SECRET") == (not text.strip() or len(text.encode("utf-8")) > MAX_SECRET_BYTES), repr(text)


def test_verification_agrees_for_a_str_secret_and_its_bytes():
    header = sign_payload("é-ключ-鍵", BODY)
    assert verify_signature("é-ключ-鍵".encode("utf-8"), BODY, header) is VALID
    assert verify_signature("é-ключ-鍵", BODY, sign_payload("é-ключ-鍵".encode("utf-8"), BODY)) is VALID


def test_signing_and_verification_share_one_key_path():
    import inspect

    assert "_key(" in inspect.getsource(module._digest)
    sources = [inspect.getsource(function) for function in (compute_signature, sign_payload, verify_signature)]
    assert not any("encode(" in source or "decode(" in source for source in sources)          # no second encoding anywhere
    assert inspect.getsource(module).count(".encode(") == 1 and inspect.getsource(module).count(".decode(") == 1


def test_a_blank_byte_secret_is_never_echoed_or_logged(caplog):
    with caplog.at_level(logging.DEBUG):
        for secret in (b"   ", b"", "\u00a0".encode()):
            with pytest.raises(WebhookSigningError) as caught:
                sign_payload(secret, BODY)
            assert repr(secret) not in repr(caught.value) and str(caught.value) == "The webhook secret is not usable."
    assert caplog.records == []


def test_the_documentation_states_the_blank_secret_rule_for_both_types():
    import inspect

    assert "whitespace-only" in inspect.getdoc(WebhookSigningError) and "UTF-8" in inspect.getdoc(module._key)
    register = " ".join((__import__("pathlib").Path(__file__).resolve().parents[1] / "docs" / "m5-open-decisions.md").read_text(encoding="utf-8").split())
    assert "arbitrary binary secrets, NUL bytes included, are valid" in register
