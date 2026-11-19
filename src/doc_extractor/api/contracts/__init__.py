"""
API Contracts package.
"""

from doc_extractor.api.contracts.documents import SCOPE_DOCUMENTS_READ, DocumentDetailResponse, validate_document_id
from doc_extractor.api.contracts.errors import ErrorDetail, ErrorEnvelope
from doc_extractor.api.contracts.records import (
    ApiKeyCreateRequest,
    ApiKeyCreationResult,
    ApiKeyRecord,
    ApiKeyRevokeResponse,
    ApiKeySummary,
    AuditEventRecord,
    DocumentProcessingMessage,
    DocumentResource,
    QueueEnvelope,
    ReviewTaskRecord,
    WebhookDeliveryRecord,
    WebhookSubscriptionRecord,
    utc_now,
)

__all__ = [
    "ErrorDetail",
    "ErrorEnvelope",
    "SCOPE_DOCUMENTS_READ",
    "DocumentDetailResponse",
    "validate_document_id",
    "ApiKeyCreateRequest",
    "ApiKeyCreationResult",
    "ApiKeyRecord",
    "ApiKeyRevokeResponse",
    "ApiKeySummary",
    "AuditEventRecord",
    "DocumentProcessingMessage",
    "DocumentResource",
    "QueueEnvelope",
    "ReviewTaskRecord",
    "WebhookDeliveryRecord",
    "WebhookSubscriptionRecord",
    "utc_now",
]
