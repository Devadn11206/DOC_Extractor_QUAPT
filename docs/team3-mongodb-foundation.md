# MongoDB foundation (Team Member 3, Phase 2A) and distributed processing (Phase 3)

Status: **foundation only.** It provides the connection, the settings, the tenant-scoped repository base, safe serialization
and the central index registry, plus the **MongoDB `ReviewTaskRepository`** (Phase 2A-3) and the **MongoDB `ApiKeyRepository`** (Phase 2A-4), and (Phase 2B) a durable, tenant-isolated, append-only,
hash-chained **MongoDB audit repository**. It is a database hash chain only, NOT a claim of SEC-008 compliance (see "Audit repository").
Nothing here resolves a decision of `docs/m5-open-decisions.md`; all of them stay Open.

## What exists

| Piece | Module (`src/doc_extractor/infrastructure/mongo/`) |
| --- | --- |
| Settings and the connection-string policy | `settings.py` |
| One connection per process: connect, verify the replica set, health, transactions, close | `connection.py` |
| Tenant-scoped repository base (mandatory tenant filter, filter and update allow-lists, typed errors) | `repository.py` |
| BSON-safe serialization (UTC, millisecond precision, enums, reserved names) | `serialization.py` |
| Central index registry and idempotent bootstrap | `indexes.py` |
| Typed persistence errors with fixed messages | `errors.py` |
| `ReviewTaskRepository` over MongoDB (Phase 2A-3) | `review_tasks.py` |
| `ApiKeyRepository` over MongoDB (Phase 2A-4) | `api_keys.py` |

## Configuration

Read through the API's existing `DOC_API_*` allow-list (`api/config.py`), documented in `.env.example`. Leave all of them unset to
run without MongoDB. Set either `DOC_API_MONGO_URI` or `DOC_API_MONGO_URI_FILE` (a mounted secret, preferred), plus
`DOC_API_MONGO_DATABASE` (required; there is no default). The connection string is a secret: it is held as `SecretStr`, never
logged and never part of an error or of a settings validation message.

Staging and production require TLS (`mongodb+srv` or `tls=true`), authentication, a replica set, and refuse invalid-certificate
and `directConnection` options and a non-primary read preference. Development accepts a plain standalone server. Every timeout
and pool size is bounded.

## Lifecycle

Composition (`ApplicationContainer`) builds the connection and opens nothing. The application lifespan calls
`container.startup()`, which connects, pings, verifies that the server is a replica set (and the expected one) and applies the
declared indexes. Any failure stops the application with a typed error; **there is no in-memory or other fallback in any
environment**. Shutdown closes the client. After a SUCCESSFUL start-up the lifespan publishes the configured review-task and
API-key repositories to the application state (where the request dependencies read them); a failed start-up publishes nothing.
A port set explicitly on the state is never replaced.

### Start-up rules by environment (Phase 2A-5)

| | development | staging and production |
| --- | --- | --- |
| No MongoDB configured | starts; the persistence ports stay unconfigured (503) | **refuses to start**: `PersistenceConfigurationError`, reason `database_is_required_in_staging_and_production` |
| Partial or malformed settings | refused when the settings are built | refused when the settings are built (a database or replica set without a connection string, a connection string without a database, both a string and a file, an unreadable secret file) |
| Security policy of the connection string | plain standalone allowed | TLS, authentication, no insecure TLS options, no `directConnection`, primary reads; refused when the settings are built |
| Replica set | required only if asked for | always required: the server must report one, and the expected one (checked at connect) |
| Injected repositories | honoured | honoured **only on top of** a verified database: they never waive the requirement |
| Injected connection | accepted | accepted only if it was built for the same hardened environment (a development connection is refused before it connects) |
| Unreachable server, failed replica-set check, failed index bootstrap | stops start-up, typed error | stops start-up, typed error |
| After any start-up failure | the client is closed; no repository is configured or published | the same |

There is no setting, flag or injection that turns the gate off, and no in-memory, local or mock fallback. Every error has a
fixed message and a fixed reason: no connection string, host, credential or file path. The gate does not require an audit sink (that
policy, M5-D15, is Open); when MongoDB is configured the durable audit repository is wired as described under "Audit repository". `GET /api/v1/health` gains a `database` component only when MongoDB is configured; an
outage reports `degraded` with HTTP 200 (the endpoint is also the liveness probe).

## Concerns the client imposes

Write concern `majority` with journaling, read concern `majority`, read preference `primary`, retryable reads and writes,
UTC-aware datetimes. Explicit options override anything in the connection string.

## Indexes

All indexes are declared in `indexes.py` and nowhere else; the bootstrap is idempotent and never drops or rebuilds an index (a
same-name index with different options is a typed error). Every index is tenant-first except the credential-hash lookup.
Only identity constraints are unique: review-task `(tenant_id, task_id)` and API-key `key_hash` (global) and
`(tenant_id, key_id)`. The review-task tenant and document lookup is **non-unique** because whether a document may have more than
one review task is an open product question.

## Dependencies

`pip install -e ".[mongo]"` adds `pymongo` (the asynchronous client). It is imported only when a connection is made. `fakeredis`
moved from the production `security` extra to the new test-only `test` extra.

**Production versus test installs (Phase 2A-7).** A production deployment installs `".[api,security,mongo]"`: no `dev`, no `test`.
Test-only packages are declared only under `dev` (the runner: pytest, pytest-cov, httpx) and `test` (fakeredis, the `redis<8` cap that
fakeredis needs, and the CI scripts' own imports). The complete test environment is `".[dev,api,security,mongo,test]"`. Resolved with
pip on Python 3.11 and 3.14, the base and production installs contain none of pytest, pytest-cov or fakeredis; `redis` resolves to the
production (open-ended) version there and to 7.x only in the test environment. Importing the API package imports `redis`, so anything
that imports `doc_extractor.api` needs `security` as well as `api` (the MongoDB integration job installs both). The CI job
`production-install` runs `scripts/ci/check_production_install.py` against a non-editable production install, and
`tests/test_packaging_consistency.py` and `tests/test_ci_workflow.py` fail on any test-only package in a production group or on a job
installing the wrong extras.

## Tests and what they prove

- `tests/test_mongo_*.py` (settings, connection, serialization, repository, indexes, composition and start-up) are **mocked**: they
  use the in-memory fake in `tests/mongo_fakes.py`. They prove the foundation's own behaviour, not server behaviour.
- `tests/test_mongo_integration.py` runs against a **real replica set** and is opt-in (`DOC_TEST_MONGO_URI`); without it every test
  skips and proves nothing. The CI job `mongo-integration` runs it. Locally:
  `docker compose -f deploy/compose/mongo-replica.yml up -d --wait`, then
  `DOC_TEST_MONGO_URI='mongodb://localhost:27017/?replicaSet=rs0' python -m pytest tests/test_mongo_integration.py`.

## ReviewTaskRepository (Phase 2A-3)

`MongoReviewTaskRepository` implements the existing port (`api/ports/repositories.py`) and nothing else: the port, the record
model, the transition outcomes and the in-memory reference double (`tests/review_task_double.py`) are the contract. It derives
from `TenantScopedRepository`, so every operation carries the tenant and a task of another tenant is simply absent.

- **Atomic `claim`, `release`, `complete`:** each is one conditional `find_one_and_update` with the precondition (status, and the
  owner for release and complete) in the filter, so two concurrent claims of one unassigned task give exactly one `CLAIMED`.
  When the conditional update matches nothing, one read explains why and that is the outcome (`HELD_BY_OTHER`, `ALREADY_OWNED`,
  `COMPLETED`, `ALREADY_UNASSIGNED`, `NOT_CLAIMED`, `ALREADY_COMPLETED`, `NOT_FOUND`). If another writer changed the task so that
  the precondition now holds, the step is retried (at most six times, short back-off), then a typed retryable
  `PersistenceUnavailableError` (`task_state_did_not_settle`); it is never reported as a wrong outcome.
- **`get_by_document_id`:** a document may have several tasks (no document-level uniqueness; the lifecycle decision is open). The
  port does not say which one is returned; the adapter returns the **most recent** (`created_at` descending, then `task_id`
  descending), deterministically. The reference double returns the first inserted, which is the same whenever a document has one
  task.
- **`list_page` / `list_tasks`:** filtered, ordered (oldest first, `task_id` tie-break) and paged in the query; a page past the end
  is empty. The unassigned filter is an equality with null, so it matches a stored null and a missing owner.
- **`create`:** returns the task as stored (UTC, millisecond precision). A duplicate `(tenant, task_id)` raises
  `DuplicateReviewTaskError` (a `PersistenceError` and a `ValueError`, like the double); another tenant's task raises
  `TenantScopeError`.
- **`update_status`:** the port's unconditional write, validated against the record's invariants first (as the double does). Never
  used to claim or release.
- **Composition:** `ApplicationContainer.startup()` configures the repository **after** a successful connection and index
  bootstrap, unless one was injected. A failed start-up leaves the port unconfigured. Without MongoDB nothing changes.
- **Limits:** a list with neither a status nor an assignee filter has no index for its ordering (the server sorts the tenant's
  tasks); no index was added. `metadata` keys must be valid BSON field names. Completing a task still does not touch the document
  or the audit trail (open dependency of the port).

## ApiKeyRepository (Phase 2A-4)

`MongoApiKeyRepository` implements the existing port and nothing else; the port, `ApiKeyRecord`, `ApiKeyService`,
`ApiKeyAuthenticationProvider` and the reference double are the contract. It stores and returns **records**: whether a key may
authenticate (active, revoked, expired) is decided by the provider, as with the double, so a lookup returns a revoked or expired
record rather than hiding it.

- **Hash only.** Only the SHA-256 digest is stored. `create` also refuses a `key_hash` that is not a lowercase 64-character hex
  digest (a raw token can be 64 characters long, so the record's length check alone cannot tell them apart). The adapter does
  not log, and every error has a fixed message: no hash, key id, tenant or driver text.
- **The one tenant-exempt read.** `get_by_key_hash` has only the hash, so it cannot be tenant-scoped; it uses
  `TenantScopedRepository.find_one_by_global_unique_key`, an opt-in base-class read that works only for fields declared at
  construction (here `key_hash`, the tenant-exempt unique index), by equality on a plain string, for one document. The owning
  tenant is read from the returned record. Every other operation, every write included, is tenant-scoped.
- **Identity.** `key_hash` is unique across tenants and `(tenant_id, key_id)` is unique (existing indexes, none added). Either
  violation is one `DuplicateApiKeyError` (a `PersistenceError` and a `ValueError`) with one reason, so a duplicate hash in
  another tenant is not distinguishable from a duplicate key id.
- **`revoke`** is two conditional updates, so a repeat keeps the time of the first revocation and no concurrent writer can leave
  the key active afterwards; `False` only when the tenant has no such key. **`record_last_used`** applies to an active key only
  (the port) and never moves the time backwards, so requests finishing out of order cannot make it regress.
- **`list_keys`** returns every state, oldest first (`created_at`, `key_id`); no index covers that order, so the server sorts the
  tenant's keys (a tenant has few). A stored record that does not match the model is a typed `PersistenceDataError` on every read
  that touches it: never skipped, never returned.
- **Composition.** `ApplicationContainer.startup()` configures the repository after a successful connection and index bootstrap
  (an injected one wins; a failed start-up leaves nothing configured). The application lifespan then publishes it to
  `app.state.api_key_repo`, which is where authentication reads it (never replacing a port set on the state explicitly). The
  authentication provider is built from that repository on the first request and still needs a configured audit logger, as before.
- **Cost.** Every authenticated request does one read (the hash) and one acknowledged write (`record_last_used`).

## Audit repository (Phase 2B)

`MongoAuditRepository` (`audit.py`) implements the existing `AuditRepository` port (`append_event`, `query_events`) over MongoDB, and
adds `verify_chain` and `chain_head`. `DurableAuditLogger` (unchanged) sits on top of it, so a write failure raises
`AuditUnavailableError` and a mandatory operation (API-key creation or revocation) refuses to continue unaudited.

- **One format, not two.** It uses `compute_event_hash`, `GENESIS_HASH` and `ChainVerification` of `api/audit_chain.py`:
  `hash = SHA-256(prev_hash | seq | canonical event JSON)`, one chain per tenant starting at sequence 1. A test appends the same events
  to the file-based repository and to this one and requires identical sequence numbers, previous hashes and hashes.
- **Document** (collection `audit_events`): `tenant_id`, `seq`, `prev_hash`, `hash`, `hash_version` (1), `event_json` (the exact
  canonical JSON text that the hash covers), and copies of `event_id`, `resource_id`, `action` and `timestamp_us` (exact microseconds)
  for the query indexes. `verify_chain` checks that the copies still equal the hashed event.
- **Sequence allocation, correct across processes.** A writer reads the tenant's highest record, computes `seq + 1` and the hash from that
  record, and inserts. The **unique `(tenant_id, seq)` index** lets exactly one writer win each number; a loser re-reads the new head and
  retries with a short random pause, at most `max_attempts` times (default 64). The chain therefore has no gaps and cannot fork, with
  no counter and no process-local lock. When the budget is exhausted the append **fails** (`audit_append_contended`, retryable); any other
  driver failure fails at once with no blind retry (a retry after an ambiguous failure could record the event twice). Events for one tenant
  therefore serialize through retries: heavy simultaneous writing to ONE tenant is the cost of this design.
- **Indexes** (central registry only): `audit_tenant_seq_uq` (unique), and non-unique `audit_tenant_resource_seq`, `audit_tenant_action_seq`,
  `audit_tenant_time_seq`. The append also requires the unique index to exist, and start-up verifies it **even when index bootstrap is
  switched off** (a deployment that migrates indexes separately must have created it, or start-up refuses and nothing is configured).
- **Append-only.** The repository's public surface is `append_event`, `query_events`, `verify_chain`, `chain_head` and the start-up gate:
  there is no update, delete or raw insert method, and only inserts and reads reach the driver. This is an application-level guarantee:
  the database user should also hold no update or delete privilege on the collection.
- **Queries** return the tenant's events in chain order with the port's filters (`resource_id` and `action` exactly, inclusive time bounds),
  equal to the file-based repository's results (tested over a grid of filters and pages).
- **Verification** reports the first defect with a fixed reason (never event content): an edited, reordered, inserted, duplicated
  (forked) or removed record that has a successor, a changed copy, an unsupported hash version, a record of another tenant, or a record
  outside the sequence.
- **Composition.** After a successful connection, index bootstrap and the audit-index check, `startup()` wires the repository and a
  `DurableAuditLogger` (everything is prepared first and assigned last, so a failure leaves nothing configured) and the lifespan
  publishes them to `app.state`. An injected audit repository or logger, an explicitly configured `audit_store_dir` file store, or a port
  set on the application state is never replaced; a best-effort sink is never substituted.

**What this does NOT provide (SEC-008 is not claimed).** The hash chain detects edits and removals that have a successor. It cannot detect
removal of the newest records, or a rewrite of the whole tail by someone who can write to the database, because nothing outside the
database vouches for the chain head. That needs independently signed checkpoints, an external tamper-resistant sink and verification
against them, which are future work. Decision M5-D15 (best-effort versus durable sink) stays Open.

**A consequence to decide on.** Wiring the durable logger also makes the existing API-key authentication provider write an
`api_key.authentication_failed` event (tenant `unknown`) for EVERY failed login, so an unauthenticated caller can cause one database read and
write per request, all contending for one chain. The write failure is swallowed by the provider (authentication is unaffected), and the
retry bound limits the cost per request, but this path deserves rate limiting or a separate pre-authentication sink.

## Distributed processing (Phase 3)

**Architecture in one paragraph.** A broker carries job messages; MongoDB decides everything else. RabbitMQ has no visibility timeout, no
lease extension and no per-message delay on a quorum queue, so the `QueueConsumer` contract is kept by splitting the work: the BROKER
(`infrastructure/queue/`) stores and redelivers messages; the LEASE STORE (`api/ports/processing.py`, `infrastructure/mongo/processing.py`) is the
authority on who owns a job, which attempt it is on and whether it finished. A delivery is only a request to look at a job; the claim in the
lease store decides. **RabbitMQ quorum queues are the working baseline named by the Phase 1 assessment. They are NOT an approved architecture
decision, and nothing here records stakeholder approval.**

| Piece | File | What it does |
| --- | --- | --- |
| Lease port | `api/ports/processing.py` | `ProcessingLeaseStore`: `claim`, `extend`, `verify`, `mark_completed`, `release_for_retry`, `dead_letter`, `snapshot`; `LeaseLostError`; `JobLease` with a fencing token |
| Lease store | `infrastructure/mongo/processing.py` | One record per (tenant, job key) in `processing_jobs`; every change is one conditional write |
| Queue adapter | `infrastructure/queue/rabbitmq.py` | `QueueProducer` and `QueueConsumer` over a transport and the lease store; contract unchanged |
| Transport | `infrastructure/queue/transport.py` | `aio-pika` robust connection, quorum queues, publisher confirms, wait-queue delays, dead-letter queue |
| Worker | `infrastructure/queue/worker.py` | Receive, heartbeat, fence, settle, shut down gracefully; optional Redis document lock |
| Idempotency | `infrastructure/mongo/idempotency.py` | `IdempotencyStore` whose source of truth is a unique index; Redis only coordinates the key lock |
| Redis locks | `distributed_state.py` | `acquire()`/`release()` on lock objects (ingestion needs them), `acquire_lease` with a fencing token, `extend`, `still_held` |

**Leases and fencing.** `claim` is atomic across processes: the first claim inserts the record (the unique `(tenant_id, job_key)` index lets one
of many racing workers do it), a later claim is one `find_one_and_update` that matches only a job that is queued and due, or leased and expired.
Every claim raises `fencing_token` by one. `extend`, `mark_completed`, `release_for_retry`, `dead_letter` and `verify` match only the record that still
carries the caller's token in the leased state, so a worker whose lease was taken over (a stale worker, even one that kept the same worker id)
gets `LeaseLostError` and cannot change the job. Time only decides WHEN a lease may be taken over; the token decides who may write. Clocks of
workers must be kept on NTP and the lease chosen far above the skew: a fast clock can take a healthy lease early, which is wasteful but safe.
Side effects of a job should carry `JobLease.fencing_token` (the worker exposes it and `ensure_held()`).

**Attempts and dead letters.** `max_retries` means `max_retries + 1` attempts (GR-018). An attempt is counted when a lease is claimed, so a worker
that dies mid-job also uses one. When the attempts are used up the next claim dead-letters the job instead of running it again. A failed
`nack` with retries left releases the job with its delay, publishes the delayed copy (confirmed) and then acknowledges; without retries it
publishes to the dead-letter queue (confirmed), marks the job dead-lettered and acknowledges. `defer` gives a job back without using an attempt
(graceful shutdown, a busy document).

**Delays.** A wait queue per delay tier (default 1 s, 5 s, 15 s, 1 min, 5 min, 15 min, 1 h; configurable in `BrokerSettings`) has a queue-level TTL and
dead-letters back to the main queue. A delay is rounded UP to the next tier (never early); a message that returns before its exact due time
(a delay above the last tier) is re-delayed using its `x-not-before-us` header. A message for a job another worker holds is re-queued for about
the remaining lease, without using an attempt.

**Recovery.** A worker that dies or loses its connection leaves its deliveries unacknowledged; the broker redelivers them, and the claim decides:
completed or dead-lettered jobs are acknowledged and dropped, a live lease means "come back later", an expired lease is taken over with the next
token. A failed acknowledgement after the durable completion is logged (`unconfirmed_settles`), not raised: the redelivery is dropped as
completed. The `ProcessingWorker` stops a handler when ownership is lost or when the lease store has been unreachable for a whole lease.

**Idempotency.** `MongoIdempotencyStore` keeps one record per (tenant, SHA-256 of the key) in `idempotency_records`; the unique index refuses a
second live record. The same request saving again is a replay, a DIFFERENT request raises `ConflictError` (409), an expired record is replaced by one
conditional write. Redis (when `redis_url` is set) only coordinates the per-key lock: when a lock lease expires while its holder is still running,
the unique record still admits exactly one request (tested). The synchronous interface is bridged to the event loop that owns the connection
(`start()` records it): calls from worker threads work, a call from the loop thread itself is refused with a typed error instead of deadlocking.
**Latent defect fixed:** `RedisIdempotencyStore` returned lock objects without `acquire()` and `release()`, which the ingestion path calls, so it could not
have been used for an idempotent upload; the lock objects now have them.

**Indexes** (all in `IndexRegistry`): `pj_tenant_job_uq` (unique), `pj_tenant_state_lease`, `idem_tenant_key_uq` (unique), and TTL indexes on `expires_at`
for both collections (housekeeping only: readers check expiry themselves; completed jobs are kept 14 days, dead-lettered jobs until an operator
removes them). A TTL index must be single-field, so `IndexSpec` now allows `tenant_exempt` for a TTL index as well as a unique one. The job and
idempotency unique indexes are required at start-up even when bootstrap is off.

**Composition and configuration.** `DOC_API_BROKER_URL` (or `_FILE`), `_QUEUE_PREFIX`, `_MAX_IN_FLIGHT`, `_CONNECT_TIMEOUT_SECONDS`,
`_PUBLISH_TIMEOUT_SECONDS`. Staging and production require `amqps://`, credentials and TLS verification; a URL is a secret and appears in no log or error.
A broker needs MongoDB (refused by the settings and again by `startup()`). With MongoDB configured, `startup()` now also replaces the idempotency
store by the durable one (unless one was injected) and, when a broker is configured, builds the lease store and the queue (unless producer and
consumer were both injected), connects the broker and declares its topology. Everything is prepared first and assigned last; a failure closes what was
opened, leaves nothing configured and stops start-up. No in-memory queue is ever substituted. Without a broker URL the queue ports stay unconfigured (503),
as before; the broker is optional in every environment until the upload path publishes (a decision for the router owners).

**Open decisions and dependencies (not decided here).**
- **Queue isolation.** This is the SHARED-queue baseline: all tenants share one queue and isolation is enforced by message validation (tenant header must
  equal the body's tenant; mismatches are dead-lettered) and by tenant-scoped state (leases, idempotency). Per-tenant queues or virtual hosts would isolate in
  the broker too, at the cost of many queues. That choice is open. The register's `M5-D01` is "Error-code names"; no queue-isolation decision is recorded in it,
  and the register was not modified.
- **Broker selection.** RabbitMQ is a candidate, not approved.
- **Active expiry sweeper.** Recovery of a dead worker is driven by the broker's redelivery. A worker that hangs while its connection stays open keeps its message
  unacknowledged until RabbitMQ's `consumer_timeout` closes the channel (default 30 minutes): set it above the longest job. A sweeper that finds expired leases of
  ALL tenants and republishes them was not built: the repository base is tenant-scoped on purpose, and a second cross-tenant path needs a decision.
- **Document-processing handler and entry point.** The extraction pipeline as a handler, and a worker process entry point, are not part of this phase.
- **Unverified against real services here.** No RabbitMQ, Redis or MongoDB server was available when this was written. `tests/test_queue_integration.py` (14 tests,
  CI job `queue-integration`) runs the adapter against all three and was NOT run; the queue arguments (quorum type, delivery limit, queue TTL, at-least-once
  dead-lettering) are the documented RabbitMQ 3.10+ ones and a first deployment is their verification.
- **HTTP mapping.** `BrokerUnavailableError` is a `StorageError` (generic 500 with a logged reason); a 503 mapping belongs to the API owners.
- **Deployment.** Quorum queues need a RabbitMQ cluster (three nodes for fault tolerance); the MongoDB database user should be able to create the
  collections and indexes it needs and nothing wider; keep worker clocks on NTP; set the worker lease above the heartbeat interval times a few missed beats.

## Not done here (later slices)

Independently signed checkpoints, an external tamper-resistant audit sink and verification against it (SEC-008); a start-up requirement
for an audit sink (M5-D15 is Open); a 503 mapping for a database outage (today a persistence error is a generic 500 with a logged reason, like any
`StorageError`); the unique review-task document constraint (awaits the product rule).
