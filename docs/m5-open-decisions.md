# M5 open decisions register

The decisions that the REST API foundation (Team Member 2) depends on and that are **not** ours to make. Every entry is
**Open** until its owner approves a resolution; the code never resolves one on its own and keeps the conservative behaviour.

**IDs.** `M5-Dnn` is the only label to use for these decisions, in code, tests and documents. Do **not** write a bare
`D1`, `D8`, ...: the PRD has its own, different decision table (PRD v1.5, `D1` Deployment ... `D20` Review and governance),
so "decision D8" is ambiguous. Each entry lists the *label the integration plan used* only so older notes can be traced.

**Not in this register.** Defects and review findings (tracked in commit messages and the review reports), and the choices
listed under "Choices already made in code" at the end, which are implemented but still need an owner's confirmation.

## Stages

The ordered plan from the M5 integration review. Stage 1 (foundation) is done; the rest are blocked by the decisions below.

| Stage | Scope |
| --- | --- |
| **S1** | API foundation: app factory, error envelope, auth and tenant ports, storage isolation, CI. **Done** |
| **S2** | Contract alignment: error catalogue, one status enum, published envelope JSON Schema |
| **S3** | Shared processing service (API and demo UI use one code path) |
| **S4** | Persistence adapters (with Team Member 3) |
| **S5** | Documents API: upload, status, result, list (API-001/002/003/015) |
| **S6** | Worker and synchronous single-page extraction (API-004) |
| **S7** | Review tasks and corrections (API-005/016) |
| **S8** | Webhooks (API-008, PRD 7.1) |
| **S9** | Export, masking and audit endpoints (API-017, SCH-014, API-024) |
| **S10** | Schema routers (API-006/007/018) and the API-key management router (API-019) |

## Summary

| ID | Decision | Owner | Blocks | Status |
| --- | --- | --- | --- | --- |
| [M5-D01](#m5-d01-error-code-names) | Error-code names | PRD owner | S2, S5, S7 | Open |
| [M5-D02](#m5-d02-nested-or-flat-error-envelope) | Nested or flat error envelope | PRD owner, backend lead | S2 | Open |
| [M5-D03](#m5-d03-null-confidence-and-model-version-keys) | Null confidence and `model_versions` keys | PRD owner | S5 | Open |
| [M5-D04](#m5-d04-document-status-vocabulary) | Document status vocabulary | PRD owner, Module A owner | S2, S5 | Open |
| [M5-D05](#m5-d05-records-for-rejected-files) | Records for rejected or unsupported files | PRD owner | S5 | Open |
| [M5-D06](#m5-d06-pagination-shape) | Pagination shape | PRD owner, backend lead | S2, S5 | Open |
| [M5-D07](#m5-d07-audit-values-versus-pii) | Audit values versus PII | PRD owner, Team Member 3 | S7, S9 | Open |
| [M5-D08](#m5-d08-replay-of-a-shown-once-api-key-secret) | Replay of a "shown once" API-key secret | PRD owner (security) | S10 (API-key router) | Open |
| [M5-D09](#m5-d09-webhook-secret-storage) | Webhook secret storage | Team Member 3, PRD owner | S8 | Open |
| [M5-D10](#m5-d10-synchronous-extraction-timeout) | Synchronous extraction timeout outcome | PRD owner | S6 | Open |
| [M5-D11](#m5-d11-review-task-states) | Review-task states | PRD owner | S7 | Open |
| [M5-D12](#m5-d12-auto-approval-policy) | Auto-approval policy | PRD owner (product) | S5, S7 | Open |
| [M5-D13](#m5-d13-role-vocabulary-in-tests) | Role vocabulary in tests | Team Member 2 | none (fixtures) | Open |
| [M5-D14](#m5-d14-split-of-module-a-process_document) | Split of Module A `process_document` | Phase 1 / Module A owner | S3, S5 | Open |
| [M5-D15](#m5-d15-audit-sink-policy) | Audit sink policy (best-effort versus durable) | Team Member 3, PRD owner | S10 (API-key router), S7, S9 | Open |
| [M5-D16](#m5-d16-tenant-id-policy) | Tenant-ID policy (case, Unicode normalization, format) | Team Member 3 | S4, S5 | Open |
| [M5-D17](#m5-d17-tenant-provisioning) | Tenant provisioning (explicit or on first write) | Team Member 3 | S4, S5 | Open |
| [M5-D18](#m5-d18-one-persistence-interface-for-the-document-record) | One persistence interface for the document record | Team Member 2 and 3 | S4, S5 | Open |

Fields below: **Description**, **PRD reference**, **Options** (A, B ... as proposed in the integration plan), **Plan's
recommendation** (a recommendation, not a decision), **Owner**, **Blocks**, **Status**, **Resolution**, **In the code**.

---

### M5-D01 Error-code names

- **Description.** The API answers with codes that differ from the PRD's. PRD section 19 and API-010 name `UNAUTHENTICATED`
  (401), `VALIDATION_FAILED` (422), `IDEMPOTENCY_CONFLICT` (409) and `INTERNAL` (500). The API uses `AUTHENTICATION_REQUIRED`,
  `INVALID_CREDENTIALS`, `TOKEN_EXPIRED`, `VALIDATION_ERROR`, `CONFLICT` and `INTERNAL_ERROR`; Module A's conflict code is
  `IDEMPOTENCY_KEY_CONFLICT`. M4 errors already use the PRD names. The API also introduces codes the PRD lacks
  (`SERVICE_UNAVAILABLE`, `AUDIT_UNAVAILABLE`, `FORBIDDEN_SCOPE`, `FORBIDDEN_PERMISSION`, `TENANT_ACCESS_DENIED`).
- **PRD reference.** PRD section 19 (codes marked [Proposed] are "to be fixed with the envelope decision in OQ-DOCX-04"); API-010.
- **Options.** A) align the API and Module A to the PRD and M4 names. B) amend the PRD to the API's names.
- **Plan's recommendation.** A (about 40 test references and Module A's conflict code would change).
- **Owner.** PRD owner. **Blocks.** S2, S5, S7. **Status.** Open. **Resolution.** none.
- **In the code.** Codes are unchanged. `src/doc_extractor/api/exceptions.py`, `tests/test_api_m4_exception_mapping.py`,
  `tests/test_api_model_ownership.py` (the duplicate `ConflictError` is pinned there until this is decided).

### M5-D02 Nested or flat error envelope

- **Description.** Whether errors are `{"error": {code, message, request_id, details}}` (nested, as built and as API-010 reads) or flat.
- **PRD reference.** API-010; OQ-DOCX-04.
- **Options.** A) nested as built. B) flat. **Plan's recommendation.** A (cheap now, rework if B).
- **Owner.** PRD owner, backend lead. **Blocks.** S2. **Status.** Open. **Resolution.** none.
- **In the code.** Nested everywhere (`build_error_response`).

### M5-D03 Null confidence and model-version keys

- **Description.** The canonical envelope emits `confidence` as null with a `confidence_provenance` field (a Phase 1
  field, not in the PRD) where the PRD specifies a number, and `run.model_versions` uses keys the PRD does not list.
- **PRD reference.** PRD 6.3 (output envelope); `model_versions` in the PRD data model.
- **Options.** A) amend the PRD to allow null (and add `multimodal_extractor` to the keys). B) emit 0.0 at the API boundary.
- **Plan's recommendation.** A. B invents data and contradicts the Phase 1 no-confidence policy.
- **Owner.** PRD owner. **Blocks.** S5 (result endpoint). **Status.** Open. **Resolution.** none.
- **In the code.** The envelope is not touched by the API.

### M5-D04 Document status vocabulary

- **Description.** PRD section 20 lists `queued` and `rejected_malware` as document statuses; Module A's `DocumentStatus`
  (SCH-012) has six values, and a new record starts as `processing`, not `queued`. `IngestionState` carries the extra values internally.
- **PRD reference.** PRD section 20; SCH-012; FR-ING-001 acceptance text; PRD section 19 (`rejected_malware` status).
- **Options.** A) the documents resource exposes SCH-012's six plus the two extras (the envelope stays six). B) map `queued` to `processing`.
- **Plan's recommendation.** A. B contradicts the FR-ING-001 acceptance text. A changes Module A's initial status.
- **Owner.** PRD owner, Module A owner. **Blocks.** S2, S5. **Status.** Open. **Resolution.** none.
- **In the code.** `DocumentResource.status` is Module A's `DocumentStatus` (`api/contracts/records.py`). The audit allow-list
  now derives from the enums (`api/ports/audit.py`), so it follows whatever this decision produces.

### M5-D05 Records for rejected files

- **Description.** A rejected or unsupported file leaves no record today (the PRD says nothing is stored), but a user story
  wants a visible row for it.
- **PRD reference.** US-DOCX-001; FR-ING-002/003.
- **Options.** A) the API creates a metadata-only record from the ingestion result. B) Module A creates it.
- **Plan's recommendation.** A, if a metadata row does not count as "stored" (needs a PRD clarification).
- **Owner.** PRD owner. **Blocks.** S5. **Status.** Open. **Resolution.** none.

### M5-D06 Pagination shape

- **Description.** PRD API-015 specifies `content[]`, `stats` and `paging` (page/size, default 10); the foundation's
  repository ports use `limit`/`offset` and return a bare list.
- **PRD reference.** API-015.
- **Options.** A) the PRD shape everywhere. **Plan's recommendation.** A (cheapest while no list endpoint exists).
- **Owner.** PRD owner, backend lead. **Blocks.** S2, S5. **Status.** Open. **Resolution.** none.
- **In the code.** `list_by_tenant(limit, offset)` and `list_tasks(...)` in `api/ports/repositories.py`.

### M5-D07 Audit values versus PII

- **Description.** FR-REV-002 and SEC-008 want old and new values of corrections in the audit trail; SEC-012, API-011 and
  PRD section 21 forbid values in telemetry.
- **PRD reference.** FR-REV-002; SEC-008; SEC-012; API-011; PRD section 21.
- **Options.** A) two sinks: a restricted, hash-chained audit store and value-free telemetry. B) audit values for non-PII classes only.
- **Plan's recommendation.** A. The audit store is Team Member 3's.
- **Owner.** PRD owner, Team Member 3. **Blocks.** S7, S9. **Status.** Open. **Resolution.** none.
- **In the code.** The audit metadata allow-list carries identifiers, counts and codes only (`api/ports/audit.py`).

### M5-D08 Replay of a "shown once" API-key secret

- **Description.** An idempotent replay of API-key creation would have to return the secret again, which contradicts
  "shown once". The WIP cached the plaintext secret for 24 hours to replay it.
- **PRD reference.** API-019; SEC-002.
- **Options.** A) a replay returns metadata only. B) keep the in-memory secret cache. C) reject duplicates.
- **Plan's recommendation.** A or C. B was the WIP behaviour and is removed.
- **Owner.** PRD owner (security). **Blocks.** S10 (API-key router). **Status.** Open. **Resolution.** none.
- **In the code.** Nothing caches or replays a secret; the API-key router is **not mounted** (`api/app.py`,
  `api/routers/__init__.py`); `tests/test_api_key_secret_retention.py`.

### M5-D09 Webhook secret storage

- **Description.** `WebhookSubscriptionRecord` stores a `secret_hash`, which cannot sign an HMAC; the PRD speaks of a `secretRef`.
- **PRD reference.** PRD 7.1 (webhooks); API-008.
- **Options.** A) a secret-manager reference. B) store the secret encrypted. **Plan's recommendation.** A (replace the field before any adapter exists).
- **Owner.** Team Member 3, PRD owner. **Blocks.** S8. **Status.** Open. **Resolution.** none.
- **In the code.** Flagged on `WebhookSubscriptionRecord`; no adapter or endpoint exists.

### M5-D10 Synchronous extraction timeout

- **Description.** API-004's 30 s limit has no error code or document outcome.
- **PRD reference.** API-004.
- **Options.** A) `504 EXTRACTION_TIMEOUT` [Proposed; not in the PRD], the document continues asynchronously.
- **Plan's recommendation.** A (needs a PRD addition).
- **Owner.** PRD owner. **Blocks.** S6. **Status.** Open. **Resolution.** none.

### M5-D11 Review-task states

- **Description.** Review-task states, release by a non-owner, SLA expiry, reopen and reject are unspecified. The
  foundation's `pending / in_review / approved / rejected` mixes task state with document outcome.
- **PRD reference.** FR-REV; API-005, API-016; `TASK_CLAIMED` in PRD section 19.
- **Options.** A) `unassigned` / `claimed` / `completed`, an administrator may release.
- **Plan's recommendation.** A (needs a PRD addition).
- **Owner.** PRD owner. **Blocks.** S7. **Status.** Open. **Resolution.** none.
- **In the code.** `ReviewTaskRecord` is marked PROVISIONAL (`api/contracts/records.py`); no adapter persists it.

### M5-D12 Auto-approval policy

- **Description.** The PRD expects auto-approval; Phase 1's no-confidence policy sends every model-read document to
  `needs_review`, so `document.completed` would fire only after human review.
- **PRD reference.** FR-CNF-003, FR-CNF-004.
- **Options.** A) keep for now. B) per-tenant opt-in once confidence is calibrated.
- **Plan's recommendation.** A. Do not invent a confidence proxy.
- **Owner.** PRD owner (product). **Blocks.** S5, S7. **Status.** Open. **Resolution.** none.

### M5-D13 Role vocabulary in tests

- **Description.** Some route-test fixtures use the roles `operator` and `viewer`, which are back-office roles; PRD
  section 15.5 says tenant roles are distinct (analyst, reviewer, ...).
- **PRD reference.** PRD section 15.5; SEC-003.
- **Options.** Use the PRD and M4 roles. **Owner.** Team Member 2. **Blocks.** none (fixture edits only).
- **Status.** Open (deliberately not changed in the hardening pass, to keep it minimal). **Resolution.** none.

### M5-D14 Split of Module A `process_document`

- **Description.** `process_document` ingests, renders and preprocesses in one blocking call, but the API must answer
  202 quickly and return `UNSUPPORTED_FILE` synchronously.
- **PRD reference.** API-001; ARC-003; FR-ING.
- **Options.** 1) no Module A change: the request path calls `ingest_document`, the worker later calls `process_document`
  with the same idempotency key (a second validation, hash and scan; the original upload must be persisted in between).
  2) split into `accept` and `prepare_pages`, with `process_document` unchanged in behaviour.
- **Plan's recommendation.** Option 2 (touches Module A and its tests). The replay path of option 1 was read from the code, not run.
- **Owner.** Phase 1 / Module A owner. **Blocks.** S3, S5. **Status.** Open. **Resolution.** none.
- **In the code.** Module A is untouched by the API.

### M5-D15 Audit sink policy

- **Description.** Whether a best-effort sink (`SafeStructuredAuditLogger`, which swallows its write failures) may back
  operations that are specified as mandatory audit, or whether those must refuse to run unless the configured logger
  raises on failure. Raised by the independent review (finding R7). Today the service is written to fail closed but only a
  logger that raises makes that effective, and the only logger shipped is best-effort.
- **PRD reference.** SEC-008; SEC-012.
- **Options.** A) refuse to build the service with a sink that declares `propagates_failures = False`. B) allow it with a
  warning (current behaviour). **Plan's recommendation.** none yet.
- **Owner.** Team Member 3, PRD owner. **Blocks.** S10 (API-key router), S7, S9. **Status.** Open. **Resolution.** none.
- **In the code.** Option B: `ApiKeyService` logs a warning; documented in `docs/m5-api-ownership.md`.

### M5-D16 Tenant-ID policy

- **Description.** Whether tenant identifiers are case-folded, Unicode-normalized or constrained to a format, and who issues
  them. The code folds and normalizes nothing and imposes no format: it only refuses identifiers that can leave their own
  directory, and stores non-simple identifiers under `h+<hash>` with an identity marker.
- **PRD reference.** SEC-006; SEC-001.
- **Options.** Owned by identity provisioning. **Owner.** Team Member 3. **Blocks.** S4, S5. **Status.** Open. **Resolution.** none.
- **In the code.** `src/doc_extractor/tenancy.py`; `docs/m5-api-ownership.md` (tenant isolation contract).

### M5-D17 Tenant provisioning

- **Description.** Who creates a tenant and when. Storage now provisions a tenant directory only on an explicit write (the
  review's finding R1 removed provisioning on read), so a tenant is still created implicitly by its first write. Whether a
  tenant must exist beforehand (explicit provisioning) is not decided.
- **PRD reference.** SEC-006; PRD 15.5.
- **Owner.** Team Member 3. **Blocks.** S4, S5. **Status.** Open. **Resolution.** none.
- **In the code.** `TenantDirectoryResolver.resolve(create=...)`; `tests/test_storage_read_write_separation.py`.

### M5-D18 One persistence interface for the document record

- **Description.** The one canonical record (Module A's `DocumentRecord`) is written through the synchronous,
  tenant-bound `StorageBackend.save_document_record` and read by the API through the asynchronous, tenant-per-call
  `DocumentMetadataRepository` port. Two abstractions for one entity need one source of truth before any adapter exists.
  Also open: the repository `create(tenant_id, record)` states in a docstring only that `record.tenant_id` must equal `tenant_id`.
- **PRD reference.** PRD section 20; OQ-DOCX-05 (store choice).
- **Owner.** Team Member 2 and Team Member 3. **Blocks.** S4, S5. **Status.** Open. **Resolution.** none.
- **In the code.** `doc_extractor/storage.py`, `doc_extractor/api/ports/repositories.py`; `docs/m5-api-ownership.md`.

---

## Choices already made in code, awaiting an owner's confirmation

These are implemented because the work required a behaviour, not because an owner decided it. They are reversible.

| Choice | Where | Basis |
| --- | --- | --- |
| `Retry-After` is bounded to **86,400 s**; larger values and far-future dates are clamped | `api/exceptions.py` (`MAX_RETRY_AFTER_SECONDS`) | Instruction for the hardening pass |
| The API requires an **explicit environment** (`DOC_API_ENV`) outside tests; documentation is off outside development | `api/config.py`, `.env.example` | Instruction for the hardening pass |
| Tenant-storage faults answer a generic `500 INTERNAL_ERROR`; the reason is logged server-side | `api/exceptions.py` | Instruction for the hardening pass |
| M4 `SchemaRollbackError` and `BenchmarkVerificationError` answer 422, other M4 conflicts 409 | `api/exceptions.py` (`_M4_STATUS_BY_CLASS_NAME`) | Implementer's choice; codes unchanged (see M5-D01) |
| An unwritable audit trail answers `503 AUDIT_UNAVAILABLE`; revocation stays applied | `api/services/api_key_service.py` | Implementer's choice (see M5-D15) |
| Cross-tenant access answers 404 and matches M4's wording marker `Cross-tenant access violation` | `api/exceptions.py` | PRD section 19 (404 `DOCUMENT_NOT_FOUND`); the marker is a stop-gap until M4 raises a typed error |
