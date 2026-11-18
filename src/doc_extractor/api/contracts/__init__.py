"""
API Contracts package.
"""

from doc_extractor.api.contracts.errors import ErrorDetail, ErrorEnvelope
from doc_extractor.api.contracts.records import (
    ApiKeyCreateRequest,
    ApiKeyCreationResult,
    ApiKeyRecord,
    ApiKeyRevokeResponse,
    ApiKeySummary,
    AuditEventRecord,
    DocumentProcessingMessage,
    DocumentRecord,
    QueueEnvelope,
    ReviewTaskRecord,
    WebhookDeliveryRecord,
    WebhookSubscriptionRecord,
    utc_now,
)

__all__ = [
    "ErrorDetail",
    "ErrorEnvelope",
    "ApiKeyCreateRequest",
    "ApiKeyCreationResult",
    "ApiKeyRecord",
    "ApiKeyRevokeResponse",
    "ApiKeySummary",
    "AuditEventRecord",
    "DocumentProcessingMessage",
    "DocumentRecord",
    "QueueEnvelope",
    "ReviewTaskRecord",
    "WebhookDeliveryRecord",
    "WebhookSubscriptionRecord",
    "utc_now",
]
