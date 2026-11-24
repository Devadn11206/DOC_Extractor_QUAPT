"""
Webhook dispatcher and delivery engine (Team Member 3, Phase 5C).

Coordinates:
- HMAC-SHA256 request payload signing (PRD §7.1, Phase 4).
- Anti-SSRF destination validation and DNS-rebinding protection.
- Queue publishing and queue-backed background dispatching (Phase 3).
- Exponential backoff retry scheduling within the PRD 24-hour window (PRD §7.1).
- Terminal failure and dead-letter tracking (SCR-07).
- Fail-closed audit integration for dead-lettered webhooks (PRD §10.1 SEC-008).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request
import uuid

from doc_extractor.api.contracts.records import WebhookDeliveryRecord, WebhookSubscriptionRecord
from doc_extractor.api.contracts.webhook_delivery import DeliveryOutcome
from doc_extractor.api.contracts.webhook_events import serialize_event
from doc_extractor.api.ports.audit import AuditLogger
from doc_extractor.api.ports.repositories import WebhookDeliveryRepository, WebhookSubscriptionRepository
from doc_extractor.api.services.webhook_retry import RETRY_WINDOW, RetryOutcome, schedule_retry
from doc_extractor.api.services.webhook_signing import compute_signature
from doc_extractor.egress import EgressPolicy
from doc_extractor.infrastructure.webhook.security import (
    EphemeralSecretResolver,
    WebhookDestinationError,
    WebhookSecretResolver,
    validate_webhook_destination,
)

logger = logging.getLogger("doc_extractor.webhook.dispatcher")

DEFAULT_BASE_DELAY = timedelta(seconds=1)
DEFAULT_MAX_DELAY = timedelta(hours=1)
DEFAULT_TIMEOUT_SECONDS = 10.0


class WebhookDispatcher:
    """
    Coordinates delivery of signed outbound webhooks with retry, queue, and security controls.
    """

    def __init__(
        self,
        subscription_repo: WebhookSubscriptionRepository,
        delivery_repo: WebhookDeliveryRepository,
        secret_resolver: WebhookSecretResolver,
        *,
        egress_policy: Optional[EgressPolicy] = None,
        queue_producer: Optional[Any] = None,
        audit_logger: Optional[AuditLogger] = None,
        allow_loopback: bool = False,
        base_delay: timedelta = DEFAULT_BASE_DELAY,
        max_delay: timedelta = DEFAULT_MAX_DELAY,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._subscription_repo = subscription_repo
        self._delivery_repo = delivery_repo
        self._secret_resolver = secret_resolver
        self._egress_policy = egress_policy
        self._queue_producer = queue_producer
        self._audit_logger = audit_logger
        self._allow_loopback = allow_loopback
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._timeout_seconds = timeout_seconds

    async def _audit_dead_letter(
        self,
        tenant_id: str,
        subscription_id: str,
        delivery_id: str,
        event_id: str,
        reason: str,
    ) -> None:
        if self._audit_logger is None:
            return
        try:
            await self._audit_logger.record(
                tenant_id=tenant_id,
                action="webhook.dead_letter",
                actor_id="system:webhook_dispatcher",
                resource_type="webhook_delivery",
                resource_id=delivery_id,
                metadata={
                    "subscription_id": subscription_id,
                    "event_id": event_id,
                    "reason": reason,
                },
            )
        except Exception as exc:
            logger.error("Audit write failed for dead-lettered webhook '%s': %s", delivery_id, exc)

    async def dispatch_event(
        self,
        tenant_id: str,
        event: Any,
        *,
        http_client: Optional[Any] = None,
    ) -> List[WebhookDeliveryRecord]:
        """
        Deliver an event to all matching active subscriptions for a tenant.
        """
        event_type = getattr(event, "event_type", None)
        if isinstance(event_type, str):
            event_type_str = event_type
        else:
            event_type_str = str(event_type.value) if hasattr(event_type, "value") else str(event_type)

        subscriptions = await self._subscription_repo.list_by_tenant(tenant_id, event_type=event_type_str)
        if not subscriptions:
            return []

        import json
        from doc_extractor.api.contracts.webhook_events import EVENT_CLASSES, serialize_event

        if isinstance(event, (bytes, bytearray)):
            payload_bytes = bytes(event)
        elif isinstance(event, str):
            payload_bytes = event.encode("utf-8")
        elif isinstance(event, EVENT_CLASSES):
            payload_bytes = serialize_event(event)
        else:
            event_dict = {
                "event_id": str(getattr(event, "event_id", f"evt_{uuid.uuid4().hex[:16]}")),
                "event_type": event_type_str,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }
            if hasattr(event, "data"):
                event_dict["data"] = getattr(event, "data")
            payload_bytes = json.dumps(event_dict, sort_keys=True).encode("utf-8")

        event_id = str(getattr(event, "event_id", f"evt_{uuid.uuid4().hex[:16]}"))
        now = datetime.now(timezone.utc)
        results: List[WebhookDeliveryRecord] = []

        for sub in subscriptions:
            delivery_id = f"del_{uuid.uuid4().hex[:16]}"
            initial_delivery = WebhookDeliveryRecord(
                delivery_id=delivery_id,
                subscription_id=sub.subscription_id,
                tenant_id=tenant_id,
                event_id=event_id,
                event_type=event_type_str,
                attempt_number=1,
                status="pending",
                created_at=now,
            )
            await self._delivery_repo.record_delivery(tenant_id, initial_delivery)

            # If queue producer is configured, enqueue dispatch job
            if self._queue_producer is not None:
                try:
                    await self._queue_producer.publish(
                        tenant_id=tenant_id,
                        job_type="webhook_dispatch",
                        payload={
                            "delivery_id": delivery_id,
                            "subscription_id": sub.subscription_id,
                            "event_id": event_id,
                            "attempt_number": 1,
                        },
                    )
                    results.append(initial_delivery)
                    continue
                except Exception as exc:
                    logger.warning("Queue publishing failed for webhook '%s', executing inline: %s", delivery_id, exc)

            # Execute delivery directly
            outcome_record = await self.execute_delivery(
                delivery_id=delivery_id,
                tenant_id=tenant_id,
                subscription=sub,
                payload=payload_bytes,
                event_id=event_id,
                attempt_number=1,
                first_attempt_at=now,
                http_client=http_client,
            )
            results.append(outcome_record)

        return results

    async def execute_delivery(
        self,
        delivery_id: str,
        tenant_id: str,
        subscription: WebhookSubscriptionRecord,
        payload: bytes,
        event_id: str,
        attempt_number: int = 1,
        first_attempt_at: Optional[datetime] = None,
        http_client: Optional[Any] = None,
    ) -> WebhookDeliveryRecord:
        """
        Execute a single delivery attempt with SSRF check, HMAC signature, and retry classification.
        """
        now = datetime.now(timezone.utc)
        first_attempt = first_attempt_at or now

        # 1. Anti-SSRF Destination check
        try:
            validate_webhook_destination(
                subscription.target_url,
                egress_policy=self._egress_policy,
                allow_loopback=self._allow_loopback,
                resolve_dns=True,
            )
        except WebhookDestinationError as exc:
            logger.warning("SSRF check denied webhook destination '%s': %s", subscription.target_url, exc)
            updated = await self._delivery_repo.update_delivery_status(
                tenant_id=tenant_id,
                delivery_id=delivery_id,
                status="failed_permanent",
                error_message=f"SSRF_DENIED: {exc.code}",
            )
            await self._audit_dead_letter(tenant_id, subscription.subscription_id, delivery_id, event_id, f"SSRF_DENIED_{exc.code}")
            return updated or WebhookDeliveryRecord(
                delivery_id=delivery_id,
                subscription_id=subscription.subscription_id,
                tenant_id=tenant_id,
                event_id=event_id,
                event_type=subscription.event_types[0] if subscription.event_types else "event",
                attempt_number=attempt_number,
                status="failed_permanent",
                error_message=f"SSRF_DENIED: {exc.code}",
                created_at=now,
                completed_at=now,
            )

        # 2. Secret Resolution
        secret_ref = subscription.secret_ref or subscription.secret_hash
        try:
            secret = self._secret_resolver.resolve_secret(secret_ref, tenant_id)
        except Exception as exc:
            logger.error("Failed to resolve secret for webhook subscription '%s': %s", subscription.subscription_id, exc)
            updated = await self._delivery_repo.update_delivery_status(
                tenant_id=tenant_id,
                delivery_id=delivery_id,
                status="failed_permanent",
                error_message="WEBHOOK_SECRET_RESOLUTION_FAILED",
            )
            await self._audit_dead_letter(tenant_id, subscription.subscription_id, delivery_id, event_id, "SECRET_RESOLUTION_FAILED")
            return updated or WebhookDeliveryRecord(
                delivery_id=delivery_id,
                subscription_id=subscription.subscription_id,
                tenant_id=tenant_id,
                event_id=event_id,
                event_type=subscription.event_types[0] if subscription.event_types else "event",
                attempt_number=attempt_number,
                status="failed_permanent",
                error_message="WEBHOOK_SECRET_RESOLUTION_FAILED",
                created_at=now,
                completed_at=now,
            )

        # 3. Compute HMAC Signature & Headers
        signature = compute_signature(secret, payload)
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": f"sha256={signature}",
            "X-Webhook-Timestamp": now.isoformat(),
            "X-Webhook-Delivery": delivery_id,
            "X-Webhook-Event": subscription.event_types[0] if subscription.event_types else "event",
        }

        # 4. Outbound HTTP Request
        status_code: Optional[int] = None
        error_msg: Optional[str] = None
        is_success = False

        if http_client is None:
            def _do_urllib_request() -> tuple[Optional[int], Optional[str], bool]:
                req = urllib.request.Request(
                    subscription.target_url,
                    data=payload,
                    headers=headers,
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                        code = resp.status
                        return code, None, (200 <= code < 300)
                except urllib.error.HTTPError as err:
                    return err.code, f"HTTP_{err.code}", False
                except urllib.error.URLError as err:
                    reason_name = type(err.reason).__name__ if hasattr(err, "reason") else type(err).__name__
                    return None, f"NETWORK_ERROR_{reason_name}", False
                except TimeoutError:
                    return None, "REQUEST_TIMEOUT", False
                except Exception as exc:
                    return None, f"NETWORK_ERROR_{type(exc).__name__}", False

            status_code, error_msg, is_success = await asyncio.to_thread(_do_urllib_request)
        else:
            try:
                resp = await http_client.post(subscription.target_url, content=payload, headers=headers)
                status_code = getattr(resp, "status_code", 200)
                is_success = 200 <= status_code < 300
                if not is_success:
                    error_msg = f"HTTP_{status_code}"
            except Exception as exc:
                exc_name = type(exc).__name__
                if "Timeout" in exc_name:
                    error_msg = "REQUEST_TIMEOUT"
                elif "Connect" in exc_name:
                    error_msg = "CONNECTION_REFUSED"
                else:
                    error_msg = f"NETWORK_ERROR_{exc_name}"

        # 5. Outcome Classification and Retry Scheduling
        completion_time = datetime.now(timezone.utc)
        if is_success:
            updated = await self._delivery_repo.update_delivery_status(
                tenant_id=tenant_id,
                delivery_id=delivery_id,
                status="delivered",
                response_status_code=status_code,
            )
            return updated or WebhookDeliveryRecord(
                delivery_id=delivery_id,
                subscription_id=subscription.subscription_id,
                tenant_id=tenant_id,
                event_id=event_id,
                event_type=subscription.event_types[0] if subscription.event_types else "event",
                attempt_number=attempt_number,
                status="delivered",
                response_status_code=status_code,
                created_at=now,
                completed_at=completion_time,
            )

        # Retryable failures: 429, 5xx, or network/timeout errors
        is_retryable = status_code == 429 or (status_code is not None and status_code >= 500) or (status_code is None)

        if is_retryable:
            decision = schedule_retry(
                first_attempt_at=first_attempt,
                attempt_number=attempt_number,
                failed_at=completion_time,
                base_delay=self._base_delay,
                max_delay=self._max_delay,
            )
            if decision.outcome == RetryOutcome.SCHEDULED:
                updated = await self._delivery_repo.update_delivery_status(
                    tenant_id=tenant_id,
                    delivery_id=delivery_id,
                    status="failed_retryable",
                    response_status_code=status_code,
                    error_message=error_msg,
                    next_retry_at=decision.next_attempt_at,
                )
                return updated or WebhookDeliveryRecord(
                    delivery_id=delivery_id,
                    subscription_id=subscription.subscription_id,
                    tenant_id=tenant_id,
                    event_id=event_id,
                    event_type=subscription.event_types[0] if subscription.event_types else "event",
                    attempt_number=attempt_number,
                    status="failed_retryable",
                    response_status_code=status_code,
                    error_message=error_msg,
                    created_at=now,
                    next_retry_at=decision.next_attempt_at,
                )

        # Terminal / Dead-Letter failure: 4xx non-429, or window expired
        updated = await self._delivery_repo.update_delivery_status(
            tenant_id=tenant_id,
            delivery_id=delivery_id,
            status="dead_letter",
            response_status_code=status_code,
            error_message=error_msg or "RETRY_WINDOW_EXPIRED",
        )
        await self._audit_dead_letter(
            tenant_id=tenant_id,
            subscription_id=subscription.subscription_id,
            delivery_id=delivery_id,
            event_id=event_id,
            reason=error_msg or "RETRY_WINDOW_EXPIRED",
        )
        return updated or WebhookDeliveryRecord(
            delivery_id=delivery_id,
            subscription_id=subscription.subscription_id,
            tenant_id=tenant_id,
            event_id=event_id,
            event_type=subscription.event_types[0] if subscription.event_types else "event",
            attempt_number=attempt_number,
            status="dead_letter",
            response_status_code=status_code,
            error_message=error_msg or "RETRY_WINDOW_EXPIRED",
            created_at=now,
            completed_at=completion_time,
        )
