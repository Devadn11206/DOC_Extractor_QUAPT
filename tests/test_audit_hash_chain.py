"""
Hash-chained audit trail (PRD SEC-008, FR-AUD-001). Tampering, deletion, reordering and write failures must all be
detectable or raised; none may pass as a clean audit.
"""

import asyncio
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from doc_extractor.api.audit_chain import (
    GENESIS_HASH,
    DurableAuditLogger,
    HashChainedAuditRepository,
    compute_event_hash,
)
from doc_extractor.api.contracts.records import AuditEventRecord
from doc_extractor.api.exceptions import AuditUnavailableError
from doc_extractor.api.ports.audit import SafeStructuredAuditLogger
from doc_extractor.exceptions import StorageError


def _event(tenant: str, n: int, action: str = "document.upload", resource: str = "doc_1") -> AuditEventRecord:
    return AuditEventRecord(
        event_id=f"evt_{n}",
        actor_id="key_1",
        tenant_id=tenant,
        action=action,
        resource_id=resource,
        resource_type="document",
        timestamp=datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(minutes=n),
        outcome="SUCCESS",
        metadata={"status": "accepted", "byte_count": 10 + n},
    )


def _run(coro):
    return asyncio.run(coro)


def _append_all(repo, events):
    for event in events:
        _run(repo.append_event(event))


def _chain_lines(tmp_path, tenant):
    return (tmp_path / tenant / "audit" / "events.jsonl").read_bytes().splitlines(keepends=True)


def _write_chain_lines(tmp_path, tenant, lines):
    (tmp_path / tenant / "audit" / "events.jsonl").write_bytes(b"".join(lines))


def test_appended_events_form_a_valid_chain_that_can_be_queried(tmp_path):
    repo = HashChainedAuditRepository(tmp_path)
    _append_all(repo, [_event("tenant_a", n) for n in range(3)])

    verdict = repo.verify_chain("tenant_a")
    assert verdict.valid is True and verdict.checked_events == 3

    events = _run(repo.query_events("tenant_a"))
    assert [e.event_id for e in events] == ["evt_0", "evt_1", "evt_2"]


def test_each_record_links_to_the_previous_hash(tmp_path):
    repo = HashChainedAuditRepository(tmp_path)
    _append_all(repo, [_event("tenant_a", 0), _event("tenant_a", 1)])
    first, second = [json.loads(line) for line in _chain_lines(tmp_path, "tenant_a")]
    assert first["prev_hash"] == GENESIS_HASH
    assert first["hash"] == compute_event_hash(GENESIS_HASH, 1, first["event"])
    assert second["prev_hash"] == first["hash"]
    assert second["hash"] == compute_event_hash(first["hash"], 2, second["event"])


def test_altering_a_stored_event_breaks_verification_at_that_sequence(tmp_path):
    repo = HashChainedAuditRepository(tmp_path)
    _append_all(repo, [_event("tenant_a", n) for n in range(3)])
    lines = _chain_lines(tmp_path, "tenant_a")
    record = json.loads(lines[1])
    record["event"]["outcome"] = "FAILED"  # someone rewrites history
    lines[1] = (json.dumps(record, separators=(",", ":")) + "\n").encode()
    _write_chain_lines(tmp_path, "tenant_a", lines)

    verdict = repo.verify_chain("tenant_a")
    assert verdict.valid is False
    assert verdict.first_bad_seq == 2
    assert verdict.reason == "hash mismatch"


def test_deleting_a_middle_event_is_detected(tmp_path):
    repo = HashChainedAuditRepository(tmp_path)
    _append_all(repo, [_event("tenant_a", n) for n in range(3)])
    lines = _chain_lines(tmp_path, "tenant_a")
    _write_chain_lines(tmp_path, "tenant_a", [lines[0], lines[2]])

    verdict = repo.verify_chain("tenant_a")
    assert verdict.valid is False
    assert verdict.first_bad_seq == 2
    assert verdict.reason == "sequence or link broken"


def test_reordering_events_is_detected(tmp_path):
    repo = HashChainedAuditRepository(tmp_path)
    _append_all(repo, [_event("tenant_a", n) for n in range(2)])
    lines = _chain_lines(tmp_path, "tenant_a")
    _write_chain_lines(tmp_path, "tenant_a", [lines[1], lines[0]])
    assert repo.verify_chain("tenant_a").valid is False


def test_a_corrupt_tail_refuses_further_appends_instead_of_forking(tmp_path):
    repo = HashChainedAuditRepository(tmp_path)
    _append_all(repo, [_event("tenant_a", 0)])
    path = tmp_path / "tenant_a" / "audit" / "events.jsonl"
    with open(path, "ab") as fh:
        fh.write(b"{not json\n")
    with pytest.raises(StorageError):
        _run(repo.append_event(_event("tenant_a", 1)))
    assert repo.verify_chain("tenant_a").valid is False


def test_tenants_have_independent_chains_and_never_see_each_other(tmp_path):
    repo = HashChainedAuditRepository(tmp_path)
    _append_all(repo, [_event("tenant_a", 0, resource="doc_a"), _event("tenant_b", 1, resource="doc_b")])

    a_events = _run(repo.query_events("tenant_a"))
    b_events = _run(repo.query_events("tenant_b"))
    assert [e.resource_id for e in a_events] == ["doc_a"]
    assert [e.resource_id for e in b_events] == ["doc_b"]
    assert repo.verify_chain("tenant_a").checked_events == 1
    assert repo.verify_chain("tenant_b").checked_events == 1


def test_query_filters_by_resource_action_and_window(tmp_path):
    repo = HashChainedAuditRepository(tmp_path)
    _append_all(repo, [
        _event("tenant_a", 0, action="document.upload", resource="doc_1"),
        _event("tenant_a", 1, action="document.view", resource="doc_1"),
        _event("tenant_a", 2, action="document.view", resource="doc_2"),
    ])
    views = _run(repo.query_events("tenant_a", action="document.view"))
    assert [e.resource_id for e in views] == ["doc_1", "doc_2"]
    one = _run(repo.query_events("tenant_a", resource_id="doc_1", action="document.view"))
    assert [e.event_id for e in one] == ["evt_1"]
    window = _run(repo.query_events("tenant_a", start_time=datetime(2026, 11, 1, 0, 1, tzinfo=timezone.utc)))
    assert [e.event_id for e in window] == ["evt_1", "evt_2"]


def test_a_failed_fsync_raises_and_the_chain_is_not_extended(tmp_path, monkeypatch):
    repo = HashChainedAuditRepository(tmp_path)
    _append_all(repo, [_event("tenant_a", 0)])

    def failing_fsync(_fd):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(StorageError) as exc_info:
        _run(repo.append_event(_event("tenant_a", 1)))
    assert "disk full" not in str(exc_info.value.details)  # error text is not carried into the details
    monkeypatch.undo()
    assert repo.verify_chain("tenant_a").checked_events == 1


def test_durable_logger_propagates_write_failure_as_audit_unavailable(tmp_path, monkeypatch):
    repo = HashChainedAuditRepository(tmp_path)
    logger = DurableAuditLogger(repo)
    assert logger.propagates_failures is True
    assert SafeStructuredAuditLogger.propagates_failures is False

    def failing_fsync(_fd):
        raise OSError("read-only file system")

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(AuditUnavailableError) as exc_info:
        _run(logger.log_action(
            actor_id="key_1", tenant_id="tenant_a", action="api_key.revoked",
            resource_id="key_9", resource_type="api_key",
        ))
    assert exc_info.value.status_code == 503


def test_durable_logger_strips_pii_like_metadata_before_it_is_stored(tmp_path):
    repo = HashChainedAuditRepository(tmp_path)
    logger = DurableAuditLogger(repo)
    _run(logger.log_action(
        actor_id="key_1", tenant_id="tenant_a", action="document.view", resource_id="doc_1",
        resource_type="document", metadata={"status": "accepted", "customer_name": "Asha Rao"},
    ))
    stored = (tmp_path / "tenant_a" / "audit" / "events.jsonl").read_text(encoding="utf-8")
    assert "Asha Rao" not in stored
    assert "customer_name" not in stored
    assert repo.verify_chain("tenant_a").valid is True


def test_concurrent_writers_in_one_process_never_fork_the_chain(tmp_path):
    repo = HashChainedAuditRepository(tmp_path)
    errors = []

    def writer(start):
        try:
            for n in range(start, start + 10):
                _run(repo.append_event(_event("tenant_a", n)))
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i * 10,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    verdict = repo.verify_chain("tenant_a")
    assert verdict.valid is True and verdict.checked_events == 50
