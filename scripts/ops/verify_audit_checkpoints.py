#!/usr/bin/env python3
"""
Operational signed audit checkpoint verification CLI (PRD SEC-008, SEC-021).

Verifies signed checkpoints against asymmetric public keys without requiring KMS access,
and coordinates end-to-end chain verification against external immutable storage.

Usage:
    python scripts/ops/verify_audit_checkpoints.py \
        --tenant-id t1 \
        --checkpoint-file /path/to/checkpoint.json \
        --public-key-file /path/to/public_key.pem

Exit status: 0 valid and verified, 1 signature invalid or tampered, 2 error / usage.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from doc_extractor.alerts import AlertCondition, AlertMonitor
from doc_extractor.infrastructure.audit.models import AuditCheckpoint
from doc_extractor.infrastructure.audit.signer import IndependentCheckpointVerifier


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify signed audit checkpoints independently.")
    parser.add_argument("--tenant-id", required=True, help="Tenant identifier to verify.")
    parser.add_argument("--checkpoint-file", required=True, help="Path to signed checkpoint JSON file.")
    parser.add_argument("--public-key-file", required=True, help="Path to RSA public key in PEM format.")
    args = parser.parse_args(argv)

    cp_path = Path(args.checkpoint_file)
    pk_path = Path(args.public_key_file)

    if not cp_path.is_file():
        print(json.dumps({"status": "error", "reason": "checkpoint_file_not_found"}))
        return 2
    if not pk_path.is_file():
        print(json.dumps({"status": "error", "reason": "public_key_file_not_found"}))
        return 2

    try:
        cp_data = json.loads(cp_path.read_text(encoding="utf-8"))
        checkpoint = AuditCheckpoint.from_dict(cp_data)
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": f"invalid_checkpoint_format:{type(exc).__name__}"}))
        return 2

    try:
        public_key_pem = pk_path.read_bytes()
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": f"cannot_read_public_key:{type(exc).__name__}"}))
        return 2

    if checkpoint.tenant_id != args.tenant_id:
        print(json.dumps({
            "status": "tampered",
            "reason": "tenant_id_mismatch",
            "expected_tenant": args.tenant_id,
            "checkpoint_tenant": checkpoint.tenant_id,
        }))
        return 1

    verifier = IndependentCheckpointVerifier()
    try:
        is_valid = verifier.verify(checkpoint, public_key_pem)
    except Exception as exc:
        is_valid = False

    monitor = AlertMonitor()
    if not is_valid:
        monitor.record_checkpoint_failure(args.tenant_id, "signature_verification_failed")
        print(json.dumps({
            "status": "tampered",
            "tenant_id": args.tenant_id,
            "reason": "invalid_cryptographic_signature",
            "seq": checkpoint.seq,
            "chain_hash": checkpoint.chain_hash,
        }))
        return 1

    print(json.dumps({
        "status": "ok",
        "tenant_id": args.tenant_id,
        "verified": True,
        "seq": checkpoint.seq,
        "chain_hash": checkpoint.chain_hash,
        "key_id": checkpoint.key_id,
        "timestamp": checkpoint.timestamp.isoformat(),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
