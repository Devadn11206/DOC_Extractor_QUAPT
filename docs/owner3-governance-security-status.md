# Owner 3: governance, security, infrastructure and enterprise (status)

Scope: PRD sections 9, 10, 11, ARC-001..006, SCH-001..007. This note records what the repository implements, what was
decided in code, and what still needs outside work. Each claim names the code and the test that checks it.

## Implemented in the repository

| PRD clause | What exists | Where | Tests |
| --- | --- | --- | --- |
| SCH-001..007 (schema lifecycle, four-eyes approval, versions, harness) | State machine, approval gate, immutable versions with rollback, harness metrics | `src/schema_lifecycle/` | `tests/test_schema_*.py` |
| FR-ING-003, SEC-009 (malware scan, fail closed) | ClamAV clamd INSTREAM client. Any failure is `ERROR`, never `CLEAN`. Production never uses the mock scanner. | `src/doc_extractor/malware.py` (`ClamAVSocketScanner`) | `tests/test_malware_clamav.py` |
| SEC-008, FR-AUD-001 (tamper-evident audit) | Per-tenant SHA-256 hash chain, fsynced appends, failed writes raise. Selected by `audit_store_dir`. | `src/doc_extractor/api/audit_chain.py`, `api/composition.py` | `tests/test_audit_hash_chain.py`, `tests/test_api_security_wiring.py` |
| SEC-002 (RS256 access tokens, 1 h lifetime) | Verifier pins RS256, rejects `none` and HMAC confusion, enforces lifetime, issuer, audience, tenant. Routed by the composite provider. | `src/doc_extractor/api/providers/jwt_auth.py`, `composite_auth.py` | `tests/test_api_jwt_auth.py`, `tests/test_api_security_wiring.py` |
| SEC-003 (seven least-privilege roles) | Role-to-scope matrix. Explicit `scope` claims can narrow, never widen. Every non-health route requires a credential. | `jwt_auth.py` (`ROLE_SCOPES`), `api/dependencies.py` | `tests/test_api_security_wiring.py` |
| SEC-001 (MFA for admin and schema approver) | JWT refused without the `mfa` claim for these roles | `jwt_auth.py` | `tests/test_api_jwt_auth.py` |
| SEC-011, ARC-001 (default-deny egress) | Loopback, or `DOC_EGRESS_ALLOWED_DESTINATIONS`. Enforced on the vLLM call, the litellm call and the ClamAV TCP connection. | `src/doc_extractor/egress.py`, `extractor.py`, `malware.py` | `tests/test_egress_policy.py` |
| SCH-001..006 REST endpoints | Create, version, history, status transitions, approve and rollback, each gated by its M4 permission. Tenant and schema identifier come from the principal and the path; a body naming another tenant is refused. Answers 503 until a manager is configured. | `src/doc_extractor/api/routers/schemas.py`, `api/composition.py`, `api/app.py` | `tests/test_api_schema_routes.py` |
| API-012, SEC-017 (distributed rate limiting) | Redis token bucket per authenticated principal, shared across replicas. Optimistic concurrency, so the limit is exact under contention. Fails closed when Redis is down. Enabled by `rate_limit_capacity` together with `redis_url`. | `src/doc_extractor/rate_limit.py`, `api/dependencies.py` | `tests/test_rate_limit.py` |
| NFR-010 (Prometheus metrics) | Request counts and durations by route template, rate-limit refusals, security alerts by condition. Dependency-free text exposition. Served only by the internal `metrics_app` on its own port. | `src/doc_extractor/metrics.py`, `api/metrics_middleware.py`, `api/metrics_app.py` | `tests/test_metrics.py` |
| SEC-021 (security alerts) | Alerts for repeated failed logins (by credential fingerprint), refused egress, broken audit chains, model-file changes, and per-method counters for export volume and unmask spikes. Alerts carry identifiers and counts only. | `src/doc_extractor/alerts.py`, wired into `egress.py`, `api/providers/composite_auth.py`, `scripts/ops/verify_audit_chains.py` | `tests/test_alerts.py` |
| ARC-003, NFR-006 (multi-replica locks and idempotency) | Redis `SET NX PX` leases, WATCH-guarded release, reentrant per thread. Enabled by `redis_url`. | `src/doc_extractor/distributed_state.py` | `tests/test_distributed_state.py` (fakeredis) |

## Decisions made in code

- **Refresh tokens (SEC-002): not implemented in the API process.** The API verifies access tokens only. Refresh-token
  rotation (one-time use) belongs to the identity service that holds the signing key. SEC-002 is therefore **partial**
  in this repository until that service exists.
- **Mock scanner in production:** `mock` resolves to an unconfigured scanner when `app_env=production`. Ingestion then
  rejects files, so nothing is accepted unscanned.
- **Egress is application-level.** It refuses the call before a socket is opened. Network-level deny is a deployment
  control (see open items).
- **Audit writes fail the operation.** A mandatory audit that cannot be written raises `AUDIT_UNAVAILABLE` (503). This
  is what the port's contract requires. Decision M5-D15 (audit sink policy) is still open and owned by the PRD owner.
- **Packaging:** the new runtime dependencies live in a separate `security` extra (`PyJWT[crypto]`, `cryptography`,
  `redis`, plus test-only `fakeredis`). The `api` and `dev` extras keep their documented contents, which
  `tests/test_packaging_consistency.py` pins. CI installs the extra in a separate step.

## Known gaps in the repository

- Platform tests: the full suite has pre-existing failures on Windows, not on Linux. The storage reader needs
  descriptor-relative access (`/dev/fd`), which Windows does not provide, so the product refuses those reads on Windows,
  as designed. Symlink tests also need a privilege that the Windows account lacks. Those failures are identical on a
  clean checkout of `HEAD`. Run the suite on Linux (the CI target) to get a real result.
- The litellm path is guarded only by the endpoint check. Its provider can still use configuration this repository does
  not control.
- The durable audit logger is selected by configuration, but no default `audit_store_dir` is set.
- `tests/test_ci_policy.py`, `tests/test_api_foundation.py` and `tests/test_api_model_ownership.py` fail on clean `HEAD`
  as well. They are not caused by this work.

## Open items that need people outside the repository

1. Run the production scanner against a real clamd, including an EICAR test and an offline signature bundle (SEC-009).
2. Run the distributed-state tests against a real Redis, not only fakeredis (ARC-003).
3. Deny-all egress rule in the deployment manifest (SEC-011). Platform team.
4. Scheduled hash-chain verification job and alert on failure (SEC-008, SEC-021). Needs the alerting system.
5. Refresh-token rotation in the identity service (SEC-002).
6. Independent penetration test, SAST and dependency scanning in CI (SEC-015).
7. Backup and restore drill (SEC-016), KMS-backed key management and rotation (SEC-005, SEC-013).
8. Decisions M5-D08 (replay of a shown-once API-key secret), M5-D09 (webhook secret storage) and M5-D15 (audit sink policy).
