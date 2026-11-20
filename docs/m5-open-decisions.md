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
  repository ports use `limit`/`offset` and return a bare list. For Milestone 2D, this blocks the API-015 pagination and
  filtering contract (`GET /api/v1/documents`), resolving alignment between repository port `list_by_tenant(limit, offset)`
  and the PRD API-015 envelope (`content[]`, `stats`, and `paging` with 10 per page default).
- **PRD reference.** API-015.
- **Options.** A) the PRD shape everywhere. **Plan's recommendation.** A (cheapest while no list endpoint exists).
- **Owner.** PRD owner, backend lead. **Blocks.** S2, S5. **Status.** Open. **Resolution.** none.
  - *Provisional implementation assumption (API-015):* `GET /api/v1/documents` answers the PRD envelope (`content[]`, `stats`, `paging`) with `page` (default 1) and `size` (default 10, provisional maximum 100), and `paging` carries `page`, `size`, `total_items` and `total_pages`. These names, the maximum size and the sort parameters (`sort`, `order`) are this implementation's choice, not owner-approved, and may change when this decision is made. The repository port `list_by_tenant(limit, offset)` is NOT used: it has no adapter, is `async`, returns a bare list and cannot supply totals, statistics, date filters, prefix search or sorting. The details are in "M2D API-015 Provisional Behavior and Limitations" below.
- **In the code.** `list_by_tenant(limit, offset)` and `list_tasks(...)` in `api/ports/repositories.py`; for API-015: `src/doc_extractor/api/services/document_listing.py`, `src/doc_extractor/api/services/document_read_service.py`, `src/doc_extractor/api/contracts/documents.py`, `src/doc_extractor/api/routers/documents.py`.

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
  For Milestone 2D, this blocks the authoritative persistence interface and StorageBackend read ownership: whether the API
  reads and updates document records directly via `StorageBackend` or exclusively through `DocumentMetadataRepository` (with
  `StorageBackend` restricted to page images and artifacts). Also open for M2D: which component owns persisting terminal status
  (`auto_approved`, `needs_review`, `failed`) and the `CanonicalExtractionEnvelope` upon processing completion.
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

---

## Milestone 2D unresolved decisions (M2D)

The following decisions were identified during the Milestone 2D architecture audit for REST endpoints and page image retrieval.
Per repository convention, existing decisions must not be assigned duplicate IDs; new decisions use the `M2D-Dnn` prefix.

| ID | Decision | Owner | Blocks | Status |
| --- | --- | --- | --- | --- |
| Existing: M5-D18 | Authoritative persistence interface and StorageBackend read ownership | Team Member 2 and 3 | S4, S5 | Open |
| Existing: M5-D06 | API-015 pagination and filtering contract | PRD owner, backend lead | S2, S5 | Open |
| M2D-D01 | Terminal status and canonical envelope persistence owner | Team Member 2 and 3 | S4, S5 | Open |
| M2D-D02 | SEC-007 API masking policy | PRD owner (security) | S5, S7, S9 | Open |
| M2D-D03 | Default page-image representation | PRD owner, API lead | S5 | Open |
| M2D-D04 | Authorization scopes for page-image retrieval | PRD owner, Team Member 3 | S5 | Open |
| M2D-D05 | Missing or not-yet-rendered page error code | PRD owner | S5 | Open |

### M2D Decision Details

#### M2D-D01 Terminal status and canonical envelope persistence owner

- **Description.** When document extraction completes (either auto-approved or routed to needs-review), the processing
  pipeline produces a final `DocumentStatus` and a `CanonicalExtractionEnvelope`. Who is authoritative for persisting this
  envelope and updating terminal status:
  - Option A: `StorageBackend` writes the envelope into tenant storage (e.g. `extracted/envelope.json`) and updates local record status.
  - Option B: The API layer / worker writes through a durable document/run repository owned by Team Member 3.
  - Option C: A two-tier model where the raw envelope JSON is written to `StorageBackend` while structured status and metadata live in the relational repository.
- **PRD reference.** PRD section 6.3 (Canonical Extraction Envelope), PRD section 20 (Data Model); ARC-003.
- **Owner.** Team Member 2 and Team Member 3. **Blocks.** S4, S5. **Status.** Open. **Resolution.** none.
  - *Provisional implementation assumption (API-002):* Nothing persists a terminal status or an envelope today, so `GET /api/v1/documents/{id}` returns only the persisted Module A `document_record.json` metadata. The stored `status` is returned exactly as persisted and is NOT authoritative (see "M2D API-002 Provisional Behavior and Limitations" below), and `result` is always `null`. This is not a choice among Options A, B or C.
  - *Provisional implementation assumption (API-015):* `GET /api/v1/documents` lists only the persisted Module A records and counts PERSISTED statuses in `stats`. Because no writer persists a terminal status, `stats.needs_review`, `stats.auto_approved` and `stats.failed` read 0 today; a 0 means "none persisted", not "none exist". Documents rejected before the record is saved are never listed or counted, and a document whose ingestion failed after the record was saved stays listed as `processing`. There is no document type or confidence to filter or sort by. This is not a choice among Options A, B or C.
- **In the code.** `src/doc_extractor/processing_service.py`, `doc_extractor/contracts_c.py`, `src/doc_extractor/api/services/document_read_service.py`, `src/doc_extractor/api/contracts/documents.py`.

#### M2D-D02 SEC-007 API masking policy

- **Description.** PRD SEC-007 and SCH-014 require that fields marked PII in schema definitions (`pii_class`) SHALL be masked
  by default in API responses, UI and exports, with unmasking restricted to callers with explicit role permissions. Open questions for M2D:
  - What masking transform applies at the API boundary (e.g., full redaction `***`, partial masking `****1234`, or null value with `masked=True` flag)?
  - How is visual evidence text (`evidence.text`) masked when it contains PII?
  - What exact role/permission string grants unmasking (`documents:unmask`, `pii:reveal`, or admin role)?
  - Who owns the masking transform component (API presentation layer DTO mapping vs. core extraction envelope assembly)?
- **PRD reference.** SEC-007, SCH-014, API-017, API-021, PRD section 10.1.
- **Owner.** PRD owner (security), Team Member 2. **Blocks.** S5, S7, S9. **Status.** Open. **Resolution.** none.
  - *Provisional implementation assumption (API-002):* No extracted value or evidence text is returned (`result` is always `null`), so no masking transform is chosen or applied. The client-supplied `filename` is still returned as a plain string because it is an existing field of the sanctioned `DocumentResource`; whether it must be masked is part of this decision and remains unresolved.
  - *Provisional implementation assumption (API-015):* each list row is the same `DocumentResource`, so `GET /api/v1/documents` returns, for every listed document in bulk, the client-supplied `filename`, the `sha256` content hash, the `mime_type` and the `file_size_bytes`, together with the other fields of that row (`document_id`, `status`, `page_count`, `created_at`, `updated_at`, `error_code`, `error_message`). The PRD list screen (SCR-02) does not display filenames or hashes, so this widens the exposure that API-002 introduced for one document at a time. Whether list rows must omit or mask `filename` or any other of these fields is part of this open decision. `q` searches the document ID only, never the filename.
- **In the code.** `src/schema_lifecycle/metamodel.py`, `src/doc_extractor/api/contracts/records.py`, `src/doc_extractor/api/contracts/documents.py`.

#### M2D-D03 Default page-image representation

- **Description.** PRD API-003 and US-DOCX-007 specify `GET /api/v1/documents/{id}/pages/{n}` returning the "page image". The
  storage backend maintains both `original` (raw rendered PDF page) and `preprocessed` (contrast-enhanced, deskewed) image files.
  Open questions for M2D:
  - Which image artifact should be returned by default: preprocessed image (what the model read) or original image (clean visual representation for human reviewer)?
  - What response format / Content-Type should be returned: raw binary image bytes (`image/png` / `image/jpeg`), base64 JSON payload, or a redirect (302) to a presigned object storage URL?
  - Should a query parameter (e.g. `?variant=original|preprocessed`) allow selecting the artifact?
- **PRD reference.** API-003, US-DOCX-007, PRD section 5.2.
- **Owner.** PRD owner, API lead (Team Member 2). **Blocks.** S5. **Status.** Open. **Resolution.** none.
  - *Provisional implementation assumption (M2D Slice 1):* Default to returning original rendered 200 DPI PNG bytes as `image/png` with `Content-Disposition: inline; filename="page-{page_number}.png"`, `Cache-Control: private, no-store`, and `X-Content-Type-Options: nosniff`.
- **In the code.** `src/doc_extractor/storage.py`, `src/doc_extractor/processing_service.py`, `src/doc_extractor/api/services/document_read_service.py`, `src/doc_extractor/api/routers/documents.py`.

#### M2D-D04 Authorization scopes for page-image retrieval

- **Description.** PRD API-003 states that page image retrieval is "subject to permission". PRD section 15.5 defines distinct
  roles (analyst, reviewer, auditor, admin). Open questions for M2D:
  - What specific scope governs page image access (e.g., standard `documents:read`, dedicated `pages:read`, or reviewer role requirement)?
  - Can a non-reviewer client role retrieve page images, or is image access restricted because page images may contain unmasked raw PII/PHI?
  - How does SEC-006 tenant isolation interact with page-image authorization (cross-tenant access must answer 404, but does an unauthorized tenant user answer 403 or 404)?
- **PRD reference.** API-003, SEC-003, SEC-006, PRD section 15.5.
- **Owner.** PRD owner (security), Team Member 3. **Blocks.** S5. **Status.** Open. **Resolution.** none.
  - *Provisional implementation assumption (M2D Slice 1):* Enforce provisional scope `SCOPE_DOCUMENTS_READ = "documents:read"`. Cross-tenant attempts answer concealed 404 (`DOCUMENT_NOT_FOUND`), whereas principals authenticated within the tenant who lack the scope receive 403 (`FORBIDDEN_SCOPE`).
  - *Provisional implementation assumption (API-002):* `GET /api/v1/documents/{id}` enforces the same provisional `documents:read` scope with the same 404/403 behaviour. Whether document metadata needs a different scope than page images is part of this open decision.
  - *Provisional implementation assumption (API-015):* `GET /api/v1/documents` enforces the same provisional `documents:read` scope (403 `FORBIDDEN_SCOPE` without it, 401 without authentication). Whether listing needs its own scope is part of this open decision.
- **In the code.** `src/doc_extractor/api/ports/authentication.py`, `src/doc_extractor/api/contracts/documents.py`, `src/doc_extractor/api/routers/documents.py`.

#### M2D-D05 Missing or not-yet-rendered page error code

- **Description.** When `GET /api/v1/documents/{id}/pages/{n}` is requested, the page number `n` might be out of range
  (`n > page_count` or `n < 1`), or the document may still be undergoing asynchronous Module A ingestion/rendering so page `n`
  is not yet available on disk. Open questions for M2D:
  - For out-of-range pages: Should the API return `404 DOCUMENT_NOT_FOUND` (concealing document structure per SEC-006), `404 PAGE_NOT_FOUND`, or `422/400 INVALID_PAGE_NUMBER`?
  - For pages still rendering: Should the API return `409 CONFLICT` (or `409 PAGE_PROCESSING`), `503 SERVICE_UNAVAILABLE`, or `404`?
  - Error code naming alignment with PRD section 19 and M5-D01.
- **PRD reference.** API-003, API-010, SEC-006, PRD section 19, M5-D01.
- **Owner.** PRD owner. **Blocks.** S5. **Status.** Open. **Resolution.** none.
  - *Provisional implementation assumption (M2D Slice 1):* Missing, out-of-range (`n > page_count`), or not-yet-rendered pages return concealed 404 (`DOCUMENT_NOT_FOUND`, "Document not found.") with empty details to prevent enumeration. Path parameter validation (`n < 1` or non-integer) returns 422 (`VALIDATION_ERROR`).
- **In the code.** `src/doc_extractor/api/exceptions.py`, `src/doc_extractor/api/services/document_read_service.py`.

### M2D Remediation Follow-ups

- **F5: Bounded or streaming file reads. Status: open follow-up (not implemented).** Bound or stream the reads in `DocumentReadService` so a file cannot load arbitrarily large content into memory. Three reads are affected and none has an explicit byte bound or streaming strategy today:
  - API-003 page-image reads (`get_page_image` reads the whole PNG into memory before validating it);
  - API-002 handoff reads (`module_a_handoff.json`, read in full and parsed by `get_page_image`);
  - API-002 document-record reads (`document_record.json`, read in full and parsed by `get_document`);
  - API-015 listing reads (`list_documents` reads and parses the `document_record.json` of EVERY candidate document on every request). The number of candidate directories is bounded by the provisional scan ceiling of 10,000 and the number of directory entries examined by a second ceiling of 20,000, but the SIZE of each record file is not.

  These files are written only by Module A, which is why this is deferred, but a very large file would be loaded completely. Bounded or streaming reads remain deferred; no new streaming architecture was introduced.
- **F6: Sanitized logging with fixed reason codes. Status: resolved for `DocumentReadService` only; the wider audit remains open.**
  - *Done (M2D Slice 1 remediation).* Every `logger.*` call in `src/doc_extractor/api/services/document_read_service.py` was inspected. Each now uses a constant message with a fixed reason code, an exception class name or an integer `errno`; no raw exception text, handoff URI, tenant identifier, document identifier or handoff key name is interpolated. `StorageError` details raised by this module carry a fixed `reason` code instead of the raw URI or foreign tenant value. Regression tests in `tests/test_api_documents_page_image.py` cover log forging through a newline in a URI or a handoff key, foreign tenant and document identifiers, and decode-error text, and a static AST test fails if a logger call takes an untrusted name.
  - *Inspected on the same request path, no change needed.* `api/exceptions.py`: `document_extractor_error_handler` logs only the error code, the fixed `reason` and the request id, and `unhandled_exception_handler` logs nothing.
  - *Not audited for untrusted content, still open.* Logging in these other modules is outside this follow-up: `src/doc_extractor/api/providers/api_key_auth.py` (authentication provider, on the request path), `src/doc_extractor/api/app.py`, `src/doc_extractor/api/ports/audit.py`, and `src/doc_extractor/api/composition.py` (tenant identifiers and exception text in cleanup messages), plus any server-level logging of unhandled exceptions. The review found no logger calls in `src/doc_extractor/storage.py`, `src/doc_extractor/tenancy.py` or `src/doc_extractor/handoff.py`. F6 must not be treated as closed for the API as a whole until the modules listed above are reviewed.
- **F7: Strengthen handoff structural integrity validation. Status: open follow-up (not implemented).** Validate duplicate page numbers, page sequence continuity and `page_count` consistency in the handoff. `get_page_image` still takes the first page record whose number matches and does not compare `page_count` with `pages`.

### M2D API-002 Provisional Behavior and Limitations

`GET /api/v1/documents/{document_id}` (API-002) implements only the persisted-metadata portion of the PRD contract
("status, metadata and, when ready, the output envelope"). The envelope portion is NOT implemented. No owner decision
below is approved or resolved by this behaviour; M2D-D01, M2D-D02 and M2D-D04 stay **Open**.

- **Source of the data.** The Module A `document_record.json` of the caller's tenant, read through the same hardened
  descriptor-relative walk as the page image (`DocumentReadService.get_document`), not through the path-based
  `LocalDiskStorage.get_document_record`. This reads the existing store and is not a new persistence adapter; the
  authoritative persistence interface remains open (M5-D18).
- **Response.** `DocumentDetailResponse` extends the sanctioned `DocumentResource` (`document_id`, `status`, `filename`,
  `mime_type`, `file_size_bytes`, `page_count`, `sha256`, `created_at`, `updated_at`, `error_code`, `error_message`) and adds
  `result`, typed `None` and always returned as JSON `null`. `tenant_id`, `internal_state`, `idempotency_key`, storage URIs
  and paths are never returned.
- **`status` is persisted but NOT authoritative.** Module A writes `status="processing"` (internal state `queued`) once at
  ingestion and nothing updates it. It is returned exactly as stored and never inferred, so a document whose extraction
  finished by another route (for example a synchronous extraction, whose result is not persisted) still reads `processing`.
  `queued` and `rejected_malware` from PRD section 20 are not part of the status enum (M5-D04) and are never emitted.
- **Timestamps.** `updated_at` equals `created_at` for every current record and is never synthesized. `created_at` and
  `updated_at` keep their persisted INSTANT; the serialized text may be normalized by the response DTO (for example a
  persisted `+00:00` offset can be written as `Z`), so the text is not guaranteed to be byte-identical to the stored string.
  A timestamp is validated with the same parser the DTO uses; text it rejects is an invalid record (generic `500`, reason
  `record_timestamp_invalid`).
- **`result` is always `null`** until a canonical extraction envelope is persisted (M2D-D01). Nothing is invented: there is
  no `document_type`, `schema_id`, `schema_version`, classification, confidence, model version, run id, prompt version,
  field, flag or evidence in this response.
- **Ingestion failures and the record.** Whether a document record exists depends on WHEN ingestion fails:
  - Failures before the record is saved (validation and unsupported-file rejections, malware detection, scanner
    failures) write no document record, so those documents answer the concealed 404. PRD section 19 lists a
    `rejected_malware` status; making such documents retrievable needs a persistence decision and is not done here.
  - A failure after the record has been saved (for example while saving the idempotency record or running the downstream
    callback, which Module A reports as a failed ingestion) can leave the record persisted with its original
    `processing` status, because no terminal-status update writer exists. API-002 returns the status actually stored in
    that record and does not infer a terminal status, so it can report `processing` for such a document.
  Not every failed ingestion is absent from storage.
- **`filename`** is the client-supplied name, returned only as a string and never interpreted as a path. Its exposure is
  provisional pending M2D-D02. API-002 imposes no length limit on it, because Module A imposes none (a 2,000-character
  basename is accepted and persisted) and the response DTO has none.
- **Error text is allow-listed, and a malformed stored error is refused.** A stored error is accepted only as `null`, `{}`
  (both mean no error) or an object with a non-empty string `code` and, if present, a `message` that is a string or
  `null`; other keys such as `details` and `request_id` are tolerated but never used or exposed. `error_code` /
  `error_message` then come from a fixed table (`UNSUPPORTED_FILE`, `FILE_TOO_LARGE`, `MALWARE_DETECTED`, with the PRD
  section 19 texts); any other non-empty string code becomes `INTERNAL_ERROR` with the generic message, and that unknown
  code and the stored message are withheld. Any other shape (a non-object, an object without a string `code`, a non-string
  `message`) is an invalid record: generic `500`, reason `record_error_invalid`, never a fabricated successful answer. No
  stored error exists today (the field is always empty); the validation and allow-list protect future writers.
- **Errors.** Unknown, malformed, wrong-case and foreign ids, a directory without a record, and a record whose tenant or
  document id differs from the principal's tenant or the requested id are all the same concealed `404 DOCUMENT_NOT_FOUND`.
  An unreadable, non-JSON, non-object or invalid record (missing keys, wrong types, unknown `status` or `internal_state`,
  an invalid timestamp, a malformed stored error) and any symlink, FIFO, socket, directory or other special file at the
  record path are a generic `500` with a fixed reason
  logged server-side. A shut-down container is the typed `503`.
- **Logging.** The log statements added for API-002 follow the F6 rule (fixed reason codes, exception class names, integer
  `errno`) and are covered by the same static test; the F6 scope statement above is unchanged (`DocumentReadService` only).

### M2D API-015 Provisional Behavior and Limitations

`GET /api/v1/documents` (API-015) implements only the persisted-metadata portion of the PRD contract. Every choice below is a
PROVISIONAL assumption made so the endpoint can exist before the owner decisions are taken. None of them is owner-approved;
M5-D06 and M2D-D01, M2D-D02 and M2D-D04 stay **Open**.

- **Source of the data and cost.** The endpoint enumerates the authenticated principal's OWN tenant directory through the
  same hardened descriptor-relative walk as API-002 and API-003 and reads every `document_record.json` in it
  (`DocumentReadService.list_documents`). It is a FULL SCAN on every request: there is no index and no persistence adapter,
  and the repository port `list_by_tenant` is not used. Filtering, sorting, totals and statistics are computed in memory by
  the pure `document_listing` module from one snapshot per request. Measured on a development machine (macOS, warm cache,
  local disk), one request through the route took about 0.06 s at 2,000 records and about 0.3 s at 10,000 records, with
  about 8 MB of peak memory for the scan; those figures are not a Linux, cold-cache or network-storage measurement. A real index or adapter belongs to M5-D18 and M2D-D01.
- **Provisional scan ceilings (two, both placeholders, not PRD figures).** The tenant directory is STREAMED with `os.scandir`
  (never materialized) and two limits are enforced while it is read. Exceeding either one stops the enumeration at once and
  refuses the request with the typed `DocumentListingLimitExceededError`, a generic `500`. The result is NEVER truncated and
  no partial list or statistics are produced, because a truncated list would silently shrink `total_items` and `stats`.
  - **10,000 candidate document directories** (`_MAX_LISTED_ENTRIES`): names that are valid document ids, excluding
    `idempotency`. Each candidate costs a directory open, a file read and a JSON parse. Fixed reason
    `document_listing_limit_exceeded`.
  - **20,000 directory entries of any kind** (`_MAX_EXAMINED_DIRECTORY_ENTRIES`): documents, the identity marker,
    `idempotency`, stray files and junk all count. It bounds the cost of enumerating the directory itself, so a directory
    with a huge number of unrelated entries cannot cost unbounded time or memory; enumeration stops at the first entry beyond
    the limit. Fixed reason `directory_entry_limit_exceeded`. A tenant with exactly 20,000 entries is listed; with 20,001 it is
    refused. It must stay larger than the candidate limit by at least the handful of non-document entries a real tenant
    directory holds.
  Both limits are checked before any record is read (the entry limit during enumeration, the candidate limit as soon as it
  is crossed). A legitimate tenant holding more than 10,000 documents, or more than 20,000 entries, is refused until an
  index replaces the scan (M5-D18, M2D-D01).
- **Response.** `{"content": [...], "stats": {...}, "paging": {...}}`. Each row is the sanctioned `DocumentResource` built with
  API-002's public error allow-list (a stored error message, request id and details are never returned), so it has the same
  fields as API-002 without the reserved `result` key. There is no document type, schema, confidence, `result` or any other
  field that is not persisted. `Cache-Control: private, no-store` is set.
- **`status` is persisted but NOT authoritative.** Module A writes `processing` once and nothing updates it (see the API-002
  section). The `status` filter and the `status` sort act on that stored value only.
- **`stats` are persisted-status statistics only.** The five PRD keys are `total`, `processing`, `needs_review`,
  `auto_approved` and `failed`. They are counted over the COMPLETE valid record set of the tenant, BEFORE any filter, so no
  filter, page or sort changes them. `total` counts every valid record; each other key counts the records whose persisted
  `status` equals it. `reviewed` and `rejected` records count only toward `total`, so the categories need not add up to
  `total`. Because no terminal-status writer exists, every current record is `processing`: `needs_review`, `auto_approved` and
  `failed` read 0, which means "none persisted", never "none exist".
- **Supported filters.** `status` (a `DocumentStatus` value; anything else is `422 VALIDATION_ERROR`), `from` / `to` and `q`.
  - `from` / `to` filter on `created_at` (`updated_at` is always equal to it). Both bounds are INCLUSIVE. Timestamps are
    compared as instants, so mixed UTC offsets compare correctly, and a stored or queried value without an offset is read as
    UTC. Accepted forms are ASCII ISO 8601: a calendar date (`2026-11-19`) or a datetime (`2026-11-19T10:00:00`, with an
    optional `Z` or `+hh:mm` / `-hh:mm` offset and up to six fractional digits). A date-only `from` is the first instant of
    that UTC day and a date-only `to` is its LAST instant (23:59:59.999999), so a date-only `to` includes the whole day. Epoch
    numbers, compact and week dates, impossible dates and anything else are `422`. `from` later than `to` is `422`. A `+` in an
    offset must be sent as `%2B` (a raw `+` becomes a space and is refused).
  - `q` is a case-sensitive document-ID PREFIX (1 to 128 characters), not a substring and never a filename search.
- **Unsupported filters and unknown parameters are rejected, not ignored.**
  - `documentType` and `confidenceBelow` are declared and any use of them, including an empty value, is
    `422 VALIDATION_ERROR` naming the parameter (error type `unsupported_filter`). Neither value is persisted, and silently
    ignoring them would return an unfiltered list that only looks filtered.
  - Every other query parameter NAME is also `422 VALIDATION_ERROR` (error type `unknown_parameter`): a misspelling such as
    `stauts`, a case variant such as `Status`, an invented name such as `limit`, and any tenant parameter such as `tenant_id`,
    including a name with an empty or missing value. The recognized names are exactly `page`, `size`, `status`, `from`, `to`,
    `q`, `sort`, `order`, `documentType` and `confidenceBelow`. The error is fixed and reflects nothing the client sent: it
    names no offending parameter, carries no value, and lists only the allowed names. The application logs nothing about it (a web server's own access log of the request URL is outside this code and is not covered).
  - FastAPI checks the declared parameters first, so a request that has both an invalid known parameter (for example `page=0`)
    and an unknown name is answered with the known parameter's `422`; either way it is a `422`. Authentication and scope still
    come first (`401`, `403`). A tenant HEADER (`X-Tenant-ID`) is never read; it is not a query parameter and is ignored.
  - Both are checked before any storage access.
- **Sorting.** `sort` is one of `created_at`, `document_id`, `page_count`, `file_size_bytes`, `status` (anything else is `422`),
  and `order` is `asc` or `desc` (default `created_at` `desc`, newest first). `status` sorts by the alphabetical order of its
  stored value, not by workflow order. The tie-breaker is ALWAYS `document_id` ASCENDING, whichever direction was asked for, so
  the order is total and repeatable for one data set; ascending and descending are therefore not exact mirror images when keys
  tie. Timestamps sort as instants.
- **Paging.** `page` is at least 1 and `size` is 1 to 100 (default 10; the maximum is provisional); anything else is `422`.
  `paging.total_items` counts the records that match the filters and `paging.total_pages` is `ceil(total_items / size)`, 0 when
  nothing matches. A page beyond the last returns `200` with empty `content` and the true totals. An empty tenant, or a tenant
  directory that does not exist, returns `200` with empty `content` and all-zero `stats`, and nothing is created on disk.
  A missing storage ROOT currently behaves the same way: an empty `200` list, not an error. That could conceal a storage
  misconfiguration (a wrongly configured root looks like a tenant with no documents). This is a documented limitation;
  the behavior is unchanged here, and a typed `503` for a missing root is not implemented.
- **No snapshot between requests.** Totals, statistics and the page of ONE response always agree. Across requests nothing is
  held: a document added between two page requests can shift records across a page boundary (a record can then appear on two
  consecutive pages), and `total_items` can change. Offset paging with a stable order is all this contract offers.
- **Tenant isolation.** Only the principal's tenant directory is opened. Every record is checked against the principal's tenant
  id and against the name of its directory, and every listed id is one for which `GET /api/v1/documents/{id}` succeeds.
  Authentication and scope are decided before the query parameters are judged (`401`, `403`), and parameters are validated
  before any storage access.
- **Not document candidates.** `.tenant-identity.json`, the `idempotency` directory, names that are not valid document ids,
  and document directories without a `document_record.json` are skipped: they are not candidates, do not count toward the
  10,000-candidate limit and never appear in `total_items` or `stats`. Their directory entries are still examined, so each of
  them DOES count toward the 20,000-entry examination limit (a directory without a record is also a candidate for the
  10,000 limit, because it can only be recognized by opening it).
- **Corrupt records fail the whole list.** A document-like directory that is a symlink, a regular file or another special
  entry, a record that is a symlink, FIFO, socket or directory, a record that is unreadable, not UTF-8, not JSON, not an object,
  has missing or mistyped fields, an invalid timestamp, a malformed stored error, or that claims another tenant or another
  document id is a generic `500` with a fixed reason logged server-side; nothing partial is returned and no stored value,
  tenant identifier, path or raw exception text reaches the response or the logs. A record that claims another tenant is an
  isolation fault (`500`), not a concealed `404`, because a collection has no single resource to hide. Skipping such a record
  would silently shrink every total, so it is not done. The cost is availability: ONE corrupt record makes the whole list
  unavailable until it is repaired. Partial results would need an additive response field and an owner decision.
- **Ingestion failures and the list.** Documents rejected before the record is saved (validation, unsupported file, malware,
  scanner failure) are never listed or counted, so the `rejected` status can never match and "nothing is lost" is not met for
  rejected uploads; making them listable needs a persistence decision. A document whose ingestion failed after the record was
  saved can remain listed as `processing`, which overstates `stats.processing` and understates `stats.failed`.
- **Bulk exposure of row fields.** Every listed document returns the whole sanctioned `DocumentResource` row: `document_id`,
  `status`, `filename`, `mime_type`, `file_size_bytes`, `page_count`, `sha256`, `created_at`, `updated_at`, `error_code` and
  `error_message` (see M2D-D02). In particular the client-supplied filename, the SHA-256 content hash, the MIME type and the
  file size of ALL of a tenant's documents are exposed in bulk.
- **Logging.** The log statements added for API-015 follow the F6 rule (constant messages, fixed reason codes, integer `errno`)
  and are covered by the same static test; the F6 scope statement is unchanged (`DocumentReadService` only).
