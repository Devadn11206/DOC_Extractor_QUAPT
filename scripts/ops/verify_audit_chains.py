#!/usr/bin/env python3
"""
Scheduled audit-chain verification (PRD SEC-008, SEC-021).

Walks every tenant directory under the audit store, recomputes each tenant's SHA-256 chain, and exits non-zero if any
chain is broken. Run it on a schedule (see deploy/k8s/cronjob-verify-audit.yaml); the failed job is what alerts.

Output contains tenant identifiers and the defect reason only, never event content (SEC-012).

Usage:
    python scripts/ops/verify_audit_chains.py --audit-store /var/lib/doc-extractor/storage
Exit status: 0 all chains valid, 1 at least one chain broken, 2 usage or I/O error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify every tenant's audit hash chain.")
    parser.add_argument("--audit-store", required=True, help="Storage root that holds the per-tenant audit directories.")
    args = parser.parse_args(argv)

    root = Path(args.audit_store)
    if not root.is_dir():
        print(json.dumps({"status": "error", "reason": "audit_store_not_found"}))
        return 2

    from doc_extractor.alerts import AlertMonitor
    from doc_extractor.api.audit_chain import HashChainedAuditRepository

    repo = HashChainedAuditRepository(root)
    monitor = AlertMonitor()
    broken = []
    checked = 0
    for entry in sorted(root.iterdir()):
        chain = entry / "audit" / "events.jsonl"
        if not chain.is_file():
            continue
        tenant_id = _tenant_id_from_marker(entry)
        if tenant_id is None:
            broken.append({"directory": entry.name, "reason": "identity_marker_missing"})
            continue
        try:
            verdict = repo.verify_chain(tenant_id)
        except (OSError, ValueError) as exc:
            broken.append({"tenant_id": tenant_id, "reason": f"verify_failed:{type(exc).__name__}"})
            continue
        checked += 1
        if not verdict.valid:
            broken.append({
                "tenant_id": tenant_id,
                "first_bad_seq": verdict.first_bad_seq,
                "reason": verdict.reason,
            })
            # SEC-021: a broken chain is an alert in addition to the failed job.
            monitor.report_audit_chain(tenant_id, False, verdict.reason, verdict.first_bad_seq)

    report = {"status": "broken" if broken else "ok", "chains_checked": checked, "broken": broken}
    print(json.dumps(report, sort_keys=True))
    return 1 if broken else 0


def _tenant_id_from_marker(tenant_dir: Path):
    """The tenant identity recorded in the tenant directory's marker (written at creation, see doc_extractor.tenancy)."""
    from doc_extractor.tenancy import IDENTITY_MARKER

    marker = tenant_dir / IDENTITY_MARKER
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tenant_id = data.get("tenant_id") if isinstance(data, dict) else None
    return tenant_id if isinstance(tenant_id, str) and tenant_id else None


if __name__ == "__main__":
    sys.exit(main())
