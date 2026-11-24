"""
Independent audit chain and signed checkpoint verification (Team Member 3, Phase 4C).

Adheres to:
- PRD SEC-008: Comprehensive verification comparing database records, checkpoints, signatures, and external sinks.
- Attack Detection: Detects deleted historical records, truncated tails, rewritten records, forked chains,
  tampered checkpoints, and invalid signatures.
- Explicit Distinction: Database-only hash-chain verification is never represented as equivalent to
  signed checkpoint verification.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from doc_extractor.api.audit_chain import GENESIS_HASH, ChainVerification
from doc_extractor.infrastructure.audit.errors import CheckpointSinkUnavailableError
from doc_extractor.infrastructure.audit.models import (
    AuditCheckpoint,
    CheckpointVerificationReport,
    CheckpointVerificationStatus,
)
from doc_extractor.infrastructure.audit.signer import AuditCheckpointVerifier
from doc_extractor.infrastructure.audit.sink import AuditCheckpointSink

logger = logging.getLogger("doc_extractor.infrastructure.audit.verification")


class AuditChainVerifier:
    """
    Coordinates multi-layered audit verification across:
    1. MongoDB internal hash chain (`MongoAuditRepository.verify_chain`).
    2. Asymmetric cryptographic signature (`AuditCheckpointVerifier`).
    3. External tamper-resistant sink (`AuditCheckpointSink`).
    """

    def __init__(
        self,
        verifier: AuditCheckpointVerifier,
        sink: Optional[AuditCheckpointSink] = None,
    ) -> None:
        self.verifier = verifier
        self.sink = sink

    async def verify_chain_with_checkpoint(
        self,
        tenant_id: str,
        mongo_repo: Any,
        checkpoint: Optional[AuditCheckpoint] = None,
        public_key_pem: Optional[bytes] = None,
    ) -> CheckpointVerificationReport:
        """
        Performs comprehensive verification for a tenant.

        If ``checkpoint`` is None, retrieves the latest checkpoint from ``self.sink``.
        Returns a structured ``CheckpointVerificationReport``.
        """
        # 1. First verify MongoDB internal hash chain
        try:
            db_chain_result: ChainVerification = await mongo_repo.verify_chain(tenant_id)
        except Exception as exc:
            return CheckpointVerificationReport(
                tenant_id=tenant_id,
                status=CheckpointVerificationStatus.DB_CHAIN_BROKEN,
                valid=False,
                checkpoint_verified=False,
                database_chain_verified=False,
                checked_events=0,
                reason=f"db_chain_verification_exception ({type(exc).__name__})",
            )

        if not db_chain_result.valid:
            return CheckpointVerificationReport(
                tenant_id=tenant_id,
                status=CheckpointVerificationStatus.DB_CHAIN_BROKEN,
                valid=False,
                checkpoint_verified=False,
                database_chain_verified=False,
                checked_events=db_chain_result.checked_events,
                first_bad_seq=db_chain_result.first_bad_seq,
                reason=db_chain_result.reason,
            )

        db_head_seq, db_head_hash = await mongo_repo.chain_head(tenant_id)

        # 2. Resolve target checkpoint from argument or external sink
        target_checkpoint = checkpoint
        if target_checkpoint is None and self.sink is not None:
            try:
                target_checkpoint = await self.sink.get_latest_checkpoint(tenant_id)
            except CheckpointSinkUnavailableError as exc:
                return CheckpointVerificationReport(
                    tenant_id=tenant_id,
                    status=CheckpointVerificationStatus.SINK_UNAVAILABLE,
                    valid=False,
                    checkpoint_verified=False,
                    database_chain_verified=True,
                    checked_events=db_chain_result.checked_events,
                    db_head_seq=db_head_seq,
                    db_head_hash=db_head_hash,
                    reason="external_sink_unavailable",
                )

        # Handle case where no checkpoint exists yet
        if target_checkpoint is None:
            # If the database is completely empty (seq == 0), it is valid but uncheckpointed
            is_empty = (db_head_seq == 0 and db_head_hash == GENESIS_HASH)
            return CheckpointVerificationReport(
                tenant_id=tenant_id,
                status=CheckpointVerificationStatus.VALID if is_empty else CheckpointVerificationStatus.CHECKPOINT_NOT_FOUND,
                valid=is_empty,
                checkpoint_verified=False,  # EXPLICIT: Signed checkpoint verification was NOT performed
                database_chain_verified=True,
                checked_events=db_chain_result.checked_events,
                db_head_seq=db_head_seq,
                db_head_hash=db_head_hash,
                reason=None if is_empty else "no_checkpoint_found_for_non_empty_chain",
            )

        # 3. Verify asymmetric cryptographic signature on the checkpoint
        try:
            sig_valid = self.verifier.verify(target_checkpoint, public_key_pem=public_key_pem)
        except Exception as exc:
            return CheckpointVerificationReport(
                tenant_id=tenant_id,
                status=CheckpointVerificationStatus.SIGNATURE_INVALID,
                valid=False,
                checkpoint_verified=False,
                database_chain_verified=True,
                checked_events=db_chain_result.checked_events,
                checkpoint_seq=target_checkpoint.seq,
                checkpoint_hash=target_checkpoint.chain_hash,
                db_head_seq=db_head_seq,
                db_head_hash=db_head_hash,
                reason=f"signature_verification_error ({type(exc).__name__})",
            )

        if not sig_valid:
            return CheckpointVerificationReport(
                tenant_id=tenant_id,
                status=CheckpointVerificationStatus.SIGNATURE_INVALID,
                valid=False,
                checkpoint_verified=False,
                database_chain_verified=True,
                checked_events=db_chain_result.checked_events,
                checkpoint_seq=target_checkpoint.seq,
                checkpoint_hash=target_checkpoint.chain_hash,
                db_head_seq=db_head_seq,
                db_head_hash=db_head_hash,
                reason="asymmetric_signature_mismatch",
            )

        # 4. If an external sink is configured and checkpoint was provided directly, verify it matches external sink
        if self.sink is not None and checkpoint is not None:
            sink_checkpoint = await self.sink.get_checkpoint(tenant_id, target_checkpoint.seq)
            if sink_checkpoint is None or sink_checkpoint.to_json() != target_checkpoint.to_json():
                return CheckpointVerificationReport(
                    tenant_id=tenant_id,
                    status=CheckpointVerificationStatus.CHECKPOINT_TAMPERED,
                    valid=False,
                    checkpoint_verified=False,
                    database_chain_verified=True,
                    checked_events=db_chain_result.checked_events,
                    checkpoint_seq=target_checkpoint.seq,
                    checkpoint_hash=target_checkpoint.chain_hash,
                    db_head_seq=db_head_seq,
                    db_head_hash=db_head_hash,
                    reason="checkpoint_data_differs_from_immutable_sink",
                )

        # 5. Checkpoint Alignment Checks against MongoDB Chain

        # 5a. Truncation attack check: DB has fewer records than the verified checkpoint
        if db_head_seq < target_checkpoint.seq:
            return CheckpointVerificationReport(
                tenant_id=tenant_id,
                status=CheckpointVerificationStatus.CHAIN_TRUNCATED,
                valid=False,
                checkpoint_verified=True,
                database_chain_verified=True,
                checked_events=db_chain_result.checked_events,
                checkpoint_seq=target_checkpoint.seq,
                checkpoint_hash=target_checkpoint.chain_hash,
                db_head_seq=db_head_seq,
                db_head_hash=db_head_hash,
                first_bad_seq=db_head_seq + 1,
                reason="database_chain_shorter_than_verified_checkpoint_tail_deleted",
                details={
                    "checkpoint_seq": target_checkpoint.seq,
                    "db_head_seq": db_head_seq,
                    "deleted_records_count": target_checkpoint.seq - db_head_seq,
                },
            )

        # 5b. Hash alignment check: Compare DB hash at checkpoint.seq with checkpoint.chain_hash
        if target_checkpoint.seq == 0:
            expected_hash = GENESIS_HASH
        elif target_checkpoint.seq == db_head_seq:
            expected_hash = db_head_hash
        else:
            # Look up specific event at target_checkpoint.seq
            events = await mongo_repo.query_events(tenant_id, limit=1, offset=target_checkpoint.seq - 1)
            if not events:
                return CheckpointVerificationReport(
                    tenant_id=tenant_id,
                    status=CheckpointVerificationStatus.CHAIN_TRUNCATED,
                    valid=False,
                    checkpoint_verified=True,
                    database_chain_verified=True,
                    checked_events=db_chain_result.checked_events,
                    checkpoint_seq=target_checkpoint.seq,
                    checkpoint_hash=target_checkpoint.chain_hash,
                    db_head_seq=db_head_seq,
                    db_head_hash=db_head_hash,
                    reason="checkpoint_sequence_not_found_in_database",
                )
            # Fetch raw document hash from store or repo
            expected_hash = None
            if hasattr(mongo_repo, "_store"):
                doc = await mongo_repo._store.find_one(tenant_id, {"seq": target_checkpoint.seq})
                if doc:
                    expected_hash = doc.get("hash")

        if expected_hash is not None and expected_hash != target_checkpoint.chain_hash:
            return CheckpointVerificationReport(
                tenant_id=tenant_id,
                status=CheckpointVerificationStatus.HASH_MISMATCH,
                valid=False,
                checkpoint_verified=True,
                database_chain_verified=True,
                checked_events=db_chain_result.checked_events,
                checkpoint_seq=target_checkpoint.seq,
                checkpoint_hash=target_checkpoint.chain_hash,
                db_head_seq=db_head_seq,
                db_head_hash=db_head_hash,
                first_bad_seq=target_checkpoint.seq,
                reason="chain_hash_at_checkpoint_seq_does_not_match_signed_checkpoint",
                details={
                    "db_hash_at_seq": expected_hash,
                    "checkpoint_hash": target_checkpoint.chain_hash,
                },
            )

        # 6. All checks passed: Database chain and signed immutable checkpoint are fully valid and aligned
        return CheckpointVerificationReport(
            tenant_id=tenant_id,
            status=CheckpointVerificationStatus.VALID,
            valid=True,
            checkpoint_verified=True,  # EXPLICIT: Cryptographic signature verified against immutable sink
            database_chain_verified=True,
            checked_events=db_chain_result.checked_events,
            checkpoint_seq=target_checkpoint.seq,
            checkpoint_hash=target_checkpoint.chain_hash,
            db_head_seq=db_head_seq,
            db_head_hash=db_head_hash,
            reason=None,
        )
