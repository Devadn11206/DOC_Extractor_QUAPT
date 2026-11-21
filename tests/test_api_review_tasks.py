"""
Tests for Phase 3, Slice 1: GET /api/v1/review-tasks (API-005), POST /api/v1/review-tasks/{id}/claim and /release (API-016).
PROVISIONAL behavior (decision M5-D11 is Open): states unassigned / claimed / completed, ownership by the authenticated
principal ID, idempotent repeated claim, owner-only release, no claim expiry, no display names.

The real application stack runs (real routes, dependencies, container, exception handlers). The only stand-in is the
repository port: ``tests/review_task_double.py`` is an in-memory double that is atomic per process. It is NOT a production
adapter, and these tests say nothing about cross-process atomicity (no production adapter exists).

Verified: listing filters, oldest-first ordering (instants, naive timestamps, ties), pagination, tenant isolation (the
concealed 404 and the unchanged foreign task), authorization (401 / 403 per scope), claim ownership, repeated claims, release
restrictions, concurrent claim and release races (threads, one event loop and over HTTP), that the race tests detect a
non-atomic double, fail-closed 503 without an adapter, sanitized handling of faulty or hostile adapters, and safe logging.
"""

import ast
import asyncio
import inspect
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from doc_extractor.api.app import create_app
from doc_extractor.api.composition import ApplicationContainer
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.contracts.documents import SCOPE_DOCUMENTS_READ
from doc_extractor.api.contracts.records import ReviewTaskRecord, ReviewTaskStatus
from doc_extractor.api.contracts.review_tasks import (
    SCOPE_REVIEW_TASKS_CLAIM,
    SCOPE_REVIEW_TASKS_READ,
    ReviewTaskResource,
)
from doc_extractor.api.exceptions import GENERIC_INTERNAL_MESSAGE, PortNotConfiguredError
from doc_extractor.api.ports.authentication import ApiPrincipal
from doc_extractor.api.ports.repositories import (
    ReviewTaskAssignee,
    ReviewTaskListQuery,
    ReviewTaskPage,
    ReviewTaskRepository,
    ReviewTaskTransition,
    ReviewTaskTransitionResult,
)
from doc_extractor.api.services import review_task_service as service_module
from doc_extractor.api.services.review_task_listing import build_review_task_page
from doc_extractor.exceptions import TenantMismatchError
from doc_extractor.malware import MockMalwareScanner
from tests.review_task_double import InMemoryReviewTaskRepository, RacyReviewTaskRepository
from tests.test_api_documents_page_image import MockAuthProvider

BASE = "/api/v1/review-tasks"
ALPHA = "tenant_alpha"
BETA = "tenant_beta"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
SECRET_NOTES = "SECRET-REVIEWER-NOTES-123"
SECRET_META = "SECRET-METADATA-456"

ONE = {"Authorization": "Bearer rev1"}
TWO = {"Authorization": "Bearer rev2"}
READER = {"Authorization": "Bearer reader"}
CLAIMER_ONLY = {"Authorization": "Bearer claimer-only"}
BETA_REV = {"Authorization": "Bearer beta-rev"}


def _task(task_id, *, tenant=ALPHA, doc="doc_1", status=ReviewTaskStatus.UNASSIGNED, owner=None, created=None, minutes=0, **extra):
    created = created if created is not None else T0 + timedelta(minutes=minutes)
    completed = created + timedelta(minutes=1) if status is ReviewTaskStatus.COMPLETED else None
    return ReviewTaskRecord(
        task_id=task_id, document_id=doc, tenant_id=tenant, status=status, assigned_reviewer_id=owner,
        created_at=created, updated_at=created, completed_at=completed, **extra,
    )


def _principals(app):
    both = {SCOPE_REVIEW_TASKS_READ, SCOPE_REVIEW_TASKS_CLAIM}
    table = app.state.auth_provider.principals
    table["rev1"] = ApiPrincipal(principal_id="rev_one", tenant_id=ALPHA, scopes=set(both), is_active=True)
    table["rev2"] = ApiPrincipal(principal_id="rev_two", tenant_id=ALPHA, scopes=set(both), is_active=True)
    table["reader"] = ApiPrincipal(principal_id="rev_reader", tenant_id=ALPHA, scopes={SCOPE_REVIEW_TASKS_READ}, is_active=True)
    table["claimer-only"] = ApiPrincipal(principal_id="rev_claimer", tenant_id=ALPHA, scopes={SCOPE_REVIEW_TASKS_CLAIM}, is_active=True)
    table["documents-only"] = ApiPrincipal(principal_id="doc_user", tenant_id=ALPHA, scopes={SCOPE_DOCUMENTS_READ}, is_active=True)
    table["beta-rev"] = ApiPrincipal(principal_id="rev_beta", tenant_id=BETA, scopes=set(both), is_active=True)
    table["inactive"] = ApiPrincipal(principal_id="rev_gone", tenant_id=ALPHA, scopes=set(both), is_active=False)
    for number in range(40):                                   # many reviewers of one tenant, for the HTTP race tests
        table[f"r{number}"] = ApiPrincipal(principal_id=f"rev_{number}", tenant_id=ALPHA, scopes=set(both), is_active=True)


def _make_api(tmp_path, tasks=(), repo="default"):
    settings = ApiSettings(app_env="development", storage_base_dir=str(tmp_path))
    if repo == "default":
        repo = InMemoryReviewTaskRepository(tasks)
    container = ApplicationContainer(
        api_settings=settings, auth_provider=MockAuthProvider(), malware_scanner=MockMalwareScanner(), review_task_repo=repo
    )
    app = create_app(settings=settings, container=container)
    _principals(app)
    return {"app": app, "client": TestClient(app, raise_server_exceptions=False), "repo": repo, "container": container}


@pytest.fixture
def api(tmp_path):
    tasks = [
        _task("t1", doc="doc_a", minutes=1),
        _task("t2", doc="doc_b", minutes=2, status=ReviewTaskStatus.CLAIMED, owner="rev_one"),
        _task("t3", doc="doc_c", minutes=3, status=ReviewTaskStatus.CLAIMED, owner="rev_two"),
        _task("t4", doc="doc_d", minutes=4, status=ReviewTaskStatus.COMPLETED, owner="rev_one"),
        _task("t5", doc="doc_e", minutes=5),
        _task("b1", tenant=BETA, doc="doc_beta", minutes=0),
    ]
    return _make_api(tmp_path, tasks)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())

    @property
    def text(self):
        return "\n".join(self.messages)


@pytest.fixture
def logs():
    handler = _Capture()
    names = ("doc_extractor.api.review_tasks", "doc_extractor.api.errors", "doc_extractor.api", "doc_extractor")
    loggers = [logging.getLogger(name) for name in names]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    yield handler
    for lg, level in zip(loggers, levels):
        lg.removeHandler(handler)
        lg.setLevel(level)


def _ids(response):
    return [row["task_id"] for row in response.json()["content"]]


# ============================================================================
# Record and port contracts
# ============================================================================

def test_the_provisional_task_vocabulary_is_unassigned_claimed_completed():
    assert [s.value for s in ReviewTaskStatus] == ["unassigned", "claimed", "completed"]
    assert ReviewTaskRecord(task_id="t", document_id="d", tenant_id=ALPHA).status is ReviewTaskStatus.UNASSIGNED


@pytest.mark.parametrize("fields", [
    dict(status="pending"), dict(status="in_review"), dict(status="approved"),                       # the old mixed vocabulary
    dict(status="unassigned", assigned_reviewer_id="x"),                                              # unassigned has no owner
    dict(status="claimed"), dict(status="claimed", assigned_reviewer_id="x", completed_at=T0),         # claimed: owner, not completed
    dict(status="completed"),                                                                          # completed: has a time
    dict(status="claimed", assigned_reviewer_id=""),
])
def test_the_record_rejects_states_that_contradict_the_invariants(fields):
    with pytest.raises(ValidationError):
        ReviewTaskRecord(task_id="t", document_id="d", tenant_id=ALPHA, **fields)


def test_the_public_resource_never_carries_internal_fields():
    record = _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one", reviewer_notes=SECRET_NOTES, metadata={"k": SECRET_META}, priority=9)
    resource = ReviewTaskResource.from_record(record, "rev_one")
    dumped = resource.model_dump_json()
    assert set(resource.model_dump()) == {
        "task_id", "document_id", "status", "assigned_reviewer_id", "assigned_to_me", "created_at", "updated_at", "completed_at",
    }
    assert SECRET_NOTES not in dumped and SECRET_META not in dumped and ALPHA not in dumped
    assert resource.assigned_to_me is True and ReviewTaskResource.from_record(record, "someone_else").assigned_to_me is False


def test_the_query_enforces_its_own_invariants():
    with pytest.raises(ValueError):
        ReviewTaskListQuery(assignee=ReviewTaskAssignee.MINE)                         # mine needs the principal
    with pytest.raises(ValueError):
        ReviewTaskListQuery(assignee=ReviewTaskAssignee.ALL, reviewer_id="x")         # and only mine carries it
    with pytest.raises(ValueError):
        ReviewTaskListQuery(page=0)
    with pytest.raises(ValueError):
        ReviewTaskListQuery(size=0)


def test_the_double_satisfies_the_extended_port_and_the_old_one_does_not():
    assert isinstance(InMemoryReviewTaskRepository(), ReviewTaskRepository)
    legacy = SimpleNamespace(
        get_by_id=None, get_by_document_id=None, create=None, update_status=None, list_tasks=None
    )
    assert not isinstance(legacy, ReviewTaskRepository)                               # claim / release / list_page are required


def test_the_port_documents_the_atomicity_contract_and_forbids_update_status_for_claims():
    doc = inspect.getdoc(ReviewTaskRepository)
    assert "ATOMICITY CONTRACT" in doc and "PRODUCTION PERSISTENCE DOES NOT EXIST" in doc
    assert "MUST NOT be used to claim or release" in inspect.getdoc(ReviewTaskRepository.update_status)


# ============================================================================
# Listing (API-005)
# ============================================================================

def test_the_list_returns_the_tenants_tasks_oldest_first_with_paging(api):
    response = api["client"].get(BASE, headers=ONE)
    assert response.status_code == 200
    body = response.json()
    assert _ids(response) == ["t1", "t2", "t3", "t4", "t5"]
    assert body["paging"] == {"page": 1, "size": 10, "total_items": 5, "total_pages": 1}
    assert set(body) == {"content", "paging"}
    assert response.headers["Cache-Control"] == "private, no-store"


def test_a_listed_task_carries_only_the_public_fields(tmp_path):
    api = _make_api(tmp_path, [_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one", reviewer_notes=SECRET_NOTES, metadata={"k": SECRET_META}, priority=7)])
    response = api["client"].get(BASE, headers=ONE)
    row = response.json()["content"][0]
    assert set(row) == {"task_id", "document_id", "status", "assigned_reviewer_id", "assigned_to_me", "created_at", "updated_at", "completed_at"}
    assert row["status"] == "claimed" and row["assigned_reviewer_id"] == "rev_one" and row["assigned_to_me"] is True
    assert SECRET_NOTES not in response.text and SECRET_META not in response.text and ALPHA not in response.text
    other = api["client"].get(BASE, headers=TWO).json()["content"][0]
    assert other["assigned_reviewer_id"] == "rev_one" and other["assigned_to_me"] is False


def test_the_order_is_by_instant_oldest_first_with_the_task_id_as_tie_breaker(tmp_path):
    ist = timezone(timedelta(hours=5, minutes=30))
    tasks = [
        _task("t_c", created=datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)),
        _task("t_a", created=datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)),                 # ties with t_c: id order
        _task("t_x", created=datetime(2026, 1, 1, 17, 31, tzinfo=ist)),                          # = 12:01 UTC (text sorts it last)
        _task("t_b", created=datetime(2026, 1, 1, 12, 0)),                                       # naive = UTC
        _task("t_old", created=datetime(2025, 12, 31, 23, 59, tzinfo=timezone.utc)),
    ]
    api = _make_api(tmp_path, tasks)
    assert _ids(api["client"].get(BASE, headers=ONE)) == ["t_old", "t_b", "t_x", "t_a", "t_c"]


def test_the_order_does_not_depend_on_insertion_order_or_priority(tmp_path):
    base = [_task(f"t{n}", minutes=n, priority=10 - n) for n in range(6)]
    forward = _ids(_make_api(tmp_path / "a", base)["client"].get(BASE, headers=ONE))
    backward = _ids(_make_api(tmp_path / "b", list(reversed(base)))["client"].get(BASE, headers=ONE))
    assert forward == backward == [f"t{n}" for n in range(6)]


def test_pagination_covers_every_task_once_and_a_page_past_the_end_is_empty(tmp_path):
    api = _make_api(tmp_path, [_task(f"t{n}", minutes=n) for n in range(5)])
    pages = [api["client"].get(BASE, params={"page": p, "size": 2}, headers=ONE) for p in (1, 2, 3, 4)]
    assert [_ids(r) for r in pages] == [["t0", "t1"], ["t2", "t3"], ["t4"], []]
    for response in pages:
        assert response.json()["paging"]["total_items"] == 5 and response.json()["paging"]["total_pages"] == 3
    assert pages[3].status_code == 200 and pages[3].json()["paging"]["page"] == 4


@pytest.mark.parametrize("params", [{"page": 0}, {"size": 0}, {"size": 101}, {"page": "x"}, {"size": "-1"}])
def test_invalid_paging_is_a_422(api, params):
    response = api["client"].get(BASE, params=params, headers=ONE)
    assert response.status_code == 422 and response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_the_status_filter(api):
    client = api["client"]
    assert _ids(client.get(BASE, params={"status": "unassigned"}, headers=ONE)) == ["t1", "t5"]
    assert _ids(client.get(BASE, params={"status": "claimed"}, headers=ONE)) == ["t2", "t3"]
    assert _ids(client.get(BASE, params={"status": "completed"}, headers=ONE)) == ["t4"]
    assert client.get(BASE, params={"status": "completed"}, headers=ONE).json()["paging"]["total_items"] == 1


def test_the_assignee_filter_uses_the_authenticated_principal(api):
    client = api["client"]
    assert _ids(client.get(BASE, params={"assignee": "mine"}, headers=ONE)) == ["t2", "t4"]
    assert _ids(client.get(BASE, params={"assignee": "mine"}, headers=TWO)) == ["t3"]
    assert _ids(client.get(BASE, params={"assignee": "unassigned"}, headers=ONE)) == ["t1", "t5"]
    assert _ids(client.get(BASE, params={"assignee": "all"}, headers=ONE)) == ["t1", "t2", "t3", "t4", "t5"]
    assert _ids(client.get(BASE, headers=ONE)) == _ids(client.get(BASE, params={"assignee": "all"}, headers=ONE))
    assert _ids(client.get(BASE, params={"assignee": "mine"}, headers=READER)) == []


def test_the_document_filter_and_combined_filters(api):
    client = api["client"]
    assert _ids(client.get(BASE, params={"document_id": "doc_c"}, headers=ONE)) == ["t3"]
    assert _ids(client.get(BASE, params={"document_id": "doc_nope"}, headers=ONE)) == []
    assert _ids(client.get(BASE, params={"status": "claimed", "assignee": "mine"}, headers=ONE)) == ["t2"]
    assert _ids(client.get(BASE, params={"status": "completed", "assignee": "unassigned"}, headers=ONE)) == []
    both = client.get(BASE, params={"status": "claimed", "assignee": "mine", "page": 1, "size": 1}, headers=TWO)
    assert _ids(both) == ["t3"] and both.json()["paging"]["total_items"] == 1


@pytest.mark.parametrize("params", [{"status": "pending"}, {"status": "approved"}, {"assignee": "everyone"}, {"assignee": "rev_two"}])
def test_an_invalid_filter_value_is_a_422(api, params):
    assert api["client"].get(BASE, params=params, headers=ONE).status_code == 422


def test_unknown_parameters_are_refused_and_never_echoed(api):
    response = api["client"].get(BASE, params={"tenant_id": BETA, "stauts": "claimed", "limit": "5"}, headers=ONE)
    assert response.status_code == 422 and response.json()["error"]["code"] == "VALIDATION_ERROR"
    for echoed in (BETA, "stauts", "limit"):
        assert echoed not in response.text


@pytest.mark.parametrize("name", ["documentType", "flagType", "slaAge"])
def test_the_prd_filters_without_data_are_a_422_not_silently_ignored(api, name):
    response = api["client"].get(BASE, params={name: "x"}, headers=ONE)
    assert response.status_code == 422
    assert response.json()["error"]["details"]["validation_errors"][0]["type"] == "unsupported_filter"


@pytest.mark.parametrize("value", ["a/b", "..", "doc 1", "x" * 129, "doc%00"])
def test_a_malformed_document_filter_is_a_422_without_echo(api, value):
    response = api["client"].get(BASE, params={"document_id": value}, headers=ONE)
    assert response.status_code == 422 and value not in response.text


def test_the_list_is_tenant_scoped_and_ignores_client_asserted_tenants(api):
    client = api["client"]
    assert _ids(client.get(BASE, headers=BETA_REV)) == ["b1"]
    assert client.get(BASE, headers=BETA_REV).json()["paging"]["total_items"] == 1
    spoofed = client.get(BASE, headers={**ONE, "X-Tenant-ID": BETA, "X-Tenant": BETA})
    assert "b1" not in _ids(spoofed) and spoofed.json()["paging"]["total_items"] == 5
    assert "b1" not in _ids(client.get(BASE, params={"status": "unassigned", "assignee": "all"}, headers=ONE))


def test_an_empty_queue_is_an_empty_page(tmp_path):
    response = _make_api(tmp_path)["client"].get(BASE, headers=ONE)
    assert response.status_code == 200
    assert response.json() == {"content": [], "paging": {"page": 1, "size": 10, "total_items": 0, "total_pages": 0}}


# ---- the pure listing function, directly ---------------------------------------------------------------------------------

def test_the_pure_listing_function_filters_orders_and_pages():
    tasks = [_task(f"t{n}", minutes=10 - n, doc=f"d{n % 2}") for n in range(7)]
    page = build_review_task_page(tasks, ReviewTaskListQuery(document_id="d0", page=2, size=2))
    assert page.total_items == 4                                                             # d0: t0, t2, t4, t6
    assert [t.task_id for t in page.items] == ["t2", "t0"]                                   # oldest first is t6, t4, t2, t0: page 2
    first = build_review_task_page(tasks, ReviewTaskListQuery(document_id="d0", page=1, size=2))
    assert [t.task_id for t in first.items] == ["t6", "t4"]


# ============================================================================
# Authorization and fail-closed behavior
# ============================================================================

@pytest.mark.parametrize("method, path", [("get", BASE), ("post", f"{BASE}/t1/claim"), ("post", f"{BASE}/t1/release")])
def test_every_route_requires_authentication(api, method, path):
    response = getattr(api["client"], method)(path)
    assert response.status_code == 401 and response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    bad = getattr(api["client"], method)(path, headers={"Authorization": "Bearer nobody"})
    assert bad.status_code == 401
    assert getattr(api["client"], method)(path, headers={"Authorization": "Bearer inactive"}).status_code == 401


@pytest.mark.parametrize("headers, method, path, expected", [
    (READER, "post", f"{BASE}/t1/claim", 403), (READER, "post", f"{BASE}/t2/release", 403),
    (CLAIMER_ONLY, "get", BASE, 403),
    ({"Authorization": "Bearer documents-only"}, "get", BASE, 403),
    ({"Authorization": "Bearer documents-only"}, "post", f"{BASE}/t1/claim", 403),
    (READER, "get", BASE, 200), (CLAIMER_ONLY, "post", f"{BASE}/t1/claim", 200),
])
def test_scopes_are_enforced_per_route(api, headers, method, path, expected):
    response = getattr(api["client"], method)(path, headers=headers)
    assert response.status_code == expected
    if expected == 403:
        assert response.json()["error"]["code"] == "FORBIDDEN_SCOPE"


def test_a_denied_request_changes_nothing(api):
    before = api["repo"].snapshot(ALPHA, "t1")
    assert api["client"].post(f"{BASE}/t1/claim", headers=READER).status_code == 403
    assert api["repo"].snapshot(ALPHA, "t1") == before and api["repo"].write_count == 0


@pytest.mark.parametrize("method, path", [("get", BASE), ("post", f"{BASE}/t1/claim"), ("post", f"{BASE}/t1/release")])
def test_without_a_repository_every_route_fails_closed_with_503_after_authentication(tmp_path, method, path):
    api = _make_api(tmp_path, repo=None)
    client = api["client"]
    assert getattr(client, method)(path).status_code == 401                                    # authentication first
    assert getattr(client, method)(path, headers=READER if method == "post" else CLAIMER_ONLY).status_code == 403   # then scope
    response = getattr(client, method)(path, headers=ONE)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["error"]["details"] == {"port": "ReviewTaskRepository"}


def test_the_application_ships_no_review_task_adapter_and_the_container_stays_unconfigured(tmp_path):
    settings = ApiSettings(app_env="development", storage_base_dir=str(tmp_path))
    container = ApplicationContainer(api_settings=settings, auth_provider=MockAuthProvider(), malware_scanner=MockMalwareScanner())
    assert container.review_task_repo is None
    with pytest.raises(PortNotConfiguredError):
        container.get_review_task_service(ALPHA, "rev_one")
    package = Path(inspect.getfile(service_module)).parents[1]
    for path in package.rglob("*.py"):
        assert "InMemoryReviewTaskRepository" not in path.read_text(encoding="utf-8"), path


# ============================================================================
# Claim (API-016)
# ============================================================================

def test_claiming_an_unassigned_task_makes_the_authenticated_principal_the_owner(api):
    before = api["repo"].snapshot(ALPHA, "t1")
    response = api["client"].post(f"{BASE}/t1/claim", headers=ONE)
    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "t1" and body["status"] == "claimed"
    assert body["assigned_reviewer_id"] == "rev_one" and body["assigned_to_me"] is True and body["completed_at"] is None
    stored = api["repo"].snapshot(ALPHA, "t1")
    assert stored.status is ReviewTaskStatus.CLAIMED and stored.assigned_reviewer_id == "rev_one"
    assert stored.updated_at > before.updated_at and stored.created_at == before.created_at
    assert response.headers["Cache-Control"] == "private, no-store"


def test_a_repeated_claim_by_the_owner_is_idempotent_and_writes_nothing(api):
    first = api["client"].post(f"{BASE}/t1/claim", headers=ONE)
    writes = api["repo"].write_count
    stamp = api["repo"].snapshot(ALPHA, "t1").updated_at
    for _ in range(3):
        again = api["client"].post(f"{BASE}/t1/claim", headers=ONE)
        assert again.status_code == 200 and again.json() == first.json()
    assert api["repo"].write_count == writes and api["repo"].snapshot(ALPHA, "t1").updated_at == stamp


def test_a_claim_of_an_already_owned_seed_task_is_idempotent_for_its_owner(api):
    response = api["client"].post(f"{BASE}/t2/claim", headers=ONE)                # t2 is owned by rev_one
    assert response.status_code == 200 and response.json()["assigned_to_me"] is True
    assert api["repo"].write_count == 0


def test_a_claim_of_a_task_owned_by_another_reviewer_is_a_409_naming_the_owner_and_changes_nothing(api):
    api["client"].post(f"{BASE}/t1/claim", headers=ONE)
    before = api["repo"].snapshot(ALPHA, "t1")
    response = api["client"].post(f"{BASE}/t1/claim", headers=TWO)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "TASK_CLAIMED" and error["details"] == {"claimed_by": "rev_one"}
    assert error["message"] == "This task is claimed by another reviewer."
    assert api["repo"].snapshot(ALPHA, "t1") == before                            # the task stays theirs


def test_a_claim_of_a_completed_task_is_a_409_task_completed(api):
    response = api["client"].post(f"{BASE}/t4/claim", headers=TWO)
    assert response.status_code == 409 and response.json()["error"]["code"] == "TASK_COMPLETED"
    assert api["repo"].snapshot(ALPHA, "t4").status is ReviewTaskStatus.COMPLETED and api["repo"].write_count == 0


def test_unknown_malformed_and_foreign_tasks_are_one_indistinguishable_404(api):
    client = api["client"]
    answers = set()
    for task_id in ("nope", "b1", "x" * 200, "b%201", "b.1", "b$1", "b1;", "b%C3%A91", "%20"):     # all reach the handler
        response = client.post(f"{BASE}/{task_id}/claim", headers=ONE)
        assert response.status_code == 404, task_id
        error = response.json()["error"]
        answers.add(json.dumps([error["code"], error["message"], error["details"]]))
    assert len(answers) == 1 and "REVIEW_TASK_NOT_FOUND" in next(iter(answers))
    for task_id in ("../b1", "t1%2F..%2Fb1", "t1/../b1"):                                         # never match a single task segment
        assert client.post(f"{BASE}/{task_id}/claim", headers=ONE).status_code == 404
    foreign = api["repo"].snapshot(BETA, "b1")
    assert foreign.status is ReviewTaskStatus.UNASSIGNED and foreign.assigned_reviewer_id is None
    assert client.post(f"{BASE}/b1/release", headers=ONE).json()["error"]["code"] == "REVIEW_TASK_NOT_FOUND"
    assert BETA not in client.post(f"{BASE}/b1/claim", headers=ONE).text


def test_a_tenant_can_claim_its_own_task_with_the_same_id_as_another_tenants(tmp_path):
    api = _make_api(tmp_path, [_task("same", doc="doc_a"), _task("same", tenant=BETA, doc="doc_b")])
    assert api["client"].post(f"{BASE}/same/claim", headers=BETA_REV).status_code == 200
    assert api["repo"].snapshot(ALPHA, "same").status is ReviewTaskStatus.UNASSIGNED
    assert api["repo"].snapshot(BETA, "same").assigned_reviewer_id == "rev_beta"


@pytest.mark.parametrize("path", ["claim", "release"])
def test_the_owner_comes_only_from_the_principal_so_input_is_refused(api, path):
    client = api["client"]
    for kwargs in (
        {"params": {"reviewer_id": "rev_two"}}, {"params": {"tenant_id": BETA}}, {"params": {"x": "1"}},
        {"json": {"reviewer_id": "rev_two"}}, {"json": {}}, {"content": b"x"},
        {"content": (chunk for chunk in [b"a"])},                                      # chunked, no Content-Length
    ):
        response = client.post(f"{BASE}/t1/{path}", headers=ONE, **kwargs)
        assert response.status_code == 422, kwargs
        assert "rev_two" not in response.text and BETA not in response.text
    assert api["repo"].write_count == 0
    assert client.post(f"{BASE}/t1/{path}", headers={**ONE, "X-Tenant-ID": BETA, "X-Reviewer-ID": "rev_two"}).status_code == 200
    assert api["repo"].snapshot(ALPHA, "t1").status in (ReviewTaskStatus.CLAIMED, ReviewTaskStatus.UNASSIGNED)


def test_a_header_claiming_another_identity_never_changes_the_owner(api):
    response = api["client"].post(f"{BASE}/t1/claim", headers={**ONE, "X-Reviewer-ID": "rev_two", "X-User": "rev_two"})
    assert response.status_code == 200 and response.json()["assigned_reviewer_id"] == "rev_one"


# ============================================================================
# Release (API-016)
# ============================================================================

def test_the_owner_releases_a_claimed_task(api):
    api["client"].post(f"{BASE}/t1/claim", headers=ONE)
    response = api["client"].post(f"{BASE}/t1/release", headers=ONE)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unassigned" and body["assigned_reviewer_id"] is None and body["assigned_to_me"] is False
    stored = api["repo"].snapshot(ALPHA, "t1")
    assert stored.status is ReviewTaskStatus.UNASSIGNED and stored.assigned_reviewer_id is None
    assert _ids(api["client"].get(BASE, params={"assignee": "unassigned"}, headers=TWO)).count("t1") == 1


def test_only_the_current_owner_may_release_and_a_refused_release_changes_nothing(api):
    before = api["repo"].snapshot(ALPHA, "t2")                                    # owned by rev_one
    response = api["client"].post(f"{BASE}/t2/release", headers=TWO)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_CLAIMED" and response.json()["error"]["details"] == {"claimed_by": "rev_one"}
    assert api["repo"].snapshot(ALPHA, "t2") == before and api["repo"].write_count == 0
    assert api["client"].post(f"{BASE}/t2/release", headers=READER).status_code == 403          # scope still required


def test_releasing_an_unassigned_task_is_idempotent_and_writes_nothing(api):
    before = api["repo"].snapshot(ALPHA, "t1")
    for headers in (ONE, TWO):
        response = api["client"].post(f"{BASE}/t1/release", headers=headers)
        assert response.status_code == 200 and response.json()["status"] == "unassigned"
    assert api["repo"].snapshot(ALPHA, "t1") == before and api["repo"].write_count == 0


def test_a_release_of_a_completed_task_is_a_409_task_completed(api):
    response = api["client"].post(f"{BASE}/t4/release", headers=ONE)               # even its former owner
    assert response.status_code == 409 and response.json()["error"]["code"] == "TASK_COMPLETED"
    assert api["repo"].snapshot(ALPHA, "t4").assigned_reviewer_id == "rev_one"


def test_after_a_release_another_reviewer_can_claim_and_the_old_owner_can_no_longer_release(api):
    client = api["client"]
    client.post(f"{BASE}/t1/claim", headers=ONE)
    assert client.post(f"{BASE}/t1/release", headers=ONE).status_code == 200
    assert client.post(f"{BASE}/t1/claim", headers=TWO).json()["assigned_reviewer_id"] == "rev_two"
    stale = client.post(f"{BASE}/t1/release", headers=ONE)                         # a retried release by the former owner
    assert stale.status_code == 409 and stale.json()["error"]["details"] == {"claimed_by": "rev_two"}
    assert api["repo"].snapshot(ALPHA, "t1").assigned_reviewer_id == "rev_two"


def test_there_is_no_automatic_expiry_a_claim_survives_an_old_timestamp(tmp_path):
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    api = _make_api(tmp_path, [_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one", created=old)])
    assert api["client"].post(f"{BASE}/t1/claim", headers=TWO).status_code == 409
    assert api["repo"].snapshot(ALPHA, "t1").assigned_reviewer_id == "rev_one"


def test_a_release_in_another_tenant_is_the_concealed_404_and_changes_nothing(api):
    api["client"].post(f"{BASE}/b1/claim", headers=BETA_REV)
    assert api["client"].post(f"{BASE}/b1/release", headers=ONE).status_code == 404
    assert api["repo"].snapshot(BETA, "b1").assigned_reviewer_id == "rev_beta"


# ============================================================================
# Concurrency
# ============================================================================

def _run_threads(count, work):
    """Run ``work(index)`` in ``count`` threads released together by a barrier; return their results by index."""
    barrier = threading.Barrier(count)
    results = [None] * count
    errors = []

    def runner(index):
        try:
            barrier.wait(timeout=10)
            results[index] = work(index)
        except BaseException as exc:                          # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors
    return results


def _claim_outcomes(repo, reviewers, task_id="t1"):
    return [r.outcome for r in _run_threads(
        len(reviewers), lambda i: asyncio.run(repo.claim(ALPHA, task_id, reviewers[i], T0 + timedelta(hours=1))))]


def test_concurrent_claims_by_different_reviewers_have_exactly_one_winner_across_threads():
    for round_number in range(40):
        repo = InMemoryReviewTaskRepository([_task("t1")])
        reviewers = [f"rev_{n}" for n in range(24)]
        outcomes = _claim_outcomes(repo, reviewers)
        assert outcomes.count(ReviewTaskTransition.CLAIMED) == 1, round_number
        assert outcomes.count(ReviewTaskTransition.HELD_BY_OTHER) == 23
        winner = reviewers[outcomes.index(ReviewTaskTransition.CLAIMED)]
        assert repo.snapshot(ALPHA, "t1").assigned_reviewer_id == winner and repo.write_count == 1


def test_concurrent_claims_by_the_same_reviewer_all_succeed_with_a_single_write():
    repo = InMemoryReviewTaskRepository([_task("t1")])
    outcomes = _claim_outcomes(repo, ["rev_one"] * 20)
    assert outcomes.count(ReviewTaskTransition.CLAIMED) == 1 and outcomes.count(ReviewTaskTransition.ALREADY_OWNED) == 19
    assert repo.write_count == 1 and repo.snapshot(ALPHA, "t1").assigned_reviewer_id == "rev_one"


def test_concurrent_claims_on_one_event_loop_have_exactly_one_winner():
    async def scenario():
        repo = InMemoryReviewTaskRepository([_task("t1")])
        results = await asyncio.gather(*[repo.claim(ALPHA, "t1", f"rev_{n}", T0) for n in range(30)])
        return repo, [r.outcome for r in results]

    repo, outcomes = asyncio.run(scenario())
    assert outcomes.count(ReviewTaskTransition.CLAIMED) == 1 and outcomes.count(ReviewTaskTransition.HELD_BY_OTHER) == 29


def test_the_race_tests_detect_a_non_atomic_claim():
    """The harness has teeth: the SAME scenario on a check-yield-write double yields several 'winners'."""
    async def scenario():
        repo = RacyReviewTaskRepository([_task("t1")])
        results = await asyncio.gather(*[repo.claim(ALPHA, "t1", f"rev_{n}", T0) for n in range(10)])
        return [r.outcome for r in results]

    outcomes = asyncio.run(scenario())
    assert outcomes.count(ReviewTaskTransition.CLAIMED) > 1


def test_a_release_racing_with_claims_leaves_a_consistent_single_owner_or_none():
    for round_number in range(40):
        repo = InMemoryReviewTaskRepository([_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_owner")])
        challengers = [f"rev_{n}" for n in range(11)]

        def work(index):
            if index == 0:
                return ("release", asyncio.run(repo.release(ALPHA, "t1", "rev_owner", T0 + timedelta(hours=1))).outcome)
            return ("claim", asyncio.run(repo.claim(ALPHA, "t1", challengers[index - 1], T0 + timedelta(hours=1))).outcome)

        results = _run_threads(12, work)
        release_outcome = results[0][1]
        claims = [outcome for kind, outcome in results if kind == "claim"]
        winners = [challengers[i - 1] for i, (kind, outcome) in enumerate(results) if kind == "claim" and outcome is ReviewTaskTransition.CLAIMED]
        assert release_outcome is ReviewTaskTransition.RELEASED, round_number          # the owner can always release
        assert len(winners) <= 1
        assert set(claims) <= {ReviewTaskTransition.CLAIMED, ReviewTaskTransition.HELD_BY_OTHER}
        stored = repo.snapshot(ALPHA, "t1")
        if winners:
            assert stored.assigned_reviewer_id == winners[0] and stored.status is ReviewTaskStatus.CLAIMED
        else:
            assert stored.assigned_reviewer_id is None and stored.status is ReviewTaskStatus.UNASSIGNED


def test_concurrent_http_claims_have_one_200_and_the_rest_409_naming_the_same_owner(tmp_path):
    for round_number in range(8):
        api = _make_api(tmp_path / f"r{round_number}", [_task("t1")])
        client = api["client"]
        responses = _run_threads(16, lambda i: client.post(f"{BASE}/t1/claim", headers={"Authorization": f"Bearer r{i}"}))
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [200] + [409] * 15, (round_number, statuses)
        winner = next(r for r in responses if r.status_code == 200).json()["assigned_reviewer_id"]
        for response in responses:
            if response.status_code == 409:
                assert response.json()["error"] == {
                    "code": "TASK_CLAIMED", "message": "This task is claimed by another reviewer.",
                    "request_id": response.json()["error"]["request_id"], "details": {"claimed_by": winner},
                }
        assert api["repo"].snapshot(ALPHA, "t1").assigned_reviewer_id == winner and api["repo"].write_count == 1


def test_concurrent_http_claims_of_different_tasks_do_not_interfere(tmp_path):
    api = _make_api(tmp_path, [_task(f"t{n}") for n in range(12)])
    client = api["client"]
    responses = _run_threads(12, lambda i: client.post(f"{BASE}/t{i}/claim", headers={"Authorization": f"Bearer r{i}"}))
    assert [r.status_code for r in responses] == [200] * 12
    for number in range(12):
        assert api["repo"].snapshot(ALPHA, f"t{number}").assigned_reviewer_id == f"rev_{number}"


# ============================================================================
# Faulty or hostile adapters: sanitized, never forwarded
# ============================================================================

def _scripted(**methods):
    defaults = dict(
        claim=AsyncMock(), release=AsyncMock(), list_page=AsyncMock(),
        get_by_id=AsyncMock(), get_by_document_id=AsyncMock(), create=AsyncMock(), update_status=AsyncMock(), list_tasks=AsyncMock(),
    )
    defaults.update(methods)
    return SimpleNamespace(**defaults)


def _result(outcome, task):
    return ReviewTaskTransitionResult(outcome, task)


FOREIGN = _task("b1", tenant=BETA, doc="doc_foreign", status=ReviewTaskStatus.CLAIMED, owner="rev_beta", reviewer_notes=SECRET_NOTES)


@pytest.mark.parametrize("path, outcome, record", [
    ("claim", ReviewTaskTransition.CLAIMED, FOREIGN),                                                    # another tenant's record
    ("claim", ReviewTaskTransition.CLAIMED, _task("t1", tenant=BETA, status=ReviewTaskStatus.CLAIMED, owner="rev_one")),   # valid but for the tenant
    ("claim", ReviewTaskTransition.HELD_BY_OTHER, _task("t1", tenant=BETA, status=ReviewTaskStatus.CLAIMED, owner="rev_beta")),
    ("claim", ReviewTaskTransition.COMPLETED, _task("t1", tenant=BETA, status=ReviewTaskStatus.COMPLETED)),
    ("release", ReviewTaskTransition.RELEASED, _task("t1", tenant=BETA)),                                  # valid but for the tenant
    ("release", ReviewTaskTransition.ALREADY_UNASSIGNED, _task("t1", tenant=BETA)),
    ("claim", ReviewTaskTransition.CLAIMED, _task("t1")),                                                # claimed but unassigned
    ("claim", ReviewTaskTransition.CLAIMED, _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_two")),   # not the caller's
    ("claim", ReviewTaskTransition.CLAIMED, _task("other_id", status=ReviewTaskStatus.CLAIMED, owner="rev_one")),   # another task
    ("claim", ReviewTaskTransition.HELD_BY_OTHER, _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one")),   # held by me?
    ("claim", ReviewTaskTransition.HELD_BY_OTHER, _task("t1")),                                          # held but unassigned
    ("claim", ReviewTaskTransition.COMPLETED, _task("t1")),                                              # completed but not
    ("claim", ReviewTaskTransition.RELEASED, _task("t1")),                                               # outcome of the other operation
    ("claim", ReviewTaskTransition.CLAIMED, None),
    ("release", ReviewTaskTransition.RELEASED, _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one")),   # still claimed
    ("release", ReviewTaskTransition.RELEASED, FOREIGN),
    ("release", ReviewTaskTransition.CLAIMED, _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one")),
    ("release", ReviewTaskTransition.ALREADY_UNASSIGNED, None),
])
def test_an_adapter_result_that_breaks_the_contract_is_a_generic_500_and_is_never_forwarded(tmp_path, logs, path, outcome, record):
    repo = _scripted(**{path: AsyncMock(return_value=_result(outcome, record))})
    response = _make_api(tmp_path, repo=repo)["client"].post(f"{BASE}/t1/{path}", headers=ONE)
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "INTERNAL_ERROR" and error["message"] == GENERIC_INTERNAL_MESSAGE and error["details"] == {}
    for leaked in (SECRET_NOTES, BETA, "rev_beta", "doc_foreign", '"b1"'):
        assert leaked not in response.text
    assert "reason=review_task_adapter_contract_violation" in logs.text
    assert SECRET_NOTES not in logs.text


def test_a_non_enum_outcome_is_a_generic_500(tmp_path):
    repo = _scripted(claim=AsyncMock(return_value=SimpleNamespace(outcome="claimed", task=_task("t1"))))
    assert _make_api(tmp_path, repo=repo)["client"].post(f"{BASE}/t1/claim", headers=ONE).status_code == 500


def test_a_repository_exception_is_sanitized_and_only_its_class_is_logged(tmp_path, logs):
    secret = "SECRET-DB-DETAIL postgres://user:pass@host/db"
    for path in ("claim", "release"):
        repo = _scripted(**{path: AsyncMock(side_effect=RuntimeError(secret))})
        response = _make_api(tmp_path / path, repo=repo)["client"].post(f"{BASE}/t1/{path}", headers=ONE)
        assert response.status_code == 500 and response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert "SECRET" not in response.text and "postgres" not in response.text
    assert "SECRET" not in logs.text and "postgres" not in logs.text and "pass" not in logs.text
    assert "reason=review_task_repository_fault" in logs.text and "exception=RuntimeError" in logs.text


def test_a_list_exception_is_sanitized(tmp_path, logs):
    repo = _scripted(list_page=AsyncMock(side_effect=ConnectionError("SECRET-HOST-NAME")))
    response = _make_api(tmp_path, repo=repo)["client"].get(BASE, headers=ONE)
    assert response.status_code == 500 and "SECRET" not in response.text and "SECRET" not in logs.text


def test_a_cross_tenant_signal_from_the_adapter_is_the_concealed_task_404(tmp_path):
    repo = _scripted(claim=AsyncMock(side_effect=TenantMismatchError("tenant_beta leaked")))
    response = _make_api(tmp_path, repo=repo)["client"].post(f"{BASE}/t1/claim", headers=ONE)
    assert response.status_code == 404 and response.json()["error"]["code"] == "REVIEW_TASK_NOT_FOUND"
    assert "leaked" not in response.text and BETA not in response.text


def test_the_service_passes_the_authenticated_tenant_and_principal_to_the_port(tmp_path):
    repo = _scripted(claim=AsyncMock(return_value=_result(ReviewTaskTransition.NOT_FOUND, None)))
    _make_api(tmp_path, repo=repo)["client"].post(f"{BASE}/t1/claim", headers={**ONE, "X-Tenant-ID": BETA})
    tenant, task, reviewer, now = repo.claim.await_args.args
    assert (tenant, task, reviewer) == (ALPHA, "t1", "rev_one") and now.tzinfo is not None


@pytest.mark.parametrize("bad_page", [
    ReviewTaskPage(items=(_task("t1", tenant=BETA),), total_items=1),                   # a real page, but another tenant's task
    ReviewTaskPage(items=(_task("t1"), _task("t2", tenant=BETA)), total_items=2),       # one foreign task among the tenant's own
    SimpleNamespace(items=(FOREIGN,), total_items=1),                                   # not a ReviewTaskPage at all
    SimpleNamespace(items=[_task("t1")], total_items=1),                               # not a tuple
    SimpleNamespace(items=(_task("t1"), _task("t2")), total_items=1),                  # total smaller than the page
    None,
])
def test_a_list_page_that_breaks_the_contract_is_a_generic_500(tmp_path, bad_page):
    repo = _scripted(list_page=AsyncMock(return_value=bad_page))
    response = _make_api(tmp_path, repo=repo)["client"].get(BASE, headers=ONE)
    assert response.status_code == 500 and response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert BETA not in response.text and '"b1"' not in response.text and "rev_beta" not in response.text


def test_a_page_larger_than_the_requested_size_is_refused(tmp_path):
    page = ReviewTaskPage(items=(_task("t1"), _task("t2")), total_items=2)
    repo = _scripted(list_page=AsyncMock(return_value=page))
    assert _make_api(tmp_path, repo=repo)["client"].get(BASE, params={"size": 1}, headers=ONE).status_code == 500


# ============================================================================
# Logging (SEC-012)
# ============================================================================

def test_the_logs_carry_only_outcome_task_id_and_request_id(api, logs):
    api["client"].post(f"{BASE}/t1/claim", headers=ONE)
    api["client"].post(f"{BASE}/t1/claim", headers=TWO)
    api["client"].post(f"{BASE}/t1/release", headers=ONE)
    text = logs.text
    assert "outcome=claimed" in text and "outcome=held_by_other" in text and "outcome=released" in text
    assert "task_id=t1" in text and "request_id=" in text
    for forbidden in ("rev_one", "rev_two", ALPHA, SECRET_NOTES, "Bearer", "claimed_by"):
        assert forbidden not in text


def test_the_service_logging_calls_are_constant_messages_without_identities():
    tree = ast.parse(inspect.getsource(service_module))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "logger"]
    assert calls
    for call in calls:
        assert isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)       # no f-string, no concatenation
        rendered = " ".join(ast.unparse(arg) for arg in call.args[1:])
        for forbidden in ("principal", "tenant", "message", "str(exc)", "repr", "notes", "metadata"):
            assert forbidden not in rendered, rendered


def test_the_responses_never_contain_the_tenant_or_internal_fields(api):
    for response in (
        api["client"].get(BASE, headers=ONE), api["client"].post(f"{BASE}/t1/claim", headers=ONE),
        api["client"].post(f"{BASE}/t1/claim", headers=TWO), api["client"].post(f"{BASE}/t1/release", headers=ONE),
    ):
        assert ALPHA not in response.text and "tenant_id" not in response.text
        assert "reviewer_notes" not in response.text and "metadata" not in response.text and "priority" not in response.text


# ============================================================================
# Contract
# ============================================================================

def test_the_routes_are_in_the_openapi_contract_with_their_errors(api):
    schema = api["app"].openapi()
    listing = schema["paths"][BASE]["get"]
    assert {"200", "401", "403", "422", "500", "503"} <= set(listing["responses"])
    for action in ("claim", "release"):
        operation = schema["paths"][f"{BASE}/{{task_id}}/{action}"]["post"]
        assert {"200", "401", "403", "404", "409", "422", "500", "503"} <= set(operation["responses"])
        assert "PROVISIONAL" in operation["description"]
    assert f"{BASE}/{{task_id}}/complete" not in schema["paths"]                       # completion is not part of this slice


def test_completion_and_other_excluded_routes_do_not_exist(api):
    client = api["client"]
    assert client.post(f"{BASE}/t1/complete", headers=ONE).status_code in (404, 405)
    assert client.delete(f"{BASE}/t1", headers=ONE).status_code in (404, 405)
    assert client.patch(f"{BASE}/t1", headers=ONE, json={"status": "completed"}).status_code in (404, 405)
    assert client.get(f"{BASE}/t1", headers=ONE).status_code in (404, 405)


def test_previously_accepted_routes_are_unaffected(api):
    assert api["client"].get("/api/v1/health").status_code in (200, 503)
    assert api["client"].get("/api/v1/documents", headers=ONE).status_code == 403        # review scopes do not open documents
