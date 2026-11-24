"""
Operational audit checkpoint service (Team Member 3, Phase 4C).

Coordinates:
- Generation of signed checkpoints from MongoDB chain head.
- Asymmetric signing via KMS or development double.
- Durable persistence to tamper-resistant external WORM storage.
- Multi-layered verification across database records and signed checkpoints.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

from doc_extractor.infrastructure.audit.models import (
    AuditCheckpoint,
    CheckpointVerificationReport,
)
from doc_extractor.infrastructure.audit.signer import (
    AuditCheckpointSigner,
    AuditCheckpointVerifier,
    IndependentCheckpointVerifier,
)
from doc_extractor.infrastructure.audit.sink import AuditCheckpointSink
from doc_extractor.infrastructure.audit.verification import AuditChainVerifier
from doc_extractor.tenancy import require_path_safe_tenant_id

logger = logging.getLogger("doc_extractor.infrastructure.audit.service")


class AuditCheckpointService:
    """
    High-level operational service managing audit checkpoint creation, external storage, and verification.
    """

    def __init__(
        self,
        mongo_repo: Any,
        signer: AuditCheckpointSigner,
        sink: AuditCheckpointSink,
        verifier: Optional[AuditCheckpointVerifier] = None,
    ) -> None:
        self.mongo_repo = mongo_repo
        self.signer = signer
        self.sink = sink
        self.verifier = verifier or IndependentCheckpointVerifier(
            public_key_resolver=lambda kid, kver: self.signer.get_public_key(kid, kver)
        )
        self.chain_verifier = AuditChainVerifier(verifier=self.verifier, sink=self.sink)

    async def create_checkpoint(self, tenant_id: str) -> AuditCheckpoint:
        """
        Generates and signs a checkpoint from the tenant's current MongoDB audit chain head,
        and durably records it to the external immutable sink.
        """
        require_path_safe_tenant_id(tenant_id)

        # 1. Fetch current database head
        head_seq, head_hash = await self.mongo_repo.chain_head(tenant_id)

        # 2. Build unsigned checkpoint
        now_utc = datetime.now(timezone.utc)
        unsigned_checkpoint = AuditCheckpoint(
            tenant_id=tenant_id,
            seq=head_seq,
            chain_hash=head_hash,
            timestamp=now_utc,
            key_id=self.signer.get_key_id(),
            key_version=self.signer.get_key_version(),
        )

        # 3. Sign checkpoint with KMS asymmetric key
        signed_checkpoint = self.signer.sign(unsigned_checkpoint)

        # 4. Store to external immutable sink (fails closed on sink errors)
        await self.sink.store_checkpoint(signed_checkpoint)

        logger.info(
            "Audit checkpoint created: tenant=%s seq=%d key=%s#%s",
            tenant_id,
            head_seq,
            signed_checkpoint.key_id,
            signed_checkpoint.key_version,
        )

        return signed_checkpoint

    async def verify(
        self,
        tenant_id: str,
        public_key_pem: Optional[bytes] = None,
    ) -> CheckpointVerificationReport:
        """
        Runs independent verification for the tenant's audit chain against external signed checkpoints.
        """
        require_path_safe_tenant_id(tenant_id)
        return await self.chain_verifier.verify_chain_with_checkpoint(
            tenant_id=tenant_id,
            mongo_repo=self.mongo_repo,
            checkpoint=None,  # Resolves latest from sink
            public_key_pem=public_key_pem,
        )

    async def get_latest_checkpoint(self, tenant_id: str) -> Optional[AuditCheckpoint]:
        require_path_safe_tenant_id(tenant_id)
        return await self.sink.get_latest_checkpoint(tenant_id)

    async def list_checkpoints(self, tenant_id: str, limit: int = 100) -> List[AuditCheckpoint]:
        require_path_safe_tenant_id(tenant_id)
        return await self.sink.list_checkpoints(tenant_id, limit=limit)
