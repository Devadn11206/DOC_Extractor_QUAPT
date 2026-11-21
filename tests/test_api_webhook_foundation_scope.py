"""
Scope tests for the Phase 4 webhook dispatcher foundation: it is persistence-independent and does nothing in the world.

Verified: the four modules import no network, HTTP, async, thread, scheduler, database, web-framework, logging or repository code;
call no clock, sleep, random source (the one exception: ``uuid4`` for a new event id) or file or network function; behave
identically while sockets and clocks are forbidden; are used by nothing yet (no endpoint, router, worker or dispatcher exists);
and that the register documents the provisional wire format, the retry assumptions and the destination-policy requirements, with
its claim about the current ``target_url`` record checked against the real record. Synthetic data only.
"""

import ast
import inspect
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from doc_extractor.api.contracts import webhook_delivery, webhook_events
from doc_extractor.api.contracts.records import WebhookSubscriptionRecord
from doc_extractor.api.services import webhook_retry, webhook_signing

MODULES = {
    "webhook_events": webhook_events, "webhook_delivery": webhook_delivery,
    "webhook_signing": webhook_signing, "webhook_retry": webhook_retry,
}
API = Path(inspect.getsourcefile(webhook_events)).parents[1]
ROOT = API.parents[2]
REGISTER = (ROOT / "docs" / "m5-open-decisions.md").read_text(encoding="utf-8")
T = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

FORBIDDEN_IMPORTS = (
    "socket", "ssl", "select", "selectors", "http", "urllib", "requests", "httpx", "aiohttp", "websockets", "asyncio", "threading",
    "multiprocessing", "concurrent", "sched", "subprocess", "sqlite3", "sqlalchemy", "psycopg2", "redis", "boto3", "fastapi", "starlette",
    "logging", "os", "shutil", "tempfile", "pathlib", "time", "random", "secrets", "smtplib",
    "doc_extractor.api.ports", "doc_extractor.api.composition", "doc_extractor.api.dependencies", "doc_extractor.api.app",
    "doc_extractor.api.routers", "doc_extractor.api.services.review_task_service", "doc_extractor.storage",
)
FORBIDDEN_CALLS = {"now", "utcnow", "today", "time", "sleep", "monotonic", "perf_counter", "time_ns", "open", "print", "urlopen", "connect",
                   "getaddrinfo", "gethostbyname", "request", "send", "sendall", "post", "get", "put", "delete", "getenv"}


def _tree(module):
    return ast.parse(inspect.getsource(module))


def _imports(module):
    found = set()
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("name", sorted(MODULES))
def test_no_module_imports_network_database_scheduler_logging_or_repository_code(name):
    for imported in _imports(MODULES[name]):
        for forbidden in FORBIDDEN_IMPORTS:
            assert not (imported == forbidden or imported.startswith(forbidden + ".")), f"{name} imports {imported}"


@pytest.mark.parametrize("name", sorted(MODULES))
def test_no_module_calls_a_clock_sleep_file_or_network_function(name):
    offenders = []
    for node in ast.walk(_tree(MODULES[name])):
        if isinstance(node, ast.Call):
            called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if called in FORBIDDEN_CALLS:
                offenders.append((called, node.lineno))
    assert offenders == []


def test_randomness_is_used_only_to_make_a_new_event_id():
    users = {name: [n.lineno for n in ast.walk(_tree(module)) if isinstance(n, ast.Attribute) and n.attr in {"uuid4", "uuid1", "getrandbits"}]
             for name, module in MODULES.items()}
    assert {name for name, lines in users.items() if lines} == {"webhook_events"}
    source = inspect.getsource(webhook_events.new_event_id)
    assert "uuid.uuid4()" in source and "uuid1" not in inspect.getsource(webhook_events)


def test_every_module_works_unchanged_while_sockets_and_clocks_are_forbidden(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network or clock access")

    def run():
        event = webhook_events.document_failed(document_id="doc_1", reason_code="STAGE_TIMEOUT", occurred_at=T,
                                               event_id="evt_00000000000000000000000000000001")
        body = webhook_events.serialize_event(event)
        header = webhook_signing.sign_payload("synthetic-secret", body)
        decision = webhook_retry.schedule_retry(attempt_number=3, first_attempt_at=T, failed_at=T + timedelta(minutes=5),
                                                base_delay=timedelta(seconds=30), max_delay=timedelta(hours=1))
        attempt = webhook_delivery.DeliveryAttempt(
            subscription_id="sub_1", event_id=event.event_id, event_type=event.event_type, attempt_number=3,
            outcome=decision.attempt_outcome, attempted_at=T + timedelta(minutes=5), http_status=503, error_code="HTTP_5XX",
            next_attempt_at=decision.next_attempt_at)
        return body, header, decision, attempt, webhook_events.parse_event(body)

    before = run()
    for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname"):
        monkeypatch.setattr(socket, name, forbidden)
    for name in ("time", "monotonic", "sleep", "time_ns", "perf_counter"):
        monkeypatch.setattr(time, name, forbidden)
    after = run()
    assert after == before                                                                    # deterministic and offline
    assert webhook_signing.verify_signature("synthetic-secret", after[0], after[1]) is webhook_signing.SignatureVerification.VALID
    assert after[3].next_attempt_at == T + timedelta(minutes=5) + timedelta(minutes=2)


def test_nothing_in_the_package_uses_the_foundation_yet_so_no_endpoint_worker_or_dispatcher_exists():
    names = {"webhook_events", "webhook_delivery", "webhook_signing", "webhook_retry"}
    users = []
    for path in sorted(API.rglob("*.py")):
        if path.stem in names:
            continue
        text = path.read_text(encoding="utf-8")
        if any(f"import {name}" in text or f".{name} import" in text or f"{name} import" in text for name in names):
            users.append(path.relative_to(API).as_posix())
    assert users == []
    assert not [p.name for p in (API / "routers").glob("*") if "webhook" in p.name.lower()]
    assert "webhook" not in (API / "app.py").read_text(encoding="utf-8").lower().replace("webhook_subscription_repo", "").replace(
        "webhook_delivery_repo", "").replace("webhook routers are a later stage", "")


def test_no_new_port_adapter_or_persistence_object_was_added():
    ports = (API / "ports").glob("*.py")
    assert not [p.name for p in ports if "webhook" in p.name.lower()]
    for module in MODULES.values():
        classes = {name for name, obj in vars(module).items() if inspect.isclass(obj) and obj.__module__ == module.__name__}
        assert not {name for name in classes if name.endswith(("Repository", "Store", "Queue", "Adapter", "Client", "Dispatcher", "Worker"))}


# ============================================================================
# Documentation
# ============================================================================

def _section() -> str:
    marker = "### M5 Phase 4 Webhook Dispatcher Foundation Provisional Behavior and Limitations"
    assert marker in REGISTER
    return " ".join(REGISTER.split(marker, 1)[1].split()).lower()          # one line, lower case: wrapping and capitals do not matter


def test_the_register_documents_the_provisional_wire_format_and_signature_contract():
    text = _section()
    for required in ("x-webhook-signature", "sha256=", "hmac-sha256", "exact body bytes", "hmac.compare_digest", "no compatibility",
                     "provisional", "replayed", "m5-d09", "canonically", "no version field", "event_id", "at-least-once"):
        assert required in text, required
    assert webhook_signing.SIGNATURE_HEADER.lower() in text and webhook_signing.SIGNATURE_SCHEME in text


def test_the_register_documents_the_retry_assumptions_and_that_nothing_is_invented():
    text = _section()
    for required in ("required arguments", "no maximum attempt count", "24 hours", "doubling", "first attempt", "includes its end",
                     "window_expired", "not implemented"):
        assert required in text, required


def test_the_register_documents_every_destination_policy_requirement_and_that_none_is_implemented():
    text = _section()
    for required in ("requirements, not implemented", "https", "sec-004", "sec-011", "private", "loopback", "link-local", "metadata",
                     "dns rebinding", "resolve", "pin it", "redirects", "do not follow", "every delivery attempt", "timeout",
                     "response size", "callback_url", "ipv4-mapped", "egress"):
        assert required in text, required


def test_the_register_documents_the_persistence_dependencies_and_the_delivery_guarantee_limit():
    text = _section()
    for required in ("nothing here is durable", "persistent subscriptions", "event record", "outbox", "lease", "worker",
                     "webhookeventrepository", "does not exist", "remaining dependencies"):
        assert required in text, required


def test_the_register_keeps_the_related_decisions_open():
    for decision in ("M5-D09", "M5-D04", "M5-D12", "M5-D15"):
        section = REGISTER.split(f"### {decision} ", 1)[1].split("\n### ", 1)[0]
        assert "**Status.** Open" in section, decision


def test_the_documented_claim_about_the_current_target_url_is_true_of_the_real_record():
    """The register says a subscription record accepts ANY non-empty URL today (so registration needs a policy first)."""
    for url in ("http://127.0.0.1/hook", "http://169.254.169.254/latest/meta-data/", "ftp://example.test/x", "x"):
        record = WebhookSubscriptionRecord(subscription_id="sub_1", tenant_id="tenant_alpha", target_url=url,
                                           event_types=["document.completed"], secret_hash="not-a-signing-secret")
        assert record.target_url == url
    assert "accepts `http://127.0.0.1/...`" in _section()


# ============================================================================
# Hardening F5 and F6: the register's delivery-record mapping and the two added destination-policy requirements
# ============================================================================

def test_the_register_states_each_delivery_record_mapping_decision_and_leaves_the_rest_open():
    """Only the decisions the register states are asserted; the claims are checked against the real record and attempt contracts."""
    from doc_extractor.api.contracts.records import WebhookDeliveryRecord
    from doc_extractor.api.contracts.webhook_delivery import DeliveryAttempt, DeliveryOutcome

    text = _section()
    for stated in (
        "(a) `delivered` maps to `delivered`",
        "(b) `failed_retryable` maps to `failed`", "whose `next_retry_at` is not null is a non-terminal retryable failure",
        "(c) an `expired` attempt", "maps to `dead_letter`",
        "(d) the terminal statuses are `delivered` and `dead_letter`", "`pending`, and `failed` with a non-null `next_retry_at`, are not terminal",
        "(e) `failed_permanent` remains unmapped until an approved decision exists",
        "(f) `pending` has no corresponding outcome", "no completed attempt yet",
        "(g) a `deliveryattempt` has no `tenant_id` and no `delivery_id`: the dispatcher supplies both",
        "(h) `response_status_code` comes from `http_status` and `next_retry_at` from `next_attempt_at`",
        "(i) `error_code` is a fixed upper-case token, never arbitrary free text", "carried in `error_message`", "that record was not changed",
    ):
        assert stated in text, stated
    # the open question is stated as open, and neither mapping is claimed
    assert "left open:" in text and "`deliveryattempt.attempted_at` maps to the record's `created_at`" in text
    assert "or to its `completed_at`" in text and "is not decided here, and neither mapping is implemented or assumed" in text
    assert "attempted_at` is mapped to" not in text and "attempted_at` maps to `completed_at`" not in text

    # the claims are true of the real contracts
    status_description = WebhookDeliveryRecord.model_fields["status"].description
    assert all(status in status_description for status in ("pending", "delivered", "failed", "dead_letter"))
    assert {"tenant_id", "delivery_id"} <= set(WebhookDeliveryRecord.model_fields)
    assert not {"tenant_id", "delivery_id"} & set(DeliveryAttempt.model_fields)
    assert {"response_status_code", "next_retry_at", "created_at", "completed_at", "error_message"} <= set(WebhookDeliveryRecord.model_fields)
    assert {"http_status", "next_attempt_at", "attempted_at", "error_code"} <= set(DeliveryAttempt.model_fields)
    assert "transient failure" in WebhookDeliveryRecord.model_fields["next_retry_at"].description          # a retry is due: non-terminal
    assert "trigger" in WebhookDeliveryRecord.model_fields["created_at"].description
    assert "resolution" in WebhookDeliveryRecord.model_fields["completed_at"].description
    assert {member.value for member in DeliveryOutcome} == {"delivered", "failed_retryable", "failed_permanent", "expired"}
    assert "pending" not in {member.value for member in DeliveryOutcome}


def test_the_error_code_is_a_fixed_token_while_the_records_error_message_stays_free_text():
    """Statement (i): the attempt cannot carry free text; the record field that would carry the token is not constrained."""
    from pydantic import ValidationError

    from doc_extractor.api.contracts.records import WebhookDeliveryRecord
    from doc_extractor.api.contracts.webhook_delivery import DeliveryAttempt, DeliveryOutcome

    base = dict(subscription_id="sub_1", event_id="evt_00000000000000000000000000000001", event_type="document.failed", attempt_number=1,
                outcome=DeliveryOutcome.FAILED_PERMANENT, attempted_at=T)
    assert DeliveryAttempt(error_code="HTTP_4XX", **base).error_code == "HTTP_4XX"
    for free_text in ("a free text message", "lower_case", "ERROR with spaces", "X"):
        with pytest.raises(ValidationError):
            DeliveryAttempt(error_code=free_text, **base)
    record = WebhookDeliveryRecord(delivery_id="dlv_1", tenant_id="tenant_alpha", subscription_id="sub_1", event_id=base["event_id"],
                                   event_type="document.failed", error_message="any free text at all", created_at=T)
    assert record.error_message == "any free text at all"                      # why the register says the persistence slice must constrain it


def test_a_record_built_from_the_stated_mappings_only_has_the_documented_statuses_and_retry_state():
    """Only (a), (b), (c), (g) and (h) are used. ``created_at`` is a time the dispatcher chose, NOT derived from ``attempted_at`` (left
    open); ``completed_at`` and ``error_message`` are not set, because no decided mapping feeds them."""
    from doc_extractor.api.contracts.records import WebhookDeliveryRecord
    from doc_extractor.api.contracts.webhook_delivery import DeliveryAttempt, DeliveryOutcome

    dispatched = T - timedelta(minutes=10)                                     # a dispatcher-chosen time, independent of the attempt's
    base = dict(subscription_id="sub_1", event_id="evt_00000000000000000000000000000001", event_type="document.failed",
                attempt_number=3, attempted_at=T)
    cases = [
        (DeliveryAttempt(outcome=DeliveryOutcome.DELIVERED, http_status=200, **base), "delivered"),
        (DeliveryAttempt(outcome=DeliveryOutcome.FAILED_RETRYABLE, http_status=503, next_attempt_at=T + timedelta(minutes=4), **base), "failed"),
        (DeliveryAttempt(outcome=DeliveryOutcome.EXPIRED, **base), "dead_letter"),
    ]
    records = {}
    for attempt, status in cases:
        record = WebhookDeliveryRecord(
            delivery_id="dlv_1", tenant_id="tenant_alpha",                                              # (g) supplied by the dispatcher
            subscription_id=attempt.subscription_id, event_id=attempt.event_id, event_type=attempt.event_type,
            attempt_number=attempt.attempt_number, status=status,                                       # (a) (b) (c)
            response_status_code=attempt.http_status, next_retry_at=attempt.next_attempt_at,            # (h)
            created_at=dispatched)
        records[status] = record
        assert record.completed_at is None and record.error_message is None                            # nothing decided feeds them
    assert records["failed"].next_retry_at == T + timedelta(minutes=4)                                 # (b) non-terminal: a retry is due
    assert records["delivered"].next_retry_at is None and records["dead_letter"].next_retry_at is None  # (d) the terminal statuses
    assert records["delivered"].response_status_code == 200 and records["dead_letter"].response_status_code is None


def test_no_webhook_module_performs_or_assumes_a_record_mapping():
    """No undocumented mapping behavior: no module imports the persistence records or names their fields or statuses in code."""
    forbidden_names = {"WebhookDeliveryRecord", "WebhookSubscriptionRecord", "dead_letter", "completed_at", "created_at", "next_retry_at",
                       "response_status_code", "error_message", "delivery_id", "tenant_id"}
    for name, module in MODULES.items():
        tree = _tree(module)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                    docstrings.add(id(first.value))
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.keyword) and node.arg:
                used.add(node.arg)
            elif isinstance(node, ast.arg):
                used.add(node.arg)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                used.add(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                used.add(node.target.id)
        assert not used & forbidden_names, (name, used & forbidden_names)
        assert "doc_extractor.api.contracts.records" not in _imports(module), name


def test_the_register_lists_tls_verification_and_the_ambient_proxy_among_the_destination_requirements():
    text = _section()
    for required in ("(8) tls verification", "certificate chain", "matches the host name", "never disabled", "not even per subscription",
                     "(9) no ambient proxy", "http_proxy", "https_proxy", "all_proxy", "no_proxy", "proxy auto-configuration",
                     "ignores them", "explicit proxy that itself enforces the same policy", "callback_url"):
        assert required in text, required
    policy = text.split("destination policy: requirements, not implemented", 1)[1].split("persistence boundary", 1)[0]
    assert policy.index("(7)") < policy.index("(8)") < policy.index("(9)") < policy.index("callback_url")


def test_no_destination_check_dispatcher_or_http_client_was_added_with_those_requirements():
    for module in MODULES.values():
        names = {name.lower() for name in dir(module)}
        assert not {n for n in names if any(word in n for word in ("proxy", "ssrf", "verify_tls", "certificate", "validate_url", "validate_destination"))}
    assert "not implemented" in _section() and "no code validates a destination or sends a request" in _section()


def test_the_register_documents_the_time_zone_database_limitation_and_tzdata_is_still_not_declared():
    """The register says ``tzdata`` is not a declared dependency and was not added here: true of the dependency files today."""
    text = _section()
    for stated in ("known test-environment limitation (not fixed here)", "america/new_york", "needs a time-zone database",
                   "`tzdata` is not a declared dependency and was deliberately not added by this change",
                   "skip with a visible reason", "doc_fail_on_skip=1", "a skip fails the job", "future ci hardening"):
        assert stated in text, stated
    for name in ("requirements.txt", "requirements-dev.txt", "requirements-ui.txt", "requirements-vlm.txt", "pyproject.toml"):
        path = ROOT / name
        if path.exists():
            assert "tzdata" not in path.read_text(encoding="utf-8").lower(), name
