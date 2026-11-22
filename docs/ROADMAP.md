# Roadmap: Six-Layer IDP Alignment & 5-Phase Delivery Plan (PRD v1.5)

## A. Six-Layer Capability-to-Phase Mapping

| Functional Layer | Capabilities Covered | Target Phase |
| --- | --- | --- |
| **1. Ingestion & Preprocessing** | PDF (vector & raster), TIFF, JPEG, PNG; OpenCV deskewing, auto-rotation, CLAHE contrast (`FR-ING-001`–`006`) | **Phase 1** |
| | Multi-format expansion (`DOCX`, `HEIC`), multi-page packet streams, mobile de-noising & shadow removal | **Phase 3** |
| | Packet splitting & stitching (separating bundled multi-document uploads at document boundaries) | **Phase 4** |
| **2. Multi-Modal Vision & Text Recognition** | Hybrid OCR/HTR & VDU (`pypdfium2` native text, `GLM-OCR` + text LLM, `Qwen3-VL-4B`); basic tables, checkboxes (`OMR`), character grids (`FR-PRS-001`–`005`) | **Phase 1–2** |
| | Borderless tables, nested grids, radio buttons, company stamp & signature detection | **Phase 3–4** |
| **3. Zero-Shot & Few-Shot Classification** | Deterministic + VLM category routing (`structured`, `unstructured`, `handwritten`) & safe `UNKNOWN` fallback (`FR-CLS-001`–`007`, `GR-004`) | **Phase 1–2** |
| | Expanded 7+ launch types & unstructured collateral (brochures, pamphlets, policy wordings) across regional languages | **Phase 3** |
| | Zero-shot routing & semantic/visual classification for previously unseen document types without retraining | **Phase 4** |
| **4. Schema-Driven, Context-Aware Extraction** | Canonical `ModuleCPipeline`, multipage aggregation, semantic KV extraction, repeating groups, `[page, x0, y0, x1, y1]` grounding (`FR-EXT-001`–`007`, `SCH-013`) | **Phase 1** |
| | Prose/entity extraction for medical summaries (`DOC-03`), brochures & policy wordings; calibrated confidence (`M6`) | **Phase 2–3** |
| | Self-service Dynamic Schema Studio (`SCR-05`) & schema discovery for unseen layouts | **Phase 4** |
| **5. Automated Validation & Normalization** | ISO 8601 dates, E.164 phones, currency, PAN/Aadhaar/IFSC format checks, arithmetic & chronology rules (`FR-VAL-001`–`006`) | **Phase 1–2** |
| | Address normalization & cleaning; opt-in Master Data / internal VPC API verification (ID, postal, policy lookups) | **Phase 3 / 5** |
| **6. HITL & Governance** | Conservative review gating (`REQUIRED_FIELD_MISSING`, `FIELD_VALUE_CONFLICT`, `PARTIAL_PAGE_COVERAGE`, `EVIDENCE_MISSING`, `GR-002`/`003`/`027`) | **Phase 1** |
| | Side-by-side review workspace (`SCR-04`), reviewer overrides (`FR-CLS-004`), immutable audit logs (`FR-AUD-001`), encryption, ClamAV, RBAC/4-eyes (`SEC-001`–`012`) | **Phase 2** |
| | Automated PII masking pipelines (`SEC-007`) | **Phase 2–3** |
| | Continuous active learning (HITL corrections -> prompt/fine-tuning loop), SOC2/HIPAA/GDPR compliance hardening | **Phase 5** |

---

## B. Five-Phase Execution Plan

### Phase 1 – Engine Stabilization & Canonical Pipeline
- **Single Canonical Pipeline:** Unify extraction, grounding, normalization, and envelope assembly under `ModuleCPipeline` (supporting `VLMAdapter` / `Qwen3-VL-4B`, `OCRTextAdapter` / `GLM-OCR`, and `PdfiumTextParserAdapter`—with no OS-level OCR dependency).
- **Multipage & Grounding:** Page-by-page extraction, per-page `CoordinateTransform` to 200-DPI original pixels (`[x0, y0, x1, y1]` per `SCH-013`), cross-page deduplication + conflict flagging (`FIELD_VALUE_CONFLICT`), and `PARTIAL_PAGE_COVERAGE`.
- **Safe Validation & Confidence Integrity:** Eliminate synthetic confidence values (`ConfidenceProvenance` + `None` when unmeasured), preserve valid fields when required fields are absent (`REQUIRED_FIELD_MISSING`), and block `UNKNOWN` documents from silently binding a schema.

### Phase 2 – MVP Release (Gate G1)
- **3 MVP Document Types (`DOC-01`, `DOC-02`, `DOC-03`):** Governed by `SchemaLifecycleManager` + four-eyes `SchemaApprovalService` (`SCH-004`/`SCH-005`).
- **Confidence Calibration & G1 Benchmark Sign-off:** Implement calibrated field confidence (`M6 <= 3.0 pts`) enabling safe `auto_approve`; run GPU benchmarks on adjudicated `locked_test` sets (`M1 >= 92%` all / `>= 95%` key fields, `M11 = 0` escapes, `GR-003 <= 0.5%` false-fill) and specialist cost/accuracy evaluation (`FR-EVL-001`).
- **Review UI, API & Production Security:** Deliver REST API (`API-*`), side-by-side review workspace (`SCR-04`), immutable audit logs (`FR-AUD-001`), live ClamAV (`SEC-009`), durable message broker, distributed Redis idempotency, vLLM/Ollama serving (`ARC-004`/`005`), AES-256 storage encryption (`SEC-005`), PII-safe logging/masking, and zero-egress enforcement (`ARC-001`, `SEC-011`).

### Phase 3 – v1 Release (Gate G2)
- **7+ Launch Document Types & Unstructured Prose:** Onboard remaining launch types (`DOC-04`–`DOC-08` incl. loans, credit cards, claims, prescriptions, policy wordings) plus unstructured brochures, pamphlets, and leaflets.
- **Languages & Noisy-Channel Formats:** Support regional Indian languages (`hi`, `te`, `ta`, `ml`, `en`), additional formats (`DOCX`, `HEIC`), mobile CV enhancement (de-noising, shadow removal), address normalization, and downstream connectors.
- **Richer Visual Elements:** Borderless tables, radio buttons, and stamp/signature detection.

### Phase 4 – Universal IDP
- **Zero-Shot Unseen Documents:** Classify and extract previously unseen document categories without retraining.
- **Self-Service Schema Studio (`SCR-05`):** Expose `DynamicSchemaCompiler`, `DynamicExtractionAdapter`, and testing/approval workflows to tenant admins.
- **Packet Splitting & Complex Layouts:** Auto-split/stitch bundled multi-document uploads and parse nested grids / dense multi-page layouts.

### Phase 5 – Advanced Enterprise IDP (Gate G3)
- **Cross-Document & Master Data Intelligence:** Case/packet-level reconciliation across documents plus governed Master Data / internal VPC API verification.
- **Agentic Recovery & Continuous Active Learning:** Automated low-confidence crop/re-prompt recovery and human-feedback fine-tuning loops gated by `GR-005`.
- **Enterprise HA & Compliance:** Multi-replica active-active workers, GPU autoscaling, and full SOC2 / HIPAA / GDPR compliance certification.
