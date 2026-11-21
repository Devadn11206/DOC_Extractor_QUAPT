"""
``..`` is refused as a path COMPONENT, not as a substring (review finding R6).

``resolve_uri_to_path`` used to reject any URI containing the two characters ``..``. The tenant validator accepts ``a..b``
(an ordinary name), so such a tenant could store artifacts but never resolve their own logical URI. Needs no FastAPI.
"""

import os

import pytest

from doc_extractor.exceptions import CrossTenantStorageAccessError, StorageError
from doc_extractor.storage import LocalDiskStorage
from doc_extractor.tenancy import require_path_safe_tenant_id

# Valid tenant identifiers, including every spelling that merely CONTAINS dots.
VALID_TENANTS = ["a..b", "...", "..a", "a..", "a...b", "tenant.alpha", "v1..2", "tenant_alpha", "Acme Corp", "org:123", "测试"]


@pytest.mark.parametrize("tenant_id", VALID_TENANTS)
def test_a_valid_tenant_round_trips_its_own_uri(tmp_path, tenant_id):
    require_path_safe_tenant_id(tenant_id)                                   # the validator accepts it...
    storage = LocalDiskStorage(tmp_path, tenant_id=tenant_id)
    artifact = storage.store_original_document("doc_1", b"%PDF-1.4 x")
    page = storage.store_page_image("doc_1", 1, b"\x89PNG", "png")
    page_uri = f"{tenant_id}/doc_1/pages/{page.file_path.name}"               # the pipeline builds page URIs from the tenant and file
    for stored, uri in ((artifact, artifact.storage_uri), (page, page_uri)):
        resolved = storage.resolve_uri_to_path(uri)                          # ...and so does the resolver
        assert resolved == stored.file_path.resolve() and resolved.is_file()
    assert artifact.storage_uri.split("/")[0] == tenant_id


def test_a_tenant_with_dots_never_reaches_another_tenants_directory(tmp_path):
    victim = LocalDiskStorage(tmp_path, tenant_id="victim")
    victim.store_original_document("doc_1", b"VICTIM")
    dotted = LocalDiskStorage(tmp_path, tenant_id="a..b")
    dotted.store_original_document("doc_1", b"mine")
    with pytest.raises(CrossTenantStorageAccessError):
        dotted.resolve_uri_to_path("victim/doc_1/source/original.pdf")
    with pytest.raises(CrossTenantStorageAccessError):
        victim.resolve_uri_to_path("a..b/doc_1/source/original.pdf")
    assert victim.resolve_uri_to_path("victim/doc_1/source/original.pdf").read_bytes() == b"VICTIM"


@pytest.mark.parametrize("uri", [
    "tenant_a/../tenant_b/doc_1/source/original.pdf", "../tenant_b/doc_1", "tenant_a/doc_1/..", "tenant_a/..", "..",
    "tenant_a/doc_1/../../tenant_b", "a..b/../tenant_b/doc_1", "..\\tenant_b", "tenant_a\\doc_1", "/etc/passwd",
    "tenant_a/doc_1/source/\x00original.pdf", "tenant_a/doc_1/source/orig\ninal.pdf", "tenant_a/doc_1/\x7f",
])
def test_a_real_dot_dot_component_a_backslash_a_root_or_a_control_character_is_still_refused(tmp_path, uri):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_a")
    with pytest.raises(StorageError) as raised:
        storage.resolve_uri_to_path(uri)
    assert raised.value.message == "Unsafe storage URI detected."


@pytest.mark.parametrize("uri", [
    "tenant_a/doc..1/source/original.pdf", "tenant_a/doc_1/source/file..pdf", "tenant_a/doc_1/source/..hidden",
    "tenant_a/doc_1/a...b/x", "tenant_a/doc_1/./source/original.pdf",
])
def test_dots_inside_a_component_are_ordinary(tmp_path, uri):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_a")
    resolved = storage.resolve_uri_to_path(uri)
    assert os.path.commonpath([resolved, storage._tenant_dir().resolve()]) == str(storage._tenant_dir().resolve())


@pytest.mark.parametrize("uri", ["other/doc_1/x", "tenant_a_/doc_1/x", "TENANT_A/doc_1/x", "tenant_a./doc_1/x", "/tenant_a/doc_1/x"])
def test_the_first_segment_must_be_exactly_the_tenant(tmp_path, uri):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_a")
    with pytest.raises(StorageError):
        storage.resolve_uri_to_path(uri)
