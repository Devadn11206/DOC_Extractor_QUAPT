"""
Tests for the review-task COMPLETION PORT FOUNDATION of Phase 3, Slice 2A, PROVISIONAL (decision M5-D11 is Open).

Covered: the extended ``ReviewTaskRepository`` contract (``complete`` and its explicit outcomes), the atomic in-memory test
double under ``tests/``, ``ReviewTaskService.complete`` (outcome mapping and the checks of what an adapter returns), tenant
isolation, ownership and state restrictions, repeated and concurrent completions (threads, one event loop), completion racing
release and claim, and that the race tests detect a non-atomic double.

NOT covered, by design: there is no completion endpoint, no production adapter, and this contract covers the review-task
RECORD only. It claims no atomic update of document status, no persisted corrections and no durable audit record.
"""

import ast
import asyncio
import inspect
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from doc_extractor.api.contracts.records import ReviewTaskRecord, ReviewTaskStatus
from doc_extractor.api.exceptions import (
    ReviewTaskClaimedError,
    ReviewTaskCompletedError,
    ReviewTaskNotClaimedError,
    ReviewTaskNotFoundError,
)
from doc_extractor.api.ports.repositories import (
    ReviewTaskRepository,
    ReviewTaskTransition,
    ReviewTaskTransitionResult,
)
from doc_extractor.api.routers import review_tasks as router_module
from doc_extractor.api.services import review_task_service as service_module
from doc_extractor.api.services.review_task_service import ReviewTaskService
from doc_extractor.exceptions import StorageError, TenantMismatchError
from tests.review_task_double import InMemoryReviewTaskRepository, RacyReviewTaskRepository

ALPHA = "tenant_alpha"
BETA = "tenant_beta"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
LATER = T0 + timedelta(hours=2)
SECRET_NOTES = "SYNTH-NOTES-846"


def _task(task_id, *, tenant=ALPHA, status=ReviewTaskStatus.UNASSIGNED, owner=None, completed_at=None, **extra):
    if status is ReviewTaskStatus.COMPLETED and completed_at is None:
        completed_at = T0 + timedelta(minutes=5)
    return ReviewTaskRecord(
        task_id=task_id, document_id="doc_synth", tenant_id=tenant, status=status, assigned_reviewer_id=owner,
        created_at=T0, updated_at=T0, completed_at=completed_at, **extra,
    )


def _repo(*tasks):
    return InMemoryReviewTaskRepository(tasks)


def _run(coro):
    return asyncio.run(coro)


def _complete(repo, task_id="t1", reviewer="rev_one", tenant=ALPHA, now=LATER):
    return _run(repo.complete(tenant, task_id, reviewer, now))


# ============================================================================
# The port contract
# ============================================================================

def test_the_port_requires_complete_and_a_repository_without_it_is_not_a_port():
    assert isinstance(InMemoryReviewTaskRepository(), ReviewTaskRepository)
    lacking = SimpleNamespace(
        claim=None, release=None, list_page=None, get_by_id=None, get_by_document_id=None, create=None,
        update_status=None, list_tasks=None,
    )
    assert not isinstance(lacking, ReviewTaskRepository)


def test_the_completion_outcomes_are_explicit_and_additive():
    values = {t.value for t in ReviewTaskTransition}
    assert {"completion_recorded", "already_completed", "not_claimed"} <= values
    assert {"claimed", "already_owned", "released", "already_unassigned", "held_by_other", "completed", "not_found"} <= values


def test_the_port_documents_the_boundary_of_the_completion_contract():
    doc = inspect.getdoc(ReviewTaskRepository.complete)
    assert "REVIEW-TASK RECORD ONLY" in doc and "NOT atomic with any of those" in doc
    assert "document's status" in doc and "audit" in doc and "corrections" in doc
    assert "PROVISIONAL" in doc and "M5-D11" in doc
    assert "ATOMICITY CONTRACT" in inspect.getdoc(ReviewTaskRepository)


def test_the_double_documents_that_it_is_the_task_record_only_and_stays_under_tests():
    assert "task RECORD only" in inspect.getdoc(InMemoryReviewTaskRepository.complete)
    assert Path(inspect.getfile(InMemoryReviewTaskRepository)).parent.name == "tests"


def test_no_production_adapter_for_complete_exists_and_the_container_is_still_unconfigured(tmp_path):
    from doc_extractor.api.composition import ApplicationContainer
    from doc_extractor.api.config import ApiSettings
    from doc_extractor.malware import MockMalwareScanner
    from tests.test_api_documents_page_image import MockAuthProvider

    container = ApplicationContainer(
        api_settings=ApiSettings(app_env="development", storage_base_dir=str(tmp_path)),
        auth_provider=MockAuthProvider(), malware_scanner=MockMalwareScanner(),
    )
    assert container.review_task_repo is None
    package = Path(inspect.getfile(service_module)).parents[2]                    # src/doc_extractor
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "InMemoryReviewTaskRepository" not in text, path
    classes = []
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                isinstance(item, ast.AsyncFunctionDef) and item.name == "complete" for item in node.body
            ):
                classes.append(node.name)
    # the Protocol itself, the service that calls it, and (Phase 2A-3) the ONE production adapter, the MongoDB repository, which
    # the container wires only after a verified connection (asserted above: unconfigured stays None). Any other class that
    # implements ``complete``, and any in-memory substitute (asserted above), still fails this test.
    assert sorted(classes) == ["MongoReviewTaskRepository", "ReviewTaskRepository", "ReviewTaskService"]


def test_there_is_no_completion_endpoint():
    tree = ast.parse(inspect.getsource(router_module))
    paths = [n.args[0].value for n in ast.walk(tree) if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant)
             and isinstance(n.args[0].value, str) and n.args[0].value.startswith("/review-tasks")]
    assert sorted(paths) == ["/review-tasks", "/review-tasks/{task_id}/claim", "/review-tasks/{task_id}/release"]
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "complete" not in called


# ============================================================================
# The atomic double: ownership and state
# ============================================================================

def test_the_owner_completes_a_claimed_task():
    repo = _repo(_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one"))
    result = _complete(repo)
    assert result.outcome is ReviewTaskTransition.COMPLETION_RECORDED
    task = result.task
    assert task.status is ReviewTaskStatus.COMPLETED and task.assigned_reviewer_id == "rev_one"
    assert task.completed_at == LATER and task.updated_at == LATER and task.created_at == T0
    assert repo.snapshot(ALPHA, "t1") == task and repo.write_count == 1


def test_a_repeated_completion_by_the_completer_is_idempotent_and_writes_nothing():
    repo = _repo(_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one"))
    first = _complete(repo, now=LATER)
    again = _complete(repo, now=LATER + timedelta(days=1))
    assert again.outcome is ReviewTaskTransition.ALREADY_COMPLETED
    assert again.task == first.task and again.task.completed_at == LATER            # the first completion time stays
    assert repo.write_count == 1


def test_an_unassigned_task_cannot_be_completed():
    repo = _repo(_task("t1"))
    result = _complete(repo)
    assert result.outcome is ReviewTaskTransition.NOT_CLAIMED
    assert result.task.status is ReviewTaskStatus.UNASSIGNED and repo.write_count == 0


def test_a_task_claimed_by_another_reviewer_cannot_be_completed_and_stays_theirs():
    repo = _repo(_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_two"))
    before = repo.snapshot(ALPHA, "t1")
    result = _complete(repo)
    assert result.outcome is ReviewTaskTransition.HELD_BY_OTHER and result.task == before
    assert repo.snapshot(ALPHA, "t1") == before and repo.write_count == 0


def test_a_task_completed_by_someone_else_is_refused_even_when_nobody_is_recorded():
    other = _repo(_task("t1", status=ReviewTaskStatus.COMPLETED, owner="rev_two"))
    anonymous = _repo(_task("t1", status=ReviewTaskStatus.COMPLETED, owner=None))
    for repo in (other, anonymous):
        result = _complete(repo)
        assert result.outcome is ReviewTaskTransition.COMPLETED and repo.write_count == 0
        assert result.task.assigned_reviewer_id != "rev_one"


def test_a_missing_task_and_another_tenants_task_are_not_found_and_untouched():
    repo = _repo(_task("b1", tenant=BETA, status=ReviewTaskStatus.CLAIMED, owner="rev_one"))
    for task_id in ("nope", "b1"):
        result = _complete(repo, task_id)
        assert result.outcome is ReviewTaskTransition.NOT_FOUND and result.task is None
    assert repo.snapshot(BETA, "b1").status is ReviewTaskStatus.CLAIMED and repo.write_count == 0


def test_tasks_with_the_same_id_in_two_tenants_complete_independently():
    repo = _repo(
        _task("same", status=ReviewTaskStatus.CLAIMED, owner="rev_one"),
        _task("same", tenant=BETA, status=ReviewTaskStatus.CLAIMED, owner="rev_one"),
    )
    assert _complete(repo, "same", tenant=BETA).outcome is ReviewTaskTransition.COMPLETION_RECORDED
    assert repo.snapshot(ALPHA, "same").status is ReviewTaskStatus.CLAIMED
    assert repo.snapshot(BETA, "same").status is ReviewTaskStatus.COMPLETED


def test_completion_touches_only_its_own_task_and_the_returned_record_is_a_copy():
    repo = _repo(
        _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one", reviewer_notes=SECRET_NOTES, metadata={"k": "v"}),
        _task("t2", status=ReviewTaskStatus.CLAIMED, owner="rev_one"),
    )
    other_before = repo.snapshot(ALPHA, "t2")
    result = _complete(repo, "t1")
    result.task.metadata["k"] = "mutated"
    assert repo.snapshot(ALPHA, "t1").metadata == {"k": "v"}
    assert repo.snapshot(ALPHA, "t2") == other_before
    assert repo.snapshot(ALPHA, "t1").reviewer_notes == SECRET_NOTES              # the contract never rewrites notes


def test_slice_1_claim_and_release_semantics_are_preserved_after_completion():
    repo = _repo(_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one"))
    _complete(repo)
    claim = _run(repo.claim(ALPHA, "t1", "rev_one", LATER))
    release = _run(repo.release(ALPHA, "t1", "rev_one", LATER))
    assert claim.outcome is ReviewTaskTransition.COMPLETED and release.outcome is ReviewTaskTransition.COMPLETED
    assert repo.snapshot(ALPHA, "t1").status is ReviewTaskStatus.COMPLETED and repo.write_count == 1


def test_the_full_lifecycle_through_the_port():
    repo = _repo(_task("t1"))
    assert _run(repo.claim(ALPHA, "t1", "rev_one", T0)).outcome is ReviewTaskTransition.CLAIMED
    assert _run(repo.release(ALPHA, "t1", "rev_one", T0)).outcome is ReviewTaskTransition.RELEASED
    assert _complete(repo).outcome is ReviewTaskTransition.NOT_CLAIMED
    assert _run(repo.claim(ALPHA, "t1", "rev_two", T0)).outcome is ReviewTaskTransition.CLAIMED
    assert _complete(repo, reviewer="rev_one").outcome is ReviewTaskTransition.HELD_BY_OTHER
    assert _complete(repo, reviewer="rev_two").outcome is ReviewTaskTransition.COMPLETION_RECORDED
    assert _complete(repo, reviewer="rev_two").outcome is ReviewTaskTransition.ALREADY_COMPLETED
    assert _complete(repo, reviewer="rev_one").outcome is ReviewTaskTransition.COMPLETED


# ============================================================================
# Concurrency
# ============================================================================

def _run_threads(count, work):
    barrier = threading.Barrier(count)
    results, errors = [None] * count, []

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


def test_concurrent_completions_by_the_owner_have_exactly_one_write_across_threads():
    for round_number in range(40):
        repo = _repo(_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one"))
        outcomes = [r.outcome for r in _run_threads(24, lambda i: _complete(repo, now=T0 + timedelta(seconds=i)))]
        assert outcomes.count(ReviewTaskTransition.COMPLETION_RECORDED) == 1, round_number
        assert outcomes.count(ReviewTaskTransition.ALREADY_COMPLETED) == 23
        assert repo.write_count == 1
        stored = repo.snapshot(ALPHA, "t1")
        assert stored.status is ReviewTaskStatus.COMPLETED and stored.completed_at == stored.updated_at


def test_concurrent_completions_by_different_reviewers_let_only_the_owner_succeed():
    for round_number in range(40):
        repo = _repo(_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_0"))
        results = _run_threads(16, lambda i: _complete(repo, reviewer=f"rev_{i}"))
        outcomes = [r.outcome for r in results]
        assert outcomes[0] is ReviewTaskTransition.COMPLETION_RECORDED, round_number
        # a non-owner is refused either while the task is still claimed or once the owner has completed it
        assert all(o in (ReviewTaskTransition.HELD_BY_OTHER, ReviewTaskTransition.COMPLETED) for o in outcomes[1:])
        assert repo.write_count == 1 and repo.snapshot(ALPHA, "t1").assigned_reviewer_id == "rev_0"


def test_concurrent_completions_on_one_event_loop_have_exactly_one_recorded():
    async def scenario():
        repo = _repo(_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one"))
        results = await asyncio.gather(*[repo.complete(ALPHA, "t1", "rev_one", LATER) for _ in range(30)])
        return repo, [r.outcome for r in results]

    repo, outcomes = asyncio.run(scenario())
    assert outcomes.count(ReviewTaskTransition.COMPLETION_RECORDED) == 1
    assert outcomes.count(ReviewTaskTransition.ALREADY_COMPLETED) == 29 and repo.write_count == 1


def test_the_race_tests_detect_a_non_atomic_completion():
    """The harness has teeth: the same scenario on a read-yield-write double records several completions."""
    async def scenario():
        repo = RacyReviewTaskRepository([_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one")])
        results = await asyncio.gather(*[repo.complete(ALPHA, "t1", "rev_one", LATER) for _ in range(10)])
        return repo, [r.outcome for r in results]

    repo, outcomes = asyncio.run(scenario())
    assert outcomes.count(ReviewTaskTransition.COMPLETION_RECORDED) > 1 and repo.write_count > 1


def test_a_completion_racing_a_release_by_the_owner_has_exactly_one_winner():
    complete_won = release_won = 0
    for round_number in range(60):
        repo = _repo(_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one"))

        def work(index):
            if index == 0:
                return ("complete", _complete(repo))
            return ("release", _run(repo.release(ALPHA, "t1", "rev_one", LATER)))

        (_, completion), (_, release) = _run_threads(2, work)
        stored = repo.snapshot(ALPHA, "t1")
        if completion.outcome is ReviewTaskTransition.COMPLETION_RECORDED:
            complete_won += 1
            assert release.outcome is ReviewTaskTransition.COMPLETED, round_number          # the release lost: it is completed
            assert stored.status is ReviewTaskStatus.COMPLETED
        else:
            release_won += 1
            assert completion.outcome is ReviewTaskTransition.NOT_CLAIMED, round_number
            assert release.outcome is ReviewTaskTransition.RELEASED
            assert stored.status is ReviewTaskStatus.UNASSIGNED and stored.assigned_reviewer_id is None
        assert repo.write_count == 1                                                        # never both
    assert complete_won + release_won == 60


def test_a_racing_complete_and_release_are_never_both_applied_on_an_event_loop():
    async def scenario():
        repo = _repo(_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one"))
        completion, release = await asyncio.gather(
            repo.complete(ALPHA, "t1", "rev_one", LATER), repo.release(ALPHA, "t1", "rev_one", LATER)
        )
        return repo, completion.outcome, release.outcome

    repo, completion, release = asyncio.run(scenario())
    assert (completion, release) in {
        (ReviewTaskTransition.COMPLETION_RECORDED, ReviewTaskTransition.COMPLETED),
        (ReviewTaskTransition.NOT_CLAIMED, ReviewTaskTransition.RELEASED),
    }
    assert repo.write_count == 1


def test_the_race_tests_detect_complete_and_release_both_applying_on_a_non_atomic_double():
    async def scenario():
        repo = RacyReviewTaskRepository([_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one")])

        # the racy completion reads, then yields; the (atomic) release runs inside that window; the completion then writes blindly
        completion, release = await asyncio.gather(
            repo.complete(ALPHA, "t1", "rev_one", LATER), repo.release(ALPHA, "t1", "rev_one", LATER)
        )
        return completion.outcome, release.outcome

    completion, release = asyncio.run(scenario())
    assert (completion, release) == (ReviewTaskTransition.COMPLETION_RECORDED, ReviewTaskTransition.RELEASED)   # an impossible pair


def test_a_completion_racing_claims_by_others_never_changes_the_owner():
    for round_number in range(30):
        repo = _repo(_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one"))

        def work(index):
            if index == 0:
                return _complete(repo)
            return _run(repo.claim(ALPHA, "t1", f"rev_{index}", LATER))

        results = _run_threads(10, work)
        assert results[0].outcome is ReviewTaskTransition.COMPLETION_RECORDED, round_number
        assert all(r.outcome in (ReviewTaskTransition.HELD_BY_OTHER, ReviewTaskTransition.COMPLETED) for r in results[1:])
        stored = repo.snapshot(ALPHA, "t1")
        assert stored.assigned_reviewer_id == "rev_one" and stored.status is ReviewTaskStatus.COMPLETED
        assert repo.write_count == 1


def test_concurrent_completions_in_two_tenants_do_not_interfere():
    repo = _repo(
        _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one"),
        _task("t1", tenant=BETA, status=ReviewTaskStatus.CLAIMED, owner="rev_one"),
    )
    results = _run_threads(20, lambda i: _complete(repo, tenant=ALPHA if i % 2 == 0 else BETA))
    outcomes = [r.outcome for r in results]
    assert outcomes.count(ReviewTaskTransition.COMPLETION_RECORDED) == 2 and repo.write_count == 2
    assert repo.snapshot(ALPHA, "t1").status is repo.snapshot(BETA, "t1").status is ReviewTaskStatus.COMPLETED


# ============================================================================
# The service: outcome mapping and the checks of what an adapter returns
# ============================================================================

def _service(repo, tenant=ALPHA, principal="rev_one"):
    return ReviewTaskService(repo, tenant, principal)


def _scripted(**methods):
    defaults = dict(claim=AsyncMock(), release=AsyncMock(), complete=AsyncMock(), list_page=AsyncMock())
    defaults.update(methods)
    return SimpleNamespace(**defaults)


def _result(outcome, task):
    return ReviewTaskTransitionResult(outcome, task)


def test_the_service_completes_a_claimed_task_for_the_authenticated_principal():
    repo = _repo(_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one"))
    task = _run(_service(repo).complete("t1"))
    assert task.status is ReviewTaskStatus.COMPLETED and task.assigned_reviewer_id == "rev_one"
    again = _run(_service(repo).complete("t1"))
    assert again == task and repo.write_count == 1                                          # idempotent for the completer


@pytest.mark.parametrize("record, expected", [
    (_task("t1"), ReviewTaskNotClaimedError),
    (_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_two"), ReviewTaskClaimedError),
    (_task("t1", status=ReviewTaskStatus.COMPLETED, owner="rev_two"), ReviewTaskCompletedError),
    (None, ReviewTaskNotFoundError),
])
def test_the_service_maps_each_domain_outcome_to_its_sanitized_error(record, expected):
    repo = _repo(*([record] if record else []))
    with pytest.raises(expected) as caught:
        _run(_service(repo).complete("t1", request_id="req_1"))
    error = caught.value
    assert error.request_id == "req_1"
    if expected is ReviewTaskClaimedError:
        assert error.details == {"claimed_by": "rev_two"} and error.code == "TASK_CLAIMED"
    else:
        assert error.details == {}
    codes = {ReviewTaskNotClaimedError: "TASK_NOT_CLAIMED", ReviewTaskCompletedError: "TASK_COMPLETED",
             ReviewTaskNotFoundError: "REVIEW_TASK_NOT_FOUND"}
    if expected in codes:
        assert error.code == codes[expected]
    assert repo.write_count == 0


@pytest.mark.parametrize("task_id", ["", "a b", "../x", "x" * 129, "a/b", "nul\x00"])
def test_a_malformed_task_id_is_the_concealed_not_found_before_any_repository_call(task_id):
    repo = _scripted()
    with pytest.raises(ReviewTaskNotFoundError):
        _run(_service(repo).complete(task_id))
    assert repo.complete.await_count == 0


def test_the_service_passes_exactly_the_bound_tenant_and_principal_to_the_port():
    repo = _scripted(complete=AsyncMock(return_value=_result(ReviewTaskTransition.NOT_FOUND, None)))
    with pytest.raises(ReviewTaskNotFoundError):
        _run(_service(repo, tenant=BETA, principal="rev_beta").complete("t1"))
    tenant, task_id, reviewer, now = repo.complete.await_args.args
    assert (tenant, task_id, reviewer) == (BETA, "t1", "rev_beta") and now.tzinfo is not None


def test_a_cross_tenant_signal_from_the_adapter_is_the_concealed_not_found():
    repo = _scripted(complete=AsyncMock(side_effect=TenantMismatchError("tenant_beta leaked")))
    with pytest.raises(ReviewTaskNotFoundError) as caught:
        _run(_service(repo).complete("t1"))
    assert "leaked" not in str(caught.value.message) and BETA not in str(caught.value.details)


def test_a_repository_exception_is_a_sanitized_storage_fault_and_only_its_class_is_logged(caplog):
    secret = "SYNTH-DB-DETAIL postgres://u:p@host/db"
    repo = _scripted(complete=AsyncMock(side_effect=RuntimeError(secret)))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(StorageError) as caught:
            _run(_service(repo).complete("t1"))
    assert caught.value.details == {"reason": "review_task_repository_fault"}
    assert secret not in caplog.text and "postgres" not in caplog.text and "exception=RuntimeError" in caplog.text


FOREIGN_CLAIMED = _task("t1", tenant=BETA, status=ReviewTaskStatus.CLAIMED, owner="rev_one")


def _completed_without_time():
    return ReviewTaskRecord.model_construct(
        task_id="t1", document_id="doc_synth", tenant_id=ALPHA, status=ReviewTaskStatus.COMPLETED,
        assigned_reviewer_id="rev_one", priority=0, created_at=T0, updated_at=T0, completed_at=None, reviewer_notes=None,
        metadata={},
    )


@pytest.mark.parametrize("outcome, record", [
    # a record of another tenant, in every shape that would otherwise be valid
    (ReviewTaskTransition.COMPLETION_RECORDED, _task("t1", tenant=BETA, status=ReviewTaskStatus.COMPLETED, owner="rev_one")),
    (ReviewTaskTransition.ALREADY_COMPLETED, _task("t1", tenant=BETA, status=ReviewTaskStatus.COMPLETED, owner="rev_one")),
    (ReviewTaskTransition.NOT_CLAIMED, _task("t1", tenant=BETA)),
    (ReviewTaskTransition.HELD_BY_OTHER, _task("t1", tenant=BETA, status=ReviewTaskStatus.CLAIMED, owner="rev_beta")),
    (ReviewTaskTransition.COMPLETED, _task("t1", tenant=BETA, status=ReviewTaskStatus.COMPLETED, owner="rev_beta")),
    # another task
    (ReviewTaskTransition.COMPLETION_RECORDED, _task("other", status=ReviewTaskStatus.COMPLETED, owner="rev_one")),
    # a state that contradicts the outcome
    (ReviewTaskTransition.COMPLETION_RECORDED, _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one")),      # still claimed
    (ReviewTaskTransition.COMPLETION_RECORDED, _task("t1")),
    (ReviewTaskTransition.COMPLETION_RECORDED, _task("t1", status=ReviewTaskStatus.COMPLETED, owner="rev_two")),    # not the caller's
    (ReviewTaskTransition.COMPLETION_RECORDED, _task("t1", status=ReviewTaskStatus.COMPLETED, owner=None)),
    (ReviewTaskTransition.COMPLETION_RECORDED, _completed_without_time()),                                         # no completion time
    (ReviewTaskTransition.ALREADY_COMPLETED, _task("t1", status=ReviewTaskStatus.COMPLETED, owner="rev_two")),
    (ReviewTaskTransition.ALREADY_COMPLETED, _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one")),
    (ReviewTaskTransition.COMPLETED, _task("t1", status=ReviewTaskStatus.COMPLETED, owner="rev_one")),             # the caller's own
    (ReviewTaskTransition.COMPLETED, _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_two")),
    (ReviewTaskTransition.NOT_CLAIMED, _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one")),
    (ReviewTaskTransition.HELD_BY_OTHER, _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one")),            # held by me?
    (ReviewTaskTransition.HELD_BY_OTHER, _task("t1")),
    # an outcome of another operation, or no record
    (ReviewTaskTransition.CLAIMED, _task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one")),
    (ReviewTaskTransition.CLAIMED, _task("t1", status=ReviewTaskStatus.COMPLETED, owner="rev_one")),           # a valid completed record
    (ReviewTaskTransition.RELEASED, _task("t1", status=ReviewTaskStatus.COMPLETED, owner="rev_one")),
    (ReviewTaskTransition.ALREADY_UNASSIGNED, _task("t1")),
    (ReviewTaskTransition.RELEASED, _task("t1")),
    (ReviewTaskTransition.COMPLETION_RECORDED, None),
    (ReviewTaskTransition.ALREADY_COMPLETED, None),
])
def test_a_completion_result_that_breaks_the_contract_is_a_generic_fault_and_never_forwarded(outcome, record):
    repo = _scripted(complete=AsyncMock(return_value=_result(outcome, record)))
    with pytest.raises(StorageError) as caught:
        _run(_service(repo).complete("t1"))
    assert caught.value.details == {"reason": "review_task_adapter_contract_violation"}
    assert "rev_beta" not in str(caught.value.message)


def test_a_non_enum_completion_outcome_is_a_fault():
    repo = _scripted(complete=AsyncMock(return_value=SimpleNamespace(outcome="completion_recorded", task=_task("t1"))))
    with pytest.raises(StorageError):
        _run(_service(repo).complete("t1"))


@pytest.mark.parametrize("operation", ["claim", "release"])
@pytest.mark.parametrize("outcome", [
    ReviewTaskTransition.COMPLETION_RECORDED, ReviewTaskTransition.ALREADY_COMPLETED, ReviewTaskTransition.NOT_CLAIMED,
])
def test_the_completion_outcomes_are_a_fault_when_a_claim_or_release_returns_them(operation, outcome):
    """Slice 1 stays strict: the new outcomes can never be accepted for claim or release."""
    valid_looking = {
        ReviewTaskTransition.COMPLETION_RECORDED: _task("t1", status=ReviewTaskStatus.COMPLETED, owner="rev_one"),
        ReviewTaskTransition.ALREADY_COMPLETED: _task("t1", status=ReviewTaskStatus.COMPLETED, owner="rev_one"),
        ReviewTaskTransition.NOT_CLAIMED: _task("t1"),                          # exactly what a valid ``complete`` would return
    }[outcome]
    repo = _scripted(**{operation: AsyncMock(return_value=_result(outcome, valid_looking))})
    with pytest.raises(StorageError) as caught:
        _run(getattr(_service(repo), operation)("t1"))
    assert caught.value.details == {"reason": "review_task_adapter_contract_violation"}


def test_the_completion_log_has_only_the_outcome_task_id_and_request_id(caplog):
    repo = _repo(_task("t1", status=ReviewTaskStatus.CLAIMED, owner="rev_one", reviewer_notes=SECRET_NOTES))
    with caplog.at_level(logging.DEBUG):
        _run(_service(repo).complete("t1", request_id="req_9"))
        with pytest.raises(ReviewTaskCompletedError):
            _run(_service(repo, principal="rev_two").complete("t1", request_id="req_10"))
    assert "Review task complete (outcome=completion_recorded, task_id=t1, request_id=req_9)" in caplog.text
    assert "outcome=completed" in caplog.text
    for forbidden in ("rev_one", "rev_two", ALPHA, SECRET_NOTES, "claimed_by"):
        assert forbidden not in caplog.text


def test_the_service_logging_calls_remain_constant_messages_without_identities():
    tree = ast.parse(inspect.getsource(service_module))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "logger"]
    assert calls
    for call in calls:
        assert isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
        rendered = " ".join(ast.unparse(arg) for arg in call.args[1:])
        for forbidden in ("principal", "tenant", "message", "str(exc)", "repr", "notes", "metadata"):
            assert forbidden not in rendered, rendered


def test_the_service_complete_validates_nothing_and_touches_no_other_store():
    """The service has the repository and nothing else: no document store, no schema source, no audit logger."""
    parameters = inspect.signature(ReviewTaskService.__init__).parameters
    assert set(parameters) == {"self", "repository", "tenant_id", "principal_id", "clock"}
    source = inspect.getsource(ReviewTaskService.complete)
    for forbidden in ("audit", "document_metadata", "update_status", "envelope", "correction", "schema"):
        assert forbidden not in source.split('"""')[2].lower()
