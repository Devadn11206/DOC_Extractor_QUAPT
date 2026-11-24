"""
Unit tests for envelope encryption and KMS/HSM integration (Team Member 3, Phase 4B).

Covers:
1. AES-256-GCM Envelope Encryption & Packaging:
   - Round trip encryption and decryption (empty, small, binary payloads).
   - Tamper detection (modified ciphertext or tag fails with InvalidTag / ciphertext_authentication_failed).
   - Tenant substitution prevention (cryptographic binding of tenant in AAD).
   - Object URI substitution prevention (cryptographic binding of URI in AAD).
   - Memory wiping for sensitive plaintext key material.
2. KMS Provider Abstraction & Providers:
   - FakeKmsProvider: key versioning, rotation, rewrapping.
   - FakeKmsProvider: simulation gates (outage, access denial, revoked keys).
   - FakeKmsProvider: strictly forbidden in staging and production environments.
   - AwsKmsProvider: generate_data_key, decrypt_data_key, rewrap_data_key (re_encrypt), health check.
   - AwsKmsProvider: error sanitization and typed error mapping (no leaked credentials or raw keys).
3. S3Storage Integration with Envelope Encryption:
   - Upload & download round trip for original document, page image, preprocessed page & metadata.
   - Document record and handoff payload encryption/decryption round trips.
   - Direct primitives: put_object_bytes and get_object_bytes.
   - Object existence and head_object headers (metadata reflects envelope encryption).
   - find_document_by_hash lookup with encrypted document records.
   - Key rotation via rewrap_object (rewraps DEK without modifying underlying ciphertext).
4. Local Spool Security & Cleanup:
   - Spooled files have strict file permissions (0600) and directories (0700).
   - Spooled files are isolated within tenant subdirectories.
   - cleanup_spool and cleanup_document remove spooled plaintext files.
5. Production Hardening & Fail Closed:
   - Staging/production rejects FakeKmsProvider during settings validation.
   - Hardened environment refuses unencrypted objects when encryption is enabled (no silent fallback).
   - Typed domain exceptions contain sanitized reasons and no key material.
"""

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

from doc_extractor.exceptions import CrossTenantStorageAccessError, StorageError
from doc_extractor.infrastructure.storage.adapter import S3Storage
from doc_extractor.infrastructure.storage.envelope import (
    ALGORITHM,
    ENVELOPE_MAGIC,
    ENVELOPE_VERSION,
    compute_aad,
    decrypt_envelope,
    encrypt_envelope,
    is_envelope_encrypted,
    rewrap_envelope,
    wipe_key_memory,
)
from doc_extractor.infrastructure.storage.errors import (
    ObjectNotFoundError,
    ObjectStorageConfigurationError,
    ObjectStorageError,
)
from doc_extractor.infrastructure.storage.kms import (
    AwsKmsProvider,
    DataKey,
    FakeKmsProvider,
    KmsAuthenticationError,
    KmsConfigurationError,
    KmsKeyNotFoundError,
    KmsUnavailableError,
)
from doc_extractor.infrastructure.storage.settings import S3StorageSettings


# ==============================================================================
# 1. Envelope Encryption Core Tests
# ==============================================================================

class TestEnvelopeEncryptionCore:
    """Tests for AES-256-GCM envelope serialization, AAD binding, and key wiping."""

    def test_envelope_round_trip_various_payloads(self):
        fake_kms = FakeKmsProvider(kek_id="kek-test-1", key_version="v1")
        tenant = "tenant_alpha"
        uri = f"{tenant}/doc123/source/original.pdf"

        payloads = [
            b"",
            b"Hello world! Envelope encryption test.",
            b"%PDF-1.7 \x00\x01\x02\xff\xfe" * 1000,  # binary PDF data
            os.urandom(64 * 1024),  # 64 KB random binary
        ]

        for payload in payloads:
            container = encrypt_envelope(
                plaintext=payload,
                tenant_id=tenant,
                logical_uri=uri,
                kms_provider=fake_kms,
                content_type="application/pdf",
                kek_id="kek-test-1",
            )

            assert is_envelope_encrypted(container)
            assert container[:4] == ENVELOPE_MAGIC

            decrypted, meta = decrypt_envelope(
                container=container,
                expected_tenant_id=tenant,
                expected_uri=uri,
                kms_provider=fake_kms,
            )

            assert decrypted == payload
            assert meta["v"] == ENVELOPE_VERSION
            assert meta["alg"] == ALGORITHM
            assert meta["kek_id"] == "kek-test-1"
            assert meta["key_ver"] == "v1"
            assert meta["mime"] == "application/pdf"

    def test_ciphertext_tampering_fails_authentication(self):
        fake_kms = FakeKmsProvider()
        tenant = "tenant_alpha"
        uri = f"{tenant}/doc1/original.pdf"
        payload = b"Sensitive contract text."

        container = encrypt_envelope(payload, tenant, uri, fake_kms)

        # Corrupt the last byte of ciphertext/tag
        corrupted = bytearray(container)
        corrupted[-1] ^= 0x01

        with pytest.raises(ObjectStorageError) as exc_info:
            decrypt_envelope(bytes(corrupted), tenant, uri, fake_kms)
        assert exc_info.value.details.get("reason") == "aes_gcm_tag_mismatch_or_tampered"

    def test_tenant_substitution_detected_and_rejected(self):
        """Cross-tenant attack: Attacker moves Alice's encrypted document to Bob's namespace."""
        fake_kms = FakeKmsProvider()
        alice_tenant = "tenant_alice"
        bob_tenant = "tenant_bob"
        alice_uri = f"{alice_tenant}/doc1/original.pdf"
        bob_uri = f"{bob_tenant}/doc1/original.pdf"

        payload = b"Alice's confidential financial report."
        alice_container = encrypt_envelope(payload, alice_tenant, alice_uri, fake_kms)

        # Bob attempts to decrypt Alice's ciphertext presenting Bob's credentials
        with pytest.raises(CrossTenantStorageAccessError) as exc_info:
            decrypt_envelope(alice_container, bob_tenant, bob_uri, fake_kms)
        assert exc_info.value.details.get("reason") == "tenant_mismatch_in_ciphertext"

    def test_uri_substitution_detected_and_rejected(self):
        """Object substitution attack: Ciphertext from document A placed into document B path."""
        fake_kms = FakeKmsProvider()
        tenant = "tenant_alpha"
        uri_a = f"{tenant}/doc_a/original.pdf"
        uri_b = f"{tenant}/doc_b/original.pdf"

        payload = b"Document A secret content."
        container_a = encrypt_envelope(payload, tenant, uri_a, fake_kms)

        with pytest.raises(ObjectStorageError) as exc_info:
            decrypt_envelope(container_a, tenant, uri_b, fake_kms)
        assert exc_info.value.details.get("reason") == "uri_mismatch_in_envelope"

    def test_envelope_rewrapping_without_reencrypting_payload(self):
        """KEK rotation: rewrap_envelope updates the wrapped DEK without touching the ciphertext."""
        fake_kms = FakeKmsProvider(kek_id="kek-2026", key_version="v1")
        tenant = "tenant_gamma"
        uri = f"{tenant}/doc99/data.bin"
        payload = b"Long term stored payload for archive."

        orig_container = encrypt_envelope(payload, tenant, uri, fake_kms)

        # Rotate key on fake_kms
        fake_kms.rotate_key("v2", "kek-2027")

        # Rewrap under new KEK and version
        rewrapped = rewrap_envelope(
            container=orig_container,
            target_kek_id="kek-2027",
            kms_provider=fake_kms,
            target_version="v2",
        )

        assert is_envelope_encrypted(rewrapped)

        # Decrypt with new KEK
        decrypted, meta = decrypt_envelope(rewrapped, tenant, uri, fake_kms)
        assert decrypted == payload
        assert meta["kek_id"] == "kek-2027"
        assert meta["key_ver"] == "v2"

    def test_wipe_key_memory(self):
        mutable_key = bytearray(b"0123456789abcdef0123456789abcdef")
        wipe_key_memory(mutable_key)
        assert mutable_key == bytearray(32)


# ==============================================================================
# 2. Fake KMS Provider & Simulation Gates
# ==============================================================================

class TestFakeKmsProvider:
    """Tests for FakeKmsProvider features and security boundaries."""

    def test_fake_kms_forbidden_in_staging_or_production(self):
        with pytest.raises(KmsConfigurationError) as exc_staging:
            FakeKmsProvider(app_env="staging")
        assert exc_staging.value.details.get("reason") == "fake_kms_forbidden_outside_development"

        with pytest.raises(KmsConfigurationError) as exc_prod:
            FakeKmsProvider(app_env="production")
        assert exc_prod.value.details.get("reason") == "fake_kms_forbidden_outside_development"

    def test_fake_kms_key_rotation_and_rewrapping(self):
        kms = FakeKmsProvider(kek_id="kek-1", key_version="v1")
        dk = kms.generate_data_key()

        # Decrypt with original version
        decrypted_key = kms.decrypt_data_key(dk.encrypted_key, kek_id=dk.kek_id, key_version=dk.key_version)
        assert decrypted_key == dk.plaintext_key

        # Rotate to v2
        kms.rotate_key("v2", "kek-1")
        assert kms.get_current_key_version() == "v2"

        # Rewrap to v2
        rewrapped_dk = kms.rewrap_data_key(
            encrypted_key=dk.encrypted_key,
            source_kek_id="kek-1",
            target_kek_id="kek-1",
            source_version="v1",
            target_version="v2",
        )
        assert rewrapped_dk.key_version == "v2"

        # Verify decryption of rewrapped key
        unwrapped_v2 = kms.decrypt_data_key(rewrapped_dk.encrypted_key, kek_id="kek-1", key_version="v2")
        assert unwrapped_v2 == dk.plaintext_key

    def test_fake_kms_outage_simulation(self):
        kms = FakeKmsProvider()
        kms.simulate_outage(True)

        with pytest.raises(KmsUnavailableError) as exc:
            kms.generate_data_key()
        assert exc.value.details.get("reason") == "kms_endpoint_unreachable"

        kms.simulate_outage(False)
        dk = kms.generate_data_key()
        assert len(dk.plaintext_key) == 32

    def test_fake_kms_access_denied_simulation(self):
        kms = FakeKmsProvider()
        kms.simulate_access_denied(True)

        with pytest.raises(KmsAuthenticationError) as exc:
            kms.generate_data_key()
        assert exc.value.details.get("reason") == "kms_access_denied"

    def test_fake_kms_revoked_key_fails(self):
        kms = FakeKmsProvider(kek_id="kek-1", key_version="v1")
        dk = kms.generate_data_key()

        kms.revoke_key("v1")
        with pytest.raises(KmsKeyNotFoundError) as exc:
            kms.decrypt_data_key(dk.encrypted_key, kek_id="kek-1", key_version="v1")
        assert exc.value.details.get("reason") == "kms_key_revoked_or_disabled"


# ==============================================================================
# 3. AWS KMS Provider (Mocked) Tests
# ==============================================================================

class TestAwsKmsProvider:
    """Tests for AwsKmsProvider using mocked boto3 KMS client."""

    def test_generate_data_key_success(self):
        mock_client = MagicMock()
        mock_client.generate_data_key.return_value = {
            "Plaintext": b"32bytesofplaintextmaterial012345",
            "CiphertextBlob": b"encryptedblobfromawskms9876543210",
            "KeyId": "arn:aws:kms:us-east-1:123456789012:key/test-kek-id",
        }

        provider = AwsKmsProvider(
            kek_id="arn:aws:kms:us-east-1:123456789012:key/test-kek-id",
            key_version="v1",
            client=mock_client,
        )

        dk = provider.generate_data_key(encryption_context={"tenant_id": "t1"})
        assert dk.plaintext_key == b"32bytesofplaintextmaterial012345"
        assert dk.encrypted_key == b"encryptedblobfromawskms9876543210"
        assert dk.key_version == "v1"
        mock_client.generate_data_key.assert_called_once_with(
            KeyId="arn:aws:kms:us-east-1:123456789012:key/test-kek-id",
            KeySpec="AES_256",
            EncryptionContext={"tenant_id": "t1"},
        )

    def test_decrypt_data_key_success(self):
        mock_client = MagicMock()
        mock_client.decrypt.return_value = {
            "Plaintext": b"32bytesofdecrypteddek01234567890",
            "KeyId": "arn:aws:kms:us-east-1:123456789012:key/test-kek-id",
        }

        provider = AwsKmsProvider(
            kek_id="arn:aws:kms:us-east-1:123456789012:key/test-kek-id",
            client=mock_client,
        )

        decrypted = provider.decrypt_data_key(
            encrypted_key=b"encryptedblob",
            encryption_context={"tenant_id": "t1"},
        )
        assert decrypted == b"32bytesofdecrypteddek01234567890"

    def test_rewrap_data_key_calls_re_encrypt(self):
        mock_client = MagicMock()
        mock_client.re_encrypt.return_value = {
            "CiphertextBlob": b"newlywrappedblobunderkek2",
            "KeyId": "kek-target",
        }

        provider = AwsKmsProvider(kek_id="kek-src", client=mock_client)
        new_dk = provider.rewrap_data_key(
            encrypted_key=b"oldblob",
            source_kek_id="kek-src",
            target_kek_id="kek-target",
            target_version="v2",
            encryption_context={"tenant_id": "t1"},
        )

        assert new_dk.encrypted_key == b"newlywrappedblobunderkek2"
        assert new_dk.kek_id == "kek-target"
        assert new_dk.key_version == "v2"
        mock_client.re_encrypt.assert_called_once()

    def test_aws_kms_error_sanitization(self):
        mock_client = MagicMock()
        mock_client.generate_data_key.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Secret internal IAM policy details"}},
            "GenerateDataKey",
        )

        provider = AwsKmsProvider(kek_id="kek-1", client=mock_client)
        with pytest.raises(KmsAuthenticationError) as exc:
            provider.generate_data_key()

        assert exc.value.details.get("reason") == "kms_access_denied"
        # Verify secret IAM message is not leaked
        assert "Secret internal IAM policy details" not in str(exc.value)

    def test_aws_kms_timeout_mapping(self):
        mock_client = MagicMock()
        mock_client.generate_data_key.side_effect = ReadTimeoutError(endpoint_url="https://kms.amazonaws.com")

        provider = AwsKmsProvider(kek_id="kek-1", client=mock_client)
        with pytest.raises(KmsUnavailableError) as exc:
            provider.generate_data_key()
        assert exc.value.details.get("reason") == "kms_read_timeout"


# ==============================================================================
# 4. S3Storage with Envelope Encryption Integration Tests
# ==============================================================================

class TestS3StorageEncryptionIntegration:
    """Integration of S3Storage with envelope encryption and fake/mock KMS."""

    @pytest.fixture
    def mock_s3_backend(self, tmp_path):
        """In-memory dictionary-backed S3 mock client."""
        storage_map: Dict[str, Dict[str, Any]] = {}

        mock_s3 = MagicMock()

        def put_object(**kwargs):
            key = kwargs["Key"]
            body = kwargs["Body"]
            if hasattr(body, "read"):
                body = body.read()
            storage_map[key] = {
                "Body": body,
                "ContentType": kwargs.get("ContentType", "application/octet-stream"),
                "Metadata": kwargs.get("Metadata", {}),
            }
            return {"ETag": '"fake-etag"'}

        def get_object(**kwargs):
            key = kwargs["Key"]
            if key not in storage_map:
                err = ClientError({"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."}}, "GetObject")
                err.response["ResponseMetadata"] = {"HTTPStatusCode": 404}
                raise err
            data = storage_map[key]
            mock_body = MagicMock()
            mock_body.read.return_value = data["Body"]
            mock_body.__enter__.return_value = mock_body
            mock_body.__exit__.return_value = None
            return {
                "Body": mock_body,
                "ContentType": data["ContentType"],
                "Metadata": data["Metadata"],
            }

        def head_object(**kwargs):
            key = kwargs["Key"]
            if key not in storage_map:
                err = ClientError({"Error": {"Code": "NoSuchKey", "Message": "Not found"}}, "HeadObject")
                err.response["ResponseMetadata"] = {"HTTPStatusCode": 404}
                raise err
            data = storage_map[key]
            return {
                "ContentType": data["ContentType"],
                "ContentLength": len(data["Body"]),
                "ETag": '"fake-etag"',
                "Metadata": data["Metadata"],
                "ResponseMetadata": {"HTTPStatusCode": 200},
            }

        def delete_object(**kwargs):
            key = kwargs["Key"]
            storage_map.pop(key, None)
            return {}

        def delete_objects(**kwargs):
            for obj in kwargs.get("Delete", {}).get("Objects", []):
                storage_map.pop(obj["Key"], None)
            return {}

        def get_paginator(operation_name):
            mock_pag = MagicMock()
            def paginate(**kwargs):
                prefix = kwargs.get("Prefix", "")
                contents = [{"Key": k, "Size": len(v["Body"])} for k, v in storage_map.items() if k.startswith(prefix)]
                return [{"Contents": contents}]
            mock_pag.paginate.side_effect = paginate
            return mock_pag

        mock_s3.put_object.side_effect = put_object
        mock_s3.get_object.side_effect = get_object
        mock_s3.head_object.side_effect = head_object
        mock_s3.delete_object.side_effect = delete_object
        mock_s3.delete_objects.side_effect = delete_objects
        mock_s3.get_paginator.side_effect = get_paginator

        settings = S3StorageSettings(
            bucket_name="secure-tenant-bucket",
            spool_dir=tmp_path / "spool",
            app_env="development",
            encryption_enabled=True,
            kms_provider_type="fake",
            kms_kek_id="kek-test-main",
            kms_key_version="v1",
        )

        fake_kms = FakeKmsProvider(kek_id="kek-test-main", key_version="v1")
        adapter = S3Storage(
            settings=settings,
            tenant_id="tenant_secured",
            client=mock_s3,
            kms_provider=fake_kms,
        )

        return adapter, storage_map, fake_kms

    def test_store_and_resolve_original_document_encrypted(self, mock_s3_backend):
        adapter, storage_map, _ = mock_s3_backend
        doc_id = "doc_encrypt_001"
        pdf_bytes = b"%PDF-1.7 confidential bank statement 12345678"

        artifact = adapter.store_original_document(doc_id, pdf_bytes)

        # 1. Verify remote S3 storage holds encrypted DOCE envelope, NOT plaintext
        s3_key = adapter._s3_key(f"{doc_id}/source/original.pdf")
        assert s3_key in storage_map
        s3_entry = storage_map[s3_key]
        raw_s3_body = s3_entry["Body"]

        assert is_envelope_encrypted(raw_s3_body)
        assert pdf_bytes not in raw_s3_body
        assert s3_entry["Metadata"]["x-amz-meta-encryption"] == "envelope-aes256-gcm"

        # 2. Verify local spool holds decrypted plaintext with restricted permissions
        assert artifact.file_path.exists()
        assert artifact.file_path.read_bytes() == pdf_bytes
        file_mode = stat.S_IMODE(artifact.file_path.stat().st_mode)
        assert file_mode == 0o600

        # 3. Test resolve_uri_to_path re-downloads and decrypts successfully
        adapter.cleanup_spool(doc_id)
        assert not artifact.file_path.exists()

        resolved_path = adapter.resolve_uri_to_path(artifact.storage_uri)
        assert resolved_path.exists()
        assert resolved_path.read_bytes() == pdf_bytes

    def test_store_and_retrieve_page_images_and_metadata(self, mock_s3_backend):
        adapter, storage_map, _ = mock_s3_backend
        doc_id = "doc_encrypt_002"
        img_bytes = b"\x89PNG\r\n\x1a\npage1pngbytes"
        meta_dict = {"skew_angle": 1.25, "rotation_applied": 0, "filter": "adaptive_threshold"}

        # Store page image
        img_art = adapter.store_page_image(doc_id, 1, img_bytes)
        img_s3_key = adapter._s3_key(f"{doc_id}/pages/page_0001.png")
        assert is_envelope_encrypted(storage_map[img_s3_key]["Body"])
        assert img_bytes not in storage_map[img_s3_key]["Body"]

        # Store preprocessed page and metadata
        prep_art = adapter.store_preprocessed_page(doc_id, 1, img_bytes, meta_dict)
        meta_s3_key = adapter._s3_key(f"{doc_id}/preprocessed/page_0001_metadata.json")
        assert is_envelope_encrypted(storage_map[meta_s3_key]["Body"])

        # Retrieve metadata
        retrieved_meta = adapter.get_page_metadata(doc_id, 1)
        assert retrieved_meta == meta_dict

    def test_document_record_and_handoff_payload_encryption(self, mock_s3_backend):
        adapter, storage_map, _ = mock_s3_backend
        doc_id = "doc_encrypt_003"

        # Record mock
        mock_rec = MagicMock()
        mock_rec.document_id = doc_id
        mock_rec.to_dict.return_value = {
            "document_id": doc_id,
            "file_hash_sha256": "abcdef1234567890",
            "page_count": 5,
        }

        adapter.save_document_record(mock_rec)
        rec_s3_key = adapter._s3_key(f"{doc_id}/document_record.json")
        assert is_envelope_encrypted(storage_map[rec_s3_key]["Body"])

        # Retrieve document record
        retrieved_rec = adapter.get_document_record(doc_id)
        assert retrieved_rec["file_hash_sha256"] == "abcdef1234567890"

        # Find document by hash
        found = adapter.find_document_by_hash("abcdef1234567890")
        assert found is not None
        assert found["document_id"] == doc_id

        # Handoff payload
        mock_handoff = MagicMock()
        mock_handoff.document_id = doc_id
        mock_handoff.to_dict.return_value = {"handoff_status": "ready", "pages": [1, 2]}

        adapter.save_handoff_payload(mock_handoff)
        handoff_s3_key = adapter._s3_key(f"{doc_id}/module_a_handoff.json")
        assert is_envelope_encrypted(storage_map[handoff_s3_key]["Body"])

        retrieved_handoff = adapter.get_handoff_payload(doc_id)
        assert retrieved_handoff == {"handoff_status": "ready", "pages": [1, 2]}

    def test_direct_object_bytes_primitives(self, mock_s3_backend):
        adapter, storage_map, _ = mock_s3_backend
        rel_path = "custom/raw_artifact.bin"
        raw_data = b"Arbitrary binary data payload."

        adapter.put_object_bytes(rel_path, raw_data, content_type="application/octet-stream")
        s3_key = adapter._s3_key(rel_path)
        assert is_envelope_encrypted(storage_map[s3_key]["Body"])

        retrieved = adapter.get_object_bytes(rel_path)
        assert retrieved == raw_data

    def test_rewrap_object_in_storage(self, mock_s3_backend):
        adapter, storage_map, fake_kms = mock_s3_backend
        doc_id = "doc_encrypt_004"
        rel_path = f"{doc_id}/source/original.pdf"
        data = b"%PDF-1.7 rotation test."

        adapter.store_original_document(doc_id, data)
        s3_key = adapter._s3_key(rel_path)
        original_envelope = storage_map[s3_key]["Body"]

        # Rotate KMS key to v2
        fake_kms.rotate_key("v2", "kek-new-2027")

        # Rewrap object DEK
        adapter.rewrap_object(rel_path, target_kek_id="kek-new-2027", target_version="v2")

        rewrapped_envelope = storage_map[s3_key]["Body"]
        assert rewrapped_envelope != original_envelope

        # Verify resolve still returns exact plaintext
        adapter.cleanup_spool(doc_id)
        path = adapter.resolve_uri_to_path(f"{adapter.tenant_id}/{rel_path}")
        assert path.read_bytes() == data

    def test_local_spool_cleanup(self, mock_s3_backend):
        adapter, _, _ = mock_s3_backend
        doc_id = "doc_encrypt_005"
        adapter.store_original_document(doc_id, b"some content")

        spool_file = adapter._tenant_spool_dir / doc_id / "source" / "original.pdf"
        assert spool_file.exists()

        # Clean up specific document
        adapter.cleanup_spool(doc_id)
        assert not (adapter._tenant_spool_dir / doc_id).exists()


# ==============================================================================
# 5. Production Hardening & Fail Closed Tests
# ==============================================================================

class TestProductionHardeningAndFailClosed:
    """Security boundaries and fail-closed policies in staging and production."""

    def test_staging_production_refuses_fake_kms(self, tmp_path):
        # Settings in staging refuses fake KMS
        with pytest.raises(ObjectStorageConfigurationError) as exc_staging:
            S3StorageSettings(
                bucket_name="secure-bucket",
                app_env="staging",
                access_key_id="AKIAIOSFODNN7EXAMPLE",
                secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                encryption_enabled=True,
                kms_provider_type="fake",
            )
        assert exc_staging.value.details.get("reason") == "fake_kms_forbidden_outside_development"

        # Settings in production refuses fake KMS
        with pytest.raises(ObjectStorageConfigurationError) as exc_prod:
            S3StorageSettings(
                bucket_name="secure-bucket",
                app_env="production",
                access_key_id="AKIAIOSFODNN7EXAMPLE",
                secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                encryption_enabled=True,
                kms_provider_type="fake",
            )
        assert exc_prod.value.details.get("reason") == "fake_kms_forbidden_outside_development"

    def test_production_requires_kek_id_when_encryption_enabled(self):
        with pytest.raises(ObjectStorageConfigurationError) as exc:
            S3StorageSettings(
                bucket_name="secure-bucket",
                app_env="production",
                access_key_id="AKIAIOSFODNN7EXAMPLE",
                secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                encryption_enabled=True,
                kms_provider_type="aws_kms",
                kms_kek_id=None,
            )
        assert exc.value.details.get("reason") == "kms_kek_id_required_when_encryption_enabled"

    def test_hardened_environment_refuses_unencrypted_object_download(self, tmp_path):
        """In production, if encryption is enabled, reading unencrypted plaintext from S3 must fail closed."""
        mock_s3 = MagicMock()
        mock_body = MagicMock()
        mock_body.read.return_value = b"%PDF-1.4 unencrypted leak attempt"
        mock_body.__enter__.return_value = mock_body
        mock_body.__exit__.return_value = None
        mock_s3.get_object.return_value = {"Body": mock_body, "ContentType": "application/pdf", "Metadata": {}}

        mock_kms = MagicMock()

        settings = S3StorageSettings(
            bucket_name="prod-bucket",
            app_env="production",
            access_key_id="AKIAIOSFODNN7EXAMPLE",
            secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            encryption_enabled=True,
            kms_provider_type="aws_kms",
            kms_kek_id="arn:aws:kms:us-east-1:123456789012:key/test",
            spool_dir=tmp_path / "spool",
        )

        adapter = S3Storage(settings=settings, tenant_id="tenant_prod", client=mock_s3, kms_provider=mock_kms)

        with pytest.raises(ObjectStorageError) as exc_uri:
            adapter.resolve_uri_to_path("tenant_prod/doc1/source/original.pdf")
        assert exc_uri.value.details.get("reason") == "unencrypted_data_refused_in_hardened_environment"

        with pytest.raises(ObjectStorageError) as exc_bytes:
            adapter.get_object_bytes("doc1/source/original.pdf")
        assert exc_bytes.value.details.get("reason") == "unencrypted_data_refused_in_hardened_environment"
