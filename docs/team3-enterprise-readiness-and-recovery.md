# Enterprise Readiness, Deployment Profiles & Disaster Recovery Runbook (Team Member 3, Phase 6)

Status: **Phase 6 delivered (Enterprise Readiness and Final Integration).**
Provides comprehensive operational documentation, deployment architecture profiles (SaaS AWS Mumbai, Private Cloud/VPC, and Air-gapped On-Premise), backup and disaster recovery procedures satisfying PRD targets (RPO $\le$ 15 min, RTO $\le$ 4 hours), production observability, multi-component health diagnostics, security alerting, and cross-phase integration verification.

---

## 1. Executive Summary & Operational Targets

- **SLA & Availability:** Target 99.5% service availability in standard deployments; 99.9% in High Availability (HA) multi-AZ deployments.
- **Recovery Point Objective (RPO):** $\le$ 15 minutes for all metadata, audit trails, and document storage (`SEC-016`).
- **Recovery Time Objective (RTO):** $\le$ 4 hours for total infrastructure and service restoration (`SEC-016`).
- **Data Residency (`SEC-019`):** SaaS profile strictly pinned to AWS Mumbai (`ap-south-1`). Private and on-premise profiles enforce customer-controlled sovereign boundaries.
- **Fail-Closed Security Posture:** Zero in-memory production fallbacks; unconfigured ports return HTTP 503; default-deny outbound egress (`SEC-011`); AES-256 envelope encryption at rest (`SEC-005`); independently verifiable signed audit checkpoints under WORM retention (`SEC-008`).

---

## 2. Enterprise Deployment Profiles

The Document Extraction Engine supports three distinct deployment profiles from a single unified codebase:

```
+---------------------------------------------------------------------------------------------------+
|                                      DEPLOYMENT PROFILES                                          |
+---------------------------------+---------------------------------+-------------------------------+
| 1. Vendor SaaS (AWS Mumbai)     | 2. Private Cloud / VPC          | 3. Air-Gapped On-Premise      |
+---------------------------------+---------------------------------+-------------------------------+
| AWS ap-south-1 (Mumbai)         | Customer VPC (AWS/GCP/Azure)    | Bare-Metal / OpenShift K8s    |
| MongoDB Atlas / DocumentDB (RS) | Customer MongoDB Replica Set    | Self-Hosted 3-node Mongo RS   |
| AWS S3 + Object Lock (WORM)     | MinIO Enterprise / S3 Storage   | MinIO / Ceph with Object Lock |
| AWS KMS (Multi-tenant KEKs)     | Customer KMS / Vault Transit    | PKCS#11 Hardware HSM / Vault  |
| Amazon MQ / RabbitMQ Quorum     | Customer RabbitMQ Quorum Cluster| Self-Hosted RabbitMQ Cluster  |
| ElastiCache Redis Cluster       | Customer Redis Cluster          | Local Redis HA Replica Set    |
| Egress: Strict Allow-List       | Egress: Internal VPC Only       | Egress: Zero Outbound (Deny)  |
+---------------------------------+---------------------------------+-------------------------------+
```

### Profile 1: Vendor-Hosted SaaS (AWS Mumbai `ap-south-1`)
- **Infrastructure:** Managed Kubernetes (EKS) across three Availability Zones in AWS Mumbai (`ap-south-1a`, `ap-south-1b`, `ap-south-1c`).
- **Database:** MongoDB Atlas (M30+ dedicated cluster) or AWS DocumentDB with replica set `rs0`, TLS 1.3, SCRAM-SHA-256 authentication, automated continuous backups with point-in-time recovery (1-second granularity).
- **Object Storage:** Amazon S3 bucket located in `ap-south-1` with S3 Versioning, default KMS encryption (`aws:kms`), and S3 Object Lock in COMPLIANCE mode for the signed audit checkpoint prefix (`audit-checkpoints/`).
- **Key Management:** AWS KMS Customer Managed Keys (CMKs) in `ap-south-1` with annual rotation enabled. Per-tenant envelope encryption wrapping AES-256-GCM DEKs.
- **Message Broker:** Amazon MQ for RabbitMQ (Quorum queues) with TLS 1.2+ mutual authentication.
- **Distributed State:** Amazon ElastiCache for Redis (Cluster mode enabled, Multi-AZ with auto-failover, in-transit encryption).
- **Network Egress:** Locked by Kubernetes `NetworkPolicy` and application `EgressPolicy` permitting only AWS KMS, S3 gateway endpoints, and internal microservices.

### Profile 2: Customer-Controlled Private Cloud / VPC
- **Infrastructure:** Customer-managed Kubernetes or container runtime hosted inside dedicated AWS, Google Cloud, or Microsoft Azure VPCs.
- **Database:** Customer-hosted MongoDB 7.0+ replica set (minimum 3 data-bearing nodes), TLS required, tenant-first indexing verified on startup.
- **Object Storage:** S3-compatible enterprise object store (AWS S3, MinIO Enterprise, Ceph RADOS Gateway) with Object Lock retention.
- **Key Management:** Integration via `AwsKmsProvider` or customer KMS provider (HashiCorp Vault Transit Engine, Azure Key Vault, or GCP Cloud KMS).
- **Network Boundaries:** Default-deny egress (`deploy/k8s/networkpolicy-deny-egress.yaml`). No data leaves the customer's VPC boundary.

### Profile 3: Air-Gapped On-Premise
- **Infrastructure:** Bare-metal Linux or Red Hat OpenShift cluster with zero internet connectivity.
- **Malware Scanning (`SEC-009`):** ClamAV daemon (`clamd`) running locally via TCP/socket with daily offline signature bundles transferred via secure optical media or internal proxy repository.
- **Key Management:** Dedicated Hardware Security Module (HSM) via PKCS#11 or local HashiCorp Vault.
- **Model Execution (`ARC-004`):** Local GPU inference servers (vLLM / Triton) hosting pinned `Qwen3-VL-8B-Instruct` model weights. External cloud model providers are disabled by configuration.
- **Network Egress:** Loopback only (`DOC_EGRESS_ALLOWED_DESTINATIONS=127.0.0.1`); all non-local socket connections are rejected before opening.

---

## 3. Disaster Recovery & Backup Runbook

To guarantee RPO $\le$ 15 minutes and RTO $\le$ 4 hours per PRD `SEC-016`, operators must adhere to the following backup schedules and restoration procedures.

### 3.1 Backup Architecture & Schedule Matrix

| Data Tier | Storage Mechanism | Backup Method | Schedule / Frequency | RPO Target |
| --- | --- | --- | --- | --- |
| **Metadata & Identity** | MongoDB (`users`, `tenants`, `schemas`, `leases`, `webhooks`) | Continuous Oplog Slicing + Snapshot | Daily full snapshot + continuous oplog archive | $\le$ 15 minutes |
| **Audit Trails** | MongoDB (`audit_events`) + File Hash Chain | Atomic fsync append + Signed Checkpoints | Event-driven + Checkpoint externalization every 15 min | $\le$ 15 minutes |
| **Checkpoints (WORM)** | S3 Object Lock (Immutable Bucket) | Asynchronous Object Lock append | Continuous | $\le$ 15 minutes |
| **Document Storage** | S3 / MinIO Encrypted Objects | S3 Cross-Region/Zone Replication + Versioning | Continuous replication + Object versioning | $\le$ 15 minutes |
| **Encryption Keys** | AWS KMS / Hardware HSM | Multi-region CMK replication / HSM partition backup | Real-time HSM replication | 0 minutes |

### 3.2 MongoDB Point-in-Time Recovery (PITR) Procedure

In the event of database node corruption, disaster, or accidental data truncation:

1. **Isolate Database Traffic:**
   ```bash
   kubectl scale deployment doc-extractor-api --replicas=0
   kubectl scale deployment doc-extractor-worker --replicas=0
   ```
2. **Provision Clean Replica Set:**
   Launch fresh MongoDB instances initialized as replica set `rs0`.
3. **Restore Base Snapshot:**
   ```bash
   mongorestore --host="rs0/mongo-0:27017,mongo-1:27017,mongo-2:27017" \
     --ssl --sslCAFile=/etc/ssl/mongo-ca.pem \
     --username=admin --password="${MONGO_RESTORE_PASSWORD}" \
     --authenticationDatabase=admin \
     --drop --gzip --archive=/backups/mongo/daily_base_snapshot.archive.gz
   ```
4. **Replay Oplog to Target Timestamp (PITR within 15 min):**
   ```bash
   # Replay oplog up to target recovery point (e.g., 2026-11-24T18:45:00Z)
   TARGET_TS="1732473900:1"
   mongorestore --host="rs0/mongo-0:27017,mongo-1:27017,mongo-2:27017" \
     --ssl --sslCAFile=/etc/ssl/mongo-ca.pem \
     --username=admin --password="${MONGO_RESTORE_PASSWORD}" \
     --authenticationDatabase=admin \
     --oplogReplay --oplogLimit="${TARGET_TS}" /backups/mongo/oplog_slice/
   ```
5. **Verify Index Integrity:**
   Restart one API pod to execute `ensure_indexes` bootstrap:
   ```bash
   kubectl scale deployment doc-extractor-api --replicas=1
   kubectl logs -l app.kubernetes.io/name=doc-extractor-api --tail=100
   ```

### 3.3 Audit Chain & Checkpoint Reconciliation Procedure

Following a database restoration, operators must verify audit integrity against external immutable storage:

1. **Execute File Hash Chain Scan:**
   ```bash
   python scripts/ops/verify_audit_chains.py --audit-store /var/lib/doc-extractor/storage
   ```
2. **Execute Signed Checkpoint Verification:**
   ```bash
   python scripts/ops/verify_audit_checkpoints.py \
     --tenant-id "${TENANT_ID}" \
     --checkpoint-file "/mnt/s3_worm_checkpoints/${TENANT_ID}/latest.json" \
     --public-key-file "/etc/pki/audit_signer_public.pem"
   ```
3. **Detect & Alert on Discrepancies:**
   If `verify_audit_checkpoints.py` reports `tampered` or `invalid_cryptographic_signature`, an immediate security incident is declared.

### 3.4 S3 Document Storage Disaster Recovery Procedure

1. **Bucket Accidental Deletion / Object Overwrite:**
   - S3 Versioning is permanently enabled. To restore deleted documents:
   ```bash
   # Remove delete markers from affected tenant prefix
   aws s3api list-object-versions --bucket "${S3_BUCKET}" --prefix "tenants/${TENANT_ID}/" \
     --query 'DeleteMarkers[?IsLatest==`true`].[Key, VersionId]' --output text | \
     while read key versionId; do
       aws s3api delete-object --bucket "${S3_BUCKET}" --key "$key" --version-id "$versionId"
     done
   ```
2. **Key Rotation & Server-Side Rewrapping:**
   - When a KMS KEK is rotated, existing objects remain decryptable using their wrapped DEKs.
   - To rewrap encrypted objects under the new KEK version without decrypting ciphertext payloads, execute `S3Storage.rewrap_object(relative_path, new_kek_id, new_key_version)`.

---

## 4. Production Observability & Diagnostics

### 4.1 Multi-Component Health Diagnostics (`GET /health`)

The health endpoint provides real-time readiness and liveness diagnostics across all active infrastructure components without blocking or dropping active connections:

```json
{
  "status": "healthy",
  "service": "module-a-ingestion",
  "version": "1.0.0",
  "api_version": "v1",
  "environment": "production",
  "egress_isolated": true,
  "target_render_dpi": 200,
  "malware_scan_enabled": true,
  "malware_scanner_type": "clamav",
  "malware_scanner_ready": true,
  "malware_scanner_engine": "ClamAVSocketScanner",
  "database": {
    "status": "ok",
    "replica_set": "rs0",
    "latency_ms": 2.4
  },
  "queue": {
    "status": "ok",
    "backend": "rabbitmq"
  },
  "redis": {
    "status": "ok"
  },
  "storage": {
    "status": "ok",
    "backend": "s3"
  },
  "kms": {
    "status": "ok",
    "provider": "aws_kms"
  }
}
```

- If an infrastructure component (MongoDB, RabbitMQ, Redis, S3, or KMS) experiences latency or an outage, the component is marked `"status": "degraded"` or `"unhealthy"`, and overall service status transitions to `"degraded"` while continuing to serve cached probes, preventing cascading pod restarts.

### 4.2 Security & Operational Alerts (`SEC-021`)

The centralized `AlertMonitor` (`src/doc_extractor/alerts.py`) emits rate-limited, structured alerts across eight canonical conditions:

1. `repeated_failed_logins`: Emitted when failed authentication attempts on a credential fingerprint exceed 5 within 5 minutes.
2. `unusual_export_volume`: Emitted when extracted data export exceeds 10,000 records within 1 hour.
3. `unmask_spike`: Emitted when PII unmasking requests for a tenant/actor exceed 20 within 1 hour.
4. `egress_attempt`: Emitted immediately upon any outbound connection attempt to an unauthorized destination (`SEC-011`).
5. `audit_chain_failure`: Emitted when an audit hash mismatch or sequence gap is detected.
6. `model_file_change`: Emitted when local model weight SHA-256 hashes differ from pinned release hashes (`SEC-014`).
7. `queue_worker_failure`: Emitted when asynchronous processing encounters terminal queue or lease failure.
8. `audit_checkpoint_failure`: Emitted when signed checkpoint generation, verification, or WORM sink append fails.

---

## 5. Security & PRD Compliance Assessment

| Requirement | Description | Status | Verification & Evidence |
| --- | --- | --- | --- |
| **SEC-001** | Internal identity & MFA for Admin/Approver | **Compliant** | `tests/test_identity_and_authentication.py`, `tests/test_tenant_identity_webhook_security.py` |
| **SEC-002** | RS256 JWTs (1h max), rotating refresh tokens, API keys | **Compliant** | Single-use conditional token consumption, family revocation on replay, persistent MongoDB stores. |
| **SEC-003** | 7 canonical roles, least privilege, four-eyes approval | **Compliant** | `tests/test_schema_governance_rbac.py`, `tests/test_api_security_wiring.py` |
| **SEC-004** | TLS 1.2+ and mutual TLS service-to-service | **Operational** | Configured via deployment ingress and service mesh (mTLS). |
| **SEC-005** | AES-256 envelope encryption with KMS/HSM | **Partially Outstanding** | Implemented in `src/doc_extractor/infrastructure/storage/`. Pending M5-D18 spooling elimination or volume encryption. |
| **SEC-006** | Strict tenant isolation across DB, queue, storage, cache | **Compliant** | Tenant-first indexing across all collections; path-safe tenant IDs enforced. |
| **SEC-007** | PII masking and unmask rate tracking | **Compliant** | Redaction transforms, audit trail logging, and `unmask_spike` alerts. |
| **SEC-008** | Tamper-evident audit chain & signed checkpoints | **Partially Outstanding** | Implemented in `src/doc_extractor/infrastructure/audit/`. Pending physical Object Lock bucket and M5-D15. |
| **SEC-009** | Malware scanning, fail-closed, no mock in production | **Compliant** | `ClamAVSocketScanner` enforces fail-closed; mock scanner strictly rejected in staging/production. |
| **SEC-011** | Default-deny outbound egress | **Compliant** | Application-level `EgressPolicy` + K8s `NetworkPolicy`. |
| **SEC-012** | Telemetry and logs free of PII and credentials | **Compliant** | Credentials logged as 16-char SHA-256 fingerprints; document payloads excluded from error messages. |
| **SEC-013** | Secrets manager integration and rotation | **Compliant** | Passwords/keys read via `SecretStr` / files; zero plaintext credentials in git or images. |
| **SEC-014** | Pinned model weights & hash integrity verification | **Compliant** | Model file verification in `AlertMonitor.check_model_files`. |
| **SEC-015** | Secure SDLC: SAST, dependency scanning, pen-test | **Operational** | Clean dependency checks in CI; external penetration test scheduled prior to GA. |
| **SEC-016** | Backup schedule (RPO $\le$ 15m, RTO $\le$ 4h) | **Compliant (Procedures Documented)** | Documented runbooks in Section 3; physical restore drill to be conducted quarterly. |
| **SEC-017** | Distributed rate limiting per client | **Compliant** | Redis token bucket (`RedisTokenBucket`) across replicas. |
| **SEC-019** | Data residency (AWS Mumbai `ap-south-1`) | **Compliant** | Configured in deployment manifests; S3 bucket and KMS CMKs restricted to Mumbai region. |
| **SEC-020** | UI session timeout & CSRF/CSP protections | **Compliant** | 15-minute token expiry, secure headers middleware. |
| **SEC-021** | Security alerts across canonical conditions | **Compliant** | `AlertMonitor` emits alerts for 8 conditions; tested in `test_alerts.py`. |

---

## 6. Open Architecture Decisions Register

All open integration decisions remain explicitly **Open** in `docs/m5-open-decisions.md`:

- **M5-D08 (Replay of Shown-Once API Key):** Preserved as Open; API key router remains unmounted.
- **M5-D09 (Webhook Secret Storage):** Preserved as Open; `secret_ref` interface implemented pending enterprise secrets manager backend selection.
- **M5-D15 (Audit Sink Policy):** Preserved as Open; synchronous vs. asynchronous failure semantics pending PRD owner resolution.
- **M5-D16 (Tenant-ID Policy):** Preserved as Open; strict path safety enforced without lossy case-folding or normalization.
- **M5-D17 (Tenant Provisioning):** Preserved as Open; explicit administrative provisioning enforced pending self-service SaaS policy.
- **M5-D18 (One Persistence Interface for Document Record):** Preserved as Open; blocks migration of descriptor-based readers to direct S3 streaming.
