"""
REAL S3 / MinIO integration tests (Team Member 3, Phase 4A). OPT-IN: they need a running S3-compatible service.

They run only when ``DOC_TEST_S3_BUCKET`` or ``DOC_TEST_S3_ENDPOINT_URL`` is configured:

    docker run -d --name minio -p 9000:9000 -p 9001:9001 -e "MINIO_ROOT_USER=minioadmin" -e "MINIO_ROOT_PASSWORD=minioadmin" minio/minio server /data
    DOC_TEST_S3_ENDPOINT_URL='http://127.0.0.1:9000' DOC_TEST_S3_BUCKET='doc-test-bucket' DOC_TEST_S3_ACCESS_KEY='minioadmin' DOC_TEST_S3_SECRET_KEY='minioadmin' python -m pytest tests/test_storage_s3_integration.py

Each test runs against the real S3-compatible service, creates objects under unique document IDs and cleans them up.
Without the environment variables, every test skips with its reason; a skipped test proves nothing.
"""

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest
from pydantic import SecretStr

ENDPOINT = os.environ.get("DOC_TEST_S3_ENDPOINT_URL", "").strip()
BUCKET = os.environ.get("DOC_TEST_S3_BUCKET", "").strip()
REGION = os.environ.get("DOC_TEST_S3_REGION", "us-east-1").strip()
ACCESS_KEY = os.environ.get("DOC_TEST_S3_ACCESS_KEY", "").strip()
SECRET_KEY = os.environ.get("DOC_TEST_S3_SECRET_KEY", "").strip()

requires_real_s3 = pytest.mark.skipif(
    not (BUCKET and (ENDPOINT or ACCESS_KEY)),
    reason="set DOC_TEST_S3_BUCKET and DOC_TEST_S3_ENDPOINT_URL (or credentials) to run against real S3/MinIO",
)


@pytest.fixture(scope="module")
def real_s3_settings(tmp_path_factory):
    if not (BUCKET and (ENDPOINT or ACCESS_KEY)):
        pytest.skip("S3 integration test environment variables not configured")

    from doc_extractor.infrastructure.storage.settings import S3StorageSettings

    spool = tmp_path_factory.mktemp("s3_integration_spool")
    return S3StorageSettings(
        bucket_name=BUCKET,
        endpoint_url=ENDPOINT or None,
        region_name=REGION,
        access_key_id=SecretStr(ACCESS_KEY) if ACCESS_KEY else None,
        secret_access_key=SecretStr(SECRET_KEY) if SECRET_KEY else None,
        force_path_style=True if ENDPOINT else None,
        spool_dir=spool,
        app_env="development",
    )


@pytest.fixture(scope="module")
def ensure_bucket_exists(real_s3_settings):
    import boto3
    from botocore.config import Config

    cfg = Config(connect_timeout=5, read_timeout=10, s3={"addressing_style": "path"})
    client = boto3.client(
        "s3",
        endpoint_url=real_s3_settings.endpoint_url,
        region_name=real_s3_settings.region_name,
        aws_access_key_id=real_s3_settings.get_effective_access_key_id(),
        aws_secret_access_key=real_s3_settings.get_effective_secret_access_key(),
        config=cfg,
    )
    try:
        client.head_bucket(Bucket=real_s3_settings.bucket_name)
    except Exception:
        try:
            client.create_bucket(Bucket=real_s3_settings.bucket_name)
        except Exception as exc:
            pytest.skip(f"Could not connect to S3/MinIO or create bucket {real_s3_settings.bucket_name}: {exc}")
    return client


@requires_real_s3
def test_real_s3_health_check(real_s3_settings, ensure_bucket_exists):
    from doc_extractor.infrastructure.storage.adapter import S3Storage

    storage = S3Storage(settings=real_s3_settings, tenant_id="tenant_it")
    health = storage.check_health()
    assert health["status"] == "ok"
    assert health["bucket"] == real_s3_settings.bucket_name


@requires_real_s3
def test_real_s3_original_document_lifecycle(real_s3_settings, ensure_bucket_exists):
    from doc_extractor.infrastructure.storage.adapter import S3Storage

    doc_id = f"doc_it_{uuid.uuid4().hex[:8]}"
    storage = S3Storage(settings=real_s3_settings, tenant_id="tenant_it")
    pdf_content = b"%PDF-1.4 Real S3 integration test PDF document"

    # Store
    artifact = storage.store_original_document(doc_id, pdf_content)
    assert artifact.document_id == doc_id
    assert artifact.file_size_bytes == len(pdf_content)
    assert artifact.storage_uri == f"tenant_it/{doc_id}/source/original.pdf"

    # Resolve URI to local path
    resolved_path = storage.resolve_uri_to_path(artifact.storage_uri)
    assert resolved_path.is_file()
    assert resolved_path.read_bytes() == pdf_content

    # Direct S3 primitives
    rel_path = f"{doc_id}/source/original.pdf"
    assert storage.object_exists(rel_path) is True
    bytes_back = storage.get_object_bytes(rel_path)
    assert bytes_back == pdf_content

    # Cleanup
    storage.cleanup_document(doc_id)
    assert storage.object_exists(rel_path) is False


@requires_real_s3
def test_real_s3_pages_and_metadata_lifecycle(real_s3_settings, ensure_bucket_exists):
    from doc_extractor.infrastructure.storage.adapter import S3Storage

    doc_id = f"doc_pages_{uuid.uuid4().hex[:8]}"
    storage = S3Storage(settings=real_s3_settings, tenant_id="tenant_it")

    p1_bytes = b"\x89PNG real page 1 image"
    p2_bytes = b"\x89PNG real page 2 image"
    storage.store_page_image(doc_id, 1, p1_bytes, "png")
    storage.store_page_image(doc_id, 2, p2_bytes, "png")

    # List page images
    pages = storage.list_page_images(doc_id)
    assert len(pages) == 2
    assert pages[0].page_number == 1
    assert pages[1].page_number == 2

    # Preprocessed page and metadata
    prep_bytes = b"\x89PNG preprocessed page 1"
    meta = {"dpi": 200, "deskew": -0.5}
    storage.store_preprocessed_page(doc_id, 1, prep_bytes, meta, "png")

    meta_back = storage.get_page_metadata(doc_id, 1)
    assert meta_back == meta

    # Cleanup preprocessed only
    storage.cleanup_preprocessed(doc_id)
    assert storage.get_page_metadata(doc_id, 1) is None
    # Original pages should still exist
    assert len(storage.list_page_images(doc_id)) == 2

    # Cleanup entire document
    storage.cleanup_document(doc_id)
    assert len(storage.list_page_images(doc_id)) == 0


@requires_real_s3
def test_real_s3_tenant_isolation(real_s3_settings, ensure_bucket_exists):
    from doc_extractor.exceptions import CrossTenantStorageAccessError
    from doc_extractor.infrastructure.storage.adapter import S3Storage

    doc_id = f"doc_iso_{uuid.uuid4().hex[:8]}"
    storage_alpha = S3Storage(settings=real_s3_settings, tenant_id="tenant_alpha")
    storage_beta = S3Storage(settings=real_s3_settings, tenant_id="tenant_beta")

    storage_alpha.store_original_document(doc_id, b"alpha content")

    # Tenant beta attempting to resolve tenant alpha's URI is refused
    with pytest.raises(CrossTenantStorageAccessError):
        storage_beta.resolve_uri_to_path(f"tenant_alpha/{doc_id}/source/original.pdf")

    # Tenant beta object_exists for alpha's document key in beta's namespace returns False
    assert storage_beta.object_exists(f"{doc_id}/source/original.pdf") is False

    storage_alpha.cleanup_document(doc_id)
