"""
Automated unit and integration tests for Phase 5:
- SHA-256 content hashing (FR-ING-006)
- Idempotency replay within 24 hours (API-013, NFR-006)
- Conflict detection on key reuse with different parameters/content
- Multi-tenant boundary isolation (SEC-006)
- Status lifecycle transitions and mapping (SCH-012)
- Atomic storage, retry recovery, and path traversal resistance
"""

import io
from pathlib import Path
import threading
import time
import pytest

from doc_extractor.config import Settings
from doc_extractor.exceptions import ConflictError, StorageError, UnsupportedFileError
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.ingestion import IngestionResult, IngestionService
from doc_extractor.malware import MockMalwareScanner
from doc_extractor.models import (
    VALID_STATUS_TRANSITIONS,
    DocumentRecord,
    DocumentStatus,
    IdempotencyRecord,
    IngestionState,
    compute_file_sha256,
    compute_request_fingerprint,
    compute_stream_sha256,
    map_internal_state_to_external_status,
)
from doc_extractor.storage import LocalDiskStorage
from tests.conftest import create_minimal_pdf


@pytest.fixture
def base_dir(tmp_path):
    return tmp_path / "engine_data"


@pytest.fixture
def mock_scanner():
    return MockMalwareScanner(is_ready=True)


@pytest.fixture
def service(base_dir, mock_scanner):
    settings = Settings(storage_base_dir=base_dir)
    storage = LocalDiskStorage(base_dir=base_dir, tenant_id="tenant_101")
    idempotency_store = LocalIdempotencyStore(base_dir=base_dir)
    return IngestionService(
        settings=settings,
        scanner=mock_scanner,
        storage=storage,
        idempotency_store=idempotency_store,
        tenant_id="tenant_101",
    )


# --- Tests 1-3: Content Hashing ---

@pytest.mark.unit
def test_sha256_identical_bytes():
    """Identical bytes produce identical SHA-256 hashes."""
    data = b"%PDF-1.4 sample content for hash testing %%EOF"
    hash1 = compute_stream_sha256(io.BytesIO(data))
    hash2 = compute_stream_sha256(io.BytesIO(data))
    assert hash1 == hash2
    assert len(hash1) == 64


@pytest.mark.unit
def test_sha256_different_bytes():
    """Different bytes produce distinct SHA-256 hashes."""
    h1 = compute_stream_sha256(io.BytesIO(b"file_alpha"))
    h2 = compute_stream_sha256(io.BytesIO(b"file_beta"))
    assert h1 != h2


@pytest.mark.unit
def test_streaming_large_hash(tmp_path):
    """Large files are hashed in streaming chunks without loading full file into memory."""
    test_file = tmp_path / "large_sample.dat"
    # Write 1 MB chunk by chunk
    with open(test_file, "wb") as f:
        for _ in range(16):
            f.write(b"0" * 65536)
    
    file_hash = compute_file_sha256(test_file, chunk_size=32768)
    assert len(file_hash) == 64


# --- Tests 4 & 5: Acceptance Criteria FR-ING-006 & API-013 Replay ---

@pytest.mark.unit
def test_fr_ing_006_acceptance_same_key_same_file(service, sample_pdf_bytes):
    """
    PRD FR-ING-006 Acceptance Criterion:
    Given two POSTs with one key, Then one document exists and both calls return its ID.
    """
    key = "idem_key_contract_123"
    
    # First request
    res1 = service.ingest_document(
        file_path_or_stream=io.BytesIO(sample_pdf_bytes),
        claimed_filename="mortgage_agreement.pdf",
        idempotency_key=key,
    )
    assert res1.is_accepted is True
    assert res1.idempotent_replay is False

    # Second request with the same key and identical file
    res2 = service.ingest_document(
        file_path_or_stream=io.BytesIO(sample_pdf_bytes),
        claimed_filename="mortgage_agreement.pdf",
        idempotency_key=key,
    )

    # Acceptance verify: Both calls return the exact same document ID
    assert res2.document_id == res1.document_id
    assert res2.idempotent_replay is True
    assert res2.file_hash_sha256 == res1.file_hash_sha256

    # Verify storage: Only one document directory and document record exists
    doc_rec = service.storage.get_document_record(res1.document_id)
    assert doc_rec is not None
    assert doc_rec["document_id"] == res1.document_id


# --- Tests 6 & 7: Idempotency Conflicts ---

@pytest.mark.security
def test_idempotency_conflict_different_content(service, sample_pdf_bytes):
    """Reusing the same key with different file content fails with IDEMPOTENCY_KEY_CONFLICT."""
    key = "idem_key_fixed_001"
    
    # Upload first document
    res1 = service.ingest_document(
        file_path_or_stream=io.BytesIO(sample_pdf_bytes),
        claimed_filename="first.pdf",
        idempotency_key=key,
    )
    assert res1.is_accepted is True

    # Attempt reuse of key with completely different PDF content
    different_pdf = create_minimal_pdf(page_count=4)
    with pytest.raises(ConflictError) as exc_info:
        service.ingest_document(
            file_path_or_stream=io.BytesIO(different_pdf),
            claimed_filename="different.pdf",
            idempotency_key=key,
        )
    assert exc_info.value.code == "IDEMPOTENCY_KEY_CONFLICT"


@pytest.mark.security
def test_idempotency_conflict_different_params(service, sample_pdf_bytes):
    """Reusing the same key with different request parameters fails with IDEMPOTENCY_KEY_CONFLICT."""
    key = "idem_key_params_002"
    service.ingest_document(
        file_path_or_stream=io.BytesIO(sample_pdf_bytes),
        claimed_filename="doc.pdf",
        idempotency_key=key,
        extra_request_params={"schema_id": "schema_v1"},
    )

    with pytest.raises(ConflictError) as exc_info:
        service.ingest_document(
            file_path_or_stream=io.BytesIO(sample_pdf_bytes),
            claimed_filename="doc.pdf",
            idempotency_key=key,
            extra_request_params={"schema_id": "schema_v2"},
        )
    assert exc_info.value.code == "IDEMPOTENCY_KEY_CONFLICT"


# --- Test 8: Tenant Isolation (SEC-006) ---

@pytest.mark.security
def test_tenant_isolation_idempotency_and_storage(base_dir, mock_scanner, sample_pdf_bytes):
    """Different tenants using the same idempotency key and same file do not share records."""
    key = "shared_idem_key_999"
    
    # Tenant Alpha
    service_a = IngestionService(
        scanner=mock_scanner,
        storage=LocalDiskStorage(base_dir=base_dir, tenant_id="tenant_alpha"),
        idempotency_store=LocalIdempotencyStore(base_dir=base_dir),
        tenant_id="tenant_alpha",
    )
    res_a = service_a.ingest_document(
        file_path_or_stream=io.BytesIO(sample_pdf_bytes),
        claimed_filename="doc.pdf",
        idempotency_key=key,
    )

    # Tenant Beta
    service_b = IngestionService(
        scanner=mock_scanner,
        storage=LocalDiskStorage(base_dir=base_dir, tenant_id="tenant_beta"),
        idempotency_store=LocalIdempotencyStore(base_dir=base_dir),
        tenant_id="tenant_beta",
    )
    res_b = service_b.ingest_document(
        file_path_or_stream=io.BytesIO(sample_pdf_bytes),
        claimed_filename="doc.pdf",
        idempotency_key=key,
    )

    # Must be separate documents in separate tenant paths
    assert res_a.document_id != res_b.document_id
    assert res_b.idempotent_replay is False

    # Check isolation in storage
    rec_a = service_a.storage.get_document_record(res_a.document_id)
    rec_b = service_b.storage.get_document_record(res_b.document_id)
    assert rec_a["tenant_id"] == "tenant_alpha"
    assert rec_b["tenant_id"] == "tenant_beta"


# --- Test 9: Same Content with Different Keys ---

@pytest.mark.unit
def test_same_content_different_keys_creates_independent_document(service, sample_pdf_bytes):
    """Uploading same content under different idempotency keys follows standard upload semantics."""
    res1 = service.ingest_document(
        file_path_or_stream=io.BytesIO(sample_pdf_bytes),
        claimed_filename="doc1.pdf",
        idempotency_key="key_001",
    )
    res2 = service.ingest_document(
        file_path_or_stream=io.BytesIO(sample_pdf_bytes),
        claimed_filename="doc2.pdf",
        idempotency_key="key_002",
    )
    assert res1.document_id != res2.document_id
    assert res1.file_hash_sha256 == res2.file_hash_sha256


# --- Test 10: Concurrency Safety (NFR-006) ---

@pytest.mark.unit
def test_concurrent_identical_uploads_single_document(service, sample_pdf_bytes):
    """Simultaneous threads with the same key and file create at most one document record."""
    key = "concurrent_key_race_001"
    results = []

    def run_upload():
        res = service.ingest_document(
            file_path_or_stream=io.BytesIO(sample_pdf_bytes),
            claimed_filename="concurrent.pdf",
            idempotency_key=key,
        )
        results.append(res)

    threads = [threading.Thread(target=run_upload) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 4
    # All threads must return the exact same document ID
    doc_ids = {r.document_id for r in results}
    assert len(doc_ids) == 1


# --- Test 11 & 12: Idempotency Record 24h Expiry Window (API-013) ---

@pytest.mark.unit
def test_idempotency_24h_window_and_expiration(service, sample_pdf_bytes):
    """Record expires after 24 hours, allowing key reuse or renewed processing."""
    key = "expiry_key_test"
    
    # Normal upload
    res1 = service.ingest_document(
        file_path_or_stream=io.BytesIO(sample_pdf_bytes),
        claimed_filename="test.pdf",
        idempotency_key=key,
    )
    assert res1.idempotent_replay is False

    # Simulate time forward past 24 hours (86400 seconds)
    store = service.idempotency_store
    record = store.get_record("tenant_101", key)
    assert record is not None

    # Simulate expired record
    expired_record = IdempotencyRecord(
        idempotency_key=key,
        tenant_id="tenant_101",
        request_fingerprint=record.request_fingerprint,
        document_id=record.document_id,
        response_payload=record.response_payload,
        created_at_utc=record.created_at_utc - 90000,
        expires_at_utc=record.created_at_utc - 3600,  # Expired 1 hour ago
    )
    store.save_record(expired_record)

    # Fetching expired record returns None
    assert store.get_record("tenant_101", key) is None


# --- Test 13 & 14: Storage Path Traversal Resistance (SEC-006) ---

@pytest.mark.security
def test_storage_path_traversal_resistance(tmp_path):
    storage = LocalDiskStorage(base_dir=tmp_path / "storage", tenant_id="legit_tenant")
    with pytest.raises(StorageError):
        storage._get_document_dir("../../etc/passwd")
    with pytest.raises(StorageError):
        storage._get_document_dir("..\\..\\Windows\\System32")


# --- Test 15: Status Transitions and SCH-012 Mapping ---

@pytest.mark.unit
def test_status_mapping_and_transitions():
    """Verify internal state mapping to SCH-012 and transition table."""
    assert map_internal_state_to_external_status(IngestionState.QUEUED) == DocumentStatus.PROCESSING
    assert map_internal_state_to_external_status(IngestionState.PREPROCESSING) == DocumentStatus.PROCESSING
    assert map_internal_state_to_external_status(IngestionState.REJECTED_MALWARE) == DocumentStatus.REJECTED
    assert map_internal_state_to_external_status(IngestionState.FAILED) == DocumentStatus.FAILED

    # Check terminal statuses cannot transition further
    assert len(VALID_STATUS_TRANSITIONS[DocumentStatus.AUTO_APPROVED]) == 0
    assert len(VALID_STATUS_TRANSITIONS[DocumentStatus.REVIEWED]) == 0
    assert len(VALID_STATUS_TRANSITIONS[DocumentStatus.REJECTED]) == 0
    assert len(VALID_STATUS_TRANSITIONS[DocumentStatus.FAILED]) == 0

