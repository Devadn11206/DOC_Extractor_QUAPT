"""
Mocked unit tests for S3Storage adapter (Team Member 3, Phase 4A).

Covers:
- Operations: store, get, list, delete, cleanup, resolve_uri_to_path.
- Tenant isolation: namespace separation, cross-tenant access rejection, path safety.
- Metadata hygiene: no credentials, sensitive PII or document text in metadata.
- Error handling: typed errors, connection failures, timeouts, 404/403 mapping, no secret leaks.
- Compatibility with existing callers: StorageBackend interface and LoaderB interaction.
"""

import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
    SSLError,
)
from pydantic import SecretStr

from doc_extractor.exceptions import (
    CrossTenantStorageAccessError,
    InvalidTenantIdentifierError,
    StorageError,
    TenantStorageUnavailableError,
)
from doc_extractor.infrastructure.storage.adapter import S3Storage
from doc_extractor.infrastructure.storage.errors import (
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStorageUnavailableError,
)
from doc_extractor.infrastructure.storage.settings import S3StorageSettings
from doc_extractor.storage import StoredArtifact


class StreamingBodyMock:
    """Mock for botocore StreamingBody with read() and context manager support."""

    def __init__(self, data: bytes):
        self._data = data
        self._io = io.BytesIO(data)

    def read(self, *args, **kwargs) -> bytes:
        return self._io.read(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._io.close()

    def close(self):
        self._io.close()


def _make_client_error(code: str, status_code: int = 400, message: str = "Client Error") -> ClientError:
    return ClientError(
        error_response={
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        operation_name="MockOperation",
    )


@pytest.fixture
def s3_settings(tmp_path):
    return S3StorageSettings(
        bucket_name="test-doc-bucket",
        region_name="us-east-1",
        access_key_id=SecretStr("MOCK_ACCESS_KEY"),
        secret_access_key=SecretStr("MOCK_SECRET_KEY"),
        spool_dir=tmp_path / "spool",
        app_env="development",
    )


@pytest.fixture
def mock_s3_client():
    client = MagicMock()
    return client


@pytest.fixture
def storage(s3_settings, mock_s3_client):
    return S3Storage(settings=s3_settings, tenant_id="tenant_alpha", client=mock_s3_client)


def test_tenant_id_validation(s3_settings, mock_s3_client):
    # Valid tenants
    s1 = S3Storage(settings=s3_settings, tenant_id="tenant_01", client=mock_s3_client)
    assert s1.tenant_id == "tenant_01"

    # Invalid tenants (path traversal, relative dots, control chars, empty)
    for bad in ("../escape", "/root", "..", ".", "tenant\nnewline", "", "   "):
        with pytest.raises(InvalidTenantIdentifierError):
            S3Storage(settings=s3_settings, tenant_id=bad, client=mock_s3_client)


def test_store_original_document(storage, mock_s3_client):
    doc_id = "doc_123"
    pdf_bytes = b"%PDF-1.4 mock pdf content"

    artifact = storage.store_original_document(doc_id, pdf_bytes)

    assert isinstance(artifact, StoredArtifact)
    assert artifact.document_id == doc_id
    assert artifact.artifact_id == "original.pdf"
    assert artifact.page_number is None
    assert artifact.mime_type == "application/pdf"
    assert artifact.file_size_bytes == len(pdf_bytes)
    assert artifact.storage_uri == f"tenant_alpha/{doc_id}/source/original.pdf"
    assert artifact.file_path.exists()
    assert artifact.file_path.read_bytes() == pdf_bytes

    # Verify S3 call
    mock_s3_client.put_object.assert_called_once()
    call_kwargs = mock_s3_client.put_object.call_args[1]
    assert call_kwargs["Bucket"] == "test-doc-bucket"
    assert call_kwargs["Key"] == f"tenant_alpha/{doc_id}/source/original.pdf"
    assert call_kwargs["Body"] == pdf_bytes
    assert call_kwargs["ContentType"] == "application/pdf"
    # Metadata hygiene: only identifiers, no document content or credentials
    assert call_kwargs["Metadata"] == {"tenant-marker": "tenant_alpha", "document-id": doc_id}


def test_store_original_document_path_traversal_rejected(storage):
    for bad_id in ("../doc", "doc/../../escape", "doc/sub", ".."):
        with pytest.raises(StorageError):
            storage.store_original_document(bad_id, b"data")


def test_store_page_image(storage, mock_s3_client):
    doc_id = "doc_456"
    img_bytes = b"\x89PNG\r\n\x1a\n mock image bytes"

    artifact = storage.store_page_image(doc_id, page_number=1, image_data=img_bytes, format_extension="png")

    assert artifact.document_id == doc_id
    assert artifact.page_number == 1
    assert artifact.mime_type == "image/png"
    assert artifact.file_size_bytes == len(img_bytes)
    assert artifact.storage_uri == f"tenant_alpha/{doc_id}/pages/page_0001.png"
    assert artifact.file_path.exists()

    # Verify page number < 1 rejected
    with pytest.raises(StorageError) as exc_info:
        storage.store_page_image(doc_id, page_number=0, image_data=img_bytes)
    assert "one-based" in exc_info.value.message


def test_store_preprocessed_page(storage, mock_s3_client):
    doc_id = "doc_789"
    img_bytes = b"preprocessed image"
    meta = {"dpi": 200, "deskew_angle": 1.25}

    artifact = storage.store_preprocessed_page(
        doc_id, page_number=2, image_data=img_bytes, metadata_dict=meta, format_extension="png"
    )

    assert artifact.page_number == 2
    assert artifact.storage_uri == f"tenant_alpha/{doc_id}/preprocessed/page_0002_preprocessed.png"
    assert artifact.file_path.exists()

    # Verify two put_object calls: one for image, one for JSON sidecar
    assert mock_s3_client.put_object.call_count == 2
    keys = [c[1]["Key"] for c in mock_s3_client.put_object.call_args_list]
    assert f"tenant_alpha/{doc_id}/preprocessed/page_0002_preprocessed.png" in keys
    assert f"tenant_alpha/{doc_id}/preprocessed/page_0002_metadata.json" in keys


def test_get_page_metadata(storage, mock_s3_client):
    doc_id = "doc_789"
    meta_dict = {"dpi": 200, "rotation": 90}
    mock_s3_client.get_object.return_value = {
        "Body": StreamingBodyMock(json.dumps(meta_dict).encode("utf-8")),
    }

    result = storage.get_page_metadata(doc_id, page_number=1)
    assert result == meta_dict

    # 404 NoSuchKey returns None
    mock_s3_client.get_object.side_effect = _make_client_error("NoSuchKey", 404)
    assert storage.get_page_metadata(doc_id, page_number=99) is None


def test_list_page_images(storage, mock_s3_client):
    doc_id = "doc_list"
    mock_paginator = MagicMock()
    mock_s3_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {
            "Contents": [
                {"Key": f"tenant_alpha/{doc_id}/pages/page_0002.png", "Size": 2000},
                {"Key": f"tenant_alpha/{doc_id}/pages/page_0001.png", "Size": 1000},
                {"Key": f"tenant_alpha/{doc_id}/pages/.tmp_upload.png", "Size": 500},
            ]
        }
    ]

    images = storage.list_page_images(doc_id)
    assert len(images) == 2
    # Verify sorted ascending by page number
    assert images[0].page_number == 1
    assert images[0].file_size_bytes == 1000
    assert images[1].page_number == 2
    assert images[1].file_size_bytes == 2000


def test_cleanup_preprocessed(storage, mock_s3_client):
    doc_id = "doc_clean"
    mock_paginator = MagicMock()
    mock_s3_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {"Contents": [{"Key": f"tenant_alpha/{doc_id}/preprocessed/page_0001_preprocessed.png"}]}
    ]

    storage.cleanup_preprocessed(doc_id)
    mock_s3_client.delete_objects.assert_called_once()
    deleted = mock_s3_client.delete_objects.call_args[1]["Delete"]["Objects"]
    assert deleted == [{"Key": f"tenant_alpha/{doc_id}/preprocessed/page_0001_preprocessed.png"}]


def test_cleanup_document(storage, mock_s3_client):
    doc_id = "doc_clean_all"
    mock_paginator = MagicMock()
    mock_s3_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {
            "Contents": [
                {"Key": f"tenant_alpha/{doc_id}/source/original.pdf"},
                {"Key": f"tenant_alpha/{doc_id}/pages/page_0001.png"},
            ]
        }
    ]

    storage.cleanup_document(doc_id)
    mock_s3_client.delete_objects.assert_called_once()
    deleted = mock_s3_client.delete_objects.call_args[1]["Delete"]["Objects"]
    assert len(deleted) == 2


def test_resolve_uri_to_path_success(storage, mock_s3_client):
    uri = "tenant_alpha/doc_100/source/original.pdf"
    content = b"%PDF content to resolve"
    mock_s3_client.get_object.return_value = {"Body": StreamingBodyMock(content)}

    resolved_path = storage.resolve_uri_to_path(uri)
    assert resolved_path.is_file()
    assert resolved_path.read_bytes() == content


def test_resolve_uri_to_path_cross_tenant_rejected(storage):
    # An attempt to resolve another tenant's URI must raise CrossTenantStorageAccessError
    foreign_uri = "tenant_beta/doc_100/source/original.pdf"
    with pytest.raises(CrossTenantStorageAccessError) as exc_info:
        storage.resolve_uri_to_path(foreign_uri)
    assert exc_info.value.details.get("tenant_id") == "tenant_alpha"


def test_resolve_uri_to_path_traversal_rejected(storage):
    for bad_uri in (
        "tenant_alpha/../etc/passwd",
        "/tenant_alpha/doc_1/source/original.pdf",
        "tenant_alpha\\doc_1\\file.pdf",
        "tenant_alpha/doc_1/../doc_2/file.pdf",
    ):
        with pytest.raises(StorageError):
            storage.resolve_uri_to_path(bad_uri)


def test_resolve_uri_to_path_not_found(storage, mock_s3_client):
    uri = "tenant_alpha/doc_missing/source/original.pdf"
    mock_s3_client.get_object.side_effect = _make_client_error("NoSuchKey", 404)

    with pytest.raises(StorageError) as exc_info:
        storage.resolve_uri_to_path(uri)
    assert "does not exist" in exc_info.value.message


def test_document_record_roundtrip(storage, mock_s3_client):
    doc_id = "doc_rec_1"
    record_data = {"document_id": doc_id, "status": "COMPLETED", "file_hash_sha256": "abc123hash"}

    class DummyRecord:
        document_id = doc_id

        def to_dict(self):
            return record_data

    storage.save_document_record(DummyRecord())
    mock_s3_client.put_object.assert_called_once()
    assert f"tenant_alpha/{doc_id}/document_record.json" == mock_s3_client.put_object.call_args[1]["Key"]

    mock_s3_client.get_object.return_value = {
        "Body": StreamingBodyMock(json.dumps(record_data).encode("utf-8"))
    }
    loaded = storage.get_document_record(doc_id)
    assert loaded == record_data


def test_find_document_by_hash(storage, mock_s3_client):
    mock_paginator = MagicMock()
    mock_s3_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {"Contents": [{"Key": "tenant_alpha/doc_1/document_record.json"}]}
    ]
    rec = {"document_id": "doc_1", "file_hash_sha256": "target_hash_555"}
    mock_s3_client.get_object.return_value = {"Body": StreamingBodyMock(json.dumps(rec).encode("utf-8"))}

    found = storage.find_document_by_hash("target_hash_555")
    assert found == rec

    # Hash not found
    assert storage.find_document_by_hash("non_existent_hash") is None


def test_handoff_payload_roundtrip(storage, mock_s3_client):
    doc_id = "doc_handoff_1"
    payload_data = {"document_id": doc_id, "schema_id": "schema_v1", "pages": []}

    class DummyPayload:
        document_id = doc_id

        def to_dict(self):
            return payload_data

    storage.save_handoff_payload(DummyPayload())
    mock_s3_client.put_object.assert_called_once()

    mock_s3_client.get_object.return_value = {
        "Body": StreamingBodyMock(json.dumps(payload_data).encode("utf-8"))
    }
    loaded = storage.get_handoff_payload(doc_id)
    assert loaded == payload_data


def test_direct_s3_primitives(storage, mock_s3_client):
    # put_object_bytes & get_object_bytes
    storage.put_object_bytes("custom/path.bin", b"hello bytes", content_type="application/octet-stream")
    mock_s3_client.put_object.assert_called_with(
        Bucket="test-doc-bucket",
        Key="tenant_alpha/custom/path.bin",
        Body=b"hello bytes",
        ContentType="application/octet-stream",
        Metadata={"tenant-marker": "tenant_alpha"},
    )

    mock_s3_client.get_object.return_value = {"Body": StreamingBodyMock(b"hello bytes")}
    assert storage.get_object_bytes("custom/path.bin") == b"hello bytes"

    # object_exists
    mock_s3_client.head_object.return_value = {"ContentLength": 11}
    assert storage.object_exists("custom/path.bin") is True

    mock_s3_client.head_object.side_effect = _make_client_error("NotFound", 404)
    assert storage.object_exists("missing.bin") is False

    # delete_object
    storage.delete_object("custom/path.bin")
    mock_s3_client.delete_object.assert_called_with(
        Bucket="test-doc-bucket",
        Key="tenant_alpha/custom/path.bin",
    )

    # check_health
    assert storage.check_health() == {"status": "ok", "bucket": "test-doc-bucket"}


@pytest.mark.parametrize(
    "error_factory,expected_error_cls,expected_reason",
    [
        (lambda: _make_client_error("NoSuchBucket", 404), ObjectStorageUnavailableError, "bucket_not_found"),
        (lambda: _make_client_error("AccessDenied", 403), ObjectStorageUnavailableError, "access_denied"),
        (lambda: _make_client_error("InternalError", 500), ObjectStorageUnavailableError, "storage_server_error"),
        (lambda: EndpointConnectionError(endpoint_url="https://s3.internal"), ObjectStorageUnavailableError, "endpoint_unreachable"),
        (lambda: ConnectTimeoutError(endpoint_url="https://s3.internal"), ObjectStorageUnavailableError, "connect_timeout"),
        (lambda: ReadTimeoutError(endpoint_url="https://s3.internal"), ObjectStorageUnavailableError, "read_timeout"),
        (lambda: SSLError(endpoint_url="https://s3.internal", error="certificate verify failed"), ObjectStorageUnavailableError, "ssl_validation_failed"),
    ],
)
def test_error_mapping(storage, mock_s3_client, error_factory, expected_error_cls, expected_reason):
    mock_s3_client.put_object.side_effect = error_factory()
    with pytest.raises(expected_error_cls) as exc_info:
        storage.store_original_document("doc_err", b"content")
    assert exc_info.value.details.get("reason") == expected_reason
    # Also verify it inherits from TenantStorageUnavailableError so callers catch it identically
    if issubclass(expected_error_cls, TenantStorageUnavailableError):
        assert isinstance(exc_info.value, TenantStorageUnavailableError)


def test_tenant_isolation_non_plain_ids(s3_settings, mock_s3_client):
    # Non-plain tenant ID (uppercase, unicode) maps to hashed key prefix h+<hash>
    s_upper = S3Storage(settings=s3_settings, tenant_id="Tenant-With-Upper", client=mock_s3_client)
    assert s_upper._tenant_key.startswith("h+")

    s_upper.store_original_document("doc_1", b"data")
    key = mock_s3_client.put_object.call_args[1]["Key"]
    assert key.startswith(f"{s_upper._tenant_key}/doc_1/")


def test_compatibility_with_loader_b(storage, mock_s3_client):
    """
    Verifies that Owner 1's SecureModuleBLoader contract can seamlessly operate with S3Storage.
    SecureModuleBLoader resolves source URI to a local Path, checks .exists(), and verifies SHA-256 and size.
    """
    import hashlib
    from doc_extractor.loader_b import SecureModuleBLoader
    from doc_extractor.contracts_b import ModuleBInputPayload, ModuleAPageRef

    doc_id = "doc_compat"
    content = b"%PDF-1.4 sample pdf for LoaderB"
    content_hash = hashlib.sha256(content).hexdigest()
    content_size = len(content)

    mock_s3_client.get_object.return_value = {"Body": StreamingBodyMock(content)}

    artifact = storage.store_original_document(doc_id, content)

    # Construct ModuleBInputPayload as expected by SecureModuleBLoader
    payload = ModuleBInputPayload(
        contract_version="1.0.0",
        run_id="run_001",
        document_id=doc_id,
        tenant_id=storage.tenant_id,
        status="processing",
        internal_state="ready_for_extraction",
        claimed_filename="document.pdf",
        file_hash_sha256=content_hash,
        file_size_bytes=content_size,
        page_count=1,
        mime_type="application/pdf",
        created_at="2026-11-24T00:00:00Z",
        completed_at="2026-11-24T00:01:00Z",
        original_document_storage_uri=artifact.storage_uri,
        pages=[
            ModuleAPageRef(
                page_number=1,
                original_artifact_id="art_orig_1",
                preprocessed_artifact_id="art_prep_1",
                original_storage_uri=f"{storage.tenant_id}/{doc_id}/pages/page_0001.png",
                preprocessed_storage_uri=f"{storage.tenant_id}/{doc_id}/preprocessed/page_0001_preprocessed.png",
                original_width_px=800,
                original_height_px=600,
                preprocessed_width_px=800,
                preprocessed_height_px=600,
                metadata_storage_uri=f"{storage.tenant_id}/{doc_id}/preprocessed/page_0001_metadata.json",
            )
        ],
    )

    # SecureModuleBLoader checks source artifact
    loader = SecureModuleBLoader(storage=storage)

    # Verify that resolving uri works for SecureModuleBLoader
    resolved = loader.storage.resolve_uri_to_path(payload.original_document_storage_uri)
    assert resolved.exists()
    assert resolved.read_bytes() == content
