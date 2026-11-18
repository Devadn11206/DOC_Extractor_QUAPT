# API foundation: who owns which record, queue and boundary

Read this before adding a model, a queue or an adapter. The rule is **one authoritative type per concept**:
extend the owner, never add a competitor. `tests/test_api_model_ownership.py` fails when a type named here is
missing, or when a second `DocumentRecord` appears.

## Records and DTOs

| Type | Module | Role | Persisted | Owner |
| --- | --- | --- | --- | --- |
| `DocumentRecord` | `doc_extractor.models` | **The one durable document record**: SCH-012 status, internal ingestion state, hash, claimed filename, idempotency key, error. Frozen. | Written at ingestion by `StorageBackend.save_document_record`; the API reads it through the `DocumentMetadataRepository` port | Module A (Phase 1) |
| `DocumentResource` | `doc_extractor.api.contracts.records` | **Response DTO** mapped from `DocumentRecord` (`from_record`). Omits `tenant_id` and `internal_state`; reduces the error to code and message (API-011). Not a domain model. | Never | API (Team Member 2) |
| `IdempotencyRecord` | `doc_extractor.models` | Upload idempotency (FR-ING-006, API-013), 24 h window | `IdempotencyStore` | Module A |
| `ReviewTaskRecord` | `doc_extractor.api.contracts.records` | The review domain's **own** entity. No authoritative model of a review task exists (Module B's `ReviewerOverrideRequest` is a one-shot type override, not a task). **Provisional**: its status vocabulary is open (decision D11). | `ReviewTaskRepository` port (no adapter yet) | API / Team Member 2, vocabulary: PRD owner |
| `ApiKeyRecord`, `ApiKeySummary`, `ApiKeyCreationResult`, `ApiKeyCreateRequest`, `ApiKeyRevokeResponse` | `...records` | API credentials: only a SHA-256 hash is stored; the secret is shown once and never cached or replayed (decision D8 open) | `ApiKeyRepository` port | API |
| `WebhookSubscriptionRecord`, `WebhookDeliveryRecord` | `...records` | Webhook subscriptions and delivery attempts (PRD 7.1). `secret_hash` cannot sign an HMAC: replace with a secret reference before an adapter exists (decision D9) | Ports, no adapter yet | API; secret storage: Team Member 3 |
| `AuditEventRecord` | `...records` | One platform audit event: identifiers and codes only (SEC-008, SEC-012). Corrections carrying old/new values are decision D7. | `AuditRepository` port | API shape; store and hash chain: Team Member 3 |
| `LifecycleEvent` | `schema_lifecycle.events` | Schema-lifecycle transitions inside the schema domain. Feeds the audit store; does not replace `AuditEventRecord` | M4 repository | M4 |
| `CanonicalExtractionEnvelope` | `doc_extractor.contracts_c` | The extraction result (PRD 6.3). **Not modified by the API.** | By the document/run store (Team Member 3) | Module C (Phase 1) |

PRD section 20 also lists `documentType`, `schemaId` and `schemaVersion` on a document. Module A's record does not
carry them yet. When the document endpoints need them, **add them to Module A's `DocumentRecord`** (owner approval),
then to `DocumentResource`. Do not reintroduce a parallel API record.

## Messaging: two different things

| | Module A stage hand-off | Durable job message |
| --- | --- | --- |
| Types | `ModuleAHandoffPayload`, `QueueAdapter`, `LocalMemoryQueueAdapter` (`doc_extractor.handoff`) | `DocumentProcessingMessage`, `QueueEnvelope`, `QueueProducer`, `QueueConsumer` (`doc_extractor.api`) |
| Meaning | The **result of ingestion** passed from Module A to Module B inside one pipeline run; carries page geometry and storage URIs | A **job**: "process document X" from the API to a worker (ARC-003); identifiers and options only |
| Durability | None: in-memory, development only, lost on restart | Required: survives restarts, redelivery with lease and `ack`/`nack`, dead-letter |
| Who produces | `ModuleAPipeline` | The API after accepting an upload |
| Who consumes | Module B | A worker, which then reads the persisted hand-off **by document id** |
| Adapter | `LocalMemoryQueueAdapter` | None yet (broker choice: Team Member 3) |

A job message never embeds a hand-off, page image or extracted content. Do not add a third queue abstraction.

## Tenant isolation contract (SEC-006)

- The tenant comes only from the authenticated principal (`ApiPrincipal.tenant_id`).
- Storage keys the tenant directory from the **exact** identity (`doc_extractor.tenancy`): ids of `[a-z0-9_-]` keep their
  own name (existing data is untouched), every other id maps to `h+<32 hex>` with a verified identity marker. Nothing
  is case-folded or normalized. Symlinks and aliases of another tenant's directory are refused.
- A cross-tenant resource is **404**, never 403 (the API maps the cross-tenant exceptions accordingly).
- An object-store backend must keep the same guarantees: a deterministic key per exact identity, identity verified on
  access, no aliasing.

## Audit semantics

Two different things must not be confused: **best-effort audit logging** and **mandatory durable auditing**.

| | Best-effort logging | Mandatory durable auditing |
| --- | --- | --- |
| Implementation in this repository | `SafeStructuredAuditLogger` (writes through Python `logging`) | **None.** A logger over `AuditRepository` is Team Member 3's (SEC-008) |
| A failed write | Caught and logged (exception *type* only); the caller is **not** told | **Must raise**, so the caller can refuse or roll back |
| Durable, append-only, hash-chained | No: exactly as durable as the process's log handlers | Required |
| Fail-closed | **No** | Yes, because failures propagate |

The service logic *is* written to fail closed, but only a logger that raises can make it effective:

| Operation | Audit requirement | If the logger **raises** | If the logger is best-effort (`SafeStructuredAuditLogger`) |
| --- | --- | --- | --- |
| API-key creation | Mandatory | The new key is revoked again and its secret is never returned; the caller gets `503 AUDIT_UNAVAILABLE` (effect `rolled_back`) | The key is created and returned; a lost write is **not** detected. Only an *invalid* event still raises |
| API-key revocation | Mandatory | The key stays revoked (access only shrinks); the caller gets `503 AUDIT_UNAVAILABLE` (effect `applied`) | The key is revoked; a lost write is **not** detected |
| Authentication failure | Best effort | Logged, never fatal: a failed write must not lock every client out | Same |

- There is no silent fallback: an unconfigured audit logger is `503 SERVICE_UNAVAILABLE` (port `AuditLogger`). Nothing is
  substituted for it.
- A logger that does not propagate failures declares `propagates_failures = False`; `ApiKeyService` logs a warning when it
  is built with one. This is a signal, not enforcement: whether such a sink may be used at all is an audit-storage policy
  decision for Team Member 3 and the owners (decision M5-D15 in `docs/m5-open-decisions.md`).
- `SafeStructuredAuditLogger` logs the *type* of its own write failure and never its text (which could carry a secret or a path).
- The API key router (API-019) is not mounted (decision M5-D08), so no HTTP route currently exercises these paths.

## What depends on Team Member 3 (not implemented here)

| Area | Needed from Team Member 3 |
| --- | --- |
| Persistence | Adapters for `DocumentMetadataRepository` (over Module A's record), `ReviewTaskRepository`, `ApiKeyRepository`, webhooks, audit; atomic status change plus review-task creation |
| Tenant identity | The tenant-ID policy: case folding, Unicode normalization, issuing format (nothing is folded or normalized in code); who provisions tenants |
| Authentication | User sessions / JWT (SEC-001/002), the source of the MFA claim, how the first administrator key is created, a scope catalogue and the role-by-endpoint matrix (SEC-003) |
| Queue | Broker, lease longer than a GPU job or heartbeats, retries and dead-letter behind `document.failed` |
| Locking | Cross-process idempotency (unique tenant+key), compare-and-set for status and task claim |
| Object storage | A backend for `StorageBackend` that serves page images and keeps the isolation contract above; encryption (SEC-005) |
| Audit | A real `AuditRepository` with the hash chain (SEC-008); where corrections' old/new values may live (D7) |
| Operations | Environment/secret source for production settings, a real malware scanner (SEC-009), a rate-limit store (API-012), Prometheus metrics |

Until those exist, ports answer **503 `SERVICE_UNAVAILABLE`**. Nothing falls back to a weaker mode silently.
