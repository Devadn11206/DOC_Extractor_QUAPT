"""
SEC-006 tenant isolation of local storage and the idempotency store. Needs NO FastAPI, so it always runs
(it used to live in a module gated on the optional ``api`` extra and was skipped without it).

What is proven here, on the REAL filesystem of the test run:
- identifiers that could leave their directory (or are oversized / not Unicode) are refused with typed errors;
- two different identities never share a directory, whatever the filesystem folds or normalizes
  (case, NFC/NFD); no identity is silently case-folded or normalized;
- existing data is used where it already is and is not modified or migrated;
- symlinks, aliases, forged URIs and identity-marker tampering are refused.
"""

import json
import os
import threading
import unicodedata
from datetime import datetime, timezone

import pytest

from doc_extractor.exceptions import (
    InvalidTenantIdentifierError,
    StorageError,
    TenantStorageIsolationError,
)
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.models import IdempotencyRecord
from doc_extractor.storage import LocalDiskStorage
from doc_extractor.tenancy import (
    HASHED_KEY_PREFIX,
    IDENTITY_MARKER,
    MAX_TENANT_ID_BYTES,
    TenantDirectoryResolver,
    require_path_safe_tenant_id,
    tenant_id_path_problem,
    tenant_storage_key,
)

UNSAFE = [
    "", ".", "..", "tenant_a/../tenant_b", "../x", "a/b", "/abs", "a\\b", "..\\x",
    "a\x00b", "a\nb", "a\tb", "a\x7fb",
]
# Valid tenant IDs of any style stay valid: the guard is not a format rule.
ORDINARY = ["tenant_alpha", "default_tenant", "t-1", "org:123", "Acme Corp", "a..b", "...", "tenant.alpha", "测试"]
NFC, NFD = unicodedata.normalize("NFC", "é1"), unicodedata.normalize("NFD", "é1")


class Rec:
    """A minimal document record for save_document_record."""

    def __init__(self, document_id, owner):
        self.document_id, self.owner = document_id, owner

    def to_dict(self):
        return {"document_id": self.document_id, "owner": self.owner}


def put(storage: LocalDiskStorage, document_id="doc_1"):
    storage.save_document_record(Rec(document_id, storage.tenant_id))


def owner_of(storage: LocalDiskStorage, document_id="doc_1"):
    record = storage.get_document_record(document_id)
    return None if record is None else record["owner"]


# -- the shared guard -----------------------------------------------------------------------------

@pytest.mark.parametrize("tenant_id", UNSAFE)
def test_unsafe_tenant_ids_are_rejected(tenant_id):
    assert tenant_id_path_problem(tenant_id, windows=False) is not None
    with pytest.raises(InvalidTenantIdentifierError):
        require_path_safe_tenant_id(tenant_id, windows=False)


@pytest.mark.parametrize("tenant_id", ORDINARY)
def test_ordinary_tenant_ids_are_not_restricted(tenant_id):
    assert tenant_id_path_problem(tenant_id, windows=False) is None
    assert tenant_id_path_problem(tenant_id, windows=True) is None or ":" in tenant_id
    assert require_path_safe_tenant_id(tenant_id, windows=False) == tenant_id      # returned unchanged: never normalized


def test_a_drive_prefix_is_rejected_only_under_windows_semantics():
    assert tenant_id_path_problem("C:", windows=True) == "drive_prefix"
    assert tenant_id_path_problem("C:", windows=False) is None
    assert tenant_id_path_problem("org:123", windows=True) is None


def test_non_strings_are_rejected_and_the_value_is_never_echoed():
    assert tenant_id_path_problem(None) == "not_a_string"
    assert tenant_id_path_problem(123) == "not_a_string"
    with pytest.raises(InvalidTenantIdentifierError) as exc:
        require_path_safe_tenant_id("tenant_a/../tenant_b")
    assert "tenant_a" not in str(exc.value) and "tenant_a" not in str(exc.value.details)
    assert isinstance(exc.value, ValueError) and isinstance(exc.value, StorageError)       # typed, and usable as a validator error


# -- oversized and malformed identities -----------------------------------------------------------

def test_the_length_limit_is_in_utf8_bytes_and_the_boundary_is_exact():
    assert tenant_id_path_problem("a" * MAX_TENANT_ID_BYTES) is None
    assert tenant_id_path_problem("a" * (MAX_TENANT_ID_BYTES + 1)) == "too_long"
    assert tenant_id_path_problem("é" * 127) is None            # 254 bytes
    assert tenant_id_path_problem("é" * 128) == "too_long"      # 256 bytes although only 128 characters


def test_a_lone_surrogate_is_a_typed_error_not_a_unicode_encode_crash():
    assert tenant_id_path_problem("tenant\ud800") == "invalid_unicode"
    with pytest.raises(InvalidTenantIdentifierError):
        tenant_storage_key("tenant\ud800")


@pytest.mark.parametrize("size", [MAX_TENANT_ID_BYTES + 1, 400, 5000])
def test_an_oversized_tenant_id_is_a_typed_error_not_an_os_error(tmp_path, size):
    for factory in (lambda: LocalDiskStorage(tmp_path, tenant_id="x" * size),
                    lambda: LocalIdempotencyStore(tmp_path).get_record("x" * size, "k")):
        with pytest.raises(InvalidTenantIdentifierError) as exc:
            factory()
        assert exc.value.code == "INVALID_TENANT_IDENTIFIER" and exc.value.details == {"reason": "too_long"}


def test_a_maximum_length_identity_works_because_the_directory_name_is_a_fixed_length_key(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="é" * 127)          # 254 bytes: would exceed a name limit if used raw
    put(storage)
    assert owner_of(storage) == "é" * 127
    assert len(os.listdir(tmp_path)) == 1 and len(os.listdir(tmp_path)[0]) < 64


# -- the storage key ------------------------------------------------------------------------------

@pytest.mark.parametrize("tenant_id", ["tenant_a", "default_tenant", "t-1", "a1_b2-c3", "test_tenant"])
def test_simple_lowercase_ids_keep_their_own_name_so_existing_data_stays_where_it_is(tenant_id):
    assert tenant_storage_key(tenant_id) == tenant_id


@pytest.mark.parametrize("tenant_id", ["Tenant_X", "ACME", "org:123", "Acme Corp", "测试", NFC, NFD, "a" * 64, "con", "nul", "com1", "a+b", "a.b"])
def test_every_other_identity_gets_a_deterministic_case_stable_key(tenant_id):
    key = tenant_storage_key(tenant_id)
    assert key == tenant_storage_key(tenant_id)                        # deterministic
    assert key.startswith(HASHED_KEY_PREFIX) and len(key) == len(HASHED_KEY_PREFIX) + 32
    assert key.isascii() and key == key.lower()                        # nothing for a filesystem to fold or normalize
    assert set(key[len(HASHED_KEY_PREFIX):]) <= set("0123456789abcdef")


def test_distinct_identities_never_share_a_key_and_nothing_is_case_folded_or_normalized():
    assert tenant_storage_key("Tenant_X") != tenant_storage_key("tenant_x")
    assert tenant_storage_key("TENANT_x") != tenant_storage_key("Tenant_X")
    assert NFC != NFD and tenant_storage_key(NFC) != tenant_storage_key(NFD)
    assert tenant_storage_key("tenant_x") == "tenant_x"                # plain key untouched
    keys = {tenant_storage_key(t) for t in ORDINARY + ["Tenant_X", "tenant_x", NFC, NFD, "TENANT_X"]}
    assert len(keys) == len(set(ORDINARY + ["Tenant_X", "tenant_x", NFC, NFD, "TENANT_X"]))


def test_the_hashed_namespace_cannot_be_forged_by_choosing_an_identity_equal_to_a_victims_key():
    victim_key = tenant_storage_key("Victim")
    assert victim_key.startswith(HASHED_KEY_PREFIX)
    forged = tenant_storage_key(victim_key)                            # the attacker's tenant id IS the victim's key
    assert forged != victim_key and forged.startswith(HASHED_KEY_PREFIX)


def test_plain_keys_can_never_look_like_hashed_keys():
    assert "+" not in "".join(c for c in "abcdefghijklmnopqrstuvwxyz0123456789_-")
    for tenant_id in ("h", "h-abc", "h_abc", "habc"):
        assert tenant_storage_key(tenant_id) == tenant_id and not tenant_id.startswith(HASHED_KEY_PREFIX)


# -- real-filesystem isolation: collisions --------------------------------------------------------

@pytest.mark.parametrize("a,b", [("Tenant_X", "tenant_x"), ("TENANT_x", "Tenant_X"), (NFC, NFD), ("Acme", "ACME")])
def test_identities_that_could_alias_on_this_filesystem_never_share_data(tmp_path, a, b):
    first, second = LocalDiskStorage(tmp_path, tenant_id=a), LocalDiskStorage(tmp_path, tenant_id=b)
    put(first)
    assert owner_of(second) is None                                     # the second tenant cannot see the first's document
    put(second)
    assert owner_of(first) == a and owner_of(second) == b
    assert not os.path.samefile(first._tenant_dir(create=False), second._tenant_dir(create=False))


def test_case_variants_are_separate_even_when_each_is_resolved_first(tmp_path):
    for number, order in enumerate((("Tenant_X", "tenant_x"), ("tenant_x", "Tenant_X"))):
        base = tmp_path / f"root{number}"                 # distinct names: the roots themselves must not alias
        base.mkdir()
        dirs = [TenantDirectoryResolver(base).resolve(t, create=True) for t in order]
        assert not os.path.samefile(*dirs)


def test_the_same_identity_always_resolves_to_the_same_directory(tmp_path):
    first = LocalDiskStorage(tmp_path, tenant_id="Org:7")._tenant_dir(create=True)
    again = LocalDiskStorage(tmp_path, tenant_id="Org:7")._tenant_dir(create=True)
    assert first == again and first.name == tenant_storage_key("Org:7")


def test_both_stores_agree_on_the_tenant_directory(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="Tenant_X")
    store = LocalIdempotencyStore(tmp_path)
    assert store._resolver.resolve("Tenant_X", create=True) == storage._tenant_dir(create=True)


# -- existing data is preserved -------------------------------------------------------------------

def test_a_plain_tenant_keeps_the_original_directory_layout(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_alpha")
    put(storage)
    assert (tmp_path / "tenant_alpha" / "doc_1" / "document_record.json").is_file()


def test_an_existing_legacy_directory_named_exactly_like_the_tenant_is_used_and_left_untouched(tmp_path):
    legacy = tmp_path / "Acme Corp" / "doc_1"
    legacy.mkdir(parents=True)
    (legacy / "document_record.json").write_text(json.dumps({"document_id": "doc_1", "owner": "legacy"}))
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    storage = LocalDiskStorage(tmp_path, tenant_id="Acme Corp")
    assert owner_of(storage) == "legacy"                                # old data found where it already was
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before    # nothing added, moved or deleted
    assert not any(name.startswith(HASHED_KEY_PREFIX) for name in os.listdir(tmp_path))
    assert not (tmp_path / "Acme Corp" / IDENTITY_MARKER).exists()


def test_a_legacy_directory_is_never_shared_with_a_differently_spelled_identity(tmp_path):
    legacy = tmp_path / "Acme" / "doc_1"
    legacy.mkdir(parents=True)
    (legacy / "document_record.json").write_text(json.dumps({"document_id": "doc_1", "owner": "legacy"}))
    try:
        other = LocalDiskStorage(tmp_path, tenant_id="acme")
        put(other)
    except TenantStorageIsolationError as exc:                           # case-insensitive filesystem: refused outright
        assert exc.details["reason"] == "directory_alias_collision"
    else:                                                                # case-sensitive filesystem: kept apart
        assert LocalDiskStorage(tmp_path, tenant_id="Acme").get_document_record("doc_1")["owner"] == "legacy"
        assert owner_of(other) == "acme"
    assert json.loads((legacy / "document_record.json").read_text())["owner"] == "legacy"   # the legacy record is intact


def test_an_unusual_identity_with_no_legacy_directory_is_created_hashed_with_a_marker(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="Org:7")
    put(storage)
    directory = tmp_path / tenant_storage_key("Org:7")
    marker = json.loads((directory / IDENTITY_MARKER).read_text())
    assert marker == {"schema": 1, "tenant_id": "Org:7", "key": directory.name}


# -- identity verification ------------------------------------------------------------------------

def _hashed_dir(tmp_path, tenant_id="Org:7"):
    put(LocalDiskStorage(tmp_path, tenant_id=tenant_id))
    return tmp_path / tenant_storage_key(tenant_id)


def test_a_marker_naming_another_identity_is_refused(tmp_path):
    directory = _hashed_dir(tmp_path)
    (directory / IDENTITY_MARKER).write_text(json.dumps({"schema": 1, "tenant_id": "Org:8", "key": directory.name}))
    with pytest.raises(TenantStorageIsolationError) as exc:
        owner_of(LocalDiskStorage(tmp_path, tenant_id="Org:7"))
    assert exc.value.details["reason"] == "identity_marker_mismatch"


def test_a_hashed_directory_without_its_marker_is_refused(tmp_path):
    directory = _hashed_dir(tmp_path)
    (directory / IDENTITY_MARKER).unlink()
    with pytest.raises(TenantStorageIsolationError) as exc:
        owner_of(LocalDiskStorage(tmp_path, tenant_id="Org:7"))
    assert exc.value.details["reason"] == "identity_marker_missing"


@pytest.mark.parametrize("content,reason", [("not json", "identity_marker_corrupt"), ("[1, 2]", "identity_marker_corrupt")])
def test_a_corrupt_marker_is_refused(tmp_path, content, reason):
    directory = _hashed_dir(tmp_path)
    (directory / IDENTITY_MARKER).write_text(content)
    with pytest.raises(TenantStorageIsolationError) as exc:
        owner_of(LocalDiskStorage(tmp_path, tenant_id="Org:7"))
    assert exc.value.details["reason"] == reason


def test_a_symlinked_marker_is_refused(tmp_path):
    directory = _hashed_dir(tmp_path)
    forged = tmp_path / "forged.json"
    forged.write_text(json.dumps({"schema": 1, "tenant_id": "Org:7", "key": directory.name}))
    (directory / IDENTITY_MARKER).unlink()
    os.symlink(forged, directory / IDENTITY_MARKER)
    with pytest.raises(TenantStorageIsolationError):
        owner_of(LocalDiskStorage(tmp_path, tenant_id="Org:7"))


def test_a_tenant_cannot_adopt_another_tenants_hashed_directory_by_naming_it(tmp_path):
    victim_dir = _hashed_dir(tmp_path, "Victim")
    attacker = LocalDiskStorage(tmp_path, tenant_id=victim_dir.name)       # the attacker's id IS the victim's directory name
    assert owner_of(attacker) is None
    put(attacker)
    assert LocalDiskStorage(tmp_path, tenant_id="Victim").get_document_record("doc_1")["owner"] == "Victim"
    assert attacker._tenant_dir(create=True).name != victim_dir.name


def test_a_marker_in_a_legacy_directory_naming_someone_else_is_refused(tmp_path):
    legacy = tmp_path / "Legacy Co"
    legacy.mkdir()
    (legacy / IDENTITY_MARKER).write_text(json.dumps({"schema": 1, "tenant_id": "Somebody Else", "key": "Legacy Co"}))
    with pytest.raises(TenantStorageIsolationError):
        owner_of(LocalDiskStorage(tmp_path, tenant_id="Legacy Co"))


def test_resolving_without_create_never_creates_anything(tmp_path):
    resolver = TenantDirectoryResolver(tmp_path / "does-not-exist-yet")
    path = resolver.resolve("Org:9", create=False)
    assert path.name == tenant_storage_key("Org:9") and not path.exists() and not resolver.base_dir.exists()


def test_concurrent_first_use_of_one_new_tenant_converges_on_one_valid_directory(tmp_path):
    resolvers = [TenantDirectoryResolver(tmp_path) for _ in range(8)]
    barrier, results, errors = threading.Barrier(8), [], []

    def worker(resolver):
        try:
            barrier.wait()
            results.append(resolver.resolve("Org:Race", create=True))
        except Exception as exc:                                        # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(r,)) for r in resolvers]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(set(results)) == 1
    assert sorted(os.listdir(tmp_path)) == [tenant_storage_key("Org:Race")]       # no leftover staging directories


# -- symlinks -------------------------------------------------------------------------------------

def test_a_symlinked_tenant_directory_is_refused(tmp_path):
    put(LocalDiskStorage(tmp_path, tenant_id="tenant_v"))
    os.symlink(tmp_path / "tenant_v", tmp_path / "tenant_s")
    with pytest.raises(TenantStorageIsolationError) as exc:
        owner_of(LocalDiskStorage(tmp_path, tenant_id="tenant_s"))
    assert exc.value.details["reason"] == "tenant_directory_is_a_symlink"


def test_a_symlinked_document_directory_into_another_tenant_is_refused(tmp_path):
    victim = LocalDiskStorage(tmp_path, tenant_id="tenant_v")
    put(victim, "doc_9")
    mine = LocalDiskStorage(tmp_path, tenant_id="tenant_s")
    mine._tenant_dir(create=True)
    os.symlink(tmp_path / "tenant_v" / "doc_9", tmp_path / "tenant_s" / "doc_9")
    with pytest.raises(TenantStorageIsolationError) as exc:
        mine.get_document_record("doc_9")
    assert exc.value.details["reason"] == "symlink_inside_tenant_storage"


def test_a_symlinked_subdirectory_cannot_redirect_writes_into_another_tenant(tmp_path):
    outside = tmp_path / "tenant_v" / "doc_1" / "pages"
    outside.mkdir(parents=True)
    mine = LocalDiskStorage(tmp_path, tenant_id="tenant_s")
    (mine._get_document_dir("doc_1", create=True)).mkdir(parents=True, exist_ok=True)
    os.symlink(outside, tmp_path / "tenant_s" / "doc_1" / "pages")
    with pytest.raises(TenantStorageIsolationError):
        mine.store_page_image("doc_1", 1, b"png-bytes")
    assert list(outside.iterdir()) == []                                  # nothing was written into the victim


def test_a_symlinked_record_file_is_refused_instead_of_followed(tmp_path):
    secret = tmp_path / "tenant_v" / "doc_1"
    secret.mkdir(parents=True)
    (secret / "document_record.json").write_text(json.dumps({"document_id": "doc_1", "owner": "victim"}))
    mine = LocalDiskStorage(tmp_path, tenant_id="tenant_s")
    os.makedirs(mine._get_document_dir("doc_1", create=True), exist_ok=True)
    os.symlink(secret / "document_record.json", tmp_path / "tenant_s" / "doc_1" / "document_record.json")
    with pytest.raises(TenantStorageIsolationError):
        mine.get_document_record("doc_1")


def test_symlinked_documents_and_records_are_skipped_by_the_hash_search(tmp_path):
    victim = LocalDiskStorage(tmp_path, tenant_id="tenant_v")
    victim.save_document_record(type("R", (), {"document_id": "doc_1", "to_dict": lambda self: {"document_id": "doc_1", "file_hash_sha256": "H"}})())
    mine = LocalDiskStorage(tmp_path, tenant_id="tenant_s")
    mine._tenant_dir(create=True)
    os.symlink(tmp_path / "tenant_v" / "doc_1", tmp_path / "tenant_s" / "doc_1")
    assert mine.find_document_by_hash("H") is None


def test_a_symlinked_idempotency_directory_is_refused(tmp_path):
    (tmp_path / "tenant_v" / "idempotency").mkdir(parents=True)
    store = LocalIdempotencyStore(tmp_path)
    store._get_record_path("tenant_s", "k", create=True)                 # provisions tenant_s and its idempotency directory
    (tmp_path / "tenant_s" / "idempotency").rmdir()
    os.symlink(tmp_path / "tenant_v" / "idempotency", tmp_path / "tenant_s" / "idempotency")
    with pytest.raises(TenantStorageIsolationError):
        store.get_record("tenant_s", "k")


# -- logical URIs and cross-tenant access ---------------------------------------------------------

@pytest.mark.parametrize("tenant_id", ["tenant_a", "Tenant_X", "Org:7", NFD])
def test_logical_uris_stay_tenant_prefixed_and_resolve_to_the_real_file_for_any_identity(tmp_path, tenant_id):
    storage = LocalDiskStorage(tmp_path, tenant_id=tenant_id)
    artifact = storage.store_original_document("doc_1", b"%PDF-1.4 test")
    assert artifact.storage_uri == f"{tenant_id}/doc_1/source/original.pdf"          # the hand-off contract is unchanged
    assert storage.resolve_uri_to_path(artifact.storage_uri) == artifact.file_path.resolve()
    assert artifact.file_path.read_bytes() == b"%PDF-1.4 test"


def test_a_uri_of_another_tenant_or_an_alias_of_this_one_is_refused(tmp_path):
    mine, other = LocalDiskStorage(tmp_path, tenant_id="tenant_x"), LocalDiskStorage(tmp_path, tenant_id="tenant_b")
    other.store_original_document("doc_1", b"secret")
    for uri in ("tenant_b/doc_1/source/original.pdf", "Tenant_X/doc_1/source/original.pdf", "tenant_xx/doc_1/x",
                "tenant_x//doc_1/x", "tenant_x/../tenant_b/doc_1/x", "/etc/passwd", "tenant_x\\..\\x"):
        with pytest.raises(StorageError):
            mine.resolve_uri_to_path(uri)


def test_a_uri_through_a_symlink_is_refused(tmp_path):
    mine = LocalDiskStorage(tmp_path, tenant_id="tenant_x")
    mine.store_original_document("doc_1", b"mine")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside, tmp_path / "tenant_x" / "doc_1" / "link")
    with pytest.raises(StorageError):
        mine.resolve_uri_to_path("tenant_x/doc_1/link/secret.txt")


def test_one_tenant_cannot_read_anothers_document_by_guessing_its_id(tmp_path):
    a, b = LocalDiskStorage(tmp_path, tenant_id="tenant_a"), LocalDiskStorage(tmp_path, tenant_id="Tenant_B")
    put(a, "doc_1")
    assert owner_of(b, "doc_1") is None and b.find_document_by_hash("anything") is None
    assert b.list_page_images("doc_1") == [] and b.get_handoff_payload("doc_1") is None


def test_a_tenant_id_reassigned_after_construction_is_revalidated(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_a")
    storage.tenant_id = "tenant_a/../tenant_b"
    with pytest.raises(InvalidTenantIdentifierError):
        storage._get_document_dir("doc_1")


def test_storage_errors_never_echo_the_tenant_id(tmp_path):
    with pytest.raises(InvalidTenantIdentifierError) as exc:
        LocalDiskStorage(tmp_path, tenant_id="tenant_a/../tenant_b")
    assert "tenant_b" not in exc.value.message and "tenant_b" not in str(exc.value.details)


@pytest.mark.parametrize("tenant_id", UNSAFE)
def test_storage_refuses_an_unsafe_tenant_id_with_a_typed_error(tmp_path, tenant_id):
    with pytest.raises(InvalidTenantIdentifierError):
        LocalDiskStorage(tmp_path, tenant_id=tenant_id)


# -- idempotency store ----------------------------------------------------------------------------

def _record(tenant_id, key="k1"):
    now = datetime.now(timezone.utc).timestamp()
    return IdempotencyRecord(
        idempotency_key=key, tenant_id=tenant_id, request_fingerprint="f", document_id="doc_1",
        response_payload={}, created_at_utc=now, expires_at_utc=now + 3600,
    )


@pytest.mark.parametrize("tenant_id", ["tenant_a/../tenant_b", "../x", "a/b", "..", "x" * 400])
def test_the_idempotency_store_rejects_instead_of_collapsing_the_tenant_id(tmp_path, tenant_id):
    store = LocalIdempotencyStore(tmp_path)
    with pytest.raises(InvalidTenantIdentifierError):
        store.get_record(tenant_id, "k1")
    with pytest.raises(InvalidTenantIdentifierError):
        store.save_record(_record(tenant_id))
    with pytest.raises(InvalidTenantIdentifierError):
        store.delete_record(tenant_id, "k1")


def test_idempotency_records_of_two_tenants_stay_separate(tmp_path):
    store = LocalIdempotencyStore(tmp_path)
    store.save_record(_record("tenant_a"))
    assert store.get_record("tenant_a", "k1") is not None
    assert store.get_record("tenant_b", "k1") is None


@pytest.mark.parametrize("a,b", [("Tenant_X", "tenant_x"), (NFC, NFD)])
def test_idempotency_records_of_aliasing_identities_stay_separate(tmp_path, a, b):
    store = LocalIdempotencyStore(tmp_path)
    store.save_record(_record(a))
    assert store.get_record(a, "k1") is not None
    assert store.get_record(b, "k1") is None


def test_the_idempotency_lock_key_is_the_unambiguous_tenant_key_pair(tmp_path):
    store = LocalIdempotencyStore(tmp_path)
    assert store.get_key_lock("a:b", "c") is not store.get_key_lock("a", "b:c")      # a joined string would alias these
    assert store.get_key_lock("t", "k") is store.get_key_lock("t", "k")
    assert store.get_key_lock("t1", "k") is not store.get_key_lock("t2", "k")


# -- the alias branch, exercised on ANY filesystem --------------------------------------------------

def test_an_alias_of_an_existing_directory_is_refused_on_a_simulated_case_insensitive_filesystem(tmp_path, monkeypatch):
    """CI runs on a case-sensitive filesystem where an alias never occurs naturally: simulate one."""
    (tmp_path / "Acme").mkdir()
    real_lexists = os.path.lexists

    def insensitive_lexists(path):
        path = os.fspath(path)
        parent, name = os.path.split(path)
        return real_lexists(path) or (os.path.isdir(parent) and any(e.lower() == name.lower() for e in os.listdir(parent)))

    monkeypatch.setattr(os.path, "lexists", insensitive_lexists)
    with pytest.raises(TenantStorageIsolationError) as exc:
        LocalDiskStorage(tmp_path, tenant_id="acme")._tenant_dir()
    assert exc.value.details == {"reason": "directory_alias_collision"} and exc.value.code == "TENANT_STORAGE_ISOLATION_FAILED"
    assert sorted(os.listdir(tmp_path)) == ["Acme"]                       # nothing was created or changed


def test_the_exact_spelling_is_still_found_on_a_simulated_insensitive_filesystem(tmp_path, monkeypatch):
    (tmp_path / "acme").mkdir()
    real_lexists = os.path.lexists
    monkeypatch.setattr(os.path, "lexists", lambda p: real_lexists(p) or os.path.basename(os.fspath(p)).lower() == "acme")
    assert LocalDiskStorage(tmp_path, tenant_id="acme")._tenant_dir() == tmp_path.resolve() / "acme"
