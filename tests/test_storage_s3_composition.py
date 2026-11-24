"""
Tests for S3 storage composition and no-fallback guarantees (Team Member 3, Phase 4A).

Verifies:
- ApplicationContainer wires S3Storage when storage_backend == "s3".
- ApplicationContainer wires LocalDiskStorage when storage_backend == "local".
- Fail-closed behavior: When S3 is configured, failures NEVER fall back to local disk or memory.
- DocumentReadService fails closed when S3 is configured (linking to Open Decision M5-D18).
"""

from unittest.mock import MagicMock, patch
import pytest

pytest.importorskip("multipart", reason="the 'api' extra provides python-multipart")
pytest.importorskip("redis", reason="the 'security' extra provides Redis for API composition")

from pydantic import SecretStr

from doc_extractor.api.composition import ApplicationContainer
from doc_extractor.api.config import ApiSettings
from doc_extractor.exceptions import TenantStorageUnavailableError
from doc_extractor.infrastructure.storage.adapter import S3Storage
from doc_extractor.infrastructure.storage.errors import ObjectStorageUnavailableError
from doc_extractor.storage import LocalDiskStorage


def test_default_storage_is_local(tmp_path):
    settings = ApiSettings(
        app_env="development",
        storage_base_dir=tmp_path / "storage",
    )
    container = ApplicationContainer(api_settings=settings)
    storage = container.get_storage("tenant_alpha")
    assert isinstance(storage, LocalDiskStorage)
    assert storage.tenant_id == "tenant_alpha"

    components = container.get_tenant_components("tenant_alpha")
    assert isinstance(components.storage, LocalDiskStorage)


def test_configured_s3_storage_wiring(tmp_path):
    settings = ApiSettings(
        app_env="development",
        storage_base_dir=tmp_path / "storage",
        storage_backend="s3",
        s3_bucket="my-app-bucket",
        s3_region="us-west-2",
        s3_access_key_id=SecretStr("AKIAIOSFODNN7EXAMPLE"),
        s3_secret_access_key=SecretStr("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    )
    container = ApplicationContainer(api_settings=settings)
    storage = container.get_storage("tenant_alpha")
    assert isinstance(storage, S3Storage)
    assert storage.tenant_id == "tenant_alpha"
    assert storage.bucket_name == "my-app-bucket"

    components = container.get_tenant_components("tenant_alpha")
    assert isinstance(components.storage, S3Storage)


def test_s3_configured_never_falls_back_to_local_disk_on_error(tmp_path):
    settings = ApiSettings(
        app_env="development",
        storage_base_dir=tmp_path / "storage",
        storage_backend="s3",
        s3_bucket="my-app-bucket",
        s3_access_key_id=SecretStr("AKIAIOSFODNN7EXAMPLE"),
        s3_secret_access_key=SecretStr("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    )
    container = ApplicationContainer(api_settings=settings)
    s3_storage = container.get_storage("tenant_alpha")
    assert isinstance(s3_storage, S3Storage)

    # Mock client failure
    mock_client = MagicMock()
    mock_client.put_object.side_effect = ObjectStorageUnavailableError("endpoint_unreachable")
    s3_storage._injected_client = mock_client

    # Calling storage operation must raise ObjectStorageUnavailableError / TenantStorageUnavailableError
    # It must NEVER silently fall back to writing to local disk
    with pytest.raises(TenantStorageUnavailableError) as exc_info:
        s3_storage.store_original_document("doc_err", b"%PDF data")
    assert exc_info.value.details.get("reason") == "endpoint_unreachable"

    # Verify nothing was written to local storage base dir
    assert not (tmp_path / "storage" / "tenant_alpha").exists()


def test_document_read_service_fails_closed_under_s3(tmp_path):
    settings = ApiSettings(
        app_env="development",
        storage_base_dir=tmp_path / "storage",
        storage_backend="s3",
        s3_bucket="my-app-bucket",
        s3_access_key_id=SecretStr("AKIAIOSFODNN7EXAMPLE"),
        s3_secret_access_key=SecretStr("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
    )
    container = ApplicationContainer(api_settings=settings)

    # Calling get_document_read_service under S3 backend fails closed
    with pytest.raises(TenantStorageUnavailableError) as exc_info:
        container.get_document_read_service("tenant_alpha")
    assert exc_info.value.details.get("reason") == "document_read_service_requires_local_disk_storage"
