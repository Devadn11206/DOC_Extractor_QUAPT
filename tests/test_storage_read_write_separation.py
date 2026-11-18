"""
Reads never provision storage; writes do, and a filesystem failure is a typed error (review finding R1).

Before this change ``LocalDiskStorage`` resolved EVERY document path with ``create=True``: a plain GET for a tenant that
had no directory created that directory plus its identity marker, and on a read-only storage root the read raised a bare
``PermissionError`` (a 500) where the original code returned ``None`` (a 404). Needs no FastAPI, so it always runs.
"""

import os
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
