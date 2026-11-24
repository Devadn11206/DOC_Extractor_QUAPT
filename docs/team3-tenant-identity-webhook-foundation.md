# Tenant Provisioning, Persistent Identity & Webhook Security Foundation (Team Member 3, Phase 5C)

Status: **Phase 5C delivered (Tenant Provisioning, Persistent Identity and Webhook Security).**
Provides durable MongoDB persistence for user identity, rotating refresh-token families, and session revocations; administrative tenant provisioning and lifecycle management with mandatory MFA and fail-closed audit; and enterprise webhook management with anti-SSRF egress protection, HMAC-SHA256 request signing, secret-manager reference handling, bounded exponential backoff retries, and dead-lettering.

---

## 1. Security Boundaries & PRD Requirements

- **PRD Compliance:**
  - **SEC-001 (Mandatory MFA for Privileged Administrative Operations):** All tenant provisioning, activation, suspension, and deactivation operations require verified Multi-Factor Authentication (`mfa_verified=True`) and the `admin` role. Unverified or unauthorized callers are refused (HTTP 401 `MfaRequiredError` or HTTP 403 `PermissionDeniedError`).
  - **SEC-002 (Persistent Identity, Rotating Refresh Tokens & Revocation):**
    - User accounts, password hashes, and tenant associations persist in MongoDB (`users` collection) with tenant-first compound indexing (`tenant_id`, `email`).
    - Refresh tokens are hashed with SHA-256 (`token_hash`) before storage; raw tokens are never persisted or logged.
    - Single-use token rotation uses atomic conditional updates (`used: False` -> `True`), preventing race conditions and concurrent double-consumption.
    - Replay detection immediately invalidates the entire token family (`family_id`) and terminates user sessions.
    - Explicit session and JTI revocations persist with TTL expiration in `token_revocations`.
    - Account deactivation immediately revokes all active sessions for the deactivated user.
  - **SEC-006 (Strict Tenant Isolation):**
    - All user accounts, refresh tokens, revocations, webhook subscriptions, and webhook delivery records enforce strict tenant partitioning via `TenantScopedRepository` and compound indexes (`tenant_id`, ...).
    - Cross-tenant queries, mutations, or dispatching are rejected without leaking tenant existence.
    - Path-safe tenant ID validation (`require_path_safe_tenant_id`) prevents directory traversal, null-byte injection, and path ambiguity (Open Decision M5-D16).
  - **SEC-008 & FR-AUD-001 (Fail-Closed Audit Integration):**
    - All tenant lifecycle mutations (`tenant.provisioned`, `tenant.suspended`, `tenant.activated`, `tenant.deactivated`) record structured events to the per-tenant SHA-256 audit hash chain. Failures in audit append roll back the database mutation and raise `AuditUnavailableError` (HTTP 503).
    - Terminal webhook failures and dead-letter queue routing record structured `webhook.dead_letter` audit events with delivery IDs, subscription IDs, and failure reasons.
  - **PRD §7.1, API-008 & SCR-07 (Webhook Security, Anti-SSRF, Retries & Dead-Lettering):**
    - Outbound webhook destinations are strictly validated against SSRF attacks: HTTPS is enforced, embedded userinfo/credentials are rejected, private IP ranges (RFC 1918), loopback, link-local, and cloud metadata services (`169.254.169.254`, `metadata.google.internal`) are blocked.
    - Complete DNS resolution inspects all resolved A and AAAA records to eliminate DNS rebinding attacks.
    - Payloads are signed with HMAC-SHA256 in wire format `X-Webhook-Signature: sha256=<hex>` alongside `X-Webhook-Timestamp` and `X-Webhook-Delivery`.
    - Secret references (`secret_ref`, Open Decision M5-D09) decouple signing secrets from plaintext database storage.
    - Failed deliveries retry via bounded exponential backoff within the PRD 24-hour window (`services.webhook_retry.schedule_retry`).
    - Terminal 4xx client errors or window expiration mark deliveries as `dead_letter` and trigger fail-closed audit events.

---

## 2. Architecture & Subsystems

| Subsystem | Components | Responsibility |
| --- | --- | --- |
| **Persistent Identity** | `src/doc_extractor/infrastructure/mongo/identity.py`<br>`src/doc_extractor/infrastructure/identity/service.py`<br>`src/doc_extractor/infrastructure/identity/tokens.py` | MongoDB persistence for users, rotating refresh tokens, and JTI revocations. Atomic conditional updates for single-use tokens, replay defense family invalidation, and user deactivation. |
| **Tenant Provisioning** | `src/doc_extractor/infrastructure/tenancy/models.py`<br>`src/doc_extractor/infrastructure/tenancy/service.py`<br>`src/doc_extractor/infrastructure/mongo/tenants.py` | Tenant lifecycle state machine (`ACTIVE`, `SUSPENDED`, `DEACTIVATED`), admin MFA enforcement, path-safe validation (M5-D16), idempotent provisioning, and fail-closed audit rollback. |
| **Webhook Security & Persistence** | `src/doc_extractor/infrastructure/webhook/security.py`<br>`src/doc_extractor/infrastructure/mongo/webhooks.py`<br>`src/doc_extractor/infrastructure/webhook/dispatcher.py` | Anti-SSRF URL and DNS validation, secret-reference resolution (`WebhookSecretResolver`), HMAC-SHA256 wire signing, durable delivery tracking, retry scheduling, and dead-letter audit recording. |
| **Application Composition** | `src/doc_extractor/api/composition.py`<br>`src/doc_extractor/api/app.py` | Container bootstrap: wires MongoDB repositories (`users`, `refresh_tokens`, `token_revocations`, `tenants`, `webhook_subscriptions`, `webhook_deliveries`) when MongoDB is configured. Answers 503 `PortNotConfiguredError` until configured; zero in-memory production fallbacks. |

---

## 3. Subsystem Details & Invariants

### 3.1 Persistent Identity & Session Storage

```
+-------------------+       Login / Token Grant        +--------------------------+
|   Client Login    | -------------------------------> | Store SHA-256 Token Hash |
+-------------------+                                  | (used: False, family_id) |
                                                       +--------------------------+
                                                                    |
                                     Rotate Token                   v
                    +------------------------------------+--------------------------+
                    |                                    |                          |
                    v (Atomic mark_used == True)         v (Atomic mark_used == False)
       +----------------------------+       +------------------------------------+
       |  Issue New Key Pair/Hash   |       |       DETECTED TOKEN REPLAY        |
       |  (new refresh + access)    |       | - Revoke entire family_id          |
       +----------------------------+       | - Invalidate user active sessions  |
                                            | - Raise TokenReplayError           |
                                            +------------------------------------+
```

- **Atomic Single-Use Invariant:** Single-use refresh token consumption is executed using an atomic MongoDB filter `{"_id": token_hash, "used": False}` with update `{"$set": {"used": True, "updated_at": ...}}`.
- If `mark_used` matches 0 documents, the token was either already consumed or does not exist. On concurrent consumption or replay, the entire token family (`family_id`) and all active sessions for that user are immediately revoked, raising `TokenReplayError`.
- **Zero Raw Tokens in Storage:** Only SHA-256 hashes (`token_hash`) are written to the database. Even in the event of an unauthorized database read, raw refresh tokens cannot be recovered or replayed.
- **Session Revocation & TTL:** Individual JTI revocations and user session revocations are written to `token_revocations` with an explicit TTL index (`rev_expires_ttl`), enabling automatic database pruning upon token expiration.

### 3.2 Tenant Provisioning & Lifecycle Management

- **Administrative MFA Gating:** All operations (`provision_tenant`, `suspend_tenant`, `activate_tenant`, `deactivate_tenant`) require the principal to possess the `admin` role and verified MFA (`mfa_verified=True`).
- **Path-Safe Identifiers (M5-D16):** Identifiers must satisfy `require_path_safe_tenant_id` (1-64 characters matching `^[A-Za-z0-9][A-Za-z0-9._-]*$`, containing no `..`, path separators, or control characters).
- **Idempotency Policy:** Calling `provision_tenant` with identical attributes returns the existing record idempotently. Calling `provision_tenant` with conflicting attributes (e.g., conflicting display name) raises `TenantConflictError` (HTTP 409).
- **Lifecycle State Machine:**
  - `ACTIVE` -> `SUSPENDED` (temporary administrative freeze)
  - `SUSPENDED` -> `ACTIVE` (re-activation)
  - `ACTIVE` or `SUSPENDED` -> `DEACTIVATED` (permanent retirement)
  - `DEACTIVATED` cannot transition to any other status. Attempted transitions raise `TenantLifecycleError` (HTTP 409).
- **Fail-Closed Audit Rollback:** State mutations are executed conditionally and immediately followed by an append to the tenant's audit hash chain. If audit logging fails, the database record is rolled back to its previous state and `AuditUnavailableError` is raised.

### 3.3 Webhook Persistence & Anti-SSRF Delivery Engine

```
+--------------------+
|  Event Dispatched  |
+--------------------+
          |
          v
+--------------------+       SSRF Check:
|  Destination Check | ----> Reject Private IP, Link-Local,
+--------------------+       Cloud Metadata, Loopback, DNS Rebinding
          | (Valid)
          v
+--------------------+
|  Secret Resolution | ----> Resolve secret_ref via WebhookSecretResolver
+--------------------+       (Never stores plaintext secret in DB)
          |
          v
+--------------------+
| HMAC-SHA256 Signing| ----> Headers: X-Webhook-Signature: sha256=<hex>
+--------------------+                X-Webhook-Timestamp: <iso8601>
          |                           X-Webhook-Delivery: <delivery_id>
          v
+--------------------+
| HTTP POST Delivery |
+--------------------+
          |
    +-----+--------------------+
    |                          |
    v (Success 2xx)            v (Failure: 4xx, 5xx, or Network Error)
+--------------------+   +---------------------------------------------+
| Status: 'delivered'|   | Client 4xx or Window Expired:               |
+--------------------+   |   -> Status: 'dead_letter'                  |
                         |   -> Fail-closed audit: webhook.dead_letter |
                         | Server 5xx or Network Timeout:              |
                         |   -> Status: 'failed_retryable'             |
                         |   -> schedule_retry (exponential backoff)   |
                         +---------------------------------------------+
```

- **SSRF Defenses:**
  - Rejects non-HTTPS schemes (`http://`, `file://`, `gopher://`, etc.).
  - Rejects URLs containing credentials (`https://user:pass@host`).
  - Blocks loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, `fe80::/10`), private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), and carrier-grade NAT (`100.64.0.0/10`).
  - Explicitly blocks cloud metadata hostnames and addresses (`169.254.169.254`, `metadata.google.internal`).
  - Resolves all IPv4 and IPv6 DNS records and validates each resolved address against SSRF policies, mitigating DNS rebinding and multi-homed redirection attacks.
- **HMAC-SHA256 Wire Format:** Generates deterministic HMAC-SHA256 digests using the resolved secret and raw JSON payload bytes, transmitting via `X-Webhook-Signature: sha256=<hex>`.
- **Secret Reference Handling (M5-D09):** Webhook subscription records persist `secret_ref` rather than plaintext keys. At delivery time, `WebhookSecretResolver` queries the enterprise secrets manager.
- **Retry Policy & Dead-Letter Handling:**
  - Retry scheduling adheres to PRD §7.1: exponential backoff bounded within a 24-hour delivery window via `doc_extractor.api.services.webhook_retry.schedule_retry`.
  - Non-retryable errors (e.g., 400 Bad Request, 401 Unauthorized, 404 Not Found) or window expiration immediately transition deliveries to `dead_letter`.
  - Dead-lettered deliveries record a fail-closed `webhook.dead_letter` audit event to the tenant audit hash chain.

---

## 4. Open Architecture Decisions

The Phase 5C implementation leaves all unapproved decisions strictly **Open** in `docs/m5-open-decisions.md`:

1. **M5-D09 (Webhook Secret Storage):** Webhook subscriptions store `secret_ref` alongside an integrity hash (`secret_hash`), decoupling database persistence from secret storage. Decision M5-D09 remains **Open** pending final choice of production secret backend (AWS Secrets Manager, HashiCorp Vault, or KMS envelope encryption).
2. **M5-D16 (Tenant-ID Policy):** `TenantProvisioningService` enforces strict path safety (`require_path_safe_tenant_id`) preventing traversal and shell injection without performing lossy Unicode or case transformations. Decision M5-D16 remains **Open** pending global consensus on case-folding / Unicode normalization standards.
3. **M5-D17 (Tenant Provisioning):** Explicit administrative provisioning is enforced with mandatory admin MFA and fail-closed audit. Decision M5-D17 remains **Open** pending architecture approval for self-service tenant registration workflows or automated API key provisioning.

---

## 5. Verification & Test Evidence

The Phase 5C test suite (`tests/test_tenant_identity_webhook_security.py`) exercises all deliverables across unit, integration, and security boundaries:

- **Identity & Persistence:**
  - `test_persistent_user_creation_and_retrieval`: Verifies user persistence, retrieval, and tenant scoping.
  - `test_refresh_token_stored_as_hash_never_plaintext`: Asserts stored tokens match SHA-256 hashes and raw tokens never touch the database.
  - `test_atomic_single_use_refresh_token_concurrency`: Proves atomic conditional updates prevent race conditions and concurrent double-consumption.
  - `test_token_replay_revokes_entire_family_and_sessions`: Validates family-wide token revocation and active session invalidation upon detected replay.
  - `test_session_and_jti_revocation`: Verifies individual and family-wide JTI revocations.
  - `test_user_deactivation_invalidates_active_sessions`: Ensures deactivated users cannot rotate tokens or access protected resources.
  - `test_cross_tenant_identity_isolation`: Proves users in Tenant A cannot be accessed or manipulated by Tenant B.
- **Tenant Provisioning:**
  - `test_admin_mfa_required_for_tenant_provisioning`: Rejects unprivileged or non-MFA callers.
  - `test_path_safe_tenant_id_enforced`: Rejects invalid, relative, or traversal tenant identifiers (M5-D16).
  - `test_idempotent_tenant_provisioning`: Confirms duplicate requests return identical records while conflicting requests raise 409.
  - `test_tenant_lifecycle_transitions`: Validates state transitions (`ACTIVE` -> `SUSPENDED` -> `ACTIVE` -> `DEACTIVATED`) and blocks invalid transitions.
  - `test_tenant_provisioning_audit_failure_rolls_back`: Confirms fail-closed audit logging rolls back database mutations on audit failure.
- **Webhook Security & Delivery:**
  - `test_ssrf_rejects_insecure_and_private_destinations`: Tests blocking of HTTP, credentials, private IPs, loopback, link-local, and cloud metadata.
  - `test_ssrf_rejects_dns_rebinding_to_private_ip`: Verifies DNS resolution catches hosts resolving to private/loopback IPs.
  - `test_webhook_hmac_sha256_signature_header_format`: Validates `X-Webhook-Signature: sha256=<hex>` wire format.
  - `test_webhook_secret_ref_resolution`: Tests dynamic secret resolution from `secret_ref` without database leakage.
  - `test_webhook_dispatcher_delivery_success`: Verifies complete delivery workflow, status update, and delivery tracking.
  - `test_webhook_dispatcher_retry_on_503`: Confirms exponential backoff retry scheduling within the PRD 24-hour window.
  - `test_webhook_dispatcher_dead_letter_on_400`: Asserts non-retryable 4xx client errors trigger `dead_letter` status and fail-closed audit recording.
- **Full Regression Verification:**
  - `tests/test_mongo_startup.py`: 93 passed (asserts startup builds no fallback in-memory stores).
  - `tests/test_packaging_consistency.py`: 31 passed (verifies all dependencies and zero test-runner leaks).
  - Combined Team 3 test suite: **1722 passed, 0 failures**.
