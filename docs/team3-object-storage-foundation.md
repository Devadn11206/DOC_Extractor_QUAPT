# Object Storage Foundation & Envelope Encryption (Team Member 3, Phase 4A & 4B)

Status: **Phase 4A & 4B delivered (Object Storage Foundation + Envelope Encryption & KMS Integration).**
It provides an S3-compatible, tenant-isolated object storage backend with authenticated AES-256-GCM envelope encryption backed by AWS KMS or customer KMS/HSM providers, supporting SaaS (AWS S3), private cloud (MinIO, Ceph, Dell ECS), and on-premise deployment profiles.

---

## 1. Security Boundaries & Constraints

- **SEC-005 Compliance Status:** **Partially Outstanding (Not Fully Complete).**
  - **Remote resting storage:** Fully compliant. All tenant documents, page images, metadata sidecars, and processing handoffs are encrypted before upload to S3 using authenticated AES-256-GCM with per-object Data Encryption Keys (DEKs) wrapped under KMS Key Encryption Keys (KEKs).
  - **Local spool constraint:** Partially outstanding. Callers requiring POSIX filesystem descriptors (e.g. `SecureModuleBLoader` reading via `resolve_uri_to_path()`) require local disk spooling. Spool files are protected via restricted permissions (`0700` directory, `0600` file mode) and tenant directory isolation. However, full SEC-005 compliance without relying on underlying volume encryption (`spool_is_protected=True`) requires eliminating local disk spooling in favor of in-memory streaming descriptors, which is blocked on **Open Architecture Decision M5-D18**.
- **Production Hardening:**
  - Production deployments strictly prohibit `FakeKmsProvider` (`fake_kms_forbidden_outside_development`).
  - Production deployments require explicit KEK identifiers (`kms_kek_id`).
  - If encryption is enabled in staging or production, attempting to read unencrypted plaintext from S3 fails closed (`unencrypted_data_refused_in_hardened_environment`).
- **Fail-Closed Policy:** Remote storage errors **NEVER** silently fall back to local disk or in-memory storage.
- **Private Cloud & On-Premise Support:** No AWS-specific proprietary behaviors are hardcoded; configurable endpoints, path-style addressing, and custom CA bundles ensure seamless private-cloud and MinIO operation.
- **Open Decisions:** Architectural decisions M5-D18 and related persistence interface questions remain **Open**.

---

## 2. Architecture & Modules

The object storage subsystem resides in `src/doc_extractor/infrastructure/storage/`:

| Module | Purpose |
| --- | --- |
| `settings.py` | `S3StorageSettings`: Validated immutable configuration for S3 and KMS (TLS policy, credentials, bounded timeouts, key policies). |
| `errors.py` | Typed storage exceptions (`ObjectStorageError`, `ObjectStorageUnavailableError`, `ObjectNotFoundError`, `ObjectStorageConfigurationError`). |
| `envelope.py` | Binary container packaging (`DOCE`), authenticated AES-256-GCM envelope cipher, deterministic AAD construction, key memory wiping. |
| `kms/base.py` | Abstract `KmsProvider` interface and `DataKey` dataclass. |
| `kms/aws_kms.py` | `AwsKmsProvider`: AWS KMS integration using boto3 (generate data key, decrypt, server-side re-encrypt). |
| `kms/fake_kms.py` | `FakeKmsProvider`: In-memory development double with simulation hooks (forbidden outside development). |
| `kms/errors.py` | Typed sanitized KMS exceptions (`KmsError`, `KmsUnavailableError`, `KmsAuthenticationError`, `KmsKeyNotFoundError`, `KmsConfigurationError`). |
| `adapter.py` | `S3Storage`: Full `StorageBackend` implementation integrating S3 operations with envelope encryption. |
| `__init__.py` | Clean package exports. |

---

## 3. Envelope Encryption Architecture (PRD SEC-005)

### Binary Container Format (`DOCE`)
To prevent base64 overhead on large payloads (multi-megabyte scanned PDFs and high-DPI page images), objects are packaged in a compact binary container:
```
+-----------------------------------------------------------------------------------+
| Magic (4B) | Version (2B) | Header Length (4B) | JSON Header (variable) | Ciphertext + Tag |
|   'DOCE'   |   uint16=1   |      uint32        |       UTF-8 JSON       |  AES-256-GCM   |
+-----------------------------------------------------------------------------------+
```

### JSON Header Metadata
The header contains non-sensitive envelope metadata required for unwrap and rotation:
```json
{
  "v": 1,
  "alg": "AES-256-GCM",
  "kek_id": "arn:aws:kms:us-east-1:123456789012:key/...",
  "key_ver": "v1",
  "edek": "<base64 encrypted DEK>",
  "iv": "<base64 96-bit GCM nonce>",
  "tenant_id": "tenant_123",
  "uri": "tenant_123/doc_456/source/original.pdf",
  "mime": "application/pdf"
}
```

### Cryptographic Binding (Associated Authenticated Data)
To prevent cross-tenant and cross-object ciphertext substitution attacks (e.g. an attacker moving another tenant's encrypted ciphertext into their own namespace), deterministic AAD is cryptographically bound into the AES-256-GCM tag:
$$\text{AAD} = \text{"tenant:\{tenant\_id\}|uri:\{logical\_uri\}|alg:AES-256-GCM"}$$
Any modification to the tenant ID or logical URI fails authentication (`InvalidTag` $\to$ `ciphertext_authentication_failed`).

### Memory Handling
Plaintext DEK material is allocated as mutable `bytearray` buffers and overwritten with zeroes via `wipe_key_memory()` immediately after cipher operations.

---

## 4. Key Management (KMS / HSM) & Rotation

### Supported Providers
1. **AWS KMS (`AwsKmsProvider`):**
   - Calls `kms:GenerateDataKey` (`KeySpec="AES_256"`).
   - Calls `kms:Decrypt` with bounded timeouts.
   - Calls `kms:ReEncrypt` for server-side DEK rotation without exposing plaintext to application memory.
2. **Customer KMS / HSM:**
   - Any external KMS or hardware security module can be integrated by implementing the abstract `KmsProvider` interface (`generate_data_key`, `decrypt_data_key`, `rewrap_data_key`, `get_current_key_id`, `get_current_key_version`, `check_health`).
3. **Development Test Double (`FakeKmsProvider`):**
   - In-memory simulation using internal AES-256-GCM master keys.
   - Strict runtime barrier: instantiation raises `KmsConfigurationError` if `app_env in ("staging", "production")`.

### Key Rotation & DEK Rewrapping
When a KEK is rotated, stored objects do NOT need to be downloaded, decrypted, and re-encrypted. `S3Storage.rewrap_object(relative_path, target_kek_id, target_version)`:
1. Downloads the envelope container header from S3.
2. Calls `KmsProvider.rewrap_data_key()` (AWS KMS `ReEncrypt`).
3. Updates the container header with the newly wrapped DEK.
4. Uploads the updated envelope back to S3, preserving the underlying ciphertext and authentication tag untouched.

---

## 5. Local Spool Security & M5-D18 Blocker

### Spool Protection
When legacy descriptor-based callers invoke `resolve_uri_to_path()`:
1. The object is fetched from S3 and decrypted in memory after verifying AES-GCM tags and AAD.
2. The decrypted plaintext is spooled atomically to:
   $$\text{spool\_dir} / \text{tenant\_storage\_key(tenant\_id)} / \text{relative\_path}$$
3. Spool directories are created with `0700` mode; spooled files are created with `0600` mode.
4. Path traversal checks (`ensure_inside_tenant`) guarantee no escapes outside the tenant spool tree.
5. Explicit cleanup methods (`cleanup_spool(doc_id)` and `cleanup_document(doc_id)`) remove spooled plaintext upon task completion.

### The M5-D18 Blocker
A decrypted file on standard unencrypted local disk is not protected by S3 envelope encryption. Full SEC-005 compliance across all deployment environments requires:
- **Approach A (Infrastructure):** Running on volume-encrypted storage (e.g. AWS EBS encryption, LUKS, Kubernetes encrypted ephemeral volumes) with `spool_is_protected=True`.
- **Approach B (Architecture - Open Decision M5-D18):** Refactoring `StorageBackend` and `SecureModuleBLoader` to consume in-memory streaming readers or byte arrays directly, eliminating local disk spooling entirely.
Until M5-D18 is resolved by engineering consensus, SEC-005 is reported as **Partially Outstanding**.

---

## 6. Configuration Reference

| Setting | Environment Variable | Default | Description |
| --- | --- | --- | --- |
| `storage_backend` | `DOC_API_STORAGE_BACKEND` | `local` | `local` or `s3` |
| `s3_bucket` | `DOC_API_S3_BUCKET` | None | S3 bucket name (required if `s3`) |
| `storage_encryption_enabled` | `DOC_API_STORAGE_ENCRYPTION_ENABLED` | `false` | Enable AES-256-GCM envelope encryption |
| `kms_provider_type` | `DOC_API_KMS_PROVIDER_TYPE` | `fake` | `aws_kms` or `fake` (forbidden outside development) |
| `kms_kek_id` | `DOC_API_KMS_KEK_ID` | None | KMS Key Encryption Key (ARN or alias; required in prod) |
| `kms_key_version` | `DOC_API_KMS_KEY_VERSION` | `v1` | Active KMS key version identifier |
| `kms_endpoint_url` | `DOC_API_KMS_ENDPOINT_URL` | None | Custom KMS endpoint URL (LocalStack, KMS proxy) |
| `spool_is_protected` | `DOC_API_SPOOL_IS_PROTECTED` | `false` | Indicates host spool has volume encryption (SEC-005) |

---

## 7. Verification & Test Suites

- `tests/test_storage_s3_settings.py` (28 tests): Bucket naming, URL schemes, secret files, timeout bounds, KMS policy validation in production.
- `tests/test_storage_s3_adapter.py` (26 tests): Unencrypted S3 adapter baseline, tenant isolation, error mapping, `SecureModuleBLoader` compatibility.
- `tests/test_storage_s3_encryption.py` (25 tests):
  - Envelope encryption/decryption round trips (empty, small, binary PDF payloads).
  - AES-GCM ciphertext and tag tampering detection.
  - Cross-tenant and URI substitution rejection via AAD binding.
  - DEK memory wiping verification.
  - `FakeKmsProvider` simulation gates (rotation, outages, access denial, revocation).
  - `AwsKmsProvider` mocking (generate data key, decrypt, rewrap, sanitized errors).
  - `S3Storage` integration with encryption across all artifact types (original, page image, preprocessed page, metadata, document record, handoff payload).
  - DEK rewrapping on S3 without data re-encryption.
  - Local spool file permissions (`0600`) and cleanup.
  - Refusal of `FakeKmsProvider` and refusal of unencrypted fallback in staging/production.
- `tests/test_packaging_consistency.py` (31 tests): Verifies `storage` extra in `pyproject.toml` and `requirements-storage.txt` includes `boto3`, `botocore`, `cryptography`.
- `tests/test_storage_s3_integration.py` (4 tests): Opt-in live integration tests against real MinIO/S3.
