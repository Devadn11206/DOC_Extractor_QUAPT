# Schema Lifecycle Governance, RBAC & Four-Eyes Approval Foundation (Team Member 3, Phase 5B)

Status: **Phase 5B delivered (RBAC, Schema Governance and Four-Eyes Approval).**
Provides enterprise role-based access control (RBAC) across the seven canonical roles, four-eyes approval enforcement with author-approver duty separation and mandatory MFA, immutable version management with non-destructive rollback, durable MongoDB persistence, fail-closed audit chain integration, and clean API composition.

---

## 1. Security Boundaries & PRD Requirements

- **PRD Compliance:**
  - **SEC-003 (Seven Canonical Roles & Least Privilege):** Strictly enforces the canonical roles:
    1. `analyst` — Document submission, extraction viewing, schema reading (`schema:read`).
    2. `reviewer` — Manual human review, review-task claiming, schema reading (`schema:read`).
    3. `schema_author` — Schema definition, version creation, submission for approval (`schema:create`, `schema:read`, `schema:write`, `schema:test`).
    4. `schema_approver` — Schema approval, rejection, and version rollback (`schema:read`, `schema:approve`, `schema:rollback`). Privileged role requiring verified MFA.
    5. `integrator` — Machine API integration, webhook consumption, processing automation (`document:read`, `document:write`, `schema:read`).
    6. `auditor` — Read-only access to audit hash chains, signed checkpoints, verification reports, and schema histories (`audit:read`, `schema:read`).
    7. `admin` — System administration, user/role management, security configuration (`admin:*`, all scopes). Privileged role requiring verified MFA.
  - **SEC-001 (MFA for Privileged Operations):** All `admin` and `schema_approver` operations require verified Multi-Factor Authentication (`mfa_verified=True`) backed by trusted Phase 5A identity mechanisms (RFC 6238 TOTP or verified upstream OIDC assertions). Self-asserted caller claims are rejected (401 `MFA_REQUIRED` or 403 `FORBIDDEN`).
  - **SEC-006 (Strict Tenant Isolation):** All schema definitions, versions, active pointers, events, and approval records are partitioned with tenant-first compound indexes (`tenant_id`, ...). Cross-tenant access attempts are rejected without data leakage.
  - **SCH-001..007 (Schema Governance & Lifecycle):** Strict state machine (`draft` -> `testing` -> `approved` -> `retired`). Direct promotion to `approved` without testing and approval is blocked (409 Conflict). Immutable version records preserve exact historical schemas and diffs. Non-destructive rollback activates a prior approved version without deleting historical records.
  - **SEC-008 (Tamper-Evident Audit Integration):** All schema mutations (`schema.created`, `schema.version_created`, `schema.transitioned`, `schema.approved`, `schema.rejected`, `schema.rolled_back`) are recorded to the per-tenant SHA-256 audit hash chain. Fail-closed audit policy aborts and reverts mutations if audit recording fails (503 `AUDIT_UNAVAILABLE`).

---

## 2. Architecture & Subsystems

The Phase 5B deliverables span the governance layer, persistence layer, API routers, and application container:

| Component | Path | Responsibility |
| --- | --- | --- |
| **RBAC & Authorization** | `src/schema_lifecycle/roles.py` | `ProductionSchemaAuthorizer` & `DefaultSchemaAuthorizer`: Canonical role mapping, least-privilege scope checks, MFA gating for approver/admin, and author-approver duty separation. |
| **Approval Engine** | `src/schema_lifecycle/approval.py` | `SchemaApprovalService`: Four-eyes approval gate, benchmark harness report verification (`ApprovalPolicy`), single-use anti-replay consumption, and rejection recording. |
| **Lifecycle Manager** | `src/schema_lifecycle/manager.py` | `SchemaLifecycleManager`: State machine transitions, immutable versioning, rollback mechanics, and diff generation. |
| **MongoDB Persistence** | `src/doc_extractor/infrastructure/mongo/schemas.py` | `MongoSchemaRepository`: Production MongoDB persistence for schema snapshots, active version pointers, lifecycle events, and approval audit records with tenant-first indexing. |
| **API Wiring & Routes** | `src/doc_extractor/api/routers/schemas.py` | FastAPI router for `/schemas` endpoints enforcing RBAC permissions, MFA verification, tenant scoping, and fail-closed audit logging. |
| **Application Composition** | `src/doc_extractor/api/composition.py` | `ApplicationContainer`: Clean lifecycle bootstrap of `MongoSchemaRepository`, `SchemaLifecycleManager`, and `SchemaApprovalService`. Fail-closed (503) when unconfigured. |

---

## 3. Four-Eyes Approval Gate (SCH-005, SEC-003)

The four-eyes approval workflow enforces strict separation of duties between schema authors and approvers:

```
+-------------------+       Submit for Testing       +---------------------+
|   Schema Author   | -----------------------------> | Schema in 'testing' |
+-------------------+                                +---------------------+
          |                                                     |
          | Cannot self-approve!                                |
          X (author == approver BLOCKED)                        | Requires:
                                                                | 1. Distinct Approver (author != approver)
                                                                | 2. 'schema_approver' or 'schema:approve'
                                                                | 3. Verified MFA (mfa_verified=True)
                                                                | 4. Passing Benchmark Harness Report
                                                                | 5. Single-use replay protection
                                                                v
                                                     +---------------------+
                                                     | Schema in 'approved'|
                                                     |  (Active Pointer)   |
                                                     +---------------------+
```

### Invariants:
1. **Author Exclusion:** An author who submitted or authored a schema/version cannot approve it (`author != approver`). Attempted self-approval raises `SelfApprovalError` (HTTP 409).
2. **Distinct Authorized Approver:** The approving principal must hold the `schema_approver` role or explicit `schema:approve` permission in the matching tenant.
3. **MFA Verification:** The approving principal must have verified Multi-Factor Authentication (`principal.mfa_verified == True`). Unverified requests receive 401 `MFA_REQUIRED` or 403 `FORBIDDEN`.
4. **Harness Result Verification:** The approval submission must present a valid `BenchmarkReport` that meets or exceeds the tenant's `ApprovalPolicy` thresholds (e.g., F1 score, field-level recall). Reports with mismatches (wrong tenant, wrong schema ID, wrong version) or failing metrics are rejected with 422 `HARNESS_RESULT_INVALID`.
5. **Anti-Replay Protection:** Benchmark harness run IDs are single-use credentials recorded atomically in `schema_approval_records`. Replaying an already-consumed harness report raises `BenchmarkAlreadyUsedError` (HTTP 409).
6. **Concurrent Approval Guard:** Competing approval attempts on the same schema version are synchronized; once approved, further approval transitions are refused as invalid state transitions.

---

## 4. Schema Lifecycle & Version Management (PRD §6.1, §6.2)

### State Machine Transitions
- Supported states: `draft`, `testing`, `approved`, `retired`.
- Allowed forward transitions:
  - `draft` -> `testing` (submit for verification)
  - `testing` -> `approved` (via four-eyes approval gate)
  - `approved` -> `retired` (deprecation)
- Invalid transitions (e.g. direct `draft` -> `approved`, or `retired` -> `approved`) raise `InvalidStateTransitionError` (HTTP 409 Conflict).

### Versioning & Immutability
- Every version is immutable once stored.
- Semantic differences (`diff`) between versions are calculated across field definitions, validation rules, and metadata.
- Historical version records are never overwritten or deleted.

### Non-Destructive Rollback (SCH-006)
- Rollback allows reverting the active schema version to any previously approved version within the same tenant.
- Rollback operates non-destructively:
  - It does **not** erase subsequent versions from history.
  - It sets the active version pointer to the targeted historical version.
  - It records a `schema.rolled_back` event and audit log entry.
- Attempting to roll back to a non-existent version or a version that was never approved raises `SchemaRollbackError` (HTTP 422).

---

## 5. Durable MongoDB Persistence (`MongoSchemaRepository`)

`MongoSchemaRepository` provides production persistence backed by MongoDB with strict tenant-first compound indexes:

### Collections
1. `schema_snapshots`: Stores immutable schema versions indexed by `(tenant_id, schema_id, version)` [Unique].
2. `schema_active_pointers`: Stores the currently active approved version pointer indexed by `(tenant_id, schema_id)` [Unique].
3. `schema_events`: Append-only event history (`created`, `transitioned`, `approved`, `rolled_back`) indexed by `(tenant_id, schema_id, event_id)` and `(tenant_id, schema_id, timestamp)`.
4. `schema_approval_records`: Immutable records of four-eyes approvals and single-use harness tracking indexed by `(tenant_id, schema_id, version)` and `(tenant_id, harness_run_id)` [Unique].

### Pure Startup & Anti-Fallback Policy
- In staging, production, and development, unconfigured MongoDB settings leave `container.schema_lifecycle_manager` as `None`.
- Route calls to schema endpoints when persistence is unconfigured immediately return HTTP 503 `PortNotConfiguredError` / `SERVICE_UNAVAILABLE`.
- No in-memory fallback stores, mocks, or local memory simulations are permitted in production infrastructure (verified by static AST/token guard in `tests/test_mongo_startup.py`).

---

## 6. Tamper-Evident Audit Integration (SEC-008)

All schema operations record structured events to the tenant-scoped audit hash chain:

| Event Type | Outcome | Recorded Metadata | Fail-Closed Behavior |
| --- | --- | --- | --- |
| `schema.created` | `SUCCESS` | `schema_id`, `version`, `author_id` | Operation aborted and reverted on audit failure |
| `schema.version_created` | `SUCCESS` | `schema_id`, `version`, `author_id` | Version aborted on audit failure |
| `schema.transitioned` | `SUCCESS` | `schema_id`, `version`, `from_state`, `to_state` | State transition reverted on audit failure |
| `schema.approved` | `APPROVED` | `schema_id`, `version`, `approver_id`, `harness_run_id` | Approval reverted on audit failure |
| `schema.rejected` | `REJECTED` | `schema_id`, `version`, `approver_id`, rejection reason | Failure audit logged; HTTP 409/422 returned |
| `schema.rolled_back` | `ROLLED_BACK` | `schema_id`, `target_version`, `previous_version`, `operator_id` | Pointer restored on audit failure |

---

## 7. Open Decisions Register Alignment

- **M5-D12 (Auto-Approval Policy):** Preserved as Open. Product policy requires explicit human approval; auto-approval is not enabled without explicit tenant-level policy configuration.
- **M5-D13 (Role Vocabulary in Tests):** Preserved as Open. Canonical 7 roles (`analyst`, `reviewer`, `schema_author`, `schema_approver`, `integrator`, `auditor`, `admin`) are strictly enforced in production code. Legacy test fixtures using operator/viewer remain isolated.
- **M5-D15 (Audit Sink Policy):** Preserved as Open. Fail-closed audit policy is enforced across all schema mutation routes (unwritable audit returns HTTP 503 `AUDIT_UNAVAILABLE`).
- **M5-D16 / M5-D17 (Tenant Isolation & Provisioning):** Strictly isolates all schemas by `tenant_id`. Schemas belonging to Tenant A cannot be read, modified, or approved by Tenant B.
