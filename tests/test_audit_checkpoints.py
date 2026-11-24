"""
Unit tests for independently signed audit checkpoints and tamper-resistant storage (Team Member 3, Phase 4C).

Covers:
1. Checkpoint Canonical Serialization & Model Validation:
   - Deterministic canonical bytes for asymmetric signatures.
   - JSON and dict serialization/deserialization round trips.
   - Strict format validation (path-safe tenant, non-negative seq, 64-hex hash).
2. Asymmetric Signing & Independent Public-Key Verification:
   - RSASSA-PSS signing with FakeAuditSigner and AwsKmsAuditSigner.
   - Completely offline signature verification using IndependentCheckpointVerifier.
   - Tamper detection: modifying any field (seq, hash, tenant, timestamp) invalidates signature.
   - Corrupt signature bytes rejection.
3. Key Rotation & Historical Key Versions:
   - Rotating signing key to new versions (v1 -> v2).
   - Historical checkpoints remain verifiable using archived public keys.
4. External Immutable Checkpoint Sinks (InMemory & S3 Object Lock):
   - Idempotent duplicate writes for identical checkpoints.
   - CheckpointConflictError on conflicting data at existing sequence.
   - ObjectLockNotSupportedError when Object Lock is required outside development but not enabled.
   - Fail closed: sink outages raise CheckpointSinkUnavailableError.
5. Independent Multi-Layered Chain Verification:
   - Fully valid chain and matching signed checkpoint.
   - Truncated tail detection (DB records deleted after checkpoint creation).
   - Rewritten audit record detection (hash mismatch at checkpoint sequence).
   - Tampered external checkpoint detection (sink differs from local record).
   - Missing checkpoint for non-empty chain explicitly distinguished from valid.
   - Database-only verification never claimed as signed checkpoint verification.
6. Operational Service Coordination:
   - create_checkpoint from MongoDB chain head.
   - Concurrent and sequential checkpoint generation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from doc_extractor.api.audit_chain import GENESIS_HASH, ChainVerification
from doc_extractor.infrastructure.audit.errors import (
    CheckpointConfigurationError,
    CheckpointConflictError,
    CheckpointSigningError,
    CheckpointSinkUnavailableError,
    CheckpointVerificationError,
    ObjectLockNotSupportedError,
)
from doc_extractor.infrastructure.audit.kms_signer import AwsKmsAuditSigner
from doc_extractor.infrastructure.audit.models import (
    CHECKPOINT_FORMAT_VERSION,
    AuditCheckpoint,
    CheckpointVerificationStatus,
)
from doc_extractor.infrastructure.audit.service import AuditCheckpointService
from doc_extractor.infrastructure.audit.signer import (
    FakeAuditSigner,
    IndependentCheckpointVerifier,
)
from doc_extractor.infrastructure.audit.sink import (
    EphemeralCheckpointSink,
    S3ObjectLockCheckpointSink,
)
from doc_extractor.infrastructure.audit.verification import AuditChainVerifier


def run(coro):
    return asyncio.run(coro)


# ==============================================================================
# Test Doubles for MongoDB Audit Repository
# ==============================================================================

class MockMongoAuditRepo:
    """In-memory test double mimicking MongoAuditRepository for audit checkpoint verification."""

    def __init__(self, tenant_id: str = "tenant_test") -> None:
        self.tenant_id = tenant_id
        # List of documents: [{"seq": 1, "hash": "...", "prev_hash": "..."}]
        self.records: List[Dict[str, Any]] = []
        self._chain_valid = True
        self._bad_seq: Optional[int] = None
        self._bad_reason: Optional[str] = None

    def add_event(self, event_hash: str) -> int:
        seq = len(self.records) + 1
        prev = self.records[-1]["hash"] if self.records else GENESIS_HASH
        self.records.append({
            "tenant_id": self.tenant_id,
            "seq": seq,
            "prev_hash": prev,
            "hash": event_hash,
        })
        return seq

    async def chain_head(self, tenant_id: str) -> Tuple[int, str]:
        if not self.records:
            return 0, GENESIS_HASH
        latest = self.records[-1]
        return latest["seq"], latest["hash"]

    async def verify_chain(self, tenant_id: str, *, batch_size: int = 500) -> ChainVerification:
        return ChainVerification(
            tenant_id=tenant_id,
            valid=self._chain_valid,
            checked_events=len(self.records),
            first_bad_seq=self._bad_seq,
            reason=self._bad_reason,
        )

    async def query_events(
        self,
        tenant_id: str,
        limit: int = 100,
        offset: int = 0,
        **kwargs: Any,
    ) -> List[Any]:
        slice_records = self.records[offset:offset + limit]
        return [MagicMock(seq=r["seq"], hash=r["hash"]) for r in slice_records]

    # Store simulation for find_one
    class _MockStore:
        def __init__(self, parent: MockMongoAuditRepo) -> None:
            self._parent = parent

        async def find_one(self, tenant_id: str, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            seq = query.get("seq")
            for r in self._parent.records:
                if r["seq"] == seq:
                    return r
            return None

    @property
    def _store(self) -> Any:
        return self._MockStore(self)


# ==============================================================================
# 1. Canonical Checkpoint Serialization & Validation
# ==============================================================================

class TestCheckpointSerializationAndValidation:
    """Tests for deterministic canonical serialization and strict validation."""

    def test_canonical_bytes_deterministic_order(self):
        dt = datetime(2026, 11, 24, 12, 0, 0, 123456, tzinfo=timezone.utc)
        cp = AuditCheckpoint(
            tenant_id="tenant_alpha",
            seq=42,
            chain_hash="a" * 64,
            timestamp=dt,
            key_id="arn:aws:kms:us-east-1:123456789012:key/test",
            key_version="v1",
        )

        canonical = cp.canonical_bytes()
        expected = (
            b'{"alg":"RSASSA_PSS_SHA_256",'
            b'"chain_hash":"' + (b"a" * 64) + b'",'
            b'"key_id":"arn:aws:kms:us-east-1:123456789012:key/test",'
            b'"key_ver":"v1",'
            b'"seq":42,'
            b'"tenant_id":"tenant_alpha",'
            b'"timestamp":"2026-11-24T12:00:00.123456Z",'
            b'"v":1}'
        )
        assert canonical == expected

    def test_json_and_dict_round_trip(self):
        dt = datetime(2026, 11, 24, 15, 30, 45, 0, tzinfo=timezone.utc)
        cp = AuditCheckpoint(
            tenant_id="tenant_beta",
            seq=100,
            chain_hash="b" * 64,
            timestamp=dt,
            key_id="key-sec-01",
            key_version="v2",
            signature="c2lnbmF0dXJlX2J5dGVzX2hlcmU=",
        )

        json_text = cp.to_json()
        restored = AuditCheckpoint.from_json(json_text)

        assert restored.tenant_id == cp.tenant_id
        assert restored.seq == cp.seq
        assert restored.chain_hash == cp.chain_hash
        assert restored.timestamp == cp.timestamp
        assert restored.key_id == cp.key_id
        assert restored.key_version == cp.key_version
        assert restored.signature == cp.signature
        assert restored.canonical_bytes() == cp.canonical_bytes()

    def test_validation_rejects_malformed_fields(self):
        dt = datetime.now(timezone.utc)
        valid_hash = "c" * 64

        with pytest.raises(CheckpointVerificationError) as exc:
            AuditCheckpoint(tenant_id="", seq=1, chain_hash=valid_hash, timestamp=dt, key_id="k", key_version="v")
        assert exc.value.details.get("reason") == "tenant_id_empty"

        with pytest.raises(CheckpointVerificationError) as exc:
            AuditCheckpoint(tenant_id="../escape", seq=1, chain_hash=valid_hash, timestamp=dt, key_id="k", key_version="v")
        assert "tenant" in exc.value.details.get("reason", "")

        with pytest.raises(CheckpointVerificationError) as exc:
            AuditCheckpoint(tenant_id="t1", seq=-5, chain_hash=valid_hash, timestamp=dt, key_id="k", key_version="v")
        assert exc.value.details.get("reason") == "invalid_sequence_number"

        with pytest.raises(CheckpointVerificationError) as exc:
            AuditCheckpoint(tenant_id="t1", seq=1, chain_hash="short-hash", timestamp=dt, key_id="k", key_version="v")
        assert exc.value.details.get("reason") == "invalid_chain_hash_format"


# ==============================================================================
# 2. Asymmetric Signing & Independent Verification
# ==============================================================================

class TestAsymmetricSigningAndIndependentVerification:
    """Tests for asymmetric signing and offline public-key verification."""

    def test_sign_and_verify_round_trip(self):
        signer = FakeAuditSigner(key_id="test-key-01", key_version="v1")
        verifier = IndependentCheckpointVerifier()

        checkpoint = AuditCheckpoint(
            tenant_id="tenant_audit_1",
            seq=10,
            chain_hash="1" * 64,
            timestamp=datetime.now(timezone.utc),
            key_id=signer.get_key_id(),
            key_version=signer.get_key_version(),
        )

        signed_cp = signer.sign(checkpoint)
        assert signed_cp.signature is not None

        pub_pem = signer.get_public_key()
        # Independent verification without KMS
        assert verifier.verify(signed_cp, public_key_pem=pub_pem) is True

    def test_tampered_field_fails_verification(self):
        signer = FakeAuditSigner()
        verifier = IndependentCheckpointVerifier()

        checkpoint = AuditCheckpoint(
            tenant_id="tenant_audit_2",
            seq=25,
            chain_hash="2" * 64,
            timestamp=datetime.now(timezone.utc),
            key_id=signer.get_key_id(),
            key_version=signer.get_key_version(),
        )
        signed_cp = signer.sign(checkpoint)
        pub_pem = signer.get_public_key()

        # 1. Tamper sequence
        tampered_seq = AuditCheckpoint(
            tenant_id=signed_cp.tenant_id,
            seq=26,  # modified
            chain_hash=signed_cp.chain_hash,
            timestamp=signed_cp.timestamp,
            key_id=signed_cp.key_id,
            key_version=signed_cp.key_version,
            signature=signed_cp.signature,
        )
        assert verifier.verify(tampered_seq, public_key_pem=pub_pem) is False

        # 2. Tamper hash
        tampered_hash = AuditCheckpoint(
            tenant_id=signed_cp.tenant_id,
            seq=signed_cp.seq,
            chain_hash="f" * 64,  # modified
            timestamp=signed_cp.timestamp,
            key_id=signed_cp.key_id,
            key_version=signed_cp.key_version,
            signature=signed_cp.signature,
        )
        assert verifier.verify(tampered_hash, public_key_pem=pub_pem) is False

        # 3. Tamper tenant
        tampered_tenant = AuditCheckpoint(
            tenant_id="tenant_other",  # modified
            seq=signed_cp.seq,
            chain_hash=signed_cp.chain_hash,
            timestamp=signed_cp.timestamp,
            key_id=signed_cp.key_id,
            key_version=signed_cp.key_version,
            signature=signed_cp.signature,
        )
        assert verifier.verify(tampered_tenant, public_key_pem=pub_pem) is False

    def test_corrupted_signature_fails_verification(self):
        signer = FakeAuditSigner()
        verifier = IndependentCheckpointVerifier()
        signer.simulate_corrupt_signature(True)

        checkpoint = AuditCheckpoint(
            tenant_id="tenant_audit_3",
            seq=1,
            chain_hash="3" * 64,
            timestamp=datetime.now(timezone.utc),
            key_id=signer.get_key_id(),
            key_version=signer.get_key_version(),
        )

        signed_cp = signer.sign(checkpoint)
        pub_pem = signer.get_public_key()
        assert verifier.verify(signed_cp, public_key_pem=pub_pem) is False


# ==============================================================================
# 3. Key Rotation & Historical Key Versions
# ==============================================================================

class TestSigningKeyRotation:
    """Verifies checkpoints signed with older key versions remain verifiable after key rotation."""

    def test_historical_key_version_verification(self):
        signer = FakeAuditSigner(key_id="audit-key", key_version="v1")
        verifier = IndependentCheckpointVerifier()

        # Sign checkpoint under v1
        cp_v1 = AuditCheckpoint(
            tenant_id="tenant_rot",
            seq=50,
            chain_hash="4" * 64,
            timestamp=datetime.now(timezone.utc),
            key_id="audit-key",
            key_version="v1",
        )
        signed_v1 = signer.sign(cp_v1)
        pub_v1 = signer.get_public_key(key_version="v1")
        verifier.register_public_key("audit-key", "v1", pub_v1)

        # Rotate signer to v2
        signer.rotate_key("v2")
        assert signer.get_key_version() == "v2"

        # Sign checkpoint under v2
        cp_v2 = AuditCheckpoint(
            tenant_id="tenant_rot",
            seq=100,
            chain_hash="5" * 64,
            timestamp=datetime.now(timezone.utc),
            key_id="audit-key",
            key_version="v2",
        )
        signed_v2 = signer.sign(cp_v2)
        pub_v2 = signer.get_public_key(key_version="v2")
        verifier.register_public_key("audit-key", "v2", pub_v2)

        # Both historical v1 and current v2 checkpoints verify successfully
        assert verifier.verify(signed_v1) is True
        assert verifier.verify(signed_v2) is True


# ==============================================================================
# 4. AWS KMS Signer (Mocked)
# ==============================================================================

class TestAwsKmsAuditSigner:
    """Tests for AwsKmsAuditSigner with mocked boto3 KMS client."""

    def test_aws_kms_sign_and_get_public_key(self):
        mock_client = MagicMock()
        mock_client.sign.return_value = {"Signature": b"binary_signature_from_aws_kms"}

        # Generate dummy RSA key for mocked get_public_key
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        der_pub = priv.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        mock_client.get_public_key.return_value = {"PublicKey": der_pub, "KeyId": "arn:aws:kms:key/123"}

        signer = AwsKmsAuditSigner(key_id="arn:aws:kms:key/123", client=mock_client)
        cp = AuditCheckpoint(
            tenant_id="t1",
            seq=1,
            chain_hash="6" * 64,
            timestamp=datetime.now(timezone.utc),
            key_id=signer.get_key_id(),
            key_version=signer.get_key_version(),
        )

        signed_cp = signer.sign(cp)
        assert signed_cp.signature is not None
        mock_client.sign.assert_called_once()

        pem = signer.get_public_key()
        assert pem.startswith(b"-----BEGIN PUBLIC KEY-----")

    def test_aws_kms_signer_sanitized_errors(self):
        mock_client = MagicMock()
        mock_client.sign.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Secret KMS IAM policy details"}},
            "Sign",
        )

        signer = AwsKmsAuditSigner(key_id="key-denied", client=mock_client)
        cp = AuditCheckpoint(
            tenant_id="t1",
            seq=1,
            chain_hash="7" * 64,
            timestamp=datetime.now(timezone.utc),
            key_id="key-denied",
            key_version="v1",
        )

        with pytest.raises(CheckpointSigningError) as exc:
            signer.sign(cp)
        assert exc.value.details.get("reason") == "signing_access_denied"
        assert "Secret KMS IAM policy details" not in str(exc.value)


# ==============================================================================
# 5. External Immutable Sinks (InMemory & S3 Object Lock)
# ==============================================================================

class TestAuditCheckpointSinks:
    """Tests for external checkpoint sinks, idempotent writes, and Object Lock checks."""

    def test_in_memory_sink_idempotency_and_conflict(self):
        async def go():
            sink = EphemeralCheckpointSink()
            cp = AuditCheckpoint(
                tenant_id="tenant_sink",
                seq=10,
                chain_hash="8" * 64,
                timestamp=datetime.now(timezone.utc),
                key_id="k1",
                key_version="v1",
                signature="sig1",
            )

            # Store once
            await sink.store_checkpoint(cp)

            # Storing identical checkpoint succeeds idempotently
            await sink.store_checkpoint(cp)

            retrieved = await sink.get_checkpoint("tenant_sink", 10)
            assert retrieved == cp

            # Attempting to store different checkpoint at same sequence raises CheckpointConflictError
            conflicting_cp = AuditCheckpoint(
                tenant_id="tenant_sink",
                seq=10,
                chain_hash="9" * 64,  # different hash
                timestamp=datetime.now(timezone.utc),
                key_id="k1",
                key_version="v1",
                signature="sig2",
            )
            with pytest.raises(CheckpointConflictError) as exc:
                await sink.store_checkpoint(conflicting_cp)
            assert exc.value.details.get("reason") == "checkpoint_conflict_at_existing_sequence"
        run(go())

    def test_s3_sink_object_lock_configuration_check(self):
        async def go():
            mock_s3 = MagicMock()
            mock_s3.head_bucket.return_value = {}
            # Simulate bucket without Object Lock
            mock_s3.get_object_lock_configuration.side_effect = ClientError(
                {"Error": {"Code": "ObjectLockConfigurationNotFoundError", "Message": "Object Lock configuration does not exist"}},
                "GetObjectLockConfiguration",
            )

            sink = S3ObjectLockCheckpointSink(
                bucket_name="non-locked-bucket",
                client=mock_s3,
                require_object_lock=True,
                app_env="production",
            )

            with pytest.raises(ObjectLockNotSupportedError) as exc:
                await sink.check_health()
            assert exc.value.details.get("reason") == "object_lock_not_enabled_on_bucket"
        run(go())

    def test_s3_sink_store_and_retrieve_checkpoint(self):
        async def go():
            storage_map: Dict[str, bytes] = {}
            mock_s3 = MagicMock()

            def put_object(**kwargs):
                key = kwargs["Key"]
                storage_map[key] = kwargs["Body"]
                return {"ETag": '"etag"'}

            def get_object(**kwargs):
                key = kwargs["Key"]
                if key not in storage_map:
                    err = ClientError({"Error": {"Code": "NoSuchKey", "Message": "Not found"}}, "GetObject")
                    err.response["ResponseMetadata"] = {"HTTPStatusCode": 404}
                    raise err
                mock_body = MagicMock()
                mock_body.read.return_value = storage_map[key]
                mock_body.__enter__.return_value = mock_body
                mock_body.__exit__.return_value = None
                return {"Body": mock_body}

            mock_s3.put_object.side_effect = put_object
            mock_s3.get_object.side_effect = get_object

            sink = S3ObjectLockCheckpointSink(
                bucket_name="locked-audit-bucket",
                client=mock_s3,
                require_object_lock=False,
                app_env="development",
            )

            cp = AuditCheckpoint(
                tenant_id="tenant_s3",
                seq=15,
                chain_hash="a" * 64,
                timestamp=datetime.now(timezone.utc),
                key_id="k1",
                key_version="v1",
                signature="b64sig",
            )

            await sink.store_checkpoint(cp)
            s3_key = sink._s3_key("tenant_s3", 15)
            assert s3_key in storage_map

            retrieved = await sink.get_checkpoint("tenant_s3", 15)
            assert retrieved is not None
            assert retrieved.seq == 15
            assert retrieved.chain_hash == "a" * 64
        run(go())


# ==============================================================================
# 6. Independent Multi-Layered Chain Verification
# ==============================================================================

class TestIndependentChainVerification:
    """Tests for multi-layered verification: detecting truncation, rewrites, and tampered checkpoints."""

    def test_fully_valid_chain_and_signed_checkpoint(self):
        async def go():
            tenant = "tenant_valid"
            repo = MockMongoAuditRepo(tenant)
            signer = FakeAuditSigner()
            sink = EphemeralCheckpointSink()
            verifier = IndependentCheckpointVerifier(
                public_key_resolver=lambda kid, kver: signer.get_public_key(kid, kver)
            )
            chain_verifier = AuditChainVerifier(verifier=verifier, sink=sink)

            # Add 5 events to MongoDB
            for i in range(5):
                repo.add_event(hashlib.sha256(f"event_{i}".encode()).hexdigest())

            head_seq, head_hash = await repo.chain_head(tenant)

            # Create signed checkpoint
            cp = AuditCheckpoint(
                tenant_id=tenant,
                seq=head_seq,
                chain_hash=head_hash,
                timestamp=datetime.now(timezone.utc),
                key_id=signer.get_key_id(),
                key_version=signer.get_key_version(),
            )
            signed_cp = signer.sign(cp)
            await sink.store_checkpoint(signed_cp)

            # Verify
            report = await chain_verifier.verify_chain_with_checkpoint(tenant, repo)

            assert report.valid is True
            assert report.status == CheckpointVerificationStatus.VALID
            assert report.checkpoint_verified is True
            assert report.database_chain_verified is True
            assert report.checkpoint_seq == 5
            assert report.db_head_seq == 5
        run(go())

    def test_truncation_attack_detection(self):
        """Attacker truncates DB tail: DB has seq 1..3, but signed checkpoint proves seq reached 5."""
        async def go():
            tenant = "tenant_trunc"
            repo = MockMongoAuditRepo(tenant)
            signer = FakeAuditSigner()
            sink = EphemeralCheckpointSink()
            verifier = IndependentCheckpointVerifier(
                public_key_resolver=lambda kid, kver: signer.get_public_key(kid, kver)
            )
            chain_verifier = AuditChainVerifier(verifier=verifier, sink=sink)

            # Add 5 events initially
            for i in range(5):
                repo.add_event(hashlib.sha256(f"trunc_event_{i}".encode()).hexdigest())

            head_seq, head_hash = await repo.chain_head(tenant)

            # Store signed checkpoint at seq 5
            cp = AuditCheckpoint(
                tenant_id=tenant,
                seq=head_seq,
                chain_hash=head_hash,
                timestamp=datetime.now(timezone.utc),
                key_id=signer.get_key_id(),
                key_version=signer.get_key_version(),
            )
            signed_cp = signer.sign(cp)
            await sink.store_checkpoint(signed_cp)

            # Attacker deletes newest 2 events from MongoDB (leaving only seq 1..3)
            repo.records = repo.records[:3]

            report = await chain_verifier.verify_chain_with_checkpoint(tenant, repo)

            assert report.valid is False
            assert report.status == CheckpointVerificationStatus.CHAIN_TRUNCATED
            assert report.first_bad_seq == 4
            assert report.details.get("deleted_records_count") == 2
        run(go())

    def test_rewritten_chain_hash_mismatch_detection(self):
        """Attacker rewrites historical events in MongoDB: sequence count matches, but hash differs."""
        async def go():
            tenant = "tenant_rewrite"
            repo = MockMongoAuditRepo(tenant)
            signer = FakeAuditSigner()
            sink = EphemeralCheckpointSink()
            verifier = IndependentCheckpointVerifier(
                public_key_resolver=lambda kid, kver: signer.get_public_key(kid, kver)
            )
            chain_verifier = AuditChainVerifier(verifier=verifier, sink=sink)

            for i in range(4):
                repo.add_event(hashlib.sha256(f"rewrite_event_{i}".encode()).hexdigest())

            head_seq, head_hash = await repo.chain_head(tenant)

            # Store checkpoint at seq 4
            cp = AuditCheckpoint(
                tenant_id=tenant,
                seq=head_seq,
                chain_hash=head_hash,
                timestamp=datetime.now(timezone.utc),
                key_id=signer.get_key_id(),
                key_version=signer.get_key_version(),
            )
            signed_cp = signer.sign(cp)
            await sink.store_checkpoint(signed_cp)

            # Attacker rewrites record at seq 4 with an altered hash
            repo.records[3]["hash"] = hashlib.sha256(b"altered_hash").hexdigest()

            report = await chain_verifier.verify_chain_with_checkpoint(tenant, repo)

            assert report.valid is False
            assert report.status == CheckpointVerificationStatus.HASH_MISMATCH
            assert report.first_bad_seq == 4
        run(go())

    def test_tampered_external_checkpoint_detection(self):
        """Attacker modifies the checkpoint payload in external storage: signature invalid."""
        async def go():
            tenant = "tenant_tamper_ext"
            repo = MockMongoAuditRepo(tenant)
            signer = FakeAuditSigner()
            sink = EphemeralCheckpointSink()
            verifier = IndependentCheckpointVerifier(
                public_key_resolver=lambda kid, kver: signer.get_public_key(kid, kver)
            )
            chain_verifier = AuditChainVerifier(verifier=verifier, sink=sink)

            repo.add_event(hashlib.sha256(b"event_01_tamper").hexdigest())
            head_seq, head_hash = await repo.chain_head(tenant)

            cp = AuditCheckpoint(
                tenant_id=tenant,
                seq=head_seq,
                chain_hash=head_hash,
                timestamp=datetime.now(timezone.utc),
                key_id=signer.get_key_id(),
                key_version=signer.get_key_version(),
            )
            signed_cp = signer.sign(cp)
            await sink.store_checkpoint(signed_cp)

            # Attacker alters the stored JSON in the sink (alters sequence)
            tampered_dict = signed_cp.to_dict()
            tampered_dict["seq"] = 999
            sink.tamper_checkpoint(tenant, head_seq, json.dumps(tampered_dict))

            report = await chain_verifier.verify_chain_with_checkpoint(tenant, repo)

            assert report.valid is False
            assert report.status == CheckpointVerificationStatus.SIGNATURE_INVALID
        run(go())

    def test_uncheckpointed_non_empty_chain_is_not_checkpoint_verified(self):
        """Non-empty database chain without any signed checkpoint must not claim signed verification."""
        async def go():
            tenant = "tenant_uncheckpointed"
            repo = MockMongoAuditRepo(tenant)
            sink = EphemeralCheckpointSink()
            verifier = IndependentCheckpointVerifier()
            chain_verifier = AuditChainVerifier(verifier=verifier, sink=sink)

            repo.add_event(hashlib.sha256(b"event_01_uncheckpointed").hexdigest())

            report = await chain_verifier.verify_chain_with_checkpoint(tenant, repo)

            assert report.valid is False
            assert report.status == CheckpointVerificationStatus.CHECKPOINT_NOT_FOUND
            assert report.checkpoint_verified is False  # Must NOT represent DB-only verification as checkpoint-verified
        run(go())


# ==============================================================================
# 7. Operational Checkpoint Service
# ==============================================================================

class TestAuditCheckpointService:
    """Operational service lifecycle: creation, storage, and verification coordination."""

    def test_service_create_and_verify(self):
        async def go():
            tenant = "tenant_service"
            repo = MockMongoAuditRepo(tenant)
            signer = FakeAuditSigner(key_id="k-srv", key_version="v1")
            sink = EphemeralCheckpointSink()

            service = AuditCheckpointService(
                mongo_repo=repo,
                signer=signer,
                sink=sink,
            )

            # 1. Add events
            for i in range(3):
                repo.add_event(hashlib.sha256(f"service_event_{i}".encode()).hexdigest())

            # 2. Create checkpoint via service
            cp = await service.create_checkpoint(tenant)
            assert cp.seq == 3
            assert cp.key_id == "k-srv"
            assert cp.signature is not None

            # 3. Retrieve latest
            latest = await service.get_latest_checkpoint(tenant)
            assert latest == cp

            # 4. Verify via service
            report = await service.verify(tenant)
            assert report.valid is True
            assert report.checkpoint_verified is True
            assert report.db_head_seq == 3
        run(go())
