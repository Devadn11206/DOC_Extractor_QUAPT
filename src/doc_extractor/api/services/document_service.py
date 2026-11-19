"""
Document service boundary for the REST API (Module M5 / Phase 2).

Establishes the application service boundary between API endpoints/transports
and tenant-isolated document processing pipelines.
"""

from typing import Any, Iterator, Optional

from doc_extractor.api.contracts.tenant_bound import require_path_safe_tenant_id
from doc_extractor.api.ports.audit import AuditLogger
from doc_extractor.api.ports.queue import QueueProducer
from doc_extractor.api.ports.repositories import DocumentMetadataRepository
from doc_extractor.exceptions import InvalidTenantIdentifierError, TenantMismatchError
from doc_extractor.processing_service import (
    DocumentProcessingRequest,
    DocumentProcessingResult,
    DocumentProcessingService,
    ProcessingProgressEvent,
)


class DocumentService:
    """
    Tenant-bound application service boundary for document extraction.
    Coordinates between HTTP/queue transports and the domain DocumentProcessingService.
    """

    def __init__(
        self,
        processing_service: DocumentProcessingService,
        tenant_id: str,
        document_metadata_repo: Optional[DocumentMetadataRepository] = None,
        queue_producer: Optional[QueueProducer] = None,
        audit_logger: Optional[AuditLogger] = None,
    ) -> None:
        if not isinstance(tenant_id, str):
            raise InvalidTenantIdentifierError("Tenant ID must be a string.")
        if not tenant_id:
            raise InvalidTenantIdentifierError("Tenant ID cannot be empty.")
        if not tenant_id.strip():
            raise InvalidTenantIdentifierError("Tenant ID cannot be whitespace.")
        require_path_safe_tenant_id(tenant_id)

        if processing_service.tenant_id != tenant_id:
            raise TenantMismatchError(
                message="Tenant mismatch: processing service belongs to another tenant."
            )

        self.processing_service = processing_service
        self.tenant_id = tenant_id
        self.document_metadata_repo = document_metadata_repo
        self.queue_producer = queue_producer
        self.audit_logger = audit_logger

    def process_document(
        self,
        request: DocumentProcessingRequest,
        **kwargs: Any,
    ) -> DocumentProcessingResult:
        """
        Execute document processing synchronously via the domain processing service.
        Enforces tenant context alignment.
        """
        if request.tenant_id != self.tenant_id:
            raise TenantMismatchError(
                message="Tenant mismatch: request tenant does not match service tenant."
            )
        return self.processing_service.process_document(request, **kwargs)

    def process_document_stream(
        self,
        request: DocumentProcessingRequest,
        **kwargs: Any,
    ) -> Iterator[ProcessingProgressEvent]:
        """
        Stream progress events during document processing.
        Enforces tenant context alignment.
        """
        if request.tenant_id != self.tenant_id:
            raise TenantMismatchError(
                message="Tenant mismatch: request tenant does not match service tenant."
            )
        return self.processing_service.process_document_stream(request, **kwargs)

    def process_stored_page(
        self,
        document_id: str,
        page_number: int,
        tenant_id: Optional[str] = None,
        **kwargs: Any,
    ) -> DocumentProcessingResult:
        """
        Execute single-page processing for a previously stored document page.
        Enforces tenant context alignment.
        """
        if not isinstance(document_id, str) or isinstance(document_id, bool) or not document_id.strip():
            raise ValueError("Document ID is required and must be a non-empty string.")

        if "tenant_id" in kwargs:
            kw_tenant = kwargs.pop("tenant_id")
            if kw_tenant != self.tenant_id:
                raise TenantMismatchError(
                    message="Tenant mismatch: request tenant does not match service tenant."
                )

        if tenant_id is not None and tenant_id != self.tenant_id:
            raise TenantMismatchError(
                message="Tenant mismatch: request tenant does not match service tenant."
            )

        return self.processing_service.process_stored_page(
            document_id=document_id,
            page_number=page_number,
            tenant_id=self.tenant_id,
            **kwargs,
        )

    def process_stored_page_stream(
        self,
        document_id: str,
        page_number: int,
        tenant_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Iterator[ProcessingProgressEvent]:
        """
        Stream progress events during single-page processing for a previously stored document page.
        Enforces tenant context alignment.
        """
        if not isinstance(document_id, str) or isinstance(document_id, bool) or not document_id.strip():
            raise ValueError("Document ID is required and must be a non-empty string.")

        if "tenant_id" in kwargs:
            kw_tenant = kwargs.pop("tenant_id")
            if kw_tenant != self.tenant_id:
                raise TenantMismatchError(
                    message="Tenant mismatch: request tenant does not match service tenant."
                )

        if tenant_id is not None and tenant_id != self.tenant_id:
            raise TenantMismatchError(
                message="Tenant mismatch: request tenant does not match service tenant."
            )

        return self.processing_service.process_stored_page_stream(
            document_id=document_id,
            page_number=page_number,
            tenant_id=self.tenant_id,
            **kwargs,
        )
