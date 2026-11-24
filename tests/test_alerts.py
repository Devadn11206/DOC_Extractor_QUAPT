"""
Security alerts (PRD SEC-021): each condition raises one alert per window, carries identifiers and counts only
(SEC-012), and never fails the operation that raised it.
"""

import asyncio

import pytest

from doc_extractor.alerts import (
    AlertCondition,
    AlertMonitor,
    SlidingWindowCounter,
    credential_fingerprint,
    get_default_monitor,
    set_default_monitor,
)
from doc_extractor.api.exceptions import AuthenticationRequiredError
from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
from doc_extractor.api.providers.composite_auth import CompositeAuthenticationProvider
from doc_extractor.egress import EgressPolicy


class Capture:
    def __init__(self):
        self.alerts = []

    def __call__(self, alert):
        self.alerts.append(alert)


@pytest.fixture
def capture():
    sink = Capture()
    previous = get_default_monitor()
    set_default_monitor(AlertMonitor(sink))
    yield sink
    set_default_monitor(previous)


# -- sliding window ----------------------------------------------------------------------------------------------

def test_a_breach_alerts_once_per_window_and_again_after_it():
    counter = SlidingWindowCounter(threshold=3, window_seconds=60)
    results = [counter.record("k", now=t) for t in (0, 1, 2, 3, 4)]
    assert results[:2] == [None, None]
    assert results[2] == 3  # breach at the third event
    assert results[3] is None and results[4] is None  # suppressed inside the window
    assert counter.record("k", now=100) is None  # the old events have expired, so one event is below threshold
    assert [counter.record("k", now=t) for t in (101, 102)] == [None, 3]  # a new breach alerts again


def test_keys_are_counted_independently():
    counter = SlidingWindowCounter(threshold=2, window_seconds=60)
    assert counter.record("tenant_a", now=0) is None
    assert counter.record("tenant_b", now=1) is None
    assert counter.record("tenant_a", now=2) == 2


def test_weighted_events_sum_toward_the_threshold():
    counter = SlidingWindowCounter(threshold=1000, window_seconds=60)
    assert counter.record("t", weight=600, now=0) is None
    assert counter.record("t", weight=600, now=1) == 1200


def test_invalid_threshold_or_window_is_rejected():
    with pytest.raises(ValueError):
        SlidingWindowCounter(threshold=0, window_seconds=60)
    with pytest.raises(ValueError):
        SlidingWindowCounter(threshold=1, window_seconds=0)


# -- conditions --------------------------------------------------------------------------------------------------

def test_repeated_failed_logins_alert_by_fingerprint_and_never_carry_the_token(capture):
    monitor = AlertMonitor(capture, failed_login_threshold=3)
    token = "eyJhbGciOi.payload.secretsignature"
    fingerprint = credential_fingerprint(token)
    for second in range(2):
        monitor.record_failed_login(fingerprint, now=second)
    assert capture.alerts == []  # below the threshold
    monitor.record_failed_login(fingerprint, now=2)  # the third failure breaches
    assert len(capture.alerts) == 1
    assert monitor.record_failed_login(fingerprint, now=4) is None  # suppressed: this window already alerted
    assert capture.alerts[0].condition == AlertCondition.REPEATED_FAILED_LOGINS
    assert capture.alerts[0].subject == fingerprint
    assert "secretsignature" not in repr(capture.alerts[0])


def test_the_fingerprint_is_short_and_not_the_token():
    fingerprint = credential_fingerprint("de_live_" + "x" * 43)
    assert len(fingerprint) == 16
    assert "de_live" not in fingerprint


def test_unusual_export_volume_alerts_on_total_rows(capture):
    monitor = AlertMonitor(capture, export_rows_threshold=1000)
    monitor.record_export("tenant_a", 400, now=0)
    monitor.record_export("tenant_a", 400, now=1)
    assert capture.alerts == []
    alert = monitor.record_export("tenant_a", 400, now=2)
    assert alert is not None and alert.condition == AlertCondition.UNUSUAL_EXPORT_VOLUME
    assert alert.subject == "tenant_a" and alert.count == 1200


def test_unmask_spike_is_counted_per_tenant_and_actor(capture):
    monitor = AlertMonitor(capture, unmask_threshold=3)
    for second in range(2):
        monitor.record_unmask("tenant_a", "user_1", now=second)
    alert = monitor.record_unmask("tenant_a", "user_1", now=2)
    assert alert.condition == AlertCondition.UNMASK_SPIKE
    assert alert.subject == "tenant_a:user_1"
    assert monitor.record_unmask("tenant_a", "user_2", now=3) is None  # a different actor starts at zero


def test_every_refused_egress_alerts_once_per_window_with_the_purpose_only(capture):
    policy = EgressPolicy()
    with pytest.raises(Exception):
        policy.check_host_port("api.openai.com", 443, purpose="litellm_extraction")
    assert len(capture.alerts) == 1
    alert = capture.alerts[0]
    assert alert.condition == AlertCondition.EGRESS_ATTEMPT
    assert alert.subject == "litellm_extraction"
    assert "openai" not in repr(alert)  # the target is never recorded


def test_loopback_traffic_raises_no_egress_alert(capture):
    EgressPolicy().check_host_port("127.0.0.1", 8000, purpose="vllm_extraction")
    assert capture.alerts == []


def test_a_broken_audit_chain_alerts_immediately(capture):
    monitor = AlertMonitor(capture)
    assert monitor.report_audit_chain("tenant_a", True, None, None) is None
    alert = monitor.report_audit_chain("tenant_a", False, "hash mismatch", 7)
    assert alert.condition == AlertCondition.AUDIT_CHAIN_FAILURE
    assert alert.details == {"reason": "hash mismatch", "first_bad_seq": 7}


def test_model_files_are_checked_against_pinned_hashes(tmp_path, capture):
    import hashlib

    model = tmp_path / "adapter.safetensors"
    model.write_bytes(b"trusted weights")
    good = hashlib.sha256(b"trusted weights").hexdigest()
    monitor = AlertMonitor(capture)

    assert monitor.check_model_files({"adapter.safetensors": good}, tmp_path) == []

    model.write_bytes(b"tampered weights")
    changed = monitor.check_model_files({"adapter.safetensors": good}, tmp_path)
    assert [a.details["reason"] for a in changed] == ["hash_mismatch"]

    missing = monitor.check_model_files({"gone.bin": good}, tmp_path)
    assert [a.details["reason"] for a in missing] == ["missing"]
    assert all(a.condition == AlertCondition.MODEL_FILE_CHANGE for a in changed + missing)


def test_a_failing_sink_never_fails_the_operation():
    def broken_sink(alert):
        raise RuntimeError("pager unreachable at https://internal.example/secret")

    monitor = AlertMonitor(broken_sink)
    alert = monitor.report_audit_chain("tenant_a", False, "hash mismatch", 1)
    assert alert is not None  # the caller still gets the alert; the failure is logged by type, not text


# -- wiring ----------------------------------------------------------------------------------------------------------

class _RejectingProvider:
    async def authenticate(self, credentials, **kwargs):
        raise AuthenticationRequiredError(message="no", code="INVALID_CREDENTIALS")


class _AcceptingProvider:
    async def authenticate(self, credentials, **kwargs):
        return ApiPrincipal(principal_id="u", tenant_id="t", principal_type=PrincipalType.USER)


def test_repeated_failed_authentication_through_the_router_raises_an_alert(capture):
    set_default_monitor(AlertMonitor(capture, failed_login_threshold=3))
    router = CompositeAuthenticationProvider(jwt_provider=_RejectingProvider(), api_key_provider=_RejectingProvider())
    token = "aaa.bbb.ccc"
    for _ in range(3):
        with pytest.raises(AuthenticationRequiredError):
            asyncio.run(router.authenticate(f"Bearer {token}"))
    assert len(capture.alerts) == 1
    assert capture.alerts[0].subject == credential_fingerprint(token)
    assert token not in repr(capture.alerts[0])


def test_a_successful_authentication_raises_no_alert(capture):
    set_default_monitor(AlertMonitor(capture, failed_login_threshold=1))
    router = CompositeAuthenticationProvider(jwt_provider=_AcceptingProvider())
    asyncio.run(router.authenticate("Bearer aaa.bbb.ccc"))
    assert capture.alerts == []


def test_queue_worker_failure_alert(capture):
    monitor = AlertMonitor(capture)
    alert = monitor.record_queue_worker_failure("job_999", "lease_expired")
    assert alert is not None
    assert alert.condition == AlertCondition.QUEUE_WORKER_FAILURE
    assert alert.subject == "job_999"
    assert alert.details["reason"] == "lease_expired"


def test_audit_checkpoint_failure_alert(capture):
    monitor = AlertMonitor(capture)
    alert = monitor.record_checkpoint_failure("tenant_audit_fail", "sink_unavailable")
    assert alert is not None
    assert alert.condition == AlertCondition.AUDIT_CHECKPOINT_FAILURE
    assert alert.subject == "tenant_audit_fail"
    assert alert.details["reason"] == "sink_unavailable"
