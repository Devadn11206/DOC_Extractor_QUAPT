# Audit Checkpoint Foundation & Tamper-Resistant Storage (Team Member 3, Phase 4C)

Status: **Phase 4C delivered (Independently Signed Audit Checkpoints & External Immutable Storage).**
Extends the existing MongoDB audit hash chain (`src/doc_extractor/infrastructure/mongo/audit.py`, `src/doc_extractor/api/audit_chain.py`) with cryptographic, independently verifiable signed checkpoints and tamper-resistant external storage (S3 Object Lock), supporting SaaS (AWS S3 Object Lock / AWS KMS), private cloud (MinIO Object Lock / KMS emulators), and on-premise deployment profiles.

---

## 1. Security Boundaries & Constraints

- **SEC-008 Compliance Status:** **Partially Outstanding (Not Fully Complete).**
  - **Cryptographic checkpointing:** Fully implemented. Audit chain heads are periodically or on-demand signed into canonical checkpoints using asymmetric keys (RSASSA-PSS with SHA-256) managed by AWS KMS or customer KMS/HSM solutions.
  - **Independent verification:** Fully implemented. Signatures can be verified offline by external auditors or automated tooling using public key certificates (`IndependentCheckpointVerifier`) without requiring access to KMS or private signing keys.
  - **Tamper-resistant storage:** Fully implemented. Checkpoint records are persisted to an external immutable sink (`S3ObjectLockCheckpointSink`) configured with S3 Object Lock retention (`COMPLIANCE` or `GOVERNANCE` mode).
  - **Operational sink policy constraint (Open Decision M5-D15):** The production operational policy dictating whether an external sink outage synchronously halts API ingestion/mutations or falls back to an in-memory/durable queue buffer remains **Open**. Complete SEC-008 compliance is contingent upon resolving M5-D15 and provisioning physically locked cloud buckets in staging and production.
- **Production Hardening:**
  - Production environments strictly prohibit `FakeAuditSigner` (`fake_audit_signer_forbidden_outside_development`).
  - Production deployments require explicit signing key identifiers (`audit_signing_key_id`).
  - S3 Object Lock verification fails closed: if external immutable retention is configured but Object Lock is not enabled on the target bucket, start-up/health-checks fail with `ObjectLockNotSupportedError`.
- **Database vs Signed Checkpoint Verification:**
  - A database-only hash chain check (`verify_chain()`) is **never** represented as equivalent to signed checkpoint verification (`verify_chain_with_checkpoint()`).
  - If a tenant has audit events in MongoDB but no external signed checkpoint, verification explicitly reports `CHECKPOINT_NOT_FOUND` with `checkpoint_verified=False`.
- **Open Architecture Decisions:** M5-D15 (audit sink failure policy) and audit retention schedules remain **Open**.

---

## 2. Subsystem Architecture & Modules

The audit checkpoint subsystem resides in `src/doc_extractor/infrastructure/audit/`:

| Module | Purpose |
| --- | --- |
| `errors.py` | Typed audit checkpoint exceptions (`AuditCheckpointError`, `CheckpointSigningError`, `CheckpointVerificationError`, `CheckpointSignatureInvalidError`, `CheckpointSinkUnavailableError`, `CheckpointConflictError`, `ObjectLockNotSupportedError`). |
| `models.py` | `AuditCheckpoint` domain dataclass with deterministic canonical serialization (`canonical_bytes()`, `to_dict()`, `from_dict()`, `to_json()`, `from_json()`), and `CheckpointVerificationReport`. |
| `signer.py` | Abstract `AuditCheckpointSigner` and `AuditCheckpointVerifier` interfaces; `IndependentCheckpointVerifier` (offline RSA-PSS verification using standard `cryptography`); `FakeAuditSigner` test double. |
| `kms_signer.py` | `AwsKmsAuditSigner`: AWS KMS asymmetric signing (`RSASSA_PSS_SHA_256`) and public key retrieval (`GetPublicKey` converting SubjectPublicKeyInfo DER to PEM). |
| `sink.py` | Abstract `AuditCheckpointSink`; `EphemeralCheckpointSink` (WORM simulation); `S3ObjectLockCheckpointSink` (S3 Object Lock compliance storage with idempotency and conflict checks). |
| `verification.py` | `AuditChainVerifier`: Multi-layer verification comparing MongoDB hash chain, cryptographic signature, external sink records, and detecting truncation/rewrite attacks. |
| `service.py` | `AuditCheckpointService`: Coordinates MongoDB chain head retrieval, KMS signing, external sink persistence, and complete verification. |
| `__init__.py` | Clean package exports. |

---

## 3. Signed Checkpoint Format & Canonical Serialization

To guarantee cryptographic integrity across different platforms, JSON serializers, and programming languages, signatures are calculated over a deterministic canonical byte sequence.

### Checkpoint Fields

```json
{
  "tenant_id": "tenant_12345",
  "seq": 1420,
  "chain_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  "timestamp": "2026-11-24T12:00:00.000000Z",
  "key_id": "arn:aws:kms:us-east-1:123456789012:key/c4b8b6a2-...",
  "key_version": "v1",
  "version": 1,
  "algorithm": "RSASSA_PSS_SHA_256",
  "signature": "kH7v...<base64 signature>..."
}
```

### Deterministic Canonical Representation
Before signing or verification, the checkpoint is mapped to canonical JSON bytes:
1. `version`: Format version integer (`1`).
2. `algorithm`: Asymmetric signing algorithm (`RSASSA_PSS_SHA_256`).
3. `tenant_id`: Canonical tenant identifier (validated path-safe).
4. `seq`: Highest verified event sequence number (`int >= 0`).
5. `chain_hash`: 64-character lowercase hexadecimal SHA-256 hash at sequence `seq`.
6. `timestamp`: RFC 3339 UTC timestamp with microsecond resolution (`YYYY-MM-DDTHH:MM:SS.ffffffZ`).
7. `key_id`: Signing key identifier or ARN.
8. `key_version`: Active signing key version string.

Keys are serialized in strict alphabetical order without whitespace (`separators=(',', ':')`, `sort_keys=True`) encoded as UTF-8. Modifying even a single character or timestamp digit invalidates the cryptographic signature.

---

## 4. Asymmetric Signing & Key Lifecycle

### Signing Protocol
- **Algorithm:** RSASSA-PSS with SHA-256 (MGF1 with SHA-256, salt length matching digest size).
- **Key Storage:** Private keys are stored and executed inside KMS/HSM (e.g. AWS KMS `SIGN_VERIFY` asymmetric key pair with `RSA_2048` or `RSA_4096`).
- **No Private Key Egress:** Plaintext private keys are never exported, loaded into application memory, or logged.

### Independent Verification Path
Verification can occur in two distinct modes:
1. **Online via KMS:** `AwsKmsAuditSigner` calls AWS KMS `Verify` API.
2. **Offline Independent Verification:** `IndependentCheckpointVerifier` parses the public key certificate (SubjectPublicKeyInfo PEM) obtained via KMS `GetPublicKey` or pre-distributed out-of-band to external auditors. Verification executes entirely locally using standard Python `cryptography` primitives without network egress or AWS credentials.

### Key Rotation & Historical Verifiability
- When signing keys rotate (e.g. `v1` -> `v2`), each checkpoint records its specific `key_id` and `key_version`.
- Public keys for historical versions are retained in a versioned key store or resolver.
- Checkpoints signed under prior key versions remain permanently verifiable over their retention lifespan.

---

## 5. Tamper-Resistant External Storage (S3 Object Lock)

Signed checkpoints are stored in an external immutable sink (`S3ObjectLockCheckpointSink`) separate from primary database storage.

### S3 Object Lock Architecture
- **Object Key Layout:** Tenant-scoped and sequence-padded for chronological scanning:
  `{prefix}{tenant_key}/checkpoints/checkpoint_{seq:010d}.json`
  (e.g., `audit/tenants/tenant_alpha/checkpoints/checkpoint_0000001420.json`).
- **WORM Retention Mode:**
  - `COMPLIANCE`: Object cannot be overwritten, modified, or deleted by any user, including AWS root account, for the retention period.
  - `GOVERNANCE`: Object cannot be overwritten or deleted unless specific IAM bypass permissions are granted.
- **Fail-Closed Verification:** At startup, `check_health()` issues `GetObjectLockConfiguration`. If Object Lock is disabled on the bucket outside development, the sink raises `ObjectLockNotSupportedError`.
- **Idempotency & Conflict Detection:**
  - Writing the identical checkpoint multiple times succeeds idempotently.
  - Attempting to overwrite an existing checkpoint sequence with different hash, timestamp, or signature raises `CheckpointConflictError`.

---

## 6. Multi-Layer Independent Chain Verification

The `AuditChainVerifier` performs a comprehensive four-way cross-verification:

```
[ MongoDB Audit Records ]  <-- (1. Local Chain Hash Validation)
           |
           v (2. Chain Head Alignment: seq & chain_hash)
[ Signed Audit Checkpoint ] <-- (3. Cryptographic Signature Validation via Public Key)
           |
           v (4. Sink Identity Match)
[ S3 Object Lock (WORM) ]
```

### Attack Detection Capabilities

| Attack Scenario | Detection Mechanism | Status Code |
| --- | --- | --- |
| **Tail Truncation:** Attacker deletes newest audit records from MongoDB | Checkpoint sequence in sink > MongoDB head sequence | `CHAIN_TRUNCATED` |
| **Historical Rewrite:** Attacker alters past audit record and re-hashes DB | MongoDB hash at checkpoint sequence != Checkpoint `chain_hash` | `HASH_MISMATCH` |
| **Forked Chain:** Attacker forks audit chain from an older sequence | Intermediate hash mismatch against checkpointed hash | `HASH_MISMATCH` |
| **Tampered Checkpoint:** Attacker modifies checkpoint JSON in storage | Asymmetric signature verification fails against public key | `SIGNATURE_INVALID` |
| **Sink Substitution:** External checkpoint differs from recorded checkpoint | Checkpoint payload mismatch or duplicate conflict | `CHECKPOINT_TAMPERED` |
| **Missing Checkpoints:** Chain has events but no checkpoint exists | Checkpoint lookup returns None for non-empty chain | `CHECKPOINT_NOT_FOUND` |

---

## 7. Open Decisions & Configuration

### Configuration Settings (`.env.example` & `ApiSettings`)

```ini
DOC_API_AUDIT_CHECKPOINT_ENABLED=true           # Enables signed checkpointing
DOC_API_AUDIT_CHECKPOINT_SINK_TYPE=s3           # 'memory' (dev only) | 's3' (Object Lock)
DOC_API_AUDIT_SIGNING_KEY_ID=arn:aws:kms:...   # Asymmetric KMS signing key ID
DOC_API_AUDIT_SIGNING_KEY_VERSION=v1            # Active signing key version
DOC_API_AUDIT_OBJECT_LOCK_RETENTION_DAYS=365    # Retention period in days
DOC_API_AUDIT_OBJECT_LOCK_MODE=COMPLIANCE       # 'COMPLIANCE' | 'GOVERNANCE'
```

### Unresolved Items (Open Decisions)
1. **Decision M5-D15 (Audit Sink Failure Policy):**
   - Whether synchronous business operations must fail closed (raise HTTP 503) if the external checkpoint sink is unavailable, or whether checkpoints can be emitted asynchronously via a local durable buffer.
   - Status: **Open** (Owned by Team Member 3 and PRD Owner).
2. **Cadence Policy:**
   - Establishing the operational checkpoint trigger: time-based (e.g. hourly cron), volume-based (e.g. every 1,000 events), or transactional (e.g. at schema approval / key revocation).
   - Status: Pending operational policy decision.
