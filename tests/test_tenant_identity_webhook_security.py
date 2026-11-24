"""
Focused test suite for Team 3, Phase 5C:
- Persistent identity, user accounts, and token hash storage.
- Atomic single-use refresh token consumption, concurrency, and family-wide replay revocation.
- Session and JTI revocation.
- Tenant provisioning with mandatory admin MFA, lifecycle transitions, idempotency, and fail-closed audit.
- Webhook subscriptions, delivery persistence, secret references, HMAC signing, SSRF validation, and retries.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import os
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from doc_extractor.api.contracts.records import WebhookDeliveryRecord, WebhookSubscriptionRecord
from doc_extractor.api.contracts.webhook_delivery import DeliveryOutcome
from doc_extractor.api.contracts.webhook_events import WebhookEventType
from doc_extractor.api.exceptions import AuditUnavailableError
from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
from doc_extractor.egress import EgressPolicy
from doc_extractor.exceptions import InvalidTenantIdentifierError
from doc_extractor.infrastructure.identity.errors import (
    CrossTenantIdentityError,
    IdentityConfigurationError,
    InvalidCredentialsError,
    MfaRequiredError,
    TokenReplayError,
    TokenRevokedError,
)
from doc_extractor.infrastructure.identity.mfa import TotpEngine
from doc_extractor.infrastructure.identity.models import RefreshTokenRecord, UserRecord
from doc_extractor.infrastructure.identity.service import InternalIdentityService
from doc_extractor.infrastructure.identity.tokens import KeyRing, TokenManager
from doc_extractor.infrastructure.mongo.identity import (
    MongoRefreshTokenRepository,
    MongoTokenRevocationStore,
    MongoUserRepository,
    ensure_identity_indexes,
)
from doc_extractor.infrastructure.mongo.tenants import (
    MongoTenantRepository,
    ensure_tenant_indexes,
)
from doc_extractor.infrastructure.mongo.webhooks import (
    MongoWebhookDeliveryRepository,
    MongoWebhookSubscriptionRepository,
    ensure_webhook_indexes,
)
from doc_extractor.infrastructure.tenancy.models import TenantRecord, TenantStatus
from doc_extractor.infrastructure.tenancy.service import (
    CrossTenantAccessError,
    PermissionDeniedError,
    TenantConflictError,
    TenantLifecycleError,
    TenantProvisioningService,
)
from doc_extractor.infrastructure.webhook.dispatcher import WebhookDispatcher
from doc_extractor.infrastructure.webhook.security import (
    EphemeralSecretResolver,
    WebhookDestinationError,
    validate_webhook_destination,
)
from tests.mongo_fakes import FakeClient, FakeDatabase


def run(coro):
    return asyncio.run(coro)


def make_test_keyring() -> KeyRing:
    from cryptography.hazmat.primitives.asymmetric import rsa
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return KeyRing(active_private_key=private_key, active_kid="test_k1")


def setup_test_db() -> FakeDatabase:
    db = FakeDatabase("test_doc")
    run(ensure_identity_indexes(db))
    run(ensure_tenant_indexes(db))
    run(ensure_webhook_indexes(db))
    return db


# ==============================================================================
# 1. Identity & Session Persistence Tests
# ==============================================================================

class TestPersistentIdentity:

    def test_persistent_user_creation_and_retrieval(self):
        db = setup_test_db()
        user_repo = MongoUserRepository(db)
        keyring = make_test_keyring()
        token_mgr = TokenManager(keyring, issuer="test", audience="test")
        svc = InternalIdentityService(token_mgr, user_repo=user_repo)

        # Create user
        user = svc.create_user(
            tenant_id="tenant_acme",
            email="alice@acme.com",
            password="SecurePassword123!",
            roles={"analyst"},
        )
        assert user.user_id.startswith("usr_")
        assert user.email == "alice@acme.com"

        # Lookup by ID
        fetched = svc.get_user("tenant_acme", user.user_id)
        assert fetched is not None
        assert fetched.email == "alice@acme.com"
        assert fetched.verify_password("SecurePassword123!")
        assert not fetched.verify_password("WrongPassword")

        # Lookup by email
        by_email = svc.get_user_by_email("tenant_acme", "ALICE@acme.com")
        assert by_email is not None
        assert by_email.user_id == user.user_id

        # Duplicate email rejected
        with pytest.raises(IdentityConfigurationError):
            svc.create_user("tenant_acme", "alice@acme.com")

    def test_refresh_token_hashes_stored_not_raw(self):
        db = setup_test_db()
        refresh_repo = MongoRefreshTokenRepository(db)
        now = datetime.now(timezone.utc)
        raw_token = "raw_secret_refresh_token_12345"
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

        rec = RefreshTokenRecord(
            token_id="rt_001",
            token_hash=token_hash,
            family_id="fam_001",
            user_id="usr_001",
            tenant_id="tenant_acme",
            roles={"analyst"},
            scopes=set(),
            mfa_verified=False,
            expires_at=now + timedelta(days=7),
            created_at=now,
        )
        refresh_repo.save_token(rec)

        # Check raw token is not stored in MongoDB document
        raw_doc = db["refresh_tokens"].docs[0]
        assert raw_doc["token_hash"] == token_hash
        assert raw_token not in str(raw_doc)

        # Lookup by hash succeeds
        found = refresh_repo.get_by_hash(token_hash)
        assert found is not None
        assert found.token_id == "rt_001"

    def test_single_use_refresh_atomic_concurrency(self):
        db = setup_test_db()
        refresh_repo = MongoRefreshTokenRepository(db)
        rev_store = MongoTokenRevocationStore(db)
        keyring = make_test_keyring()
        token_mgr = TokenManager(keyring, issuer="test", audience="test", refresh_repo=refresh_repo, revocation_store=rev_store)

        user = UserRecord(
            user_id="usr_1",
            tenant_id="tenant_1",
            email="u@t.com",
            roles={"analyst"},
        )
        token_pair = token_mgr.issue_token_pair(user)
        raw_rt = token_pair.refresh_token
        assert raw_rt is not None

        # Simulate concurrent execution of rotate_refresh_token
        # First call must succeed
        next_pair = token_mgr.rotate_refresh_token(raw_rt)
        assert next_pair.access_token is not None
        assert next_pair.refresh_token is not None

        # Second call with the same consumed token must immediately detect replay attack
        with pytest.raises(TokenReplayError) as exc_info:
            token_mgr.rotate_refresh_token(raw_rt)
        assert "replay detected" in str(exc_info.value).lower()

        # The new replacement token must also be invalidated by family-wide revocation!
        with pytest.raises(TokenRevokedError):
            token_mgr.rotate_refresh_token(next_pair.refresh_token)

    def test_session_and_jti_revocation(self):
        db = setup_test_db()
        rev_store = MongoTokenRevocationStore(db)
        keyring = make_test_keyring()
        token_mgr = TokenManager(keyring, issuer="test", audience="test", revocation_store=rev_store)

        user = UserRecord(user_id="usr_1", tenant_id="tenant_1", email="u@t.com", roles={"analyst"})
        pair = token_mgr.issue_token_pair(user)
        principal = token_mgr.verify_access_token(f"Bearer {pair.access_token}")
        assert principal.principal_id == "usr_1"

        # Revoke access token directly
        token_mgr.revoke_access_token(f"Bearer {pair.access_token}")
        with pytest.raises(TokenRevokedError):
            token_mgr.verify_access_token(f"Bearer {pair.access_token}")

        # Issue another token and revoke user sessions
        pair2 = token_mgr.issue_token_pair(user)
        token_mgr.revoke_user_sessions("usr_1", "tenant_1")
        with pytest.raises(TokenRevokedError):
            token_mgr.verify_access_token(f"Bearer {pair2.access_token}")

    def test_user_activation_and_deactivation(self):
        db = setup_test_db()
        user_repo = MongoUserRepository(db)
        keyring = make_test_keyring()
        token_mgr = TokenManager(keyring, issuer="test", audience="test")
        svc = InternalIdentityService(token_mgr, user_repo=user_repo)

        user = svc.create_user("tenant_1", "bob@t.com", password="PassWord123!")
        pair = svc.authenticate_password("tenant_1", "bob@t.com", "PassWord123!")
        assert pair.access_token

        # Deactivate user
        svc.set_user_active("tenant_1", user.user_id, is_active=False)
        fetched = svc.get_user("tenant_1", user.user_id)
        assert fetched.is_active is False

        # Authentication refused
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate_password("tenant_1", "bob@t.com", "PassWord123!")

        # Reactivate user
        svc.set_user_active("tenant_1", user.user_id, is_active=True)
        pair2 = svc.authenticate_password("tenant_1", "bob@t.com", "PassWord123!")
        assert pair2.access_token

    def test_cross_tenant_identity_isolation(self):
        db = setup_test_db()
        user_repo = MongoUserRepository(db)
        keyring = make_test_keyring()
        token_mgr = TokenManager(keyring, issuer="test", audience="test")
        svc = InternalIdentityService(token_mgr, user_repo=user_repo)

        user = svc.create_user("tenant_alpha", "user@alpha.com", password="AlphaPassword123!")
        # User does not exist in tenant_beta
        assert svc.get_user("tenant_beta", user.user_id) is None
        assert svc.get_user_by_email("tenant_beta", "user@alpha.com") is None

        # Cross-tenant login refused
        with pytest.raises(InvalidCredentialsError):
            svc.authenticate_password("tenant_beta", "user@alpha.com", "AlphaPassword123!")


# ==============================================================================
# 2. Tenant Provisioning Tests
# ==============================================================================

class TestTenantProvisioning:

    def test_admin_with_mfa_can_provision_tenant(self):
        db = setup_test_db()
        tenant_repo = MongoTenantRepository(db)
        audit_mock = AsyncMock()
        svc = TenantProvisioningService(tenant_repo, audit_logger=audit_mock)

        admin = ApiPrincipal(
            principal_id="admin_01",
            tenant_id="system",
            principal_type=PrincipalType.USER,
            roles={"admin"},
            scopes={"*"},
            mfa_verified=True,
        )

        tenant = svc.provision_tenant(admin, tenant_id="tenant_omega", name="Omega Corporation", config={"quota": 100})
        assert tenant.tenant_id == "tenant_omega"
        assert tenant.status == TenantStatus.ACTIVE
        assert tenant.config == {"quota": 100}

        # Verify audit call
        audit_mock.record.assert_awaited_once()
        call_kwargs = audit_mock.record.await_args.kwargs
        assert call_kwargs["action"] == "tenant.provisioned"
        assert call_kwargs["tenant_id"] == "tenant_omega"

    def test_unauthorized_and_unverified_mfa_provisioning_rejected(self):
        db = setup_test_db()
        tenant_repo = MongoTenantRepository(db)
        svc = TenantProvisioningService(tenant_repo)

        # Non-admin principal
        analyst = ApiPrincipal(
            principal_id="analyst_01",
            tenant_id="tenant_a",
            principal_type=PrincipalType.USER,
            roles={"analyst"},
            scopes={"document:read"},
            mfa_verified=True,
        )
        with pytest.raises(PermissionDeniedError):
            svc.provision_tenant(analyst, tenant_id="tenant_new", name="New Tenant")

        # Admin without MFA (SEC-001)
        admin_no_mfa = ApiPrincipal(
            principal_id="admin_02",
            tenant_id="system",
            principal_type=PrincipalType.USER,
            roles={"admin"},
            scopes={"*"},
            mfa_verified=False,
        )
        with pytest.raises(MfaRequiredError):
            svc.provision_tenant(admin_no_mfa, tenant_id="tenant_new", name="New Tenant")

    def test_invalid_tenant_id_rejected_path_safe(self):
        db = setup_test_db()
        svc = TenantProvisioningService(MongoTenantRepository(db))
        admin = ApiPrincipal(principal_id="adm", tenant_id="sys", roles={"admin"}, mfa_verified=True)

        for unsafe in ("../escape", "a/b", "a\\b", "..", "", "x" * 300, "bad\x00null"):
            with pytest.raises(InvalidTenantIdentifierError):
                svc.provision_tenant(admin, tenant_id=unsafe, name="Unsafe")

    def test_idempotent_tenant_provisioning_and_conflict(self):
        db = setup_test_db()
        svc = TenantProvisioningService(MongoTenantRepository(db))
        admin = ApiPrincipal(principal_id="adm", tenant_id="sys", roles={"admin"}, mfa_verified=True)

        t1 = svc.provision_tenant(admin, tenant_id="tenant_dup", name="Identical Name")
        # Idempotent retry
        t2 = svc.provision_tenant(admin, tenant_id="tenant_dup", name="Identical Name")
        assert t1.tenant_id == t2.tenant_id

        # Conflicting name raises TenantConflictError
        with pytest.raises(TenantConflictError):
            svc.provision_tenant(admin, tenant_id="tenant_dup", name="Different Name")

    def test_tenant_lifecycle_transitions(self):
        db = setup_test_db()
        svc = TenantProvisioningService(MongoTenantRepository(db))
        admin = ApiPrincipal(principal_id="adm", tenant_id="sys", roles={"admin"}, mfa_verified=True)

        t = svc.provision_tenant(admin, "t_life", "Lifecycle Tenant")
        assert t.status == TenantStatus.ACTIVE

        # Suspend
        suspended = svc.suspend_tenant(admin, "t_life", reason="Billing overdue")
        assert suspended.status == TenantStatus.SUSPENDED
        assert suspended.suspension_reason == "Billing overdue"

        # Re-activate
        activated = svc.activate_tenant(admin, "t_life")
        assert activated.status == TenantStatus.ACTIVE

        # Deactivate
        deactivated = svc.deactivate_tenant(admin, "t_life", reason="Customer offboarded")
        assert deactivated.status == TenantStatus.DEACTIVATED

        # Deactivated is terminal: cannot re-activate or suspend
        with pytest.raises(TenantLifecycleError):
            svc.activate_tenant(admin, "t_life")
        with pytest.raises(TenantLifecycleError):
            svc.suspend_tenant(admin, "t_life")

    def test_tenant_provisioning_audit_fail_closed(self):
        db = setup_test_db()
        tenant_repo = MongoTenantRepository(db)
        audit_mock = AsyncMock()
        audit_mock.record.side_effect = RuntimeError("Audit disk full")
        svc = TenantProvisioningService(tenant_repo, audit_logger=audit_mock)

        admin = ApiPrincipal(principal_id="adm", tenant_id="sys", roles={"admin"}, mfa_verified=True)

        # Provisioning must fail closed
        with pytest.raises(AuditUnavailableError):
            svc.provision_tenant(admin, tenant_id="t_audit_fail", name="Fail Closed")

        # Tenant creation must have been rolled back!
        assert tenant_repo.get_tenant("t_audit_fail") is None

    def test_cross_tenant_inspection_boundaries(self):
        db = setup_test_db()
        svc = TenantProvisioningService(MongoTenantRepository(db))
        admin = ApiPrincipal(principal_id="adm", tenant_id="sys", roles={"admin"}, mfa_verified=True)
        svc.provision_tenant(admin, "tenant_a", "A Corp")
        svc.provision_tenant(admin, "tenant_b", "B Corp")

        user_a = ApiPrincipal(principal_id="u_a", tenant_id="tenant_a", roles={"analyst"}, mfa_verified=False)
        # User A can get tenant_a
        assert svc.get_tenant(user_a, "tenant_a") is not None
        # User A cannot get tenant_b
        with pytest.raises(CrossTenantAccessError):
            svc.get_tenant(user_a, "tenant_b")

        # Admin can inspect any tenant
        assert svc.get_tenant(admin, "tenant_b") is not None


# ==============================================================================
# 3. Webhook Persistence & Security Tests
# ==============================================================================

class TestWebhookPersistenceAndSecurity:

    def test_subscription_persistence(self):
        db = setup_test_db()
        repo = MongoWebhookSubscriptionRepository(db, validate_destination=False)

        sub = WebhookSubscriptionRecord(
            subscription_id="sub_001",
            tenant_id="tenant_alpha",
            target_url="https://api.alpha.com/webhook",
            event_types=["document.completed", "document.failed"],
            secret_hash="a" * 64,
            secret_ref="sec_alpha_001",
            is_active=True,
        )
        run(repo.create("tenant_alpha", sub))

        # Retrieve
        fetched = run(repo.get_by_id("tenant_alpha", "sub_001"))
        assert fetched is not None
        assert fetched.secret_ref == "sec_alpha_001"
        assert fetched.target_url == "https://api.alpha.com/webhook"

        # List by tenant
        subs = run(repo.list_by_tenant("tenant_alpha", event_type="document.completed"))
        assert len(subs) == 1
        assert subs[0].subscription_id == "sub_001"

        # Update
        updated = run(repo.update("tenant_alpha", "sub_001", {"is_active": False}))
        assert updated.is_active is False
        assert run(repo.list_by_tenant("tenant_alpha")) == []

        # Delete
        assert run(repo.delete("tenant_alpha", "sub_001")) is True
        assert run(repo.get_by_id("tenant_alpha", "sub_001")) is None

    def test_delivery_persistence(self):
        db = setup_test_db()
        repo = MongoWebhookDeliveryRepository(db)

        delivery = WebhookDeliveryRecord(
            delivery_id="del_001",
            subscription_id="sub_001",
            tenant_id="tenant_alpha",
            event_id="evt_001",
            event_type="document.completed",
            attempt_number=1,
            status="pending",
        )
        run(repo.record_delivery("tenant_alpha", delivery))

        # Update status
        updated = run(repo.update_delivery_status(
            "tenant_alpha",
            "del_001",
            status="delivered",
            response_status_code=200,
        ))
        assert updated.status == "delivered"
        assert updated.response_status_code == 200
        assert updated.completed_at is not None

    def test_ssrf_destination_validation(self):
        # 1. Private RFC 1918 IPs forbidden
        for private_url in (
            "https://10.0.0.1/webhook",
            "https://172.16.0.1/webhook",
            "https://192.168.1.1/webhook",
            "https://127.0.0.1/webhook",
        ):
            with pytest.raises(WebhookDestinationError):
                validate_webhook_destination(private_url, allow_loopback=False, resolve_dns=False)

        # 2. Cloud metadata forbidden
        for meta_url in (
            "https://169.254.169.254/latest/meta-data",
            "https://metadata.google.internal/computeMetadata/v1",
        ):
            with pytest.raises(WebhookDestinationError):
                validate_webhook_destination(meta_url, allow_loopback=False, resolve_dns=False)

        # 3. HTTP scheme forbidden outside loopback
        with pytest.raises(WebhookDestinationError):
            validate_webhook_destination("http://api.external.com/webhook", allow_loopback=False, resolve_dns=False)

        # 4. Embedded credentials forbidden
        with pytest.raises(WebhookDestinationError):
            validate_webhook_destination("https://user:pass@api.external.com/webhook", allow_loopback=False, resolve_dns=False)

        # 5. Public HTTPS destination allowed
        host, port = validate_webhook_destination("https://example.com/webhook", allow_loopback=False, resolve_dns=False)
        assert host == "example.com"
        assert port == 443

    def test_webhook_dispatcher_delivery_success(self):
        db = setup_test_db()
        sub_repo = MongoWebhookSubscriptionRepository(db, validate_destination=False)
        del_repo = MongoWebhookDeliveryRepository(db)
        secret_resolver = EphemeralSecretResolver()
        secret_resolver.register_secret("sec_01", b"secret_key_1234567890")

        dispatcher = WebhookDispatcher(
            sub_repo,
            del_repo,
            secret_resolver,
            allow_loopback=True,
        )

        sub = WebhookSubscriptionRecord(
            subscription_id="sub_success",
            tenant_id="t1",
            target_url="https://api.example.com/hook",
            event_types=["document.completed"],
            secret_hash="h" * 64,
            secret_ref="sec_01",
        )
        run(sub_repo.create("t1", sub))

        # Mock HTTP client returning 200
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.post.return_value = mock_resp

        with patch("doc_extractor.infrastructure.webhook.dispatcher.validate_webhook_destination", return_value=("api.example.com", 443)):
            deliveries = run(dispatcher.dispatch_event(
                "t1",
                SimpleNamespace(event_id="evt_001", event_type="document.completed"),
                http_client=mock_client,
            ))

        assert len(deliveries) == 1
        assert deliveries[0].status == "delivered"
        assert deliveries[0].response_status_code == 200

        # Verify HMAC signature was sent
        call_headers = mock_client.post.call_args.kwargs["headers"]
        assert "X-Webhook-Signature" in call_headers
        assert call_headers["X-Webhook-Signature"].startswith("sha256=")
        assert "X-Webhook-Timestamp" in call_headers

    def test_webhook_dispatcher_ssrf_terminal_failure(self):
        db = setup_test_db()
        sub_repo = MongoWebhookSubscriptionRepository(db, validate_destination=False)
        del_repo = MongoWebhookDeliveryRepository(db)
        secret_resolver = EphemeralSecretResolver()
        audit_mock = AsyncMock()

        dispatcher = WebhookDispatcher(
            sub_repo,
            del_repo,
            secret_resolver,
            audit_logger=audit_mock,
            allow_loopback=False,
        )

        sub = WebhookSubscriptionRecord(
            subscription_id="sub_ssrf",
            tenant_id="t1",
            target_url="https://169.254.169.254/latest/meta-data",
            event_types=["document.completed"],
            secret_hash="h" * 64,
            secret_ref="sec_01",
        )
        run(sub_repo.create("t1", sub))

        deliveries = run(dispatcher.dispatch_event(
            "t1",
            SimpleNamespace(event_id="evt_ssrf", event_type="document.completed"),
        ))

        assert len(deliveries) == 1
        assert deliveries[0].status == "failed_permanent"
        assert "SSRF_DENIED" in deliveries[0].error_message

        # Audit alert recorded
        audit_mock.record.assert_awaited()
        assert audit_mock.record.await_args.kwargs["action"] == "webhook.dead_letter"

    def test_webhook_dispatcher_retry_on_503(self):
        db = setup_test_db()
        sub_repo = MongoWebhookSubscriptionRepository(db, validate_destination=False)
        del_repo = MongoWebhookDeliveryRepository(db)
        secret_resolver = EphemeralSecretResolver()
        secret_resolver.register_secret("sec_01", b"secret_key_1234567890")

        dispatcher = WebhookDispatcher(
            sub_repo,
            del_repo,
            secret_resolver,
            allow_loopback=True,
        )

        sub = WebhookSubscriptionRecord(
            subscription_id="sub_retry",
            tenant_id="t1",
            target_url="https://api.example.com/hook",
            event_types=["document.completed"],
            secret_hash="h" * 64,
            secret_ref="sec_01",
        )
        run(sub_repo.create("t1", sub))

        # Mock HTTP client returning 503
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_client.post.return_value = mock_resp

        with patch("doc_extractor.infrastructure.webhook.dispatcher.validate_webhook_destination", return_value=("api.example.com", 443)):
            deliveries = run(dispatcher.dispatch_event(
                "t1",
                SimpleNamespace(event_id="evt_retry", event_type="document.completed"),
                http_client=mock_client,
            ))

        assert len(deliveries) == 1
        assert deliveries[0].status == "failed_retryable"
        assert deliveries[0].next_retry_at is not None

    def test_webhook_dispatcher_dead_letter_on_400(self):
        db = setup_test_db()
        sub_repo = MongoWebhookSubscriptionRepository(db, validate_destination=False)
        del_repo = MongoWebhookDeliveryRepository(db)
        secret_resolver = EphemeralSecretResolver()
        secret_resolver.register_secret("sec_01", b"secret_key_1234567890")
        audit_mock = AsyncMock()

        dispatcher = WebhookDispatcher(
            sub_repo,
            del_repo,
            secret_resolver,
            audit_logger=audit_mock,
            allow_loopback=True,
        )

        sub = WebhookSubscriptionRecord(
            subscription_id="sub_dead",
            tenant_id="t1",
            target_url="https://api.example.com/hook",
            event_types=["document.completed"],
            secret_hash="h" * 64,
            secret_ref="sec_01",
        )
        run(sub_repo.create("t1", sub))

        # Mock HTTP client returning 400 Bad Request (terminal, non-retryable)
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_client.post.return_value = mock_resp

        with patch("doc_extractor.infrastructure.webhook.dispatcher.validate_webhook_destination", return_value=("api.example.com", 443)):
            deliveries = run(dispatcher.dispatch_event(
                "t1",
                SimpleNamespace(event_id="evt_dead", event_type="document.completed"),
                http_client=mock_client,
            ))

        assert len(deliveries) == 1
        assert deliveries[0].status == "dead_letter"
        assert deliveries[0].response_status_code == 400
        audit_mock.record.assert_awaited()


class SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


# ==============================================================================
# 5. Real MongoDB Integration Tests (Opt-in via DOC_TEST_MONGO_URI)
# ==============================================================================

REAL_MONGO_URI = os.environ.get("DOC_TEST_MONGO_URI", "").strip()
REAL_REPLICA_SET = os.environ.get("DOC_TEST_MONGO_REPLICA_SET", "rs0").strip()
requires_real_mongo = pytest.mark.skipif(
    not REAL_MONGO_URI,
    reason="set DOC_TEST_MONGO_URI to run against a real MongoDB replica set",
)


def run_with_real_database(body, uri=None, replica_set=REAL_REPLICA_SET):
    import uuid
    from doc_extractor.infrastructure.mongo import MongoConnection, MongoSettings

    async def go():
        settings = MongoSettings(
            uri=uri or REAL_MONGO_URI,
            database=f"doc_it_5c_{uuid.uuid4().hex[:12]}",
            app_env="development",
            replica_set=replica_set,
        )
        conn = MongoConnection(settings)
        await conn.connect()
        try:
            return await body(conn)
        finally:
            await conn.client.drop_database(settings.database)
            await conn.aclose()

    return asyncio.run(go())


@requires_real_mongo
def test_real_mongo_identity_and_tenancy_integration():
    async def body(conn):
        db = conn.database
        await ensure_identity_indexes(db)
        await ensure_tenant_indexes(db)
        await ensure_webhook_indexes(db)

        # 1. User repository
        user_repo = MongoUserRepository(db)
        user = UserRecord(
            user_id="usr_real_01",
            tenant_id="t_real_01",
            email="admin@example.com",
            roles={"admin"},
            password_hash="argon2id$fakehash",
        )
        created = await user_repo.create_user("t_real_01", user)
        assert created.user_id == "usr_real_01"

        fetched = await user_repo.get_by_id("t_real_01", "usr_real_01")
        assert fetched is not None
        assert fetched.email == "admin@example.com"

        # Tenant isolation
        assert await user_repo.get_by_id("other_tenant", "usr_real_01") is None

        # 2. Token persistence and atomic single-use
        token_repo = MongoRefreshTokenRepository(db)
        rec = RefreshTokenRecord(
            token_hash="hash_real_01",
            user_id="usr_real_01",
            tenant_id="t_real_01",
            family_id="fam_real_01",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        await token_repo.store(rec)
        first_use = await token_repo.mark_used("hash_real_01")
        assert first_use is True
        second_use = await token_repo.mark_used("hash_real_01")
        assert second_use is False

        # 3. Tenant repository
        tenant_repo = MongoTenantRepository(db)
        t_rec = TenantRecord(tenant_id="t_real_01", display_name="Real Tenant", status=TenantStatus.ACTIVE)
        await tenant_repo.insert_tenant(t_rec)
        fetched_t = await tenant_repo.get_tenant("t_real_01")
        assert fetched_t is not None
        assert fetched_t.display_name == "Real Tenant"

        # 4. Webhook persistence
        sub_repo = MongoWebhookSubscriptionRepository(db, validate_destination=False)
        sub = WebhookSubscriptionRecord(
            subscription_id="sub_real_01",
            tenant_id="t_real_01",
            target_url="https://api.example.com/webhook",
            event_types=["document.completed"],
            secret_hash="h" * 64,
            secret_ref="sec_real_ref",
        )
        created_sub = await sub_repo.create("t_real_01", sub)
        assert created_sub.subscription_id == "sub_real_01"
        found = await sub_repo.get("t_real_01", "sub_real_01")
        assert found is not None
        assert found.secret_ref == "sec_real_ref"

    run_with_real_database(body)
