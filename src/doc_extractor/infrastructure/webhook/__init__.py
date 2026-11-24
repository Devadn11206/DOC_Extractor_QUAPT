"""
Webhook infrastructure package exports (Team Member 3, Phase 5C).
"""

from doc_extractor.infrastructure.webhook.dispatcher import WebhookDispatcher
from doc_extractor.infrastructure.webhook.security import (
    EphemeralSecretResolver,
    WebhookDestinationError,
    WebhookSecretResolver,
    validate_webhook_destination,
)

__all__ = [
    "EphemeralSecretResolver",
    "WebhookDestinationError",
    "WebhookDispatcher",
    "WebhookSecretResolver",
    "validate_webhook_destination",
]
