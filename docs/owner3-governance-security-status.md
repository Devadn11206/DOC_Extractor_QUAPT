# Owner 3: governance, security, infrastructure and enterprise (status)

Scope: PRD sections 9, 10, 11, ARC-001..006, SCH-001..007. This note records what the repository implements, what was
decided in code, and what still needs outside work. Each claim names the code and the test that checks it.

## Implemented in the repository

| PRD clause | What exists | Where | Tests |
| --- | --- | --- | --- |
| SCH-001..007 (schema lifecycle, four-eyes approval, versions, harness) | State machine, approval gate, immutable versions with rollback, harness metrics, dual sync/async MongoDB schema repository (`MongoSchemaRepository`), four-eyes duty separation (`author != approver`), MFA verification for approvers, anti-replay single-use harness tracking, and fail-closed audit logging. | `src/schema_lifecycle/`, `src/doc_extractor/infrastructure/mongo/schemas.py`, `src/doc_extractor/api/routers/schemas.py` | `tests/test_schema_*.py`, `tests/test_api_schema_routes.py`, `tests/test_schema_governance_rbac.py` |
| FR-ING-003, SEC-009 (malware scan, fail closed) | ClamAV clamd INSTREAM client. Any failure is `ERROR`, never `CLEAN`. Production never uses the mock scanner. | `src/doc_extractor/malware.py` (`ClamAVSocketScanner`) | `tests/test_malware_clamav.py` |
| SEC-008, FR-AUD-001 (tamper-evident audit & signed checkpoints) | Per-tenant SHA-256 hash chain, fsynced appends, failed writes raise. Extended in Phase 4C with independently signed checkpoints (RSASSA-PSS SHA-256 via AWS KMS) and external immutable WORM storage (S3 Object Lock). Offline public-key verification (`IndependentCheckpointVerifier`) detects tail truncation, historical rewrites, and tampered records. SEC-008 remains partially outstanding pending physical Object Lock bucket provisioning and Open Decision M5-D15 (operational sink failure policy). | `src/doc_extractor/infrastructure/audit/`, `api/audit_chain.py` | `tests/test_audit_checkpoints.py`, `tests/test_audit_hash_chain.py` |
| SEC-002 (RS256 access tokens, 1 h lifetime, rotating refresh tokens) | RS256-only signing and verification. Max 1 h lifetime. Rotating single-use refresh tokens with replay detection and token family revocation. Key rotation with 'kid' and RFC 7517 JWKS export. Token revocation store. Product-owned `InternalIdentityService`. | `src/doc_extractor/infrastructure/identity/`, `api/providers/jwt_auth.py` | `tests/test_identity_and_authentication.py`, `tests/test_api_jwt_auth.py` |
| SEC-003 (seven least-privilege roles) | Role-to-scope matrix across canonical roles (`analyst`, `reviewer`, `schema_author`, `schema_approver`, `integrator`, `auditor`, `admin`). Explicit `scope` claims can narrow, never widen. Least privilege and default-deny boundary enforced. Users prevented from self-escalating roles or scopes. Machine API keys restricted. | `jwt_auth.py` (`ROLE_SCOPES`), `schema_lifecycle/roles.py`, `api/dependencies.py` | `tests/test_api_security_wiring.py`, `tests/test_schema_governance_rbac.py` |
| SEC-001 (MFA for admin and schema approver) | Trusted MFA enforcement: RFC 6238 TOTP engine and upstream OIDC assertion verification (`amr`/`acr`). Tokens for `admin` and `schema_approver` refused without verified MFA. Self-asserted caller `amr` claims rejected. | `src/doc_extractor/infrastructure/identity/mfa.py`, `service.py`, `jwt_auth.py` | `tests/test_identity_and_authentication.py`, `tests/test_api_jwt_auth.py` |
| SEC-011, ARC-001 (default-deny egress) | Loopback, or `DOC_EGRESS_ALLOWED_DESTINATIONS`. Enforced on the vLLM call, the litellm call and the ClamAV TCP connection. | `src/doc_extractor/egress.py`, `extractor.py`, `malware.py` | `tests/test_egress_policy.py` |
| SCH-001..006 REST endpoints | Create, version, history, status transitions, approve, rollback, and retrieval (`GET /schemas/{id}`, `GET /schemas/{id}/versions/{v}`), each gated by canonical RBAC. Tenant and schema identifier strictly verified. Wired to `MongoSchemaRepository` when configured; answers 503 until configured. Fail-closed audit logging on all mutations. | `src/doc_extractor/api/routers/schemas.py`, `api/composition.py`, `api/app.py` | `tests/test_api_schema_routes.py`, `tests/test_schema_governance_rbac.py` |
| API-012, SEC-017 (distributed rate limiting) | Redis token bucket per authenticated principal, shared across replicas. Optimistic concurrency, so the limit is exact under contention. Fails closed when Redis is down. Enabled by `rate_limit_capacity` together with `redis_url`. | `src/doc_extractor/rate_limit.py`, `api/dependencies.py` | `tests/test_rate_limit.py` |
| NFR-010 (Prometheus metrics) | Request counts and durations by route template, rate-limit refusals, security alerts by condition. Dependency-free text exposition. Served only by the internal `metrics_app` on its own port. | `src/doc_extractor/metrics.py`, `api/metrics_middleware.py`, `api/metrics_app.py` | `tests/test_metrics.py` |
| SEC-021 (security alerts) | Alerts for repeated failed logins (by credential fingerprint), refused egress, broken audit chains, model-file changes, and per-method counters for export volume and unmask spikes. Alerts carry identifiers and counts only. | `src/doc_extractor/alerts.py`, wired into `egress.py`, `api/providers/composite_auth.py`, `scripts/ops/verify_audit_chains.py` | `tests/test_alerts.py` |
| ARC-003, NFR-006 (multi-replica locks and idempotency) | Redis `SET NX PX` leases, WATCH-guarded release, reentrant per thread. Enabled by `redis_url`. | `src/doc_extractor/distributed_state.py` | `tests/test_distributed_state.py` (fakeredis) |
| ARC-003, SEC-005 (S3 object storage, envelope encryption & KMS/HSM integration) | Tenant-isolated S3 storage adapter implementing `StorageBackend` with authenticated AES-256-GCM envelope encryption. Per-object DEKs wrapped under KMS/HSM KEKs (AWS KMS and extensible KmsProvider; FakeKmsProvider forbidden outside dev). Server-side DEK rewrapping on KEK rotation. Spooled files restricted (0700/0600) and cleaned. Full SEC-005 remains partially outstanding due to temporary local spool requirement for descriptor callers and Open Decision M5-D18. | `src/doc_extractor/infrastructure/storage/` | `tests/test_storage_s3_settings.py`, `tests/test_storage_s3_adapter.py`, `tests/test_storage_s3_encryption.py`, `tests/test_storage_s3_integration.py` |
| SEC-001, SEC-002, SEC-006, SEC-008, PRD 7.1, SCR-07 (Tenant provisioning, persistent identity, rotating token families, webhook security) | MongoDB-backed user identity, refresh-token hash storage, atomic single-use token rotation, family-wide replay revocation, session/JTI revocation, administrative tenant provisioning with mandatory admin MFA, lifecycle state machine (`ACTIVE`, `SUSPENDED`, `DEACTIVATED`), fail-closed audit rollback, anti-SSRF webhook validation (private IPs, loopback, cloud metadata, DNS rebinding), HMAC-SHA256 request signing (`X-Webhook-Signature: sha256=<hex>`), secret-reference resolution (`secret_ref`), 24-hour exponential backoff retries, and dead-letter tracking with fail-closed audit logging. Pure startup with zero fallback in-memory stores. | `src/doc_extractor/infrastructure/mongo/identity.py`, `src/doc_extractor/infrastructure/mongo/tenants.py`, `src/doc_extractor/infrastructure/mongo/webhooks.py`, `src/doc_extractor/infrastructure/tenancy/`, `src/doc_extractor/infrastructure/webhook/` | `tests/test_tenant_identity_webhook_security.py`, `tests/test_mongo_startup.py` |
| SEC-016, SEC-021, NFR-010, ARC-006 (Enterprise readiness, health diagnostics, backup runbooks, ops verification) | Multi-component health diagnostics (`GET /health` reporting MongoDB, RabbitMQ, Redis, S3, and KMS connectivity), operational signed checkpoint CLI (`scripts/ops/verify_audit_checkpoints.py`), queue worker and checkpoint failure alerting in `AlertMonitor`, documented backup/recovery runbook meeting RPO $\le$ 15m and RTO $\le$ 4h across three deployment profiles (SaaS AWS Mumbai `ap-south-1`, Private Cloud/VPC, and Air-gapped On-Premise). | `src/doc_extractor/api/routers/health.py`, `src/doc_extractor/alerts.py`, `scripts/ops/verify_audit_checkpoints.py`, `docs/team3-enterprise-readiness-and-recovery.md` | `tests/test_enterprise_health_and_ops.py`, `tests/test_health.py`, `tests/test_alerts.py` |

## Decisions made in code

- **Enterprise readiness, observability & disaster recovery (Phase 6 / SEC-016, SEC-021, NFR-010, ARC-006):**
  - Multi-component health diagnostics (`GET /health`): Enhanced inspection endpoint dynamically interrogates configured infrastructure components (MongoDB cached health, RabbitMQ broker connectivity, Redis ping, S3 bucket connectivity via `head_bucket`, and KMS key status). Unconfigured components are omitted; failing components transition service status to `degraded` without crashing the process or raising unhandled 500 errors.
  - Alerting expansion (`AlertCondition`): Added `QUEUE_WORKER_FAILURE` and `AUDIT_CHECKPOINT_FAILURE` alert conditions to `AlertMonitor`, providing immediate security and operational notification for asynchronous processing stalls and checkpoint integrity breaches.
  - Operational signed checkpoint CLI (`scripts/ops/verify_audit_checkpoints.py`): Standalone verification tool capable of offline public-key signature verification (`IndependentCheckpointVerifier`) without requiring KMS privileges, enabling external auditors and scheduled cron jobs to detect historical data tampering or sequence truncations.
  - Backup & Disaster Recovery Architecture (`docs/team3-enterprise-readiness-and-recovery.md`): Formally defined operational runbooks for continuous MongoDB oplog slicing and point-in-time recovery (PITR) achieving RPO $\le$ 15m and RTO $\le$ 4h (`SEC-016`), S3 versioning and delete-marker recovery, server-side DEK rewrapping on KMS KEK rotation, and three comprehensive deployment profiles (Vendor SaaS in AWS Mumbai `ap-south-1`, Customer Private Cloud/VPC, and Air-Gapped On-Premise).

- **Tenant provisioning, persistent identity & webhook security (Phase 5C / SEC-001, SEC-002, SEC-006, SEC-008, PRD 7.1, SCR-07):**
  - Persistent identity storage: User records (`users`), refresh tokens (`refresh_tokens`), and revocations (`token_revocations`) persist in MongoDB with tenant-first compound indexes.
  - Refresh tokens are stored strictly as SHA-256 hashes (`token_hash`), never raw tokens.
  - Atomic single-use token rotation: `mark_used` executes via an atomic conditional filter `{"_id": token_hash, "used": False}` with update `{"$set": {"used": True}}`. If 0 documents are modified, replay is detected: the entire token family (`family_id`) and all active sessions for the user are immediately revoked, raising `TokenReplayError`.
  - User deactivation immediately terminates all active sessions for that user.
  - Explicit administrative tenant provisioning: Gated by `admin` role and verified MFA (`mfa_verified=True`). Enforces path-safe tenant IDs (`require_path_safe_tenant_id`, M5-D16) and lifecycle transitions (`ACTIVE`, `SUSPENDED`, `DEACTIVATED`). Idempotent creation (duplicate exact config returns existing; conflicting config raises 409). Fail-closed audit logging reverts state transitions on audit failure.
  - Webhook persistence & security: Subscriptions and delivery history persist in MongoDB (`webhook_subscriptions`, `webhook_deliveries`). Plaintext secrets are never stored in the database; secrets are resolved dynamically via `secret_ref` through `WebhookSecretResolver` (preserving M5-D09 as Open).
  - Anti-SSRF destination validation: HTTPS is required, credentials in URLs are blocked, private RFC 1918 IPs, loopback, link-local, and cloud metadata IPs (`169.254.169.254`, `metadata.google.internal`) are blocked. Complete DNS resolution inspects all resolved IP records, eliminating DNS rebinding attacks.
  - HMAC-SHA256 request signing: Signatures follow PRD wire format `X-Webhook-Signature: sha256=<hex>` alongside `X-Webhook-Timestamp` and `X-Webhook-Delivery`. Outbound HTTP client executes via standard library `urllib.request` asynchronously or accepts injected async HTTP clients without introducing runtime `httpx` dependencies.
  - Retries & Dead-lettering: Failed deliveries retry via exponential backoff within a 24-hour window (`services.webhook_retry.schedule_retry`). Terminal failures (4xx client errors or window expiration) transition to `dead_letter` and trigger fail-closed `webhook.dead_letter` audit logging.
  - Anti-fallback invariant: Pure startup builds no fallback in-memory stores; unconfigured environments return HTTP 503 `PortNotConfiguredError`.

- **Schema governance, canonical RBAC & four-eyes approval (Phase 5B / SEC-003, SCH-001..007):**
  - Canonical 7-role RBAC (`analyst`, `reviewer`, `schema_author`, `schema_approver`, `integrator`, `auditor`, `admin`) strictly enforced across all operations with default-deny semantics. Principals cannot escalate roles or expand scopes.
  - Four-eyes approval gate: An author cannot approve their own schema or version submission (`author != approver`). Approver must hold `schema_approver` or explicit `schema:approve` permission, must have verified MFA (`mfa_verified=True`), and must supply a passing benchmark harness report meeting `ApprovalPolicy` thresholds. Harness run IDs are consumed single-use to prevent replay attacks.
  - Durable MongoDB schema repository (`MongoSchemaRepository`): Implements the complete 13-method `SchemaRepository` interface backed by MongoDB collections (`schema_snapshots`, `schema_active_pointers`, `schema_events`, `schema_approval_records`) with tenant-first compound indexes. Supports both synchronous and asynchronous operations.
  - Immutable versions with semantic diff calculation and non-destructive rollback (restores prior approved version as active pointer without deleting history).
  - Fail-closed audit policy: All schema mutations record structured events to the per-tenant SHA-256 hash chain; audit failures abort and revert mutations, returning HTTP 503 `AUDIT_UNAVAILABLE`.
  - Anti-fallback invariant: Pure startup builds no fallback in-memory stores; unconfigured environments return HTTP 503 `PortNotConfiguredError`.
- **Internal identity & token lifecycle (Phase 5A / SEC-002):** The product-owned identity service
  (`src/doc_extractor/infrastructure/identity/`) implements the full token lifecycle: RS256 access tokens (max 1 h lifetime,
  SEC-002), rotating single-use refresh tokens with immediate replay detection and family revocation, KeyRing rotation with 'kid'
  tracking and RFC 7517 JWKS export, and token revocation list by 'jti'. SEC-002 is fully implemented.
- **MFA and customer OIDC federation (Phase 5A / SEC-001):** Administrators and schema approvers are strictly gated by
  verified MFA. MFA is enforced via trusted mechanisms (local RFC 6238 TOTP verification or verified upstream IdP `amr`/`acr`
  assertions); self-asserted caller claims are rejected. Customer OIDC federation (`OidcClient`, `OidcRegistry`) provides
  strict per-tenant IdP isolation; an IdP for one tenant can never authenticate another tenant. Native SAML 2.0 Web SSO is
  documented as an enterprise requirement for environments requiring legacy XML federation.
- **Mock scanner outside development:** the validated settings refuse `mock` in staging and production, and the factory
  `build_malware_scanner` enforces the same rule itself, so a direct call or unvalidated settings cannot select it either:
  the mock is built only when `app_env` is exactly `development` (case and whitespace ignored); in staging, production and any
  other or missing environment it resolves to an unconfigured scanner, and an unsupported scanner type does too. Ingestion then
  rejects files, so nothing is accepted unscanned. Each refusal is logged once with a fixed reason and no input. Scanning
  that is *disabled* is still refused only in production.
- **Egress is application-level.** It refuses the call before a socket is opened. Network-level deny is a deployment
  control (see open items).
- **Audit writes fail the operation.** A mandatory audit that cannot be written raises `AUDIT_UNAVAILABLE` (503). This
  is what the port's contract requires. Decision M5-D15 (audit sink policy) is still open and owned by the PRD owner.
- **Packaging:** the new runtime dependencies live in a separate `security` extra (`PyJWT[crypto]`, `cryptography`,
  `redis`, plus test-only `fakeredis`). The `api` and `dev` extras keep their documented contents, which
  `tests/test_packaging_consistency.py` pins. CI installs the extra in a separate step.
- **Object storage & envelope encryption (Phase 4A & 4B):** An S3-compatible adapter (`S3Storage`) implements the full
  `StorageBackend` contract. Remote storage is encrypted with authenticated AES-256-GCM envelope encryption protecting per-object
  DEKs wrapped under KMS KEKs (AWS KMS, custom KMS/HSMs; FakeKmsProvider forbidden outside dev). Key rotation supports
  server-side DEK rewrapping without re-encrypting ciphertext payloads. It introduces no fallback to local disk or memory: when
  configured, remote failures fail closed. Descriptor-relative `DocumentReadService` compatibility with S3 and local spooling
  security are tracked under Open Decision M5-D18; SEC-005 remains partially outstanding until M5-D18 is resolved or volume
  encryption is verified. Dependencies (`boto3`, `botocore`, `cryptography`) are declared in the `storage` extra.
- **Independently signed audit checkpoints & external immutable storage (Phase 4C):** Implements signed checkpoints
  (`AuditCheckpoint`) covering tenant, sequence, chain hash, timestamp, and format version, signed with asymmetric RSASSA-PSS
  (SHA-256) via AWS KMS (`AwsKmsAuditSigner`) or customer KMS/HSM (`AuditCheckpointSigner`). Independent offline verification
  (`IndependentCheckpointVerifier`) enables external auditors to verify signatures using public keys without KMS access.
  External storage sink (`S3ObjectLockCheckpointSink`) writes immutable checkpoint records under S3 Object Lock retention
  (COMPLIANCE or GOVERNANCE mode). Complete verification (`AuditChainVerifier`) detects truncated database tails, rewritten
  historical records, tampered checkpoints, and missing checkpoints. SEC-008 compliance remains partially outstanding pending
  physical Object Lock bucket deployment and resolution of Open Decision M5-D15 (operational audit sink failure policy).

## Known gaps in the repository

- Platform tests: the full suite has pre-existing failures on Windows, not on Linux. The storage reader needs
  descriptor-relative access (`/dev/fd`), which Windows does not provide, so the product refuses those reads on Windows,
  as designed. Symlink tests also need a privilege that the Windows account lacks. Those failures are identical on a
  clean checkout of `HEAD`. Run the suite on Linux (the CI target) to get a real result.
- The litellm path is guarded only by the endpoint check. Its provider can still use configuration this repository does
  not control.
- Distributed processing (Phase 3): job ownership, attempts and fencing tokens live in a MongoDB lease store; a RabbitMQ quorum-queue adapter (a working baseline, NOT an approved decision) carries the messages; idempotency is a unique MongoDB record with Redis only coordinating its lock. Tenant isolation of queues (shared queue versus per-tenant queues or virtual hosts) is open. See `docs/team3-mongodb-foundation.md`.
- Audit checkpoints and external immutable sink: delivered in `src/doc_extractor/infrastructure/audit/`. Operational integration with background workers and synchronous vs asynchronous sink failure handling are tracked under Open Decision M5-D15.
- `tests/test_ci_policy.py`, `tests/test_api_foundation.py` and `tests/test_api_model_ownership.py` fail on clean `HEAD`
  as well. They are not caused by this work.

## Open items that need people outside the repository

1. Run the production scanner against a real clamd, including an EICAR test and an offline signature bundle (SEC-009).
2. Run the distributed-state tests against a real Redis, not only fakeredis (ARC-003).
3. Deny-all egress rule in the deployment manifest (SEC-011). Platform team.
4. Scheduled hash-chain verification job and alert on failure (SEC-008, SEC-021). Needs the alerting system.
5. Customer OIDC provider registration and SAML 2.0 enterprise bridge deployment (SEC-001, SEC-002).
6. Independent penetration test, SAST and dependency scanning in CI (SEC-015).
7. Backup and restore drill (SEC-016), KMS-backed key management and rotation (SEC-005, SEC-013).
8. Decisions M5-D08 (replay of a shown-once API-key secret), M5-D09 (webhook secret storage), M5-D15 (audit sink policy), M5-D16 (tenant-ID policy standard) and M5-D17 (self-service tenant provisioning approval).
