"""
Enterprise readiness, health reporting, and operational diagnostics tests (Phase 6).
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
import pytest

from doc_extractor.api.app import create_app
from doc_extractor.api.config import ApiSettings
from doc_extractor.infrastructure.audit.models import AuditCheckpoint
from tests.mongo_fakes import FakeClient, FakeDatabase


def make_rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_key, public_pem


def sign_checkpoint(checkpoint: AuditCheckpoint, private_key) -> AuditCheckpoint:
    canonical_bytes = checkpoint.canonical_bytes()
    sig = private_key.sign(
        canonical_bytes,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(sig).decode("ascii")
    return AuditCheckpoint(
        tenant_id=checkpoint.tenant_id,
        seq=checkpoint.seq,
        chain_hash=checkpoint.chain_hash,
        timestamp=checkpoint.timestamp,
        key_id=checkpoint.key_id,
        key_version=checkpoint.key_version,
        version=checkpoint.version,
        algorithm=checkpoint.algorithm,
        signature=sig_b64,
    )


class TestEnterpriseHealthEndpoints:

    def test_health_reports_queue_when_configured(self):
        app = create_app(ApiSettings(app_env="development"))
        mock_queue = AsyncMock()
        mock_queue.health.return_value = True

        app.state.container._owned_queue = mock_queue

        with TestClient(app) as client:
            resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "queue" in data
        assert data["queue"]["status"] == "ok"
        assert data["queue"]["backend"] == "rabbitmq"

    def test_health_reports_queue_degraded_on_failure(self):
        app = create_app(ApiSettings(app_env="development"))
        mock_queue = AsyncMock()
        mock_queue.health.return_value = False

        app.state.container._owned_queue = mock_queue

        with TestClient(app) as client:
            resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["queue"]["status"] == "degraded"
        assert data["status"] == "degraded"

    def test_health_reports_redis_when_configured(self):
        app = create_app(ApiSettings(app_env="development"))
        app.state.container.engine_settings.redis_url = "redis://localhost:6379/0"

        with patch("doc_extractor.distributed_state.connect_redis") as mock_connect:
            mock_client = MagicMock()
            mock_client.ping.return_value = True
            mock_connect.return_value = mock_client

            with TestClient(app) as client:
                resp = client.get("/api/v1/health")

        assert resp.status_code == 200
        data = resp.json()
        assert "redis" in data
        assert data["redis"]["status"] == "ok"

    def test_health_reports_s3_and_kms_when_configured(self):
        settings = ApiSettings(
            app_env="development",
            storage_backend="s3",
            s3_bucket="test-bucket",
            s3_endpoint_url="http://localhost:9000",
            s3_access_key_id="test_key",
            s3_secret_access_key="test_secret",
            kms_provider="fake",
            kms_key_id="k1",
        )
        app = create_app(settings)

        with patch("doc_extractor.infrastructure.storage.adapter.S3Storage") as mock_s3_cls:
            mock_s3 = MagicMock()
            mock_s3.bucket_name = "test-bucket"
            mock_s3.check_health.return_value = {"status": "ok", "bucket": "test-bucket"}
            mock_kms = MagicMock()
            mock_kms.check_health.return_value = {"status": "ok", "provider": "fake_kms"}
            mock_s3.kms_provider = mock_kms
            mock_s3_cls.return_value = mock_s3

            with TestClient(app) as client:
                resp = client.get("/api/v1/health")

        assert resp.status_code == 200
        data = resp.json()
        assert "storage" in data
        assert data["storage"]["status"] == "ok"
        assert data["storage"]["backend"] == "s3"
        assert "kms" in data
        assert data["kms"]["status"] == "ok"


class TestAuditCheckpointVerificationCLI:

    def test_cli_valid_signed_checkpoint(self, tmp_path: Path):
        priv_key, pub_pem = make_rsa_key_pair()
        pub_path = tmp_path / "pub.pem"
        pub_path.write_bytes(pub_pem)

        cp = AuditCheckpoint(
            tenant_id="tenant_alpha",
            seq=42,
            chain_hash="a" * 64,
            timestamp=datetime.now(timezone.utc),
            key_id="k1",
            key_version="v1",
        )
        signed_cp = sign_checkpoint(cp, priv_key)
        cp_path = tmp_path / "checkpoint.json"
        cp_path.write_text(json.dumps(signed_cp.to_dict()), encoding="utf-8")

        cli_script = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "verify_audit_checkpoints.py"
        res = subprocess.run(
            [
                sys.executable,
                str(cli_script),
                "--tenant-id", "tenant_alpha",
                "--checkpoint-file", str(cp_path),
                "--public-key-file", str(pub_path),
            ],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0
        out = json.loads(res.stdout)
        assert out["status"] == "ok"
        assert out["verified"] is True
        assert out["seq"] == 42

    def test_cli_tampered_signature_rejected(self, tmp_path: Path):
        priv_key, pub_pem = make_rsa_key_pair()
        pub_path = tmp_path / "pub.pem"
        pub_path.write_bytes(pub_pem)

        cp = AuditCheckpoint(
            tenant_id="tenant_alpha",
            seq=42,
            chain_hash="a" * 64,
            timestamp=datetime.now(timezone.utc),
            key_id="k1",
            key_version="v1",
        )
        signed_cp = sign_checkpoint(cp, priv_key)
        # Tamper chain hash
        tampered_dict = signed_cp.to_dict()
        tampered_dict["chain_hash"] = "b" * 64

        cp_path = tmp_path / "tampered.json"
        cp_path.write_text(json.dumps(tampered_dict), encoding="utf-8")

        cli_script = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "verify_audit_checkpoints.py"
        res = subprocess.run(
            [
                sys.executable,
                str(cli_script),
                "--tenant-id", "tenant_alpha",
                "--checkpoint-file", str(cp_path),
                "--public-key-file", str(pub_path),
            ],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 1
        out = json.loads(res.stdout)
        assert out["status"] == "tampered"
        assert out["reason"] == "invalid_cryptographic_signature"

    def test_cli_wrong_tenant_id_rejected(self, tmp_path: Path):
        priv_key, pub_pem = make_rsa_key_pair()
        pub_path = tmp_path / "pub.pem"
        pub_path.write_bytes(pub_pem)

        cp = AuditCheckpoint(
            tenant_id="tenant_alpha",
            seq=1,
            chain_hash="a" * 64,
            timestamp=datetime.now(timezone.utc),
            key_id="k1",
            key_version="v1",
        )
        signed_cp = sign_checkpoint(cp, priv_key)
        cp_path = tmp_path / "checkpoint.json"
        cp_path.write_text(json.dumps(signed_cp.to_dict()), encoding="utf-8")

        cli_script = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "verify_audit_checkpoints.py"
        res = subprocess.run(
            [
                sys.executable,
                str(cli_script),
                "--tenant-id", "tenant_bravo",  # mismatched tenant
                "--checkpoint-file", str(cp_path),
                "--public-key-file", str(pub_path),
            ],
            capture_output=True,
            text=True,
        )
        assert res.returncode == 1
        out = json.loads(res.stdout)
        assert out["status"] == "tampered"
        assert out["reason"] == "tenant_id_mismatch"
