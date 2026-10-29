# Document Extraction Engine — Module A: Document Ingestion & Preprocessing

## Overview

This repository houses **Module A: Document Ingestion & Preprocessing** of the **Document Extraction Engine**. The engine is an on-premise, schema-driven system designed to transform scanned BFSI (banking, financial services, insurance) and healthcare documents into validated, evidence-grounded JSON.

Module A represents Stage 1 of the decoupled pipeline. It is responsible for:
- Accepting multi-format documents (PDF, JPG, PNG, TIFF) via API, UI upload, and watched folders (`FR-ING-001`).
- Magic-byte verification, file size limits, page count boundaries, and decompression bomb mitigation (`FR-ING-002`, `GR-001`).
- Integration with pre-processing malware scanning (`FR-ING-003`, `SEC-009`).
- PDF rendering using `pypdfium2` strictly at the PRD-mandated standard of **200 DPI** (`FR-ING-004`, `SCH-013`).
- OpenCV-based image preprocessing (de-skew, rotation correction, contrast normalisation) with recorded transformation metadata (`FR-ING-005`).
- Multi-page splitting and page ordering linked to document records (`FR-ING-004`).
- Secure, tenant-isolated document & page storage with duplicate handling via file hashing and idempotency keys (`FR-ING-006`, `SEC-005`, `SEC-006`).
- Private, customer-controlled processing with zero document-data egress (`ARC-001`, `SEC-011`).

---

## Security & Deployment Boundaries

1. **ARC-001 / SEC-011 (Customer Boundary & Egress Isolation)**:
   - Application configuration (`Settings`) enforces `allow_external_egress=False` and fails closed on startup if external egress is enabled.
   - **Deployment Prerequisite**: In accordance with PRD `SEC-011`, enforced network egress denial must be implemented at the infrastructure/container boundary (e.g. Kubernetes `NetworkPolicy`, VPC firewall rules, or Docker `--network none`). The application code operates 100% offline without remote network dependencies.
2. **SEC-009 (Malware Scanning)**:
   - Pluggable `MalwareScanner` interface.
   - In development/testing, `MockMalwareScanner` identifies the EICAR test signature (`FR-ING-003`).
   - If no scanner is configured, `UnconfiguredScanner` fails closed with status `FAILED` and error code `SCANNER_UNAVAILABLE`.
   - **Deployment Prerequisite**: Production deployment requires connecting a real ClamAV daemon socket or containerized signature bundle.
3. **SEC-005 & SEC-006 (Tenant Isolation & Encryption at Rest)**:
   - Storage and idempotency paths are strictly segregated by `tenant_id`: `{storage_base_dir}/{tenant_id}/{document_id}/`.
   - Path traversal (`..`, `/`, `\`) is rejected with `StorageError`.
   - Public handoff contracts expose relative storage URIs (`{tenant_id}/{doc_id}/...`) rather than host-specific filesystem paths.
   - **Deployment Prerequisite**: Backing filesystem block storage must be encrypted with AES-256 using customer-held KMS/HSM keys per PRD `SEC-005`.
4. **NFR-006 & API-013 (Concurrency & Crash Recovery)**:
   - Per-key threading locks protect identical concurrent requests.
   - Disk-persisted idempotency records allow cold process restarts to recover and serve cached document IDs within 24 hours.
   - **Deployment Prerequisite**: Multi-replica horizontal deployments require backing idempotency records with a distributed lock/store (e.g. Redis or PostgreSQL).

---

## Phase 1 Implementation Scope

## Phase Roadmap & Progress

- [x] **Phase 1 — Project Foundation**: Typed configuration, PRD API-010 error envelopes, structured JSON logging with SEC-012 PII masking, health endpoints, zero-egress guardrails, packaging (`pyproject.toml`).
- [x] **Phase 2 — Secure Document Ingestion & Input Validation**:
  - `FR-ING-001`: Multi-format input acceptance for PDF, JPG, PNG, TIFF.
  - `FR-ING-002`: Binary magic-byte sniffing (`%PDF-`, `\xFF\xD8\xFF`, `\x89PNG\r\n\x1a\n`, `II*\x00` / `MM\x00*`), extension-mismatch detection, safe PDF/TIFF page counting, dimension limits (`GR-001` decompression-bomb guard), and rejection under standard code `UNSUPPORTED_FILE`.
  - `FR-ING-003` & `SEC-009`: Pluggable offline malware scanning boundary (`MalwareScanner`, `MockMalwareScanner`, `UnconfiguredScanner`), EICAR test rejection with status `rejected_malware`, fail-closed error handling (`SCANNER_UNAVAILABLE`), and downstream pipeline gating.
  - `GR-001`: 0 crashes and 100% rejection across a malformed fuzz corpus.
  - Truthful health reporting distinguishing configured intent from live scanner readiness.
- [x] **Phase 3 — PDF Rendering Engine**:
  - `FR-ING-004` & `SCH-013`: Multi-page PDF splitting and 200-DPI rendering via `pypdfium2`.
  - Exact scale calculation `scale = target_dpi / 72.0` (200 / 72 = 2.7777777777777777).
  - 1-based page ordering, dimension tracking (`width_px`, `height_px`), format consistency (`PNG`), and parent `document_id` linkage.
  - Safe local storage abstraction (`StorageBackend`, `LocalDiskStorage`) with path traversal guards and atomic mid-document failure rollback.
  - Full pipeline gating ensuring infected, invalid, or unscanned inputs never reach rendering.
- [x] **Phase 4 — OpenCV Preprocessing Pipeline**:
  - `FR-ING-005`: De-skewing, rotation correction, contrast normalisation, and transform metadata persistence.
  - Skew estimation via Probabilistic Hough Line Transform (`HoughLinesP`).
  - PRD Acceptance Target met: 5° synthetic skew corrected to **residual skew $\le 0.5^\circ$** (measured independently on disk image).
  - Skew threshold guard: small tilts ($< 0.5^\circ$) preserved to prevent unnecessary interpolation artifacts.
  - Orientation analysis using text projection profile variance; ambiguous orientation preserved without unjustified rotation.
  - Contrast normalisation via CLAHE (L-channel in LAB space for color, direct on grayscale).
  - Sidecar metadata JSON persistence alongside preprocessed page artifacts under `{storage}/{tenant}/{doc_id}/preprocessed/`.
  - Atomic mid-document rollback on preprocessing failure, leaving original rendered images intact.
- [x] **Phase 5 — Storage, Duplicate Detection & Idempotency**:
  - `FR-ING-006` & `API-013`: Replay protection for mutating requests within a 24-hour window using `Idempotency-Key`.
  - PRD Acceptance Target met: Given two POSTs with one key, one document exists and both calls return its ID.
  - Conflict detection: Reusing an idempotency key with different file content or altered request parameters raises `IDEMPOTENCY_KEY_CONFLICT` (HTTP 409 compatible).
  - Streaming SHA-256 content hashing without unbounded memory buffering.
  - Multi-tenant isolation (`SEC-006`): Records, paths, and idempotency scopes partition strictly by tenant.
  - Concurrency safety (`NFR-006`): Per-key fine-grained locking prevents duplicate document creation under concurrent identical uploads.
  - Lifecycle alignment (`SCH-012`): Mapped internal processing states (`queued`, `validating`, `preprocessing`, `ready_for_extraction`, `rejected_malware`, `rejected_unsupported`, `failed`) to external PRD statuses (`processing`, `auto_approved`, `needs_review`, `reviewed`, `rejected`, `failed`).
- [x] **Phase 6 — Module A Finalization & Handoff Readiness**:
  - Unified pipeline coordinator `ModuleAPipeline` orchestrating full Stage 1 (Ingestion -> Validation -> Malware Gate -> 200 DPI Render -> Preprocessing -> Storage -> Handoff).
  - Versioned, serializable handoff envelope `ModuleAHandoffPayload` (v1.0.0) satisfying PRD `ARC-003` and `SCH-011`.
  - Coordinate transform inversion utility `CoordinateTransform.map_bbox_preprocessed_to_original()` mapping downstream bounding boxes back to original 200-DPI top-left coordinates per PRD `SCH-013`.
  - Queue broker adapter abstraction `QueueAdapter` and `LocalMemoryQueueAdapter` for inter-stage communication.
  - Full end-to-end integration and failure recovery test suite (79 passing tests across all modules).

---

## Prerequisites & Local Setup

- **Python**: Version 3.10 or higher.
- **Git**: Git 2.x.
- **Operating System**: Linux / Windows / macOS (tested on Windows 11 with Python 3.10.0).

### 1. Create a Virtual Environment

```bash
# Create virtual environment
python -m venv .venv

# Activate on Linux/macOS:
source .venv/bin/activate

# Activate on Windows (PowerShell):
.venv\Scripts\Activate.ps1
```

### 2. Install Dependencies

```bash
# Install development & test dependencies:
pip install -r requirements-dev.txt

# Or install in editable mode:
pip install -e .
```

### 3. Configure Environment

Copy the example environment configuration:
```bash
# On Linux/macOS:
cp .env.example .env

# On Windows (PowerShell):
Copy-Item .env.example .env
```

---

## Running Checks & Tests

Run the automated pytest suite:
```bash
pytest -v
```

Execute the module entry point:
```bash
# Using module execution with src on PYTHONPATH
$env:PYTHONPATH="src"; python -m doc_extractor.main   # PowerShell
# or
PYTHONPATH=src python -m doc_extractor.main           # Bash
```
Expected output:
```json
{"timestamp": "...", "level": "INFO", "logger": "doc_extractor", "message": "Module A foundation initialized successfully", "service": "module-a-ingestion", "environment": "development", "extra": {"status": "healthy", "service": "module-a-ingestion", "version": "0.1.0", "environment": "development", "egress_isolated": true, "target_render_dpi": 200, "malware_scan_configured": true}}
```

