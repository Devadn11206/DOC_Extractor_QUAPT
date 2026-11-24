"""
Default-deny outbound policy (PRD SEC-011, ARC-001). Only loopback and explicitly allow-listed host:port pairs may be
reached; everything else fails closed before a socket is opened.
"""

import pytest

from doc_extractor.egress import ALLOWED_DESTINATIONS_ENV, EgressDeniedError, EgressPolicy
from doc_extractor.malware import ClamAVSocketScanner, ScanStatus


def test_default_policy_denies_every_external_destination():
    policy = EgressPolicy()
    assert policy.permits_host_port("api.example.com", 443) is False
    assert policy.permits_host_port("10.20.30.40", 8000) is False
    with pytest.raises(EgressDeniedError) as exc_info:
        policy.check_host_port("api.example.com", 443, purpose="test")
    assert exc_info.value.code == "EGRESS_DENIED"
    assert "api.example.com" not in str(exc_info.value.details)  # the target is never echoed


def test_loopback_is_always_permitted_because_it_leaves_no_host():
    policy = EgressPolicy()
    for host in ("localhost", "127.0.0.1", "::1"):
        assert policy.permits_host_port(host, 8000) is True


def test_an_allow_listed_host_and_port_is_permitted_and_nothing_else_on_that_host():
    policy = EgressPolicy(["clamd.internal:3310"])
    assert policy.permits_host_port("clamd.internal", 3310) is True
    assert policy.permits_host_port("CLAMD.internal", 3310) is True  # hostnames are case-insensitive
    assert policy.permits_host_port("clamd.internal", 3311) is False
    assert policy.permits_host_port("clamd.internal.evil.example", 3310) is False


def test_malformed_allow_list_entries_are_configuration_errors():
    for bad in ("no-port", "host:", "host:99999", "host:abc"):
        with pytest.raises(ValueError):
            EgressPolicy([bad])


def test_environment_allow_list_is_comma_separated(monkeypatch):
    monkeypatch.setenv(ALLOWED_DESTINATIONS_ENV, "vllm.internal:8000, clamd.internal:3310,")
    policy = EgressPolicy.from_environment()
    assert policy.permits_host_port("vllm.internal", 8000) is True
    assert policy.permits_host_port("clamd.internal", 3310) is True
    assert policy.permits_host_port("api.openai.com", 443) is False


def test_url_checks_use_scheme_default_ports_and_refuse_unknown_schemes():
    policy = EgressPolicy(["vllm.internal:443"])
    policy.check_url("https://vllm.internal/v1/chat/completions", purpose="test")  # default 443 matches
    with pytest.raises(EgressDeniedError):
        policy.check_url("http://vllm.internal/v1", purpose="test")  # default 80 is not allow-listed
    with pytest.raises(EgressDeniedError):
        policy.check_url("ftp://vllm.internal/x", purpose="test")
    with pytest.raises(EgressDeniedError):
        policy.check_url("file:///etc/passwd", purpose="test")
    with pytest.raises(EgressDeniedError):
        policy.check_url("https://api.openai.com/v1/chat/completions", purpose="test")


def test_clamav_tcp_address_outside_the_allow_list_fails_closed(monkeypatch):
    # A non-loopback clamd that is not allow-listed: refused before any connection is attempted, reported as ERROR.
    monkeypatch.delenv(ALLOWED_DESTINATIONS_ENV, raising=False)
    scanner = ClamAVSocketScanner("clamd.corp.example:3310", timeout_seconds=1)
    result = scanner.scan_stream(_stream(b"%PDF"))
    assert result.status == ScanStatus.ERROR
    assert "egress" in (result.details or "").lower()
    assert scanner.is_operational() is False


def _stream(payload: bytes):
    import io

    return io.BytesIO(payload)
