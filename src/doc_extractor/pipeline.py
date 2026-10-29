"""
End-to-End Module A Coordinator.

Integrates the full Stage 1 pipeline:
Validation -> Malware Scanning -> Content Hashing -> PDF Rendering (200 DPI)
-> Image Preprocessing (Deskew, Orientation, CLAHE) -> Storage & Metadata
-> Serialization to ModuleAHandoffPayload (ARC-003, SCH-013).
"""

from datetime import datetime, timezone
from pathlib import Path
import time
from typing import BinaryIO, Dict, List, Optional, Union
import uuid

from doc_extractor.config import Settings
from doc_extractor.exceptions import DocumentExtractorError, DocumentProcessingError
from doc_extractor.handoff import (
    MODULE_A_CONTRACT_VERSION,
    ModuleAHandoffPayload,
    PageHandoffRecord,
    QueueAdapter,
)
from doc_extractor.idempotency import IdempotencyStore, LocalIdempotencyStore
from doc_extractor.ingestion import IngestionResult, IngestionService, ProcessingStatus
from doc_extractor.logging import setup_logging
from doc_extractor.malware import MalwareScanner, UnconfiguredScanner
from doc_extractor.models import DocumentStatus, IngestionState
from doc_extractor.preprocessing import PagePreprocessor, PreprocessingResult
from doc_extractor.renderer import DocumentRenderResult, PdfRenderer, RenderedPage
from doc_extractor.storage import LocalDiskStorage, StorageBackend


class ModuleAPipeline:
    """
    Unified end-to-end coordinator for Module A: Document Ingestion & Preprocessing.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        scanner: Optional[MalwareScanner] = None,
        storage: Optional[StorageBackend] = None,
        idempotency_store: Optional[IdempotencyStore] = None,
        queue_adapter: Optional[QueueAdapter] = None,
        tenant_id: str = "default_tenant",
    ) -> None:
        self.settings = settings or Settings()
        self.tenant_id = tenant_id
        self.scanner = scanner or UnconfiguredScanner()
        self.storage = storage or LocalDiskStorage(self.settings.storage_base_dir, tenant_id=self.tenant_id)
        self.idempotency_store = idempotency_store or LocalIdempotencyStore(self.settings.storage_base_dir)
        self.queue_adapter = queue_adapter
        self.logger = setup_logging(
            level=self.settings.log_level,
            service_name=self.settings.service_name,
            environment=self.settings.app_env,
        )

        self.ingestion_service = IngestionService(
            settings=self.settings,
            scanner=self.scanner,
            storage=self.storage,
            idempotency_store=self.idempotency_store,
            tenant_id=self.tenant_id,
        )
        self.renderer = PdfRenderer(settings=self.settings, storage=self.storage)
        self.preprocessor = PagePreprocessor(settings=self.settings, storage=self.storage)

    def process_document(
        self,
        file_path_or_stream: Union[str, Path, BinaryIO],
        claimed_filename: str,
        request_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> ModuleAHandoffPayload:
        """
        Executes the full Stage 1 pipeline:
        1. Ingest, validate magic bytes, compute SHA-256 hash, and check idempotency.
        2. Scan for malware (fails closed on detection or error).
        3. If PDF, render each page at 200 DPI via pypdfium2.
           If single image (PNG/JPG/TIFF), store as original page 1.
        4. Preprocess each page (deskew, orientation, CLAHE) and persist sidecar metadata.
        5. Assemble and optionally publish ModuleAHandoffPayload.
        """
        req_id = request_id or str(uuid.uuid4())
        r_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).isoformat()

        # Step 1 & 2: Ingestion & Malware Gating
        ingest_res = self.ingestion_service.ingest_document(
            file_path_or_stream=file_path_or_stream,
            claimed_filename=claimed_filename,
            request_id=req_id,
            idempotency_key=idempotency_key,
        )

        # If rejected or failed at ingestion stage, assemble terminal payload
        if not ingest_res.is_accepted:
            completed_at = datetime.now(timezone.utc).isoformat()
            # SCH-012 mapping: if rejected_malware or rejected_unsupported, external status is 'rejected'
            external_status = (
                DocumentStatus.REJECTED.value
                if "rejected" in ingest_res.status.value
                else DocumentStatus.FAILED.value
            )
            return ModuleAHandoffPayload(
                contract_version=MODULE_A_CONTRACT_VERSION,
                run_id=r_id,
                document_id=ingest_res.document_id,
                tenant_id=self.tenant_id,
                status=external_status,
                internal_state=ingest_res.internal_state.value,
                claimed_filename=ingest_res.filename,
                file_hash_sha256=ingest_res.file_hash_sha256 or "",
                file_size_bytes=ingest_res.file_size_bytes,
                page_count=ingest_res.page_count,
                mime_type=ingest_res.mime_type or "application/octet-stream",
                pages=[],
                created_at=created_at,
                completed_at=completed_at,
                error=ingest_res.error,
            )

        doc_id = ingest_res.document_id

        # Step 3: Page Rendering / Ingestion to 200 DPI
        try:
            # Re-open stream or pass path
            if isinstance(file_path_or_stream, (str, Path)):
                render_input = file_path_or_stream
            else:
                file_path_or_stream.seek(0)
                render_input = file_path_or_stream

            if ingest_res.mime_type == "application/pdf":
                render_result = self.renderer.render_pdf(render_input, document_id=doc_id)
                rendered_pages = render_result.pages
            else:
                # Standalone image (PNG, JPEG, TIFF): store directly as page 1
                if isinstance(render_input, (str, Path)):
                    with open(render_input, "rb") as f:
                        img_bytes = f.read()
                else:
                    render_input.seek(0)
                    img_bytes = render_input.read()

                stored = self.storage.store_page_image(
                    document_id=doc_id,
                    page_number=1,
                    image_data=img_bytes,
                    format_extension=Path(claimed_filename).suffix.lstrip(".").lower() or "png",
                )
                import cv2
                import numpy as np
                decoded = cv2.imdecode(np.frombuffer(img_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
                h, w = decoded.shape[:2] if decoded is not None else (0, 0)

                rendered_pages = [
                    RenderedPage(
                        document_id=doc_id,
                        page_number=1,
                        width_px=w,
                        height_px=h,
                        dpi=self.settings.target_render_dpi,
                        image_format="PNG",
                        image_path=stored.file_path,
                        file_size_bytes=stored.file_size_bytes,
                    )
                ]

            # Step 4: Page Preprocessing
            prep_results = self.preprocessor.preprocess_document_pages(
                document_id=doc_id,
                rendered_pages=rendered_pages,
            )

            # Step 5: Build Handoff Page Records
            page_records: List[PageHandoffRecord] = []
            for prep in prep_results:
                meta = prep.metadata
                p_num = prep.page_number
                orig_art_id = f"page_{p_num:04d}.png"
                prep_art_id = f"page_{p_num:04d}_preprocessed.png"
                meta_art_id = f"page_{p_num:04d}_metadata.json"

                # Logical storage URIs relative to tenant root: {tenant_id}/{doc_id}/...
                orig_uri = f"{self.tenant_id}/{doc_id}/pages/{orig_art_id}"
                prep_uri = f"{self.tenant_id}/{doc_id}/preprocessed/{prep_art_id}"
                meta_uri = f"{self.tenant_id}/{doc_id}/preprocessed/{meta_art_id}"

                page_records.append(
                    PageHandoffRecord(
                        page_number=p_num,
                        original_artifact_id=orig_art_id,
                        preprocessed_artifact_id=prep_art_id,
                        original_storage_uri=orig_uri,
                        preprocessed_storage_uri=prep_uri,
                        original_width_px=meta.original_width_px,
                        original_height_px=meta.original_height_px,
                        preprocessed_width_px=meta.output_width_px,
                        preprocessed_height_px=meta.output_height_px,
                        dpi=meta.dpi,
                        applied_rotation_deg=meta.applied_rotation_deg,
                        applied_deskew_deg=meta.applied_deskew_deg,
                        residual_skew_deg=meta.residual_skew_deg,
                        contrast_method=meta.contrast_method,
                        metadata_storage_uri=meta_uri,
                    )
                )

            completed_at = datetime.now(timezone.utc).isoformat()
            payload = ModuleAHandoffPayload(
                contract_version=MODULE_A_CONTRACT_VERSION,
                run_id=r_id,
                document_id=doc_id,
                tenant_id=self.tenant_id,
                status=DocumentStatus.PROCESSING.value,
                internal_state=IngestionState.READY_FOR_EXTRACTION.value,
                claimed_filename=ingest_res.filename,
                file_hash_sha256=ingest_res.file_hash_sha256 or "",
                file_size_bytes=ingest_res.file_size_bytes,
                page_count=len(page_records),
                mime_type=ingest_res.mime_type or "application/pdf",
                pages=page_records,
                created_at=created_at,
                completed_at=completed_at,
            )

            # Step 6: Publish message if queue adapter attached
            if self.queue_adapter is not None:
                self.queue_adapter.publish_message("module_a.completed", payload)

            return payload

        except Exception as exc:
            self.logger.error(
                "Document processing pipeline failed",
                extra={
                    "request_id": req_id,
                    "document_id": doc_id,
                    "extra_data": {"error": str(exc)},
                },
            )
            # Ensure failed status and rollback
            self.storage.cleanup_preprocessed(doc_id)
            completed_at = datetime.now(timezone.utc).isoformat()
            err_dict = (
                exc.to_envelope()["error"]
                if isinstance(exc, DocumentExtractorError)
                else {"code": "PROCESSING_FAILED", "message": str(exc)}
            )
            return ModuleAHandoffPayload(
                contract_version=MODULE_A_CONTRACT_VERSION,
                run_id=r_id,
                document_id=doc_id,
                tenant_id=self.tenant_id,
                status=DocumentStatus.FAILED.value,
                internal_state=IngestionState.FAILED.value,
                claimed_filename=ingest_res.filename,
                file_hash_sha256=ingest_res.file_hash_sha256 or "",
                file_size_bytes=ingest_res.file_size_bytes,
                page_count=ingest_res.page_count,
                mime_type=ingest_res.mime_type or "application/pdf",
                pages=[],
                created_at=created_at,
                completed_at=completed_at,
                error=err_dict,
            )
