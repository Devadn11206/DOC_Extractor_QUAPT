"""
Reads never provision storage; writes do, and a filesystem failure is a typed error (review finding R1).

Before this change ``LocalDiskStorage`` resolved EVERY document path with ``create=True``: a plain GET for a tenant that
had no directory created that directory plus its identity marker, and on a read-only storage root the read raised a bare
``PermissionError`` (a 500) where the original code returned ``None`` (a 404). Needs no FastAPI, so it always runs.
"""

import os
import threading
import time
import unicodedata
from pathlib import Path

import pytest

from doc_extractor.exceptions import DocumentExtractorError, StorageError, TenantStorageUnavailableError
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.models import IdempotencyRecord
from doc_extractor.storage import LocalDiskStorage
from doc_extractor.tenancy import IDENTITY_MARKER, TenantDirectoryResolver

# one plain key, one hashed key (space, colon, upper case), one hashed key that a normalizing filesystem would alias
TENANTS = ["tenant_ghost", "Ghost Corp:1", unicodedata.normalize("NFD", "café")]


class Rec:
    def __init__(self, document_id):
        self.document_id = document_id

    def to_dict(self):
        return {"document_id": self.document_id}


def idem_record(tenant_id, key="k"):
    now = time.time()
    return IdempotencyRecord(idempotency_key=key, tenant_id=tenant_id, request_fingerprint="fp", document_id="doc_1",
                             response_payload={}, created_at_utc=now, expires_at_utc=now + 3600)


def tree(base: Path):
    """Every path below ``base`` with its type, so any created directory, marker or file shows up."""
    return sorted((str(p.relative_to(base)), "d" if p.is_dir() else "f") for p in base.rglob("*"))


# -- reads leave the disk alone ------------------------------------------------------------------------

@pytest.mark.parametrize("tenant_id", TENANTS)
def test_every_storage_read_of_an_unknown_tenant_returns_nothing_and_creates_nothing(tmp_path, tenant_id):
    storage = LocalDiskStorage(tmp_path, tenant_id=tenant_id)
    assert storage.get_document_record("doc_nope") is None
    assert storage.get_page_metadata("doc_nope", 1) is None
    assert storage.list_page_images("doc_nope") == []
    assert storage.find_document_by_hash("0" * 64) is None
    assert storage.get_handoff_payload("doc_nope") is None
    storage.cleanup_preprocessed("doc_nope")                       # deleting what is not there is a no-op
    storage.cleanup_document("doc_nope")
    storage.resolve_uri_to_path(f"{tenant_id}/doc_nope/source/original.pdf")   # a pure path computation
    assert tree(tmp_path) == [], "a read provisioned tenant storage"


@pytest.mark.parametrize("tenant_id", TENANTS)
def test_reads_for_an_existing_tenant_do_not_create_the_document_directory_either(tmp_path, tenant_id):
    LocalDiskStorage(tmp_path, tenant_id=tenant_id).save_document_record(Rec("doc_1"))
    before = tree(tmp_path)
    reader = LocalDiskStorage(tmp_path, tenant_id=tenant_id)
    assert reader.get_document_record("doc_missing") is None
    assert reader.get_handoff_payload("doc_missing") is None
    assert reader.list_page_images("doc_missing") == []
    assert tree(tmp_path) == before


@pytest.mark.parametrize("tenant_id", TENANTS)
def test_idempotency_reads_and_deletes_of_an_unknown_tenant_create_nothing(tmp_path, tenant_id):
    store = LocalIdempotencyStore(tmp_path)
    assert store.get_record(tenant_id, "k") is None
    store.delete_record(tenant_id, "k")
    assert tree(tmp_path) == []


def test_the_resolver_never_creates_without_an_explicit_request(tmp_path):
    resolver = TenantDirectoryResolver(tmp_path)
    path = resolver.resolve("Ghost Corp:1")                        # default create=False
    assert not os.path.lexists(path) and tree(tmp_path) == []
    created = resolver.resolve("Ghost Corp:1", create=True)
    assert created == path and (created / IDENTITY_MARKER).is_file()


# -- writes (and only writes) provision ------------------------------------------------------------------

@pytest.mark.parametrize("tenant_id", TENANTS)
def test_each_write_operation_provisions_the_tenant_with_its_marker(tmp_path, tenant_id):
    writers = {
        "original": lambda s: s.store_original_document("doc_1", b"%PDF-1.4"),
        "record": lambda s: s.save_document_record(Rec("doc_1")),
        "handoff": lambda s: s.save_handoff_payload(Rec("doc_1")),
    }
    for name, write in writers.items():
        base = tmp_path / name
        write(LocalDiskStorage(base, tenant_id=tenant_id))
        markers = list(base.rglob(IDENTITY_MARKER))
        assert len(markers) == 1, f"{name}: the write did not provision exactly one tenant directory"
    base = tmp_path / "idempotency"
    LocalIdempotencyStore(base).save_record(idem_record(tenant_id))
    assert len(list(base.rglob(IDENTITY_MARKER))) == 1


# -- legacy directories and existing data -----------------------------------------------------------------

def test_reading_a_legacy_directory_does_not_modify_it_or_add_a_marker(tmp_path):
    legacy = tmp_path / "Acme" / "doc_0" / "source"                 # laid out by the code before tenant keys existed
    legacy.mkdir(parents=True)
    (legacy / "original.pdf").write_bytes(b"legacy")
    (tmp_path / "Acme" / "doc_0" / "document_record.json").write_text('{"document_id": "doc_0"}')
    before = tree(tmp_path)
    stat_before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    reader = LocalDiskStorage(tmp_path, tenant_id="Acme")
    assert reader.get_document_record("doc_0") == {"document_id": "doc_0"} or reader.get_document_record("doc_0") is not None
    assert reader.get_document_record("doc_absent") is None
    assert tree(tmp_path) == before
    assert {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == stat_before
    assert not list(tmp_path.rglob(IDENTITY_MARKER))


def test_a_write_into_a_legacy_directory_still_uses_it_and_adds_no_marker(tmp_path):
    (tmp_path / "Acme" / "doc_0").mkdir(parents=True)
    LocalDiskStorage(tmp_path, tenant_id="Acme").save_document_record(Rec("doc_1"))
    assert (tmp_path / "Acme" / "doc_1" / "document_record.json").is_file()
    assert [p.name for p in tmp_path.iterdir()] == ["Acme"] and not list(tmp_path.rglob(IDENTITY_MARKER))


# -- an unwritable or unreadable root ------------------------------------------------------------------

def test_reads_on_a_read_only_root_return_none_instead_of_raising(tmp_path):
    LocalDiskStorage(tmp_path, tenant_id="existing").save_document_record(Rec("doc_1"))
    os.chmod(tmp_path, 0o555)
    try:
        assert LocalDiskStorage(tmp_path, tenant_id="existing").get_document_record("doc_absent") is None
        for tenant in TENANTS:                                       # a tenant with no directory at all
            reader = LocalDiskStorage(tmp_path, tenant_id=tenant)
            assert reader.get_document_record("doc_1") is None
            assert reader.list_page_images("doc_1") == []
            assert LocalIdempotencyStore(tmp_path).get_record(tenant, "k") is None
    finally:
        os.chmod(tmp_path, 0o755)


def test_a_write_on_a_read_only_root_is_a_typed_storage_error(tmp_path):
    os.chmod(tmp_path, 0o555)
    try:
        writable = os.access(tmp_path, os.W_OK)                      # true when the suite runs as root: chmod cannot block it
        outcome = None
        try:
            LocalDiskStorage(tmp_path, tenant_id="Ghost Corp:1").save_document_record(Rec("doc_1"))
        except TenantStorageUnavailableError as exc:
            outcome = exc
        if writable:
            assert outcome is None                                    # nothing blocked the write: nothing to convert
        else:
            assert outcome is not None and isinstance(outcome, StorageError) and isinstance(outcome, DocumentExtractorError)
            assert outcome.code == "TENANT_STORAGE_UNAVAILABLE"
    finally:
        os.chmod(tmp_path, 0o755)


def _deny_directory_creation(monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied: /private/server/layout")

    monkeypatch.setattr(os, "mkdir", refuse)
    monkeypatch.setattr(Path, "mkdir", refuse)


WRITES = {
    "store_original_document": lambda s: s.store_original_document("doc_1", b"x"),
    "store_page_image": lambda s: s.store_page_image("doc_1", 1, b"\x89PNG", "png"),
    "save_document_record": lambda s: s.save_document_record(Rec("doc_1")),
    "save_handoff_payload": lambda s: s.save_handoff_payload(Rec("doc_1")),
}


@pytest.mark.parametrize("name", sorted(WRITES))
@pytest.mark.parametrize("tenant_id", ["tenant_ghost", "Ghost Corp:1"])
def test_a_directory_creation_failure_never_leaks_an_oserror(tmp_path, monkeypatch, name, tenant_id):
    storage = LocalDiskStorage(tmp_path, tenant_id=tenant_id)
    _deny_directory_creation(monkeypatch)
    with pytest.raises(TenantStorageUnavailableError) as raised:
        WRITES[name](storage)
    error = raised.value
    assert error.code == "TENANT_STORAGE_UNAVAILABLE" and isinstance(error, StorageError)
    assert "/private/server/layout" not in str(error) and "/private/server/layout" not in repr(error.details)
    assert set(error.details) == {"reason"}


def test_an_idempotency_save_failure_is_typed_too(tmp_path, monkeypatch):
    store = LocalIdempotencyStore(tmp_path)
    _deny_directory_creation(monkeypatch)
    with pytest.raises(TenantStorageUnavailableError):
        store.save_record(idem_record("tenant_ghost"))


def test_an_unreadable_root_is_a_typed_error_not_a_bare_oserror(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    LocalDiskStorage(root, tenant_id="existing").save_document_record(Rec("doc_1"))
    os.chmod(root, 0o000)
    try:
        readable = os.access(root, os.R_OK)
        try:
            LocalDiskStorage(root, tenant_id="Other Tenant").get_document_record("doc_1")
            raised = None
        except TenantStorageUnavailableError as exc:
            raised = exc
        if readable:
            assert raised is None                                     # root can read anything
        else:
            assert raised is not None and raised.details == {"reason": "storage_root_unreadable"}
    finally:
        os.chmod(root, 0o755)


def test_a_partial_directory_creation_failure_leaves_no_markerless_tenant(tmp_path, monkeypatch):
    real_rename = os.rename

    def failing_rename(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "rename", failing_rename)
    with pytest.raises(TenantStorageUnavailableError):
        LocalDiskStorage(tmp_path, tenant_id="Ghost Corp:1").save_document_record(Rec("doc_1"))
    monkeypatch.setattr(os, "rename", real_rename)
    assert [p for p in tmp_path.iterdir() if not p.name.startswith(".tmp-tenant-")] == []   # no tenant directory without its marker
    assert [p.name for p in tmp_path.iterdir()] == []                                        # and the staging directory was removed


# -- idempotency reads are strictly non-mutating (F-02) ---------------------------------------------------

def test_reading_expired_idempotency_record_does_not_mutate_disk_or_delete_file(tmp_path):
    store = LocalIdempotencyStore(tmp_path)
    now = time.time()
    expired = IdempotencyRecord(
        idempotency_key="k_exp",
        tenant_id="tenant_ghost",
        request_fingerprint="fp1",
        document_id="doc_exp",
        response_payload={"status": "done"},
        created_at_utc=now - 90000,
        expires_at_utc=now - 3600,
    )
    store.save_record(expired)
    file_path = store._get_record_path("tenant_ghost", "k_exp")
    assert file_path.is_file()
    bytes_before = file_path.read_bytes()
    mtime_before = file_path.stat().st_mtime_ns

    # Read must treat expired record as absent without deleting or altering the backing file
    assert store.get_record("tenant_ghost", "k_exp") is None
    assert file_path.is_file()
    assert file_path.read_bytes() == bytes_before
    assert file_path.stat().st_mtime_ns == mtime_before


def test_reading_expired_idempotency_record_on_read_only_root_returns_none(tmp_path):
    store = LocalIdempotencyStore(tmp_path)
    now = time.time()
    expired = IdempotencyRecord(
        idempotency_key="k_ro",
        tenant_id="tenant_ghost",
        request_fingerprint="fp2",
        document_id="doc_ro",
        response_payload={},
        created_at_utc=now - 90000,
        expires_at_utc=now - 3600,
    )
    store.save_record(expired)
    file_path = store._get_record_path("tenant_ghost", "k_ro")
    assert file_path.is_file()

    # Lock directory and file as read-only
    idem_dir = file_path.parent
    os.chmod(file_path, 0o444)
    os.chmod(idem_dir, 0o555)
    try:
        # A read must succeed and return None without attempting to unlink on the read-only volume
        assert store.get_record("tenant_ghost", "k_ro") is None
    finally:
        os.chmod(idem_dir, 0o755)
        os.chmod(file_path, 0o644)


def test_idempotency_cleanup_expired_record_removes_expired_but_keeps_valid(tmp_path):
    store = LocalIdempotencyStore(tmp_path)
    now = time.time()
    expired = IdempotencyRecord(
        idempotency_key="k_old",
        tenant_id="tenant_ghost",
        request_fingerprint="fp_old",
        document_id="doc_old",
        response_payload={},
        created_at_utc=now - 90000,
        expires_at_utc=now - 3600,
    )
    valid = IdempotencyRecord(
        idempotency_key="k_valid",
        tenant_id="tenant_ghost",
        request_fingerprint="fp_valid",
        document_id="doc_valid",
        response_payload={},
        created_at_utc=now,
        expires_at_utc=now + 3600,
    )
    store.save_record(expired)
    store.save_record(valid)

    file_expired = store._get_record_path("tenant_ghost", "k_old")
    file_valid = store._get_record_path("tenant_ghost", "k_valid")
    assert file_expired.is_file() and file_valid.is_file()

    # Explicit cleanup removes expired record
    assert store.cleanup_expired_record("tenant_ghost", "k_old") is True
    assert not file_expired.exists()

    # Explicit cleanup on valid record is a no-op
    assert store.cleanup_expired_record("tenant_ghost", "k_valid") is False
    assert file_valid.is_file()
    assert store.get_record("tenant_ghost", "k_valid") is not None


def test_saving_new_record_over_expired_record_succeeds(tmp_path):
    store = LocalIdempotencyStore(tmp_path)
    now = time.time()
    expired = IdempotencyRecord(
        idempotency_key="k_reuse",
        tenant_id="tenant_ghost",
        request_fingerprint="fp_old",
        document_id="doc_old",
        response_payload={"stage": 1},
        created_at_utc=now - 90000,
        expires_at_utc=now - 3600,
    )
    store.save_record(expired)
    assert store.get_record("tenant_ghost", "k_reuse") is None

    # Writing a new record with the same key overwrites the expired record
    fresh = IdempotencyRecord(
        idempotency_key="k_reuse",
        tenant_id="tenant_ghost",
        request_fingerprint="fp_new",
        document_id="doc_new",
        response_payload={"stage": 2},
        created_at_utc=now,
        expires_at_utc=now + 3600,
    )
    store.save_record(fresh)
    active = store.get_record("tenant_ghost", "k_reuse")
    assert active is not None
    assert active.document_id == "doc_new"
    assert active.response_payload == {"stage": 2}


# -- deterministic idempotency cleanup concurrency tests (TOCTOU mitigation) -----------------------------

def test_concurrency_cleanup_first_blocks_save_until_done_and_fresh_record_survives(tmp_path):
    store = LocalIdempotencyStore(tmp_path)
    now = time.time()
    key = "k_order_a"
    tenant = "tenant_ghost"

    expired = IdempotencyRecord(
        idempotency_key=key,
        tenant_id=tenant,
        request_fingerprint="fp_old",
        document_id="doc_old",
        response_payload={"ver": "old"},
        created_at_utc=now - 90000,
        expires_at_utc=now - 3600,
    )
    store.save_record(expired)

    cleanup_locked = threading.Event()
    cleanup_proceed = threading.Event()
    save_completed = threading.Event()

    def controlled_about_to_unlink(t, k):
        if t == tenant and k == key:
            cleanup_locked.set()
            assert cleanup_proceed.wait(timeout=5.0), "cleanup_proceed event timed out"

    store._on_cleanup_about_to_unlink = controlled_about_to_unlink

    cleanup_result = []

    def run_cleanup():
        res = store.cleanup_expired_record(tenant, key)
        cleanup_result.append(res)

    fresh = IdempotencyRecord(
        idempotency_key=key,
        tenant_id=tenant,
        request_fingerprint="fp_fresh",
        document_id="doc_fresh",
        response_payload={"ver": "fresh"},
        created_at_utc=now,
        expires_at_utc=now + 3600,
    )

    def run_save():
        store.save_record(fresh)
        save_completed.set()

    t_cleanup = threading.Thread(target=run_cleanup, daemon=True)
    t_save = threading.Thread(target=run_save, daemon=True)

    t_cleanup.start()
    assert cleanup_locked.wait(timeout=5.0), "cleanup thread did not acquire lock in time"

    # At this point, t_cleanup holds the key lock. t_save must block on that key lock.
    t_save.start()
    # Ensure t_save had time to reach the lock and is indeed blocked
    assert not save_completed.wait(timeout=0.1), "save completed while cleanup held the key lock!"
    assert t_save.is_alive()

    # Release cleanup to finish unlinking and release the lock
    cleanup_proceed.set()

    t_cleanup.join(timeout=5.0)
    t_save.join(timeout=5.0)

    assert not t_cleanup.is_alive(), "cleanup thread hung"
    assert not t_save.is_alive(), "save thread hung"
    assert cleanup_result == [True]
    assert save_completed.is_set()

    # The fresh record saved after cleanup MUST be intact on disk and readable
    active = store.get_record(tenant, key)
    assert active is not None
    assert active.document_id == "doc_fresh"
    assert active.response_payload == {"ver": "fresh"}


def test_concurrency_save_first_prevents_cleanup_from_deleting_fresh_record(tmp_path):
    store = LocalIdempotencyStore(tmp_path)
    now = time.time()
    key = "k_order_b"
    tenant = "tenant_ghost"

    expired = IdempotencyRecord(
        idempotency_key=key,
        tenant_id=tenant,
        request_fingerprint="fp_old",
        document_id="doc_old",
        response_payload={},
        created_at_utc=now - 90000,
        expires_at_utc=now - 3600,
    )
    store.save_record(expired)

    # Save fresh record first
    fresh = IdempotencyRecord(
        idempotency_key=key,
        tenant_id=tenant,
        request_fingerprint="fp_fresh",
        document_id="doc_fresh",
        response_payload={"ver": "fresh"},
        created_at_utc=now,
        expires_at_utc=now + 3600,
    )
    store.save_record(fresh)

    # Cleanup must re-verify expiry under lock and recognize the fresh record
    deleted = store.cleanup_expired_record(tenant, key)
    assert deleted is False, "cleanup deleted an unexpired, freshly renewed record!"

    active = store.get_record(tenant, key)
    assert active is not None
    assert active.document_id == "doc_fresh"


def test_concurrency_reentrant_lock_prevents_deadlock_on_external_workflow(tmp_path):
    store = LocalIdempotencyStore(tmp_path)
    now = time.time()
    key = "k_reentrant"
    tenant = "tenant_ghost"

    # Simulate an orchestrator like IngestionService that acquires get_key_lock externally
    external_lock = store.get_key_lock(tenant, key)
    assert external_lock.acquire(timeout=5.0)
    try:
        # Initial read
        assert store.get_record(tenant, key) is None

        # Internal save acquires the same per-key lock via RLock without deadlocking
        rec = IdempotencyRecord(
            idempotency_key=key,
            tenant_id=tenant,
            request_fingerprint="fp",
            document_id="doc_1",
            response_payload={},
            created_at_utc=now,
            expires_at_utc=now + 3600,
        )
        store.save_record(rec)

        # Internal delete also acquires the same per-key lock without deadlocking
        store.delete_record(tenant, key)
        assert store.get_record(tenant, key) is None
    finally:
        external_lock.release()


def test_concurrency_different_keys_proceed_independently(tmp_path):
    store = LocalIdempotencyStore(tmp_path)
    now = time.time()
    tenant = "tenant_ghost"
    key_a = "key_alpha"
    key_b = "key_beta"

    lock_a_held = threading.Event()
    save_b_completed = threading.Event()
    release_lock_a = threading.Event()

    def hold_key_a():
        with store.get_key_lock(tenant, key_a):
            lock_a_held.set()
            assert release_lock_a.wait(timeout=5.0)

    t_holder = threading.Thread(target=hold_key_a, daemon=True)
    t_holder.start()
    assert lock_a_held.wait(timeout=5.0)

    # While key_a is locked, operations on key_b must NOT block
    rec_b = IdempotencyRecord(
        idempotency_key=key_b,
        tenant_id=tenant,
        request_fingerprint="fp_b",
        document_id="doc_b",
        response_payload={"key": "b"},
        created_at_utc=now,
        expires_at_utc=now + 3600,
    )

    def write_key_b():
        store.save_record(rec_b)
        save_b_completed.set()

    t_writer = threading.Thread(target=write_key_b, daemon=True)
    t_writer.start()

    # Must complete promptly without waiting on key_a's lock
    assert save_b_completed.wait(timeout=1.0), "key_b operation was blocked by key_a lock!"
    t_writer.join(timeout=2.0)
    assert not t_writer.is_alive()

    release_lock_a.set()
    t_holder.join(timeout=2.0)
    assert not t_holder.is_alive()

    assert store.get_record(tenant, key_b) is not None
