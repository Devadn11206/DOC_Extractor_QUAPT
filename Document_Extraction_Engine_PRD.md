# Document Extraction Engine: Product Requirements Document (PRD)
## Product Requirements Document for Spec-Driven Development

**Version:** 1.1 (draft for review; aligned to the ICXBee PRD and mockup conventions)  
**Date:** 2026-10-29  
**Author:** Praveen  
**Scope:** Schema-driven extraction of 7 BFSI and healthcare document types into validated, evidence-grounded JSON; SaaS (AWS Mumbai) and customer-controlled deployments from one build  
**Flows:** Upload and intake, Document tracking, Review (queue and workbench), Schema authoring and approval, Results and export, Developer console, Audit, Security and policy, Operations dashboard  
**Derived from:** Technical Approach document (`IDP_Architecture_Proposal_without_header_table.pdf`), the Product Requirements Outline (F1–F21), and the ICXBee PRD set under `icxbee-refactor-services/docs/prd/` (conventions, identity, roles, plan entitlements)  
**Screens:** `DOCX-SCR-001` to `DOCX-SCR-010` (each with state variants, see 8.7) in `icxbee-mockups/flows/document-extraction/`. The original `SCR-01` to `SCR-10` identifiers remain valid aliases (8.6)

> **Conventions.** Numbering follows the other ICXBee PRDs: `FR-*` functional, `NFR-*` non-functional, `US-*`
> user stories with Gherkin (Section 17), `OQ-*` open questions (15.6). Module-level identifiers that ICXBee PRDs
> scope by a module segment carry the segment `DOCX`: `US-DOCX-*`, `OQ-DOCX-*`, `BR-DOCX-*`, screens
> `DOCX-SCR-*`. The requirement families already in this document (`FR-ING`, `FR-CLS`, `SCH`, `API`, `GR`, `SEC`,
> `NFR`, `MOCK`, …) keep their IDs unchanged (see 3.3). Items marked **[Proposed]** are this PRD's recommendation
> where neither the source documents nor the ICXBee PRDs give an answer; each is tracked in 15.6.

Oct 28, 2026 · @Praveen (original draft) · updated 2026-10-29

This PRD defines a schema-driven engine, deployable as SaaS or on-premise, that turns scanned BFSI and healthcare documents into validated, evidence-grounded JSON, written so each requirement can be built and tested as a spec. It merges the Technical Approach document (architecture, models, 7 document types) with the Product Requirements Outline (personas, F1–F21, roadmap), and adds mock-screen, guardrail, security and NIST CSF 2.0 requirements.

## 1. Decisions taken in this PRD

The two inputs disagree in five places. This PRD resolves each one so engineering is not blocked; the product owner confirmed or changed each one on 2026-10-29 (D1 and D2 changed, D3 to D5 accepted as drafted).

| # | Conflict | Approach document | Outline | PRD decision (confirmed 2026-10-29) |
| --- | --- | --- | --- | --- |
| D1 | Deployment | 100% on-premise, zero data leaves premises | SaaS first | **Two deployment options from one build:** vendor-hosted SaaS (AWS Mumbai, ap-south-1) and customer-controlled private cloud, VPC or on-premise. A deployment profile in configuration decides which controls apply (ARC-006). |
| D2 | Model providers | Local models only; cloud APIs excluded | Provider abstraction, swap by cost or residency | **Local models are the default; cloud APIs are included as optional providers** behind the same interface. A signed policy per tenant and document type decides whether any external provider may be called; rules in Section 9.2. The on-premise profile ships with cloud providers disabled. |
| D3 | Accuracy bar | Field F1 > 92% (structured/semi), CER < 3%, WER < 6% | 95%+ key-field accuracy; STP "80% above" | **Two bars:** F1 ≥ 92% on all fields and ≥ 95% on schema-marked key fields for structured and semi-structured types. **STP targets are deferred** until the first paying customer defines them; the escape rule is fixed now: 0 wrong key fields in auto-approved documents (GR-027). Handwritten prescriptions always route to review below the confidence threshold. |
| D4 | Latency | < 3.0 s/page on GPU | < 10 s/page | **p95 ≤ 3.0 s/page with local models on the recommended GPU profile; ≤ 10 s/page on CPU-only fallback.** Cloud-provider latency is reported, not gated. |
| D5 | MVP scope | 7 types, but prototype on CASA + Discharge Summary first | 2–3 types across InsurTech, FinTech, Healthcare | **MVP = CASA Account Opening, Insurance Application, Discharge Summary** (one per domain). The other four types are schema-only additions in v1. |

Two further inputs from the outline's answers are adopted as stated: pricing is per page, and launch languages are English, Hindi, Telugu, Tamil and Malayalam, with handwriting reading in English only at first. Malayalam is not covered by the approach document's model selection, so it is a validated-or-not gate in Section 14 rather than an assumption.

## 2. Product context

Banks, insurers and hospitals receive proposal forms, claim forms, loan and card applications, discharge summaries and prescriptions in hundreds of layouts. Template OCR breaks on each new layout, flat OCR destroys tables and checkbox state, and manual keying is slow and error-prone. Many customers will not allow PII and PHI to leave their premises, while others will accept a hosted service, so the product must run both ways and let policy decide where document data may go.

**Vision.** Onboard a new document type by writing a schema, not by building templates or retraining. Every value is typed, validated, confidence-scored and linked to the pixel region it came from. Anything the engine is unsure about goes to a person instead of being guessed.

### 2.1 Personas

| Persona | Primary need | Key screens |
| --- | --- | --- |
| Operations analyst | Upload, track and export documents with minimal rework | SCR-01, SCR-02, SCR-06 |
| Reviewer / QC | See only exceptions, verify against the source in one click, correct inline | SCR-03, SCR-04 |
| Business / product owner | Add or change a document type without engineering | SCR-05 |
| Developer / integrator | Stable API, webhooks, versioned output contract | SCR-07 |
| Compliance and audit | Traceability, access control, retention evidence | SCR-08, SCR-09 |
| Platform / security admin | Deploy on-prem, manage users, models, keys and egress policy | SCR-09, SCR-10 |

### 2.2 Goals

1. Extract the 7 launch document types into strictly typed JSON with 100% syntactically valid output.
2. Make every value traceable to a page and bounding box, with a calibrated confidence.
3. Keep document data inside the boundary the customer chooses: fully local in the on-premise profile, and sent to an external model provider only when policy allows it.
4. Let a business user add a new document type in under one day, with no code change.
5. Route uncertain fields to human review instead of guessing, especially handwriting.

### 2.3 Non-goals (this release)

- Underwriting, credit or claim decisions, or any automated action on extracted data.
- Document generation, long-term archival or document management.
- Sending document data to any external provider that policy has not approved (see D2, Section 9.2).
- Free-form question answering over documents.
- Training a foundation model; only parameter-efficient fine-tuning (PEFT/QLoRA) on customer data, run inside the deployment that holds the data.

### 2.4 Success metrics

Targets are measured on a labelled benchmark set per document type (Section 13) and gate each phase (Section 14).

| ID | Metric | Definition | Target |
| --- | --- | --- | --- |
| M1 | Field F1 | Harmonic mean of precision and recall on key-value pairs, structured and semi-structured types | ≥ 92% all fields; ≥ 95% key fields |
| M2 | Parser CER / WER | Levenshtein error of layout-parser text vs ground truth, printed text | CER < 3%, WER < 6% |
| M3 | Schema adherence | Outputs passing schema validation with zero exceptions | 100% |
| M4 | Classification accuracy | Documents routed to the correct type; unknown routed to review | ≥ 98% |
| M5 | Straight-through rate (STP) | Documents with no human touch | Deferred: to be set with the first paying customer; reported from MVP, not gated |
| M6 | Confidence calibration | Gap between stated confidence and measured accuracy | ≤ 3 points |
| M7 | Latency | Upload to validated JSON, per page | p95 ≤ 3.0 s GPU; ≤ 10 s CPU fallback |
| M8 | New document type onboarding | Schema defined to production-approved | < 1 working day |
| M9 | Unapproved data egress | Outbound connections carrying document data to a destination the policy does not allow | 0 |
| M10 | Review efficiency | Median reviewer time per flagged field | ≤ 15 s (to validate) |
| M11 | Escape rate | Auto-approved documents containing a wrong key-field value | 0 on the locked set; production sampling and auto-pause (GR-027) |

## 3. Scope and how to read this spec

### 3.1 Launch document types

Seven types in two domains. Complexity follows the approach document's matrix: structured forms are layout-predictable, handwriting is not.

| Doc ID | Document | Domain | Category | Phase | Main technical challenge |
| --- | --- | --- | --- | --- | --- |
| DOC-01 | CASA Account Opening Form | BFSI | Structured | MVP | Character-grid fields, checkbox marks, KYC blocks |
| DOC-02 | Insurance Application Form | BFSI | Structured | MVP | Medical questionnaires, dense tick grids, nominees |
| DOC-03 | Hospital Discharge Summary | Healthcare | Semi-structured | MVP | Hospital-specific layouts, multi-page medicine tables |
| DOC-04 | Loan Application Form | BFSI | Structured | v1 | Multi-column financial tables, applicant vs co-applicant |
| DOC-05 | Credit Card Application | BFSI | Structured | v1 | Disclosure tables, `[x]` vs `[ ]` disambiguation |
| DOC-06 | Insurance Claim Form | BFSI | Semi-structured | v1 | Itemised bills must sum to the claimed total |
| DOC-07 | Doctor's Prescription | Healthcare | Unstructured | v1 | Cursive handwriting, shorthand (OD, BD, TDS, SOS), ambiguous drug names |

Product brochures and policy wordings from the outline are tracked as DOC-08 (v1, fact extraction from unstructured text) and are not in the launch seven.

### 3.2 Inputs, languages and output

- **Input formats:** PDF (native and scanned, multi-page), JPG, PNG, TIFF at MVP; DOCX, XLSX, email and HTML in v1.
- **Languages at launch:** English, Hindi, Telugu, Tamil, Malayalam. Printed text first; handwriting in English only at MVP.
- **Output:** JSON (canonical), plus CSV and XML renderings in v1. Dates ISO 8601, amounts as decimals with ISO 4217 currency, booleans as true/false.

### 3.3 Spec conventions (for spec-driven development)

This document is the source of truth that code, tests and mock screens are generated from. To make that reliable:

- **Requirement IDs** are stable: `FR-<AREA>-nnn`, `SCH-nnn`, `API-nnn`, `SCR-nn`, `MOCK-nnn`, `GR-nnn`, `SEC-nnn`, `NFR-nnn`. Never reuse an ID; retire it instead.
- **Wording** follows EARS: *The system SHALL …* (ubiquitous), *WHEN … the system SHALL …* (event), *IF … THEN the system SHALL …* (unwanted case). SHALL is mandatory, SHOULD is recommended, MAY is optional.
- **Priority:** P0 = MVP gate, P1 = v1, P2 = later. Each row carries its phase through priority.
- **Acceptance criteria** are written so they can become a test without interpretation: Given / When / Then, or a measurable threshold with the benchmark that measures it.
- **Traceability:** every requirement cites its source (approach document section, or outline F-number) so that a changed source flags the requirements to re-check.
- **Ambiguity rule:** if a requirement cannot be turned into a test, it is a defect in this PRD. Raise it as a comment; do not guess in code.
- **Spec Kit ready.** Every user story in Section 17 is written in the ICXBee Gherkin style so it can be pasted into `/speckit.specify`; the pipeline is then `/speckit.clarify` → `/speckit.plan` → `/speckit.tasks` → `/speckit.analyze` → `/speckit.implement` (mapping in 16.6). The requirement families above are the stable IDs; stories, screens and tasks cite them.
- **ID families added for ICXBee alignment:** `US-DOCX-nnn` (story), `OQ-DOCX-nn` (open question), `BR-DOCX-nnn` (mockup business rule, Stage 0c), `DOCX-SCR-nnn[-state]` (screen and state, 8.6). Same retire-never-reuse rule.
- **Source tags** `A` (Technical Approach), `F` (Outline) remain; `P` marks a rule taken from an ICXBee PRD (for example `P:bo-features §7.7`).

### 3.4 Source handling summary

| Source | Reused as |
| --- | --- |
| Technical Approach document (`A`) | Sections 4, 5.1 to 5.5, 6, 12, 14; model and hardware choices |
| Product Requirements Outline (`F`) | Sections 2.1, 5, 8, 14; personas, F1–F21 |
| `icxbee-bo-features-prd.md` | Document structure, Gherkin story style, error-code catalogue, bulk-upload and export patterns (reference data upload SCH-008), screen-state suffix convention (8.6) |
| `icxbee-bo-plans-prd.md` | Plan entitlements of type `USAGE_METERED` and `LIMIT`: the vehicle for per-page pricing and quotas (NFR-016, 15.5) |
| `icxbee-bo-master-data-prd.md` | Code lists and bulk upload pattern (SCH-008); Roles list ownership |
| `icxbee-auth-registration-prd.md`, `icxbee-bo-login-prd.md` | Identity, token and role conventions (SEC-001, SEC-002, SEC-003); BO audit conventions |
| Architecture Decision Records in `docs/adr/` | ADR 0001 standalone product, Python, `/api/v1`; ADR 0002 internal identity plus SSO; ADR 0003 Spec Kit artefacts in the existing flat `specs/` folder |
| ICXBee constitution (`icxbee-refactor-services/.specify/memory/constitution.md`) | Quality gates, compliance-first sequencing, data residency (13.5, SEC-019) |
| `icxbee-mockups` (README, `TEMPLATE-mockup-brief.md`, `TEMPLATE-business-rules.md`, `MOCKUP-ASSUMPTIONS.md`) | Screen IDs, flow folder layout, generator pattern, Stage 0a/0b/0c artefacts (8.6) |

## 4. Architecture

The engine is a two-stage decoupled pipeline: a small vision model reads the page layout, then a local LLM extracts schema fields from that text, so each can change independently.

_Diagram: target architecture, 5 pipeline stages and 1 review loop. The drawing is in the live PRD: https://claude.ai/artifact/FhDRpYzjFVKERF4WiuuDsi_

Stages 3 and 4 (highlighted) are separate models, so either can be upgraded or re-quantised without touching the other. Only fully validated documents are approved automatically; everything else goes to a person.

| ID | Architectural requirement | Pri |
| --- | --- | --- |
| ARC-001 | All components SHALL run inside the deployment boundary (SaaS tenant environment or customer site); calls to external services SHALL go only to providers the policy allows (see GR-013, SEC-011). | P0 |
| ARC-002 | The parser and the extractor SHALL be separate services behind versioned interfaces so that either can be replaced by configuration (NFR-008). | P0 |
| ARC-003 | Stages SHALL communicate through a durable queue; workers SHALL be stateless, and each stage SHALL store its output under the run ID. | P0 |
| ARC-004 | Local model serving SHALL run in network-isolated containers with access only to the stage's input and output (GR-007). | P0 |
| ARC-005 | The reference stack from the approach document (Python, pypdfium2, OpenCV, Pydantic, instructor or Outlines, Ollama or vLLM, PaddleOCR-VL-1.6 or GLM-OCR, quantised Qwen2.5-7B-Instruct) is the starting point for local models and SHALL be replaceable if the interfaces and measured outcomes hold. | P0 |
| ARC-006 | One container build SHALL support three deployment profiles selected by configuration: SaaS (vendor-hosted, multi-tenant, AWS Mumbai ap-south-1), private cloud or VPC (single-tenant), and on-premise (sovereign: cloud providers disabled, outbound traffic denied). The profile SHALL set default provider policy, egress rules and key management. | P0 |
| ARC-007 | External model providers SHALL be reached only through a provider adapter that implements the same parser and extractor interfaces as the local models, applies the provider policy (Section 9.2), records every call without document content in the log, and falls back to the local model when policy disallows or the provider fails. | P0 |

## 5. Functional requirements

Organised in pipeline order. Source column: `A` = Technical Approach document section, `F` = Outline requirement number.

### 5.1 Ingestion and preprocessing

| ID | Requirement | Pri | Acceptance criteria | Source |
| --- | --- | --- | --- | --- |
| FR-ING-001 | The system SHALL accept PDF, JPG, PNG and TIFF via API, UI upload and a watched folder. | P0 | Given each format, When uploaded, Then a document record is created and status becomes `queued` within 2 s. | F1 |
| FR-ING-002 | The system SHALL verify each file by magic bytes, size limit and page limit, and reject others with an error code. | P0 | Given a renamed `.exe` as `.pdf`, Then upload is rejected `UNSUPPORTED_FILE` and nothing is stored. | GR-001 |
| FR-ING-003 | The system SHALL scan each file for malware before processing. | P0 | Given the EICAR test file, Then status is `rejected_malware` and no pipeline stage runs. | SEC-009 |
| FR-ING-004 | The system SHALL split multi-page PDFs and render each page at 200 DPI. | P0 | Given a 10-page PDF, Then 10 page images at 200 DPI exist, in order, linked to the document. | A §3 |
| FR-ING-005 | The system SHALL de-skew, correct rotation and normalise contrast per page, and record the transforms applied. | P0 | Given a benchmark page skewed 5°, Then residual skew ≤ 0.5° and the transform is in the page record. | A §3, F2 |
| FR-ING-006 | The system SHALL treat a repeated upload of the same file hash within one tenant as idempotent when the client sends the same idempotency key. | P0 | Given two POSTs with one key, Then one document exists and both calls return its ID. | NFR-006 |
| FR-ING-007 | The system SHOULD split a bundled file into logical documents and group related pages. | P1 | Given a 12-page bundle of 3 documents, Then 3 child documents with correct page ranges, ≥ 95% boundary accuracy on the benchmark. | F3 |
| FR-ING-008 | The system SHOULD accept DOCX, XLSX, email and HTML by converting to page images or text with layout. | P1 | Each format yields the same output contract as a scanned PDF. | F1 |

### 5.2 Classification and routing

| ID | Requirement | Pri | Acceptance criteria | Source |
| --- | --- | --- | --- | --- |
| FR-CLS-001 | The system SHALL classify each document into a domain (BFSI or Healthcare) and a document type, returning a confidence in \[0, 1\]. | P0 | Classification accuracy ≥ 98% (M4) on the benchmark. | A §3 stage 2, F5 |
| FR-CLS-002 | IF classification confidence is below the tenant threshold OR no schema matches, THEN the system SHALL set type `UNKNOWN` and route the document to review; it SHALL NOT run extraction. | P0 | Given an unrelated utility bill, Then type is `UNKNOWN`, no extraction output exists, a review task is created. | F5, GR-004 |
| FR-CLS-003 | WHEN a type is resolved, the system SHALL bind the current approved version of its schema and record that version on the document. | P0 | Output envelope contains `schema_id` and `schema_version` matching the approved version at processing time. | A §3 stage 2 |
| FR-CLS-004 | The reviewer SHALL be able to override the document type, which re-runs extraction with the chosen schema. | P0 | Given an override, Then a new extraction run is created and the old one is retained in history. | F16 |

### 5.3 Visual layout parsing

| ID | Requirement | Pri | Acceptance criteria | Source |
| --- | --- | --- | --- | --- |
| FR-PRS-001 | The system SHALL convert each page to layout-faithful Markdown with HTML tables, using the configured local parser model, and keep text blocks with page and bounding box. | P0 | Given a benchmark page, Then output has per-block `bbox` and CER < 3%, WER < 6% on printed text (M2). | A §3 stage 3, F6 |
| FR-PRS-002 | The system SHALL render every checkbox and tick as `[x]` or `[ ]` and record the mark's bounding box. | P0 | Checkbox state accuracy ≥ 98% on the DOC-02 and DOC-05 benchmark sets. | A §4.1, F6 |
| FR-PRS-003 | The system SHALL merge segmented character grids (for example `\|J\|O\|H\|N\|`) into one value before extraction. | P0 | Given a PAN written in 10 boxes, Then the parsed value has no spaces and equals ground truth. | A §4.1 |
| FR-PRS-004 | The system SHALL detect tables, key-value pairs, signatures and stamps as typed layout elements. | P0 | Element detection recall ≥ 90% on the benchmark. | F6 |
| FR-PRS-005 | The system SHALL report an optical confidence per text block. | P0 | Every block has `ocr_confidence` in \[0, 1\]. | A §4.2 |
| FR-PRS-006 | The system SHOULD read English handwriting and flag struck-out or overwritten entries. | P1 | Handwriting fields below the threshold are `review_required`; none is auto-approved. | F7 |
| FR-PRS-007 | The system SHOULD read Hindi, Telugu, Tamil and Malayalam printed text. | P1 | Per-language CER reported on the benchmark; a language ships only if its CER meets M2. | F4 |

### 5.4 Extraction

| ID | Requirement | Pri | Acceptance criteria | Source |
| --- | --- | --- | --- | --- |
| FR-EXT-001 | The system SHALL extract exactly the fields defined in the bound schema from the parsed text, using the local LLM, with no per-layout template or retraining. | P0 | A document of a known type from an unseen layout meets M1 without code changes. | A §3 stage 4, F8 |
| FR-EXT-002 | The system SHALL constrain model output to the schema using grammar-guided decoding. | P0 | Over 1,000 runs, zero schema-validation exceptions (M3). | A §3 stage 4 |
| FR-EXT-003 | IF a field is absent or not legible, THEN the system SHALL return `null` with `null_reason` of `absent` or `illegible`; it SHALL NOT infer a value. | P0 | Given a form with the PAN left blank, Then `pan_card_number` is null, reason `absent`. | A §7.2, F11 |
| FR-EXT-004 | The system SHALL return, for every non-null value, its source `page`, `bbox` and the supporting text snippet. | P0 | 100% of non-null values have evidence; evidence text contains the value after normalisation. | F15, GR-002 |
| FR-EXT-005 | The system SHALL extract repeating groups (nominees, dependents, medicines, line items) as arrays with stable order and, where present, a parent link. | P0 | Given 3 nominees, Then 3 array items in page order, each with evidence. | F9 |
| FR-EXT-006 | The system SHALL associate figures with the right party (applicant or co-applicant) in multi-column tables. | P1 | Party-attribution accuracy ≥ 95% on DOC-04. | A §2 |
| FR-EXT-007 | The system SHALL map checkbox markers to booleans or enumerated values defined in the schema. | P0 | `[x]` yields true, `[ ]` yields false, no mark yields null. | A §4.1 |
| FR-EXT-008 | The system MAY run a second reconciliation pass on fields below the confidence threshold and keep the result only if it agrees with evidence. | P1 | Second pass raises key-field F1 on the benchmark without raising hallucination rate. | F7, outline §7 |

### 5.5 Normalisation and validation

| ID | Requirement | Pri | Acceptance criteria | Source |
| --- | --- | --- | --- | --- |
| FR-VAL-001 | The system SHALL normalise dates to ISO 8601, amounts to decimals with currency, phone to E.164, and map values to tenant code lists. | P0 | `22/06/1985` becomes `1985-06-22`; `+91 98765 43210` becomes `+919876543210`. | F12 |
| FR-VAL-002 | The system SHALL validate format rules (PAN, Aadhaar checksum, IFSC, policy numbers) and mark failures `validation_failed`. | P0 | Given `ABCDE1234` as PAN, Then flagged `validation_failed` and routed to review. | A §3 stage 5, F13 |
| FR-VAL-003 | The system SHALL verify arithmetic: the sum of itemised amounts equals the declared total, within a configured tolerance. | P0 | Given items summing to 41,500 and a total of 45,100, Then an `ARITHMETIC_MISMATCH` flag is raised on the claim. | A §4.1 |
| FR-VAL-004 | The system SHALL enforce chronological and cross-field rules (for example discharge date ≥ admission date). | P0 | Given discharge before admission, Then both fields are flagged and the document is routed to review. | A §4.2, F13 |
| FR-VAL-005 | The system SHALL map clinical frequency shorthand (OD, BD, TDS, SOS) to standard descriptions and keep the original text. | P1 | `BD` yields `Twice daily (BD)`; unknown abbreviations are kept raw and flagged. | A §4.2 |
| FR-VAL-006 | Rules SHALL be configurable per schema and versioned with it, without a code release. | P0 | Changing a rule creates a new schema version; old documents keep the version they ran with. | outline §5 |
| FR-VAL-007 | The system SHOULD check consistency across documents in one case (name, date of birth, address). | P2 | Mismatch across KYC and application raises a case-level flag. | F13, F20 |

### 5.6 Confidence, routing and review

| ID | Requirement | Pri | Acceptance criteria | Source |
| --- | --- | --- | --- | --- |
| FR-CNF-001 | The system SHALL compute a calibrated confidence per field from parser confidence, model signals and validation outcome. | P0 | Calibration gap ≤ 3 points (M6) on the benchmark. | F14 |
| FR-CNF-002 | The system SHALL set `review_required: true` on any field whose confidence is below its threshold; default threshold 0.75 for handwriting-derived fields, tenant-configurable per field. | P0 | A handwritten drug name at 0.62 is `review_required` and not auto-approved. | A §4.2, F14 |
| FR-CNF-003 | WHEN all fields pass validation and thresholds, the system SHALL auto-approve the document; otherwise it SHALL create a review task listing only the flagged fields. | P0 | Review task contains flagged fields only; STP counted from auto-approved documents (M5). | F16 |
| FR-REV-001 | The reviewer UI SHALL show the page image beside the extracted fields and highlight the evidence box for the selected field. | P0 | Selecting a field scrolls and highlights its bbox within 300 ms. | F15, F16 |
| FR-REV-002 | The reviewer SHALL be able to accept, correct or mark-as-null a field inline; each action SHALL be audit-logged with user, time, old and new value. | P0 | Audit log contains one entry per action. | F16 |
| FR-REV-003 | A corrected value SHALL be re-validated before the task can be completed. | P0 | An invalid correction blocks completion with the failing rule shown. | F13 |
| FR-REV-004 | The system SHOULD add reviewer corrections to the benchmark candidate set, subject to approval. | P1 | Corrections appear in a pending-ground-truth list; none enters the benchmark automatically. | F17, GR-012 |

### 5.7 Delivery and audit

| ID | Requirement | Pri | Acceptance criteria | Source |
| --- | --- | --- | --- | --- |
| FR-OUT-001 | The system SHALL return results by synchronous API (single page) and asynchronous API with polling or webhook. | P0 | Contract tests in Section 7 pass. | F18 |
| FR-OUT-002 | The system SHALL emit output in the canonical JSON envelope defined in Section 6. | P0 | Output validates against the published JSON Schema. | F18 |
| FR-OUT-003 | The system SHOULD offer CSV and XML renderings and prebuilt connectors to a policy-admin, loan-origination, claims or CRM system. | P1 | One connector passes an end-to-end test against a customer sandbox. | F19 |
| FR-AUD-001 | The system SHALL record every extraction run with input hash, model versions, prompt version, schema version, timings and outputs so that it can be reproduced. | P0 | Re-running a stored run in the same environment yields identical output (deterministic decoding). | outline NFR |

## 6. Schema and output contract

The schema is the main control surface: business users define what to extract and which rules apply, and the engine adapts. Each schema compiles to a typed model (Pydantic) and a JSON Schema used for grammar-guided decoding and for output validation.

### 6.1 Schema requirements

| ID | Requirement | Pri | Acceptance criteria |
| --- | --- | --- | --- |
| SCH-001 | A schema SHALL define per field: name, type (string, integer, decimal, boolean, date, enum, object, array), description, required or optional, allowed values, format or regex, evidence required (default true), confidence threshold, and PII class. | P0 | A schema missing `type` for any field fails publish validation. |
| SCH-002 | Schemas SHALL support nested objects and repeating groups. | P0 | A schema with `nominees[]` produces an array in the output. |
| SCH-003 | A schema SHALL be authored as YAML or JSON and through the visual editor (SCR-05); both SHALL round-trip without loss. | P0 | Export from editor, re-import, diff is empty. |
| SCH-004 | A schema SHALL move through states `draft`, `testing`, `approved`, `retired`; only `approved` schemas are used in production. | P0 | Attempt to bind a `draft` schema in production returns `SCHEMA_NOT_APPROVED`. |
| SCH-005 | Promotion to `approved` SHALL require a passing test-harness run and approval by a second user (four-eyes). | P0 | Same user cannot approve own schema; harness result is attached to the approval. |
| SCH-006 | Every schema change SHALL create an immutable version with author, time and diff; rollback SHALL re-activate a prior version without deleting history. | P0 | After rollback, new documents bind the older version; the audit log shows the rollback. |
| SCH-007 | The test harness SHALL run a schema on uploaded sample documents with ground truth and report precision, recall, F1 and calibration per field. | P0 | Harness report lists every field with its F1. |
| SCH-008 | Reference data (code lists, lookup tables) SHALL be uploadable and versioned per tenant. | P1 | A state-code list is referenced by field `state` and applied in normalisation. |
| SCH-009 | A schema library of cloneable prebuilt schemas SHALL ship for the seven launch types. | P1 | Each launch type has an approved library schema. |
| SCH-010 | The system MAY draft a schema from a plain-English description, always as `draft` for human editing. | P2 | Generated schema is never auto-approved. |

### 6.2 Schema definition format (illustrative)

```yaml
schema_id: casa_account_opening
version: 3
status: approved
domain: BFSI
fields:
  - name: applicant.full_name
    type: string
    required: true
    description: Applicant name, merged from character boxes, uppercase as printed
    pii_class: direct_identifier
    confidence_threshold: 0.90
  - name: applicant.pan_card_number
    type: string
    required: false
    format: '^[A-Z]{5}[0-9]{4}[A-Z]$'
    pii_class: government_id
  - name: account_details.initial_deposit_amount
    type: decimal
    currency: INR
  - name: account_details.opt_in_services.cheque_book
    type: boolean
    source: checkbox
rules:
  - id: R1
    expression: applicant.date_of_birth <= today - 18y
    on_fail: review
```

### 6.3 Output envelope

Every value is wrapped with its confidence and evidence. The envelope below is the P0 contract; field names are normative.

```json
{
  "document_id": "d_01HZX...",
  "document_metadata": {
    "document_type": "CASA_ACCOUNT_OPENING",
    "schema_id": "casa_account_opening",
    "schema_version": 3,
    "classification_confidence": 0.96,
    "status": "needs_review",
    "page_count": 2
  },
  "fields": {
    "applicant.full_name": {
      "value": "SURESH KUMAR RAMAN",
      "confidence": 0.97,
      "null_reason": null,
      "review_required": false,
      "validation": "passed",
      "evidence": { "page": 1, "bbox": [112, 240, 610, 281], "text": "S U R E S H ..." }
    },
    "applicant.pan_card_number": {
      "value": null,
      "confidence": 0.0,
      "null_reason": "absent",
      "review_required": false,
      "validation": "not_applicable",
      "evidence": null
    }
  },
  "flags": [ { "code": "ARITHMETIC_MISMATCH", "fields": ["claim.total"] } ],
  "run": { "run_id": "r_...", "model_versions": { "parser": "...", "extractor": "..." }, "prompt_version": "..." }
}
```

| ID | Requirement | Pri |
| --- | --- | --- |
| SCH-011 | The envelope SHALL be published as JSON Schema and versioned with semantic versioning; breaking changes SHALL bump the major version and keep the previous version available for at least two releases. | P0 |
| SCH-012 | `status` SHALL be one of `processing`, `auto_approved`, `needs_review`, `reviewed`, `rejected`, `failed`. | P0 |
| SCH-013 | `bbox` SHALL be `[x0, y0, x1, y1]` in pixels of the 200 DPI page image with origin top-left. | P0 |
| SCH-014 | Fields marked PII in the schema SHALL be maskable in API responses by caller permission (see SEC-007). | P0 |

## 7. API and integration specification

REST over HTTPS inside the deployment boundary, JSON bodies, OpenAPI 3.1 published from the code. All calls need a bearer token with scopes (SEC-002).

| ID | Method and path | Purpose | Pri |
| --- | --- | --- | --- |
| API-001 | `POST /api/v1/documents` | Upload a file (multipart) with optional `document_type_hint`, `schema_id`, `callback_url`, `Idempotency-Key`; returns `202` with `document_id`. | P0 |
| API-002 | `GET /api/v1/documents/{id}` | Status, metadata and, when ready, the output envelope. | P0 |
| API-003 | `GET /api/v1/documents/{id}/pages/{n}` | Page image (for the review UI), subject to permission. | P0 |
| API-004 | `POST /api/v1/documents/{id}/extract-sync` | Single-page synchronous extraction, timeout 30 s. | P0 |
| API-005 | `GET /api/v1/review-tasks` and `POST /api/v1/review-tasks/{id}/complete` | List and complete exception tasks with corrections. | P0 |
| API-006 | `GET/POST /api/v1/schemas`, `POST /api/v1/schemas/{id}/versions`, `POST /api/v1/schemas/{id}/approve` | Schema lifecycle. | P0 |
| API-007 | `POST /api/v1/schemas/{id}/test-runs` | Run the harness on a sample set. | P0 |
| API-008 | `POST /api/v1/webhooks`, `GET /api/v1/audit-events` | Subscriptions and audit export. | P0 / P1 |
| API-009 | `GET /api/v1/health`, `GET /api/v1/metrics` | Liveness, readiness and Prometheus metrics. | P0 |

### 7.1 Events

Webhooks are signed (HMAC-SHA256 with a per-subscription secret), retried with exponential back-off for 24 hours, and delivered at least once, so consumers de-duplicate by `event_id`.

| Event | Fired when |
| --- | --- |
| `document.completed` | Status becomes `auto_approved` or `reviewed` |
| `document.needs_review` | A review task is created |
| `document.failed` | A stage fails after retries |
| `schema.approved` | A schema version is approved |

### 7.2 Error model

| ID | Requirement | Pri |
| --- | --- | --- |
| API-010 | Errors SHALL use one shape: `{ "error": { "code", "message", "request_id", "details" } }`, with stable machine-readable codes (`UNSUPPORTED_FILE`, `SCHEMA_NOT_APPROVED`, `DOCUMENT_NOT_FOUND`, `RATE_LIMITED`, `VALIDATION_FAILED`). | P0 |
| API-011 | Error messages SHALL NOT contain extracted document content or PII. | P0 |
| API-012 | `429` responses SHALL carry `Retry-After`; clients exceeding the tenant rate limit SHALL be throttled, not dropped. | P0 |
| API-013 | Mutating calls SHALL accept `Idempotency-Key`; replays within 24 hours return the original result. | P0 |
| API-014 | Contract tests generated from the OpenAPI document SHALL run in CI and block release on any breaking change without a major version bump. | P0 |

### 7.3 Endpoints the screens need but API-001 to API-009 do not cover [Proposed]

Aligning each screen's data bindings (8.8) with the API list exposed these gaps. They are added here so no screen is built against an undefined endpoint (MOCK-006). Paths follow the style of API-001 to API-009; the prefix is `/api/v1` for every endpoint in this PRD (decided 2026-10-29, as in the other ICXBee services).

| ID | Method and path | Purpose | Screen | Pri |
| --- | --- | --- | --- | --- |
| API-015 | `GET /api/v1/documents` | List with filters `status`, `documentType`, `from`, `to`, `confidenceBelow`, `q`; sort and paging (10 per page); returns `stats` (total, processing, needs_review, auto_approved, failed). | SCR-02 | P0 |
| API-016 | `POST /api/v1/review-tasks/{id}/claim`, `POST /api/v1/review-tasks/{id}/release` | Claim or release an exception task; a second claim returns `409 TASK_CLAIMED` with the owner's display name. | SCR-04, SCR-03 | P0 |
| API-017 | `GET /api/v1/documents/{id}/export?format=json\|csv\|xml` | Export one approved document; CSV and XML are P1 (FR-OUT-003). Unmasked export needs the SEC-007 permission and is audited. | SCR-06 | P0 / P1 |
| API-018 | `GET /api/v1/schemas/{id}/versions`, `POST /api/v1/schemas/{id}/rollback` | Version history and rollback (SCH-006). | SCR-05 | P0 |
| API-019 | `GET/POST /api/v1/api-keys`, `DELETE /api/v1/api-keys/{id}` | List, create (secret shown once) and revoke keys (SEC-002). | SCR-07 | P0 |
| API-020 | `GET/PUT /api/v1/policies/providers`, `POST /api/v1/policies/providers/{id}/approve`, `GET /api/v1/providers` | Provider register and per-tenant policy with four-eyes approval (GR-021, GR-022). | SCR-09 | P0 |
| API-021 | `GET/PUT /api/v1/settings/retention`, `GET/PUT /api/v1/settings/roles`, `GET/PUT /api/v1/settings/masking` | Retention (SEC-010), role matrix (SEC-003), masking rules (SEC-007). | SCR-09 | P0 |
| API-022 | `POST /api/v1/admin/auto-approval/pause`, `POST /api/v1/admin/pipeline/pause`, `POST /api/v1/admin/resume` | Kill switch (GR-015); each call audited. | SCR-09 | P0 |
| API-023 | `GET /api/v1/dashboard/kpis` | KPI tiles, confidence histogram, error patterns, drift, model versions, escape count (M1 to M11). | SCR-10 | P1 |
| API-024 | `GET /api/v1/audit-events/{id}/run` | Run reproduction view: model, prompt, schema and provider versions, corrections (FR-AUD-001). | SCR-08 | P0 |

## 8. Screens and mock-screen generation

Mock screens are generated from this section before any UI code is written, so every screen is specified, reviewed with the personas and traceable to a requirement. A screen without a spec here SHALL NOT be built.

### 8.1 Mock-screen generation requirements

| ID | Requirement | Pri | Acceptance criteria |
| --- | --- | --- | --- |
| MOCK-001 | Each screen SHALL have a screen spec containing: purpose, persona, entry points, layout regions, components, data bindings (API field paths), states, actions and transitions, validation messages, accessibility notes and linked requirement IDs. | P0 | All ten screens in 8.2 have every listed element; a lint script fails the build on a missing element. |
| MOCK-002 | Mocks SHALL be generated for all ten screens at desktop (1440 px) and tablet (1024 px) widths, in light and dark themes. SCR-03 is desktop-first. **Decided 2026-10-29 (OQ-DOCX-02): MVP is desktop-first, 1440 px, light theme only, as in every other ICXBee flow; tablet and dark theme move to v1 (MOCK-012).** | P0 (MVP: desktop, light) | MVP: 10 screens × 1 width × 1 theme rendered and stored with the spec version. v1: 10 × 2 × 2. |
| MOCK-003 | Mocks SHALL render every state in the screen's state matrix (empty, loading, populated, partial failure, error, permission denied, and screen-specific states), not only the populated state. | P0 | State matrix coverage report shows 100% of listed states rendered. |
| MOCK-004 | Mock data SHALL be synthetic. No real customer document, name, ID number or health data SHALL be used in any mock, prompt or screenshot. | P0 | Data-source manifest for mocks lists only generated or fictional data; a PII scanner on the mock repository passes. |
| MOCK-005 | The system SHALL provide a synthetic document generator that produces scanned-looking samples (skew, noise, stamps, checkboxes, handwriting-style fonts) for all seven launch types, with matching ground-truth JSON, used for mocks, demos and tests. | P0 | Each launch type has ≥ 20 synthetic samples with ground truth; none derived from a real document. |
| MOCK-006 | Every interactive element in a mock SHALL be annotated with its requirement ID and data binding so that developers and test authors can trace it. | P0 | Annotation coverage ≥ 95% of interactive elements. |
| MOCK-007 | Mocks SHALL be linked into clickable flows for the primary journeys in 8.3. | P0 | Each journey can be walked from its entry screen to its end state. |
| MOCK-008 | Confidence and status SHALL be conveyed by text or icon as well as colour; contrast SHALL meet WCAG 2.2 AA; Indic scripts (Devanagari, Telugu, Tamil, Malayalam) SHALL render correctly in the field viewer. | P0 | Accessibility checklist passes; a rendered sample in each script has no tofu characters. |
| MOCK-009 | Mocks SHALL be reviewed by one representative per persona and signed off; the sign-off, version and comments SHALL be recorded. A change to a mock that alters behaviour SHALL first change this section. | P0 | Sign-off record exists for each screen before its build task starts. |
| MOCK-010 | Mock generation SHALL be repeatable from a prompt template (8.5) plus the screen spec, so a regenerated mock after a spec change differs only where the spec changed. | P1 | Diff of two generations from an unchanged spec is limited to non-semantic differences, reviewed by a person. |

### 8.2 Screen inventory

| Screen | Name | Persona | Purpose | Key components | Pri |
| --- | --- | --- | --- | --- | --- |
| SCR-01 | Upload and intake | Ops analyst | Submit files or batches and see them accepted | Drop zone, type hint selector, batch list, validation errors | P0 |
| SCR-02 | Document list and status | Ops analyst | Find and track documents | Filterable table (status, type, date, confidence), bulk actions, progress | P0 |
| SCR-03 | Review workbench | Reviewer | Verify flagged fields against the source | Page viewer with highlight, field panel, confidence chips, flags, actions | P0 |
| SCR-04 | Review queue | Reviewer | Pick the next exception | Queue table, SLA age, assignee, claim/release | P0 |
| SCR-05 | Schema studio | Business owner | Define, test and approve schemas | Field tree editor, YAML view, rules editor, test harness results, version history | P0 |
| SCR-06 | Results and export | Ops analyst | View and export approved output | Envelope viewer, JSON/CSV/XML export, evidence drill-down | P0 |
| SCR-07 | Developer console | Integrator | Manage API keys, webhooks, view logs | Key list, webhook subscriptions, delivery log, API reference link | P0 |
| SCR-08 | Audit log explorer | Compliance | Reconstruct who did what | Event table, filters, run reproduction view, export | P0 |
| SCR-09 | Security and policy settings | Admin, compliance | Roles, retention, egress policy, masking rules | Role matrix, retention rules, provider policy, key status | P0 |
| SCR-10 | Operations dashboard | Admin, product owner | Watch accuracy, STP, latency, drift, cost | KPI tiles, confidence histogram, error patterns, model versions | P1 |

### 8.3 Primary journeys to prototype

1. **Happy path.** SCR-01 upload → SCR-02 status moves to `auto_approved` → SCR-06 view and export.
2. **Exception path.** SCR-02 shows `needs_review` → SCR-04 claim task → SCR-03 correct two fields → complete → SCR-06.
3. **New document type.** SCR-05 define schema → run test harness → request approval → second user approves → SCR-01 upload a sample of the new type.
4. **Audit.** SCR-08 search a document → open run → see model, prompt and schema versions and every correction.

### 8.4 Screen spec: SCR-03 Review workbench (reference example)

Every other screen follows this level of detail.

- **Layout.** Left 55%: page viewer with zoom, pan and page thumbnails. Right 45%: field panel grouped by schema section, flagged fields first. Top bar: document ID, type, schema version, overall status, Complete and Reject buttons.
- **Field row.** Label, value (editable), confidence chip (text plus icon), validation state, null reason, evidence snippet. Selecting a row highlights its bounding box in the viewer; clicking a box selects its row.
- **Actions.** Accept, Edit, Mark as null, Change document type, Add comment, Next flagged field (keyboard `N`), Complete task (enabled only when no flagged field is unresolved).
- **Data bindings.** `fields[*].value`, `.confidence`, `.evidence.page`, `.evidence.bbox`, `.review_required`, `.validation`, `flags[*]`.
- **Linked requirements.** FR-REV-001 to 003, FR-CLS-004, FR-CNF-002, GR-009, SEC-007.

_Diagram: SCR-03 wireframe, desktop, populated state with fictional data. The drawing is in the live PRD: https://claude.ai/artifact/FhDRpYzjFVKERF4WiuuDsi_

The wireframe shows the populated state; the other states in the table change only the regions they name.

| State | What the mock shows |
| --- | --- |
| Loading | Skeleton for viewer and panel; Complete disabled |
| Populated | Flagged fields on top, resolved count "3 of 5 resolved" |
| Low-confidence handwriting | Field shows chip "Low confidence 62%" and the cropped image next to the value |
| Validation failed | Inline message with the rule (for example "PAN format invalid"), Complete blocked |
| Arithmetic mismatch | Banner at top naming the fields that disagree, with the computed sum |
| Unknown type | Banner "Document type not recognised" with type selector, no field panel |
| Task claimed by someone else | Read-only with the owner's name |
| PII masked | Values masked with a reveal action only for roles allowed by SEC-007; each reveal is logged |
| Error | Page image failed to load; retry button; task stays claimed |

### 8.5 Mock generation prompt template

Used with the screen spec as input to an AI design tool or a front-end generator.

```text
Generate a high-fidelity mock for <SCR-ID> <name> of an on-premise document extraction product.
Users: <persona>. Width: <1440|1024>. Theme: <light|dark>.
Use only the regions, components, states and actions in the attached screen spec.
Render state: <state name>.
Use synthetic data only (fictional names, fake ID numbers). Show confidence as text plus icon.
Annotate each interactive element with its requirement ID and data binding.
Do not add features, screens or copy that are not in the spec.
```

### 8.6 Mockup repository alignment

The mocks live in the `icxbee-mockups` repository (Stage 0 of the ICXBee SDLC pipeline: plain HTML and Tailwind from the CDN, no build step, one flow per folder, one HTML file per screen and state). This subsection binds the screens in 8.2 to that structure without changing them.

| Convention in `icxbee-mockups` | Applied to this product |
| --- | --- |
| Flow folder `flows/<flow>/` | `flows/document-extraction/` |
| Screen ID `<STEM>-SCR-<NNN>`, sequential per flow; stem unique per flow (`LOGIN`, `BOFEAT`, `BOMD`, …) | Stem `DOCX`. `SCR-01` → `DOCX-SCR-001` … `SCR-10` → `DOCX-SCR-010`. The short IDs stay valid aliases so existing references in this PRD do not break. |
| State variants are suffixes on the screen ID (`BOFEAT-SCR-001-loading`, `-empty`, `-drawer`, `-delete-confirm`, `-preview-errors`) | Same: `DOCX-SCR-003-low-confidence`, file `DOCX-SCR-003-low-confidence.html`. State lists in 8.7. |
| Generator `tools/gen_<flow>.py`; HTML is never hand-written so chrome stays single-sourced | `tools/gen_document_extraction.py`, reusing the shared helpers (data table, drawer panel, confirm dialog, stat strip, form fields, bulk-upload pick/preview) |
| Shared tokens: `shared/tokens.js` (front-office, Figma-sourced) or `shared/bo-tokens.js` (back-office) | Front-office tokens and app chrome, because the users are tenant staff, not ICXBee staff **(decided 2026-10-29, OQ-DOCX-01)** |
| `flows/<flow>/flow-map.html` with every screen, its transitions and a Real/Inferred tag | Same; tag is "Specified in PRD" or "Inferred state" |
| Stage 0a brief `requirements/<flow>-mockup-requirements.md` from `TEMPLATE-mockup-brief.md` | `requirements/document-extraction-mockup-requirements.md` (content in 8.9) |
| Stage 0c rules `requirements/<flow>-business-rules.md` from `TEMPLATE-business-rules.md`, rule IDs `BR-<FLOW>-nnn` | `requirements/document-extraction-business-rules.md`, `BR-DOCX-nnn`, written after the mocks exist |
| `MOCKUP-ASSUMPTIONS.md` logs every inferred state and source conflict | Every state marked "Inferred" in 8.7 is logged there, not resolved silently |
| `README.md` status table | One row for `document-extraction` |
| Ground truth: legacy app and Figma | None exists for this product: no legacy screen, no Figma file. State this explicitly in the brief; this PRD is the ground truth. |

| ID | Requirement | Pri | Acceptance criteria |
| --- | --- | --- | --- |
| MOCK-011 | Mocks SHALL be stored under `icxbee-mockups/flows/document-extraction/` with one HTML file per screen and state, named exactly by its `DOCX-SCR-nnn[-state]` ID, in plain HTML and Tailwind with no build step. | P0 | File list equals the state matrix in 8.7; a check fails on a missing or extra file. |
| MOCK-012 | MVP mocks SHALL be desktop 1440 px, light theme only, as in every other ICXBee flow (decided 2026-10-29). Tablet 1024 px and dark theme (MOCK-002) are deferred to v1 and, when taken up, SHALL be produced by generator options, not by separate hand-made files. | P0 | One generator run emits the canonical desktop light files; no tablet or dark files exist at MVP. |
| MOCK-013 | Mocks SHALL be generated by `tools/gen_document_extraction.py`; HTML SHALL NOT be written by hand. | P0 | Re-running the generator leaves the tree unchanged (no diff). |
| MOCK-014 | `flow-map.html` SHALL show every screen and state, the journeys of 8.3 as transitions, and a "Specified in PRD" or "Inferred state" tag per node. | P0 | Each journey in 8.3 can be walked by links from entry to end state. |
| MOCK-015 | The Stage 0a brief, the Stage 0c business rules, the `MOCKUP-ASSUMPTIONS.md` entries and the README status row SHALL exist for the flow before screen build tasks start. | P0 | The four artefacts exist and cite DOCX screen IDs; MOCK-009 sign-off references them. |
| MOCK-016 | Screens SHALL be built with the shared components and tokens of the chosen chrome; a new token or component needs a reason recorded in `MOCKUP-ASSUMPTIONS.md`. | P0 | No new token or component without an assumptions entry. |
| MOCK-017 | The state matrix in 8.7 is the single list of states the generator renders; a state added to a mock SHALL first be added to 8.7 (MOCK-009). | P0 | Generator input is derived from, or checked against, 8.7. |

### 8.7 Screen and state inventory

Each screen renders the states MOCK-003 requires (default or populated, loading, empty, partial failure, error, permission denied) plus the screen-specific states below. Suffix states are **Specified** where 8.4 or the requirement tables name them and **Inferred** where this PRD proposes them from a requirement.

| Screen ID | Alias | Name | State variants (suffix on the ID) |
| --- | --- | --- | --- |
| `DOCX-SCR-001` | SCR-01 | Upload and intake | default (empty drop zone, Specified), `-uploading` (loading), `-batch` (populated batch list), `-partial-failure` (one file `UNSUPPORTED_FILE`, one `rejected_malware`, Specified by FR-ING-002/003), `-error` (`RATE_LIMITED` or API down), `-denied` |
| `DOCX-SCR-002` | SCR-02 | Document list and status | default (populated), `-loading`, `-empty`, `-filtered`, `-bulk-select`, `-error`, `-denied` |
| `DOCX-SCR-003` | SCR-03 | Review workbench | default (populated, Specified), `-loading`, `-low-confidence`, `-validation-failed`, `-arithmetic-mismatch`, `-unknown-type`, `-claimed` (task claimed by someone else), `-pii-masked`, `-error` (all Specified by 8.4), `-denied` |
| `DOCX-SCR-004` | SCR-04 | Review queue | default, `-loading`, `-empty` (no exceptions), `-claim-conflict`, `-error`, `-denied` |
| `DOCX-SCR-005` | SCR-05 | Schema studio | default (field tree), `-yaml`, `-rules`, `-test-results`, `-versions`, `-approval` (second user approves), `-approval-blocked` (same user, `SCH-005`), `-loading`, `-error`, `-denied` |
| `DOCX-SCR-006` | SCR-06 | Results and export | default (envelope viewer), `-evidence` (drill-down), `-export` (format drawer), `-export-done`, `-masked`, `-not-ready`, `-loading`, `-error`, `-denied` |
| `DOCX-SCR-007` | SCR-07 | Developer console | default, `-key-created` (secret shown once), `-webhook-add`, `-delivery-failed`, `-empty` (no keys), `-loading`, `-error`, `-denied` |
| `DOCX-SCR-008` | SCR-08 | Audit log explorer | default, `-run-reproduction`, `-chain-broken` (SEC-008 alert), `-export`, `-empty`, `-loading`, `-error`, `-denied` |
| `DOCX-SCR-009` | SCR-09 | Security and policy settings | default (tabs), `-role-matrix`, `-retention`, `-provider-policy`, `-policy-approval`, `-provider-blocked`, `-key-status`, `-kill-switch-confirm`, `-loading`, `-error`, `-denied` |
| `DOCX-SCR-010` | SCR-10 | Operations dashboard | default, `-drift-alert`, `-escape-report`, `-empty` (no data yet), `-loading`, `-error`, `-denied` |

### 8.8 Screen specifications (ICXBee format)

Same structure as the ICXBee PRDs: one table per screen with the element and its spec. SCR-03 is specified in 8.4 and is not repeated. Rows marked **[Proposed]** are this PRD's recommendation; the rest restate 8.2 and the cited requirements.

**`DOCX-SCR-001` Upload and intake** (Ops analyst; FR-ING-001 to 003, 006; API-001)

| Element | Spec |
| --- | --- |
| Header | Title "Upload documents"; description "Submit files or batches for extraction."; action "View documents" to `DOCX-SCR-002` |
| Drop zone | Accepts PDF, JPG, PNG, TIFF; shows size and page limits; click or drop; the accepted-format list comes from FR-ING-001 |
| Type hint | Optional selector bound to `document_type_hint` (approved schemas only) |
| Batch list | One row per file: name, size, status (`queued`, `rejected`), error code and plain message; no content preview |
| Actions | Upload (disabled with no file), Remove file, Clear batch |
| Bindings | `POST /api/v1/documents` with `Idempotency-Key`; `202` returns `document_id` |

**`DOCX-SCR-002` Document list and status** (Ops analyst; API-015)

| Element | Spec |
| --- | --- |
| Header | Title "Documents"; actions "Upload documents" |
| Stats | Total, Processing, Needs review, Auto-approved, Failed (counts for the whole tenant, ignoring filters) **[Proposed]** |
| Toolbar | Status, Document type, Date range, Confidence below, search by document ID |
| Table | Document ID, Type, Status (text plus icon), Confidence (min field), Pages, Uploaded, Last updated; row opens `DOCX-SCR-006`, or `DOCX-SCR-003` when `needs_review` |
| Bulk actions | Export selected, Reprocess selected (audited) **[Proposed]** |
| Footer | Pagination, "Page x of y · n items" |

**`DOCX-SCR-004` Review queue** (Reviewer; API-005, API-016)

| Element | Spec |
| --- | --- |
| Header | Title "Review queue"; description "Documents that need a person." |
| Toolbar | Document type, Assignee (Mine, Unassigned, All), Flag type, SLA age |
| Table | Task ID, Document, Type, Flagged fields (count), Flag reasons, SLA age, Assignee, Claim or Release |
| Actions | Claim, Release, "Next task" (oldest first) |
| Rules | Claiming opens `DOCX-SCR-003`; a task claimed by another user shows the owner and cannot be claimed (`-claim-conflict`) |

**`DOCX-SCR-005` Schema studio** (Business owner; SCH-001 to 010; API-006, API-007, API-018)

| Element | Spec |
| --- | --- |
| Header | Title "Schema studio"; schema name, version, status pill (`draft`, `testing`, `approved`, `retired`); actions Save draft, Run test, Request approval |
| Field tree | Nested fields and repeating groups with type, required, format, threshold, PII class (SCH-001, 002) |
| YAML view | Round-trips with the tree without loss (SCH-003) |
| Rules editor | Rules with `on_fail` outcome; changing a rule creates a new version (FR-VAL-006) |
| Test results | Per-field precision, recall, F1 and calibration from the harness (SCH-007) |
| Versions | Version, author, time, diff, Rollback (SCH-006) |
| Approval | A second user approves; the author cannot (SCH-005); harness result attached |

**`DOCX-SCR-006` Results and export** (Ops analyst; FR-OUT-001 to 003; API-002, API-017)

| Element | Spec |
| --- | --- |
| Header | Document ID, type, schema version, status; actions Export, Open evidence |
| Envelope viewer | Fields with value, confidence, validation, null reason; PII masked by default (SEC-007) |
| Evidence drill-down | Page image with the field's `bbox` highlighted |
| Export drawer | Format (JSON; CSV and XML at P1), masked or unmasked (needs permission and a reason); footer Cancel and Export; done state with "Download again" |

**`DOCX-SCR-007` Developer console** (Integrator; API-008, API-019)

| Element | Spec |
| --- | --- |
| API keys | Name, scopes, created, last used, expiry, status; Create key shows the secret once; Revoke asks for confirmation |
| Webhooks | URL, events (`document.completed`, `document.needs_review`, `document.failed`, `schema.approved`), status; Add, Test, Disable |
| Delivery log | Event ID, event, attempt count, HTTP status, next retry; no payload content |
| Link | API reference (OpenAPI) |

**`DOCX-SCR-008` Audit log explorer** (Compliance; API-008, API-024)

| Element | Spec |
| --- | --- |
| Toolbar | Actor, Event type, Document ID, Date range |
| Table | Time, Actor, Event, Object, Outcome; hash-chain status per page of results |
| Run reproduction | Model, prompt, schema, provider versions and corrections for one run (FR-AUD-001) |
| Alert | A broken hash chain shows a banner naming the first bad event (SEC-008) |
| Actions | Export (audited) |

**`DOCX-SCR-009` Security and policy settings** (Admin, compliance; API-020 to API-022)

| Element | Spec |
| --- | --- |
| Tabs | Roles, Retention, Masking, Provider policy, Keys and secrets, Kill switch |
| Role matrix | Roles of SEC-003 against permissions; separation-of-duties note |
| Retention | Per document type; shows the deletion-certificate event (SEC-010) |
| Provider policy | Register, per-tenant and per-type policy, region, conditions, monthly external usage, "Pending approval" state (GR-021 to GR-026) |
| Keys and secrets | KMS key status, rotation date, scanner signature age (SEC-009) |
| Kill switch | Pause auto-approval or the pipeline; confirm dialog; state change audited (GR-015) |

**`DOCX-SCR-010` Operations dashboard** (Admin, product owner; API-023; P1)

| Element | Spec |
| --- | --- |
| KPI tiles | M1 field F1, M5 STP (reported only), M7 latency, M9 egress count, M11 escapes |
| Charts | Confidence histogram, error patterns, drift against baseline (NFR-011), cost per page (NFR-016) |
| Panels | Model and prompt versions in use (ID.AM), patch age (SEC-023), asset inventory |
| Alerts | Escape or drift alert state with link to the audit explorer |

### 8.9 Stage 0a mockup brief for this flow

Content for `icxbee-mockups/requirements/document-extraction-mockup-requirements.md`, in the structure of `TEMPLATE-mockup-brief.md`.

```text
# Document Extraction — Mockup Requirements

Objective: Ten screens for tenant staff to upload documents, review exceptions against the
source image, author and approve extraction schemas, export results and administer policy.
Mocked before any build, per MOCK-001 to 017 of the PRD.

Ground truth references:
- Legacy: none — new flow.
- Figma: not yet connected for this flow.
- This PRD (sections 8.2, 8.4, 8.7, 8.8) is the source of truth; every inferred state is logged
  in MOCKUP-ASSUMPTIONS.md.

Screens and states: table in PRD 8.7 (DOCX-SCR-001 … 010 and their suffix states).

Design system: front-office shared/tokens.js and app chrome (decided, OQ-DOCX-01); reuse shared data
table, drawer, confirm dialog, stat strip; no new tokens without an assumptions entry.

Data / content reference: backend contract to be generated from specs/<NNN>-*/contracts/;
until then PRD sections 6.3 and 7 (API-001 … 024). Synthetic data only (MOCK-004).

Interaction / flow notes: journeys in PRD 8.3; entry points and transitions go into
flows/document-extraction/flow-map.html.

Scope: desktop 1440 px, light theme only for MVP (OQ-DOCX-02, decided).

Known conflicts / open questions: OQ-DOCX-04 error envelope, OQ-DOCX-06 per-page pricing.

Expected output: one HTML per screen/state under flows/document-extraction/ via
tools/gen_document_extraction.py; update flow-map.html, MOCKUP-ASSUMPTIONS.md, README status table.
```

## 9. Guardrails

In lending and clinical settings an unverified guess is worse than a missing value. Guardrails therefore favour null and human review over completeness. They apply at four points: before the model (input), around the model (generation), after the model (output) and around the system (operations).

| ID | Point | Guardrail | Mechanism | Acceptance test | Pri |
| --- | --- | --- | --- | --- | --- |
| GR-001 | Input | Only supported, clean files enter the pipeline. | Magic-byte check, size and page limits, malware scan, decompression-bomb limit. | Fuzz corpus of malformed files: 0 crashes, 100% rejected with a code. | P0 |
| GR-002 | Output | No value without evidence. | A non-null value must carry page, bbox and a snippet that contains it after normalisation; otherwise it is set to null and flagged. | Seeded test: model returns a value with fake evidence; result is null and `EVIDENCE_MISSING`. | P0 |
| GR-003 | Generation | Null by default. | System prompt and schema instruct: "If not legibly visible, return null. Never guess." Absent and illegible are distinct reasons. | Blank-field benchmark: false-fill rate ≤ 0.5% of blank fields. | P0 |
| GR-004 | Input | No forced classification. | Below-threshold or no matching schema yields `UNKNOWN` and review; extraction does not run. | Out-of-domain set: ≥ 98% routed to `UNKNOWN`. | P0 |
| GR-005 | Generation | Output must conform to the schema. | Grammar-guided decoding against the compiled JSON Schema; no free text. | 1,000 runs, 0 exceptions (M3). | P0 |
| GR-006 | Input | Document content is data, never instructions. | Document text is passed only inside delimited data fields; the prompt states that instructions inside documents are ignored; output is limited to schema fields so an injected instruction has no channel. | Injection test set ("ignore previous instructions…", hidden white text, instructions in stamps): 0 schema deviations, 0 behaviour changes. | P0 |
| GR-007 | Generation | The model has no tools and no memory across documents. | Local inference runs in network-isolated containers; external providers are reached only through the provider adapter (ARC-007); function calling disabled; context cleared per document. | Egress test from the local inference container: all outbound attempts blocked and alerted; adapter test: only allowed provider endpoints are reachable. | P0 |
| GR-008 | Output | Validation gate before approval. | Format, checksum, arithmetic, chronology and cross-field rules (FR-VAL) run on every document; any failure blocks auto-approval. | Rule test suite 100% pass; seeded violations all blocked. | P0 |
| GR-009 | Output | Low confidence goes to a person. | Per-field thresholds; handwriting-derived fields default to 0.75; reviewer must resolve every flagged field. | Handwriting set: 0 auto-approved fields below threshold. | P0 |
| GR-010 | System | The engine never decides. | No endpoint or connector triggers underwriting, credit, claim or clinical action; output is data for downstream systems that own the decision. | Design review plus connector test: connectors write only extracted data to staging objects. | P0 |
| GR-011 | System | No silent change in behaviour. | Any change to a model, prompt, schema or rule version requires a full benchmark regression; release blocked if M1, M3 or M6 degrade beyond tolerance (F1 − 1 pt, calibration + 1 pt). | CI gate demonstrated by a deliberately degraded model being blocked. | P0 |
| GR-012 | System | Learning needs approval. | Reviewer corrections go to a candidate list; adding to the benchmark or a fine-tuning set needs a second person's approval and a PII-handling check. | Attempt to auto-ingest a correction fails. | P1 |
| GR-013 | System | Only approved providers run. | A signed provider policy per tenant and document type lists allowed endpoints; default is local only; any endpoint not in the policy fails closed (rules in 9.2). | Configure an external URL with no policy: call refused and the run falls back to the local model. | P0 |
| GR-014 | System | Document content reaches only providers the policy approves; no PII in logs or telemetry. | PII classes from the schema drive masking; log scrubbers; telemetry carries counts and codes only. | Log scan after the full test suite finds 0 PII values. | P0 |
| GR-015 | System | Kill switch. | Admin can pause auto-approval (all documents go to review) or the whole pipeline in under 1 minute, without a deploy. | Toggle test with timing; state change audited. | P0 |
| GR-016 | Output | Reproducibility. | Deterministic decoding (temperature 0, fixed seed), pinned model and prompt versions, stored with each run. | Re-run of 100 stored runs yields identical outputs. | P0 |
| GR-017 | Output | Honest uncertainty. | Reported confidence is calibrated; the UI shows confidence and null reasons plainly and never fills a gap with an estimate. | Calibration gap ≤ 3 points (M6). | P0 |

### 9.1 Failure handling

| ID | Requirement | Pri |
| --- | --- | --- |
| GR-018 | IF a stage times out or fails, THEN the system SHALL retry up to 3 times with back-off, then mark the document `failed` with a reason code and create a review task; no partial output SHALL be marked approved. | P0 |
| GR-019 | IF the model returns output that fails schema validation despite decoding constraints, THEN the system SHALL discard it, log a defect event and route the document to review. | P0 |
| GR-020 | IF GPU capacity is exhausted, THEN the system SHALL queue and report an estimated wait, not degrade to a smaller unvalidated model. | P1 |

### 9.2 External model provider rules

Cloud APIs are included as optional providers (D2). They are allowed only under these rules, which apply in every deployment profile; the on-premise profile ships with all of them disabled.

| ID | Requirement | Pri | Acceptance criteria |
| --- | --- | --- | --- |
| GR-021 | A provider SHALL be on an approved provider register recording legal entity, processing regions, data-processing agreement, no-training and retention terms, sub-processors and next review date; an unregistered provider SHALL NOT be configurable. | P0 | Configuring an unregistered provider is refused with `PROVIDER_NOT_REGISTERED`. |
| GR-022 | External use SHALL require a signed policy per tenant and document type, created by an Admin and approved by a second person; the default is off in every profile. | P0 | A tenant with no policy never makes an external call, verified by adapter logs over the full test suite. |
| GR-023 | The policy SHALL record the processing region and contract conditions (data-processing agreement, no training on customer data, shortest retention the provider offers); calls to a region the tenant's residency rule does not allow SHALL be blocked. | P0 | A call to a disallowed region is blocked and logged `PROVIDER_REGION_BLOCKED`. |
| GR-024 | The adapter SHALL use TLS 1.2 or higher, per-tenant credentials from the secrets manager, a timeout and a size limit; send only the page images or text the stage needs; disable function calling; and log provider, model, byte count, payload hash and outcome, never content. | P0 | Log scan finds no document content; payload hash matches the stored input. |
| GR-025 | Healthcare document types (DOC-03, DOC-07, and any schema holding PHI) SHALL be treated as subject to both HIPAA and DPDP; an external provider SHALL be used for them only if the policy records a signed business associate agreement or equivalent, India-region processing and no-training terms. | P0 | With any one of the three missing, the run uses the local model and sets `PROVIDER_BLOCKED_BY_POLICY`. |
| GR-026 | Each run record and output envelope SHALL state which provider processed each stage (`local` or the provider name) and the policy version; SCR-09 SHALL show the active policy and monthly external usage per tenant. | P0 | Envelope `run` section contains `provider` per stage; SCR-09 mock and build show policy and usage. |
| GR-027 | The system SHALL NOT auto-approve a document containing a wrong key-field value (escape). Control: key fields need evidence, a passing validation and confidence at or above threshold; a sample of auto-approved documents (rate set per tenant) is sent to review; a confirmed escape pauses auto-approval for that document type (GR-015) and opens a defect; escapes are reported in SCR-10. | P0 | Locked benchmark: 0 escapes per document type. A seeded escape in the sampled set pauses auto-approval for that type within 1 minute. |

Zero escapes is a hard gate on the benchmark. In production it cannot be proven, so the sampling and the automatic pause are the control, and the escape count is a headline operational metric.

## 10. Security, privacy and compliance

The controlling constraint is that PII and PHI move only where policy allows: nowhere outside the deployment boundary by default, and to named external providers only under the rules in 9.2. Security requirements below are testable controls; Section 11 maps them to NIST CSF 2.0.

### 10.1 Security requirements

| ID | Requirement | Pri | Acceptance criteria |
| --- | --- | --- | --- |
| SEC-001 | Users SHALL authenticate through the product's internal identity service, built to the ICXBee pattern (ADR 0002), and through SSO with the customer's identity provider (OIDC; SAML where required) in every deployment profile; administrators and schema approvers SHALL use MFA. | P0 | Local passwords disabled by default; login without MFA as admin is refused. |
| SEC-002 | API clients SHALL use OAuth 2.0 client credentials or scoped API keys built as in ICXBee (CSPRNG-generated, only a SHA-256 hash stored, per-key rate limit, revocation list); tokens are self-issued RS256 JWTs that expire within 1 hour, with rotating one-time-use refresh tokens for users (ADR 0002); keys rotate at most every 90 days. | P0 | Expired token returns `401`; scope test matrix passes. |
| SEC-003 | Access SHALL be role-based and least-privilege with roles Analyst, Reviewer, Schema author, Schema approver, Integrator, Auditor and Admin; no user SHALL both author and approve the same schema version. | P0 | Permission matrix test: every role × endpoint outcome matches the matrix. |
| SEC-004 | All traffic SHALL use TLS 1.2 or higher, and service-to-service traffic SHALL use mutual TLS. | P0 | Scan finds no plaintext listener; weak ciphers rejected. |
| SEC-005 | Stored documents, extracted data, adapters and backups SHALL be encrypted at rest with AES-256, using keys held in a KMS or HSM (the customer's in private deployments, a per-tenant key in SaaS). | P0 | Storage inspection shows only ciphertext; revoking a tenant key makes its data unreadable. |
| SEC-006 | Data, queues, caches and schemas SHALL be isolated per tenant or business unit; cross-tenant access SHALL be impossible through the API. | P0 | Cross-tenant ID guessing test returns `404` for 100% of attempts. |
| SEC-007 | PII fields (schema `pii_class`) SHALL be masked by default in UI, API and exports; unmasking needs a role permission and SHALL be logged with a reason. | P0 | Reviewer without permission sees masked values; each reveal creates an audit event. |
| SEC-008 | Audit logs SHALL be append-only and tamper-evident (hash-chained), covering logins, uploads, views of unmasked data, corrections, schema changes, approvals, config changes and exports. | P0 | Altering a stored event breaks chain verification, which raises an alert. |
| SEC-009 | Every uploaded file SHALL be scanned for malware before any processing, and the scanner signatures SHALL be updated without internet access (offline update bundle). | P0 | EICAR test blocked; signature age shown in SCR-09. |
| SEC-010 | Retention SHALL be configurable per tenant and document type; originals and derived data SHALL be deleted by crypto-shred or secure delete on expiry or on request, with a deletion certificate event. | P0 | Delete request leaves no recoverable file, text, embedding or log value; verified by storage scan. |
| SEC-011 | The deployment SHALL deny all outbound network traffic by default and allow only named destinations: internal services and the external provider endpoints the policy approves. | P0 | Network policy test: connections from any service to a destination not on the allow-list fail. |
| SEC-012 | Logs, metrics, traces and error messages SHALL NOT contain document content or PII. | P0 | Automated scan of all telemetry after the test suite finds none. |
| SEC-013 | Secrets SHALL be held in a secrets manager, never in images, code or config files, and SHALL be rotatable without downtime. | P0 | Secret scan on repo and images finds none; rotation drill passes. |
| SEC-014 | Model weights, containers and dependencies SHALL be pinned by hash, signature-verified at deploy, and listed in an SBOM; model licences SHALL be reviewed for commercial and on-premise use. | P0 | Deploy with a modified weight file is refused; SBOM generated per release. |
| SEC-015 | The product SHALL follow a secure SDLC: threat model per release, SAST, dependency and container scanning in CI, and an independent penetration test before general availability. | P0 | No open critical or high findings at release; pen-test report on file. |
| SEC-016 | Backups SHALL run on a schedule with tested restore; RPO ≤ 15 minutes for metadata and RTO ≤ 4 hours. | P0 | Quarterly restore drill meets RPO and RTO. |
| SEC-017 | The API SHALL enforce rate limits and request size limits per client. | P0 | Load test beyond limit returns `429`, service stays within latency SLO for other tenants. |
| SEC-018 | Fine-tuning data and adapters SHALL stay in the customer's boundary, be access-controlled and lineage-tracked (source, consent, approver), and be deletable. | P1 | Lineage report lists every record in a training set; deleting a source removes it from future runs. |
| SEC-019 | Data residency SHALL be configurable so that storage and processing stay in a named location; the SaaS profile runs in AWS Mumbai (ap-south-1), as ICXBee does. | P0 | Deployment manifest pins location; no component can be placed outside it. |
| SEC-020 | UI sessions SHALL time out after inactivity (default 15 minutes) and the UI SHALL set standard protections (CSRF, CSP, secure cookies). | P0 | Browser security-header scan passes. |
| SEC-021 | The system SHALL raise alerts for repeated failed logins, unusual export volume, unmasking spikes, egress attempts, audit-chain failures and model-file changes. | P0 | Each condition triggers an alert in a simulation. |
| SEC-022 | An incident response plan and runbooks (data exposure, model compromise, ransomware, bad model release) SHALL exist and be exercised at least annually. | P0 | Tabletop exercise report on file. |
| SEC-023 | Critical vulnerabilities in components SHALL be patched or mitigated within 7 days, high within 30 days. | P1 | Patch-age report in SCR-10. |

### 10.2 Privacy and regulatory alignment

The product provides controls; compliance is a shared responsibility with the customer. Obligations apply by deployment.

| Regime | What the product must support | Related requirements |
| --- | --- | --- |
| India DPDP Act 2023 | Purpose limitation, retention limits, deletion on request, breach logging, data processed in India unless the transfer is permitted | SEC-010, SEC-019, SEC-008 |
| HIPAA (where PHI of US persons is processed) | Access control, audit controls, integrity, transmission security, minimum necessary, business associate agreement before PHI reaches any hosted service or cloud provider | SEC-003, SEC-004, SEC-007, SEC-008 |
| GDPR (where applicable) | Data minimisation, erasure, records of processing, automated-processing transparency | SEC-010, FR-AUD-001, GR-010 |
| RBI and IRDAI expectations for regulated entities | Data localisation, audit trails, vendor risk evidence | SEC-011, SEC-019, SEC-014 |
| ISO/IEC 27001 and SOC 2 | Control evidence produced by the product for the customer's audit | Section 11 |

Legal review of the exact obligations per customer is a dependency (Section 15); this table is a requirement-mapping aid, not legal advice. Healthcare document types are treated as subject to both HIPAA and DPDP (GR-025); where the two differ, the stricter control applies.

## 11. NIST CSF 2.0 alignment

The product is designed so a customer can show NIST Cybersecurity Framework 2.0 coverage across all six Functions. The table maps each CSF 2.0 Category to the product requirements that implement it and to the evidence the product produces. Controls that depend on the customer's own organisation (for example governance policy) are marked **Customer** and the product supplies the evidence hooks.

| Function | Category | Product requirements | Owner | Evidence produced |
| --- | --- | --- | --- | --- |
| **Govern (GV)** | GV.OC Organizational Context | Section 2.3 non-goals; GR-010 (engine never decides); Section 10.2 regime map | Product | PRD baseline, regulatory mapping per customer |
|  | GV.RM Risk Management Strategy | Section 15 risk register; GR-011 change gate; thresholds in schemas | Product, Customer | Risk register, threshold configuration history |
|  | GV.RR Roles, Responsibilities, Authorities | SEC-003 roles and separation of duties; SCH-005 four-eyes approval | Product, Customer | Role matrix export from SCR-09 |
|  | GV.PO Policy | SEC-010 retention; SEC-011 egress; GR-013 provider policy as enforced settings | Customer | Policy settings with change history |
|  | GV.OV Oversight | SCR-10 dashboard; metrics M1 to M10; SEC-008 audit | Product, Customer | Monthly accuracy, STP and incident report |
|  | GV.SC Supply Chain Risk Management | SEC-014 pinned and signed models, SBOM, licence review; SEC-015 dependency scanning; GR-021 to GR-025 external provider due diligence | Product | SBOM, signature verification log, model licence register |
| **Identify (ID)** | ID.AM Asset Management | Inventory of models, schemas, prompts, services and data stores; SEC-014 SBOM | Product | Asset inventory view in SCR-10 |
|  | ID.RA Risk Assessment | SEC-015 threat model per release; GR-006 injection tests; Section 15 | Product | Threat model, pen-test report, vulnerability reports |
|  | ID.IM Improvement | FR-REV-004 and GR-012 controlled feedback loop; SEC-022 exercise findings; GR-011 regression results | Product | Lessons-learned log, benchmark growth record |
| **Protect (PR)** | PR.AA Identity Management, Authentication, Access Control | SEC-001, SEC-002, SEC-003, SEC-006, SEC-007, SEC-020 | Product | Access reviews, permission test results |
|  | PR.AT Awareness and Training | Reviewer and admin onboarding guide; in-product masked-data notices | Customer, Product | Training completion (customer), guide versions |
|  | PR.DS Data Security | SEC-004, SEC-005, SEC-007, SEC-010, SEC-012, SEC-018, SEC-019, GR-014 | Product | Encryption configuration, deletion certificates, telemetry scan |
|  | PR.PS Platform Security | SEC-009, SEC-013, SEC-014, SEC-015, SEC-023; GR-001, GR-007 hardened, isolated inference | Product | Hardening baseline, scan results, patch-age report |
|  | PR.IR Technology Infrastructure Resilience | SEC-011 network policy; SEC-016 backup and restore; NFR-004 availability; NFR-005 horizontal scaling; GR-020 | Product, Customer | Restore drill report, capacity test |
| **Detect (DE)** | DE.CM Continuous Monitoring | SEC-021 alerts; SEC-008 chain verification; NFR-010 observability; GR-007 egress alerts | Product | Alert catalogue, monitoring dashboards |
|  | DE.AE Adverse Event Analysis | Audit explorer SCR-08; drift and anomaly detection on confidence and error patterns (NFR-011) | Product | Correlated event timeline, drift report |
| **Respond (RS)** | RS.MA Incident Management | SEC-022 runbooks; GR-015 kill switch; GR-018 failure handling | Product, Customer | Runbooks, kill-switch audit events |
|  | RS.AN Incident Analysis | FR-AUD-001 reproducible runs; SEC-008 tamper-evident logs; SCR-08 run reproduction | Product | Reproduction package per incident |
|  | RS.CO Incident Response Reporting and Communication | Notification templates and contact configuration; breach-relevant audit export | Customer, Product | Exportable incident timeline |
|  | RS.MI Incident Mitigation | GR-015 pause auto-approval; SEC-005 key revocation; SEC-013 secret rotation; model rollback (GR-011) | Product | Mitigation actions in audit log |
| **Recover (RC)** | RC.RP Incident Recovery Plan Execution | SEC-016 backup and restore; versioned schemas and models with rollback (SCH-006) | Product, Customer | Recovery drill report |
|  | RC.CO Incident Recovery Communication | Status banner and recovery notices in the UI; post-incident report template | Customer, Product | Post-incident review record |

### 11.1 CSF-specific requirements

| ID | Requirement | Pri | Acceptance criteria |
| --- | --- | --- | --- |
| SEC-024 | The product SHALL ship a CSF 2.0 profile (current and target) in machine-readable form listing, per Category, the implementing requirements and test evidence. | P0 | Profile file validates; every Category in the table above has at least one requirement and one test. |
| SEC-025 | The product SHALL produce a compliance evidence bundle (configuration, access report, audit extract, vulnerability and patch status, restore drill result) on demand from SCR-09. | P1 | Bundle generated in under 10 minutes and contains no document content. |
| SEC-026 | The CSF profile SHALL be reviewed each release; a requirement change that affects a Category SHALL update the profile in the same change. | P0 | CI check fails when a requirement tagged with a CSF Category changes without a profile change. |

The CSF covers cybersecurity outcomes. AI-specific risks (hallucination, drift, bias in handwriting recognition) are handled by Section 9 and are consistent with the NIST AI Risk Management Framework, which the customer may use alongside CSF.

## 12. Non-functional requirements

Performance targets are measured with local models on hardware profile P2 in 12.1. Slice 0 confirms them with a benchmark spike on that profile.

| ID | Area | Requirement | Target | Pri |
| --- | --- | --- | --- | --- |
| NFR-001 | Latency | Upload to validated JSON, per page, with local models on profile P2; external provider latency is reported, not gated. | p95 ≤ 3.0 s | P0 |
| NFR-002 | Latency | Same on CPU-only fallback with the same models. | p95 ≤ 10 s | P1 |
| NFR-003 | Throughput | Sustained throughput on the reference server. | ≥ 15 pages/min on profile P2 | P0 |
| NFR-004 | Availability | Service availability for a single-site deployment; high-availability option documented. | 99.5% monthly (HA: 99.9%) | P0 |
| NFR-005 | Scalability | Add worker nodes to scale throughput linearly up to 4 nodes; queue absorbs month-end bursts. | ≥ 85% scaling efficiency | P1 |
| NFR-006 | Reliability | Retries and idempotency on all mutating calls; at-least-once processing with no lost documents on node failure. | 0 lost documents in failure-injection test | P0 |
| NFR-007 | Model footprint | Parser stage VRAM; extractor stage VRAM with quantisation. | Parser < 2 GB; extractor within 8 to 16 GB | P0 |
| NFR-008 | Model flexibility | Parser and extractor sit behind interfaces; swapping a model needs configuration and a regression run, not code changes in other stages. | Swap demonstrated for one model per stage | P0 |
| NFR-009 | Deployment | One container build deploys to SaaS, private cloud or VPC, and on-premise with Helm or Compose; offline installation bundle. | Install with no internet in under 1 day | P0 |
| NFR-010 | Observability | Dashboards for accuracy, STP, confidence distribution, error patterns, latency, GPU use and cost per page; no PII. | All listed panels live | P1 |
| NFR-011 | Drift | Weekly comparison of confidence and error distributions against baseline; alert on shift beyond threshold. | Alert within 24 hours of a seeded shift | P1 |
| NFR-012 | Usability | Reviewer completes a typical flagged document (5 flagged fields) quickly. | Median ≤ 90 s (to validate) | P1 |
| NFR-013 | Accessibility | UI meets WCAG 2.2 AA. | Automated and manual audit pass | P0 |
| NFR-014 | Localisation | UI text in English at MVP; Indic script rendering in data views. | Rendering check per script | P0 |
| NFR-015 | Fine-tuning | PEFT/QLoRA fine-tuning of the extractor runs locally on the reference GPU; adapters are versioned and revertible. | One adapter trained and rolled back in test | P1 |
| NFR-016 | Cost | Cost per page (compute plus licences) reported per tenant; per-page pricing needs a metering record per processed page. | Metering matches processed page count within 0.1% | P0 |
| NFR-017 | Maintainability | Minimum 80% unit-test coverage on validation, normalisation and routing code; every pipeline stage has a contract test. | Coverage gate in CI | P0 |

### 12.1 Recommended hardware

The approach document sizes the parser at under 2 GB of GPU memory and the quantised 7B extractor at 8 to 16 GB, so one 24 GB GPU holds both. Start pilots on P1, quote P2 for production and P3 when high availability is needed. These are starting recommendations; nothing here has been measured yet, and Slice 0 replaces them with measured figures.

| Profile | Use | GPU | CPU and memory | Storage and network | Notes |
| --- | --- | --- | --- | --- | --- |
| P1 Development and pilot | Developer machines, demos, benchmark runs | 1 × 24 GB GPU; RTX 3090 or 4090 class is acceptable | 16 cores, 64 GB RAM | 1 TB NVMe | GeForce driver terms restrict data-centre use, so keep P1 to development and pilots |
| P2 Production, single node (on-premise or VPC) | Reference profile for NFR-001 and NFR-003 | 1 × data-centre GPU with 24 GB or more, for example L4 or A10 (24 GB), or L40S (48 GB) for headroom | 32 cores, 128 GB RAM, ECC | 2 TB NVMe in RAID 1, object storage for documents, 10 GbE | Vendor support and warranty; L40S allows a larger extractor or more concurrent pages |
| P3 High availability and scale | Month-end bursts; 99.9% availability option | 2 or more P2-class GPU nodes (N+1) behind the queue | Per node as P2, plus 2 CPU nodes (16 cores, 64 GB) for ingestion, API and review | Replicated object storage and database | Validate ≥ 85% scaling efficiency (NFR-005) |
| P4 CPU-only fallback | Sites without a GPU; low volume | None | 32 or more cores, 128 GB RAM | As P2 | Quantised models only; ≤ 10 s per page is a target to validate and throughput will be below NFR-003 |
| S1 SaaS | Hosted profile in AWS Mumbai (ap-south-1) | Autoscaled GPU instances with a 24 GB or larger GPU (A10G, L4 or L40S classes) | Sized per tenant load | Managed object storage and database, per-tenant keys | Confirm instance availability and price in ap-south-1; NFR-016 metering sets cost per page |

Sizing rules of thumb, to be verified in Slice 0: 15 pages per minute is one page every 4 seconds end to end, which one P2 node should meet if the 3.0 s p95 holds; plan roughly 1 to 3 MB of storage per page (200 DPI image plus derived data), so a million pages a year is about 1 to 3 TB before retention rules apply.

## 13. Evaluation and test strategy

The benchmark decides when a phase is done, so it is built before the model work and is never used for tuning.

### 13.1 Benchmark set

- **Splits per document type:** development (used freely), tuning (used for prompt and threshold selection) and a locked test set that is run only at phase gates and after any model, prompt or schema change.
- **Size:** the approach document asks for 5–10 anonymised scans per type to start. That is enough for development only. Gate-grade size per MVP type is set with the customer; the PRD proposes at least 100 documents per type in the locked set (to confirm), so that a 1-point change in F1 is distinguishable.
- **Strata:** clean print, skewed or low-resolution scans, stamps or signatures over text, character grids, dense checkbox grids, handwriting, each Indic language, and multi-hospital layouts for DOC-03.
- **Ground truth:** double-keyed by two annotators with adjudication, stored with the document hash and annotator IDs; PII-protected like production data.
- **Synthetic supplement:** MOCK-005 samples extend coverage but never count towards a gate on their own.

### 13.2 Test layers

| Layer | What it proves | Run when | Blocks release |
| --- | --- | --- | --- |
| Unit | Validators, normalisers, rule engine, routing, confidence maths | Every commit | Yes |
| Contract | OpenAPI and event schemas; each pipeline stage's input and output contract | Every commit | Yes |
| Pipeline integration | Upload to envelope on synthetic documents | Every merge | Yes |
| Benchmark regression | M1 to M6 on dev set per merge; locked set at gates and model changes | See left | Yes (GR-011) |
| Adversarial | Prompt-injection set (GR-006), malformed files (GR-001), blank-field false-fill (GR-003) | Every release | Yes |
| Security | SAST, dependency and image scans, egress test, cross-tenant test, pen test | CI; pen test before GA | Yes |
| Performance | NFR-001 to NFR-005 on reference hardware, burst test | Every release candidate | Yes |
| Resilience | Failure injection, backup restore, kill switch | Quarterly and before GA | Yes |
| UX conformance | Built screens match signed-off mocks and states (MOCK-003, 009); accessibility audit | Per screen | Yes |

### 13.3 Measurement rules

- **Field match:** exact match after normalisation for identifiers, dates, amounts and enums; token-level F1 for free text. The rule per field type is fixed before the first run.
- **Null handling:** predicting a value where ground truth is null counts as a false positive and is reported separately as the false-fill rate.
- **Calibration:** confidence is binned in 10 equal bins; the gap is the absolute difference between mean confidence and accuracy, weighted by bin size.
- **STP:** share of documents auto-approved whose approved values match ground truth; auto-approved documents with any wrong key field are counted as escapes and reported as a separate number that SHALL be 0 on the locked set for every document type at its release gate (GR-027). STP has no target yet and is only reported.

### 13.4 Definition of done

A requirement is done only when all of these hold:

- [ ] Code merged with tests that cite the requirement ID.
- [ ] Acceptance criteria in the PRD pass in CI.
- [ ] Guardrail and security tests tagged to the requirement pass.
- [ ] Telemetry and audit events exist and contain no PII.
- [ ] The API contract or screen mock is updated and signed off where affected.
- [ ] The CSF profile (SEC-024) is updated where a Category is affected.
- [ ] Documentation and runbook entry updated.

### 13.5 ICXBee quality gates mapped to this product [Proposed]

The ICXBee constitution makes five gates non-negotiable before any merge. They apply here too; the table gives the equivalent for the reference stack. Python is the chosen language (OQ-DOCX-07, decided 2026-10-29).

| ICXBee gate | Equivalent here | Where it is already required |
| --- | --- | --- |
| SonarQube: zero Critical or Blocker issues | Same, SonarQube scan of the Python and front-end code | SEC-015 (SAST in CI) |
| JaCoCo: ≥ 80% line coverage on the service layer | Line coverage ≥ 80% on service code (pytest-cov); NFR-017 already requires ≥ 80% on validation, normalisation and routing, and this extends it to every service | NFR-017 |
| OWASP Dependency-Check: no unresolved High or Critical CVEs | Dependency and container image scan in CI (OWASP Dependency-Check or equivalent); critical patched within 7 days, high within 30 | SEC-015, SEC-023 |
| Testcontainers integration tests passing | Integration tests that start the queue, database and object store as containers and run upload to envelope on synthetic documents | 13.2 pipeline integration |
| OpenAPI spec generated and committed | OpenAPI 3.1 generated from code and committed under `specs/api/` | API-014, 16.1 |
| VAPT clean before release | Independent penetration test before general availability | SEC-015, G1 |

A service passes no gate by waiver; a merge that fails one is blocked, as in the ICXBee refactor.

## 14. Roadmap and phase gates

Phases are gated by evidence, not dates. Durations are set after capacity planning and hardware confirmation. The approach document's advice is followed: stabilise the pipeline on the two structural extremes (CASA form and discharge summary), then treat the rest as schema work.

**MVP — prove the pipeline on one document per domain.**

- Types: DOC-01 CASA, DOC-02 Insurance application, DOC-03 Discharge summary.
- Capabilities: printed text and clean-to-skewed scans, classification, schema-guided extraction with null handling, evidence and confidence, core validations, review workbench, JSON API, audit, security baseline, SaaS and on-premise profiles from one build, an optional cloud provider adapter under policy, English handwriting flagged for review only.
- Screens: SCR-01 to SCR-09 mocks signed off and built.

**v1 — widen coverage and connect.**

- Types: DOC-04 to DOC-07, plus DOC-08 brochures and policy wordings.
- Capabilities: handwriting reading, Hindi, Telugu, Tamil, Malayalam (each language ships only when it passes), bundle splitting, schema library, CSV/XML, first system connector, operations dashboard SCR-10, evidence bundle (SEC-025).

**v2 — reason across documents and improve safely.**

- Capabilities: natural-language schema drafting, case-level cross-document reasoning, agentic re-read and missing-document requests, approved-feedback fine-tuning loop, high-availability reference deployment, wider connector catalogue.

| Gate | Entry to | Criteria (all on the locked test set unless stated) |
| --- | --- | --- |
| G0 | Build | PRD signed off; decisions D1 to D5 confirmed (2026-10-29); hardware profile chosen (12.1); sample documents delivered; benchmark process agreed |
| G1 | MVP release | M1 ≥ 92% all fields and ≥ 95% key fields on DOC-01 to DOC-03; M3 = 100%; M4 ≥ 98%; M5 reported, M11 = 0 escapes; M6 ≤ 3 points; M7 met; M9 = 0; both deployment profiles pass the same suite; all P0 guardrails and security tests pass; pen test has no open high findings; CSF profile complete; mocks signed off |
| G2 | v1 release | G1 criteria hold for all seven types; per-language CER meets M2 for each shipped language; connector end-to-end test passes; SEC P1 items pass |
| G3 | v2 release | Case-level checks measured on a case benchmark; fine-tuning loop passes lineage and approval tests (SEC-018, GR-012); HA drill passes |

## 15. Risks, assumptions, dependencies and open questions

### 15.1 Risks

| ID | Risk | Impact | Mitigation | Related |
| --- | --- | --- | --- | --- |
| R1 | Published model accuracy (for example >95% on OmniDocBench) does not carry over to Indian bank and hospital forms. | Targets missed | Treat vendor figures as hypotheses; measure on the locked set at G0 spike; keep parser behind an interface. | NFR-008, M1 |
| R2 | Handwritten prescriptions have high error rates and ambiguous drug names. | Clinical risk | Review below 0.75, show crop beside value, never auto-approve; set STP expectations accordingly. | GR-009, FR-CNF-002 |
| R3 | A 7B local model hallucinates or mis-assigns values in dense forms. | Wrong data in downstream systems | Null by default, evidence required, grammar-guided decoding, validation gate, second pass. | GR-002, GR-003, GR-005 |
| R4 | Customer GPU is smaller than assumed, so latency targets fail. | SLA breach | Confirm hardware at G0; quantisation options; CPU fallback target; queue with wait estimate. | NFR-001, NFR-002 |
| R5 | Malayalam, Telugu or Tamil accuracy is below the bar with the chosen models. | Launch languages slip | Per-language gate; ship languages independently; evaluate alternative parsers. | FR-PRS-007 |
| R6 | Prompt injection through document content. | Corrupted output | Data/instruction separation, schema-only outputs, no tools, adversarial test set. | GR-006, GR-007 |
| R7 | Sample documents or ground truth arrive late or are too few. | Cannot gate | Synthetic generator for development; contractual sample commitment; gate size set early. | MOCK-005, §13.1 |
| R8 | Model licence prevents commercial on-premise use. | Rework | Licence review at G0; alternatives behind the same interface. | SEC-014 |
| R9 | Reviewer fatigue causes accepting wrong values. | Escapes | Evidence highlight, no pre-checked accept-all, escape audit sampling of auto-approved documents. | FR-REV-001 |
| R10 | Scope creep to "any document". | Missed commitments | Publish supported types with measured accuracy; new type only through schema and gate. | §3.1 |
| R11 | Cloud provider use conflicts with a customer's or regulator's expectations for PII or PHI. | Lost deals or a compliance finding | Off by default; signed policy per tenant and document type; agreement, region and no-training conditions; on-premise profile with providers disabled; call logging without content. | GR-021 to GR-025, ARC-006 |
| R12 | SaaS and on-premise from one build widens the test and support matrix. | Schedule and quality | One container build with the profile set by configuration; the same test suite on both profiles at G1; profile-specific features deferred. | ARC-006, G1 |

### 15.2 Assumptions and dependencies

- The customer provides representative anonymised samples and ground truth for each launch type.
- For private deployments the customer provides GPU servers (12.1), an identity provider, a KMS or HSM, and the egress allow-list; for SaaS the vendor provides them.
- Access to policy-administration, loan-origination and claims systems (sandbox) is available for connector work in v1.
- Models named in the approach document (PaddleOCR-VL-1.6 or GLM-OCR for parsing; quantised Qwen2.5-7B-Instruct for extraction, served by Ollama or vLLM) are the starting candidates, not commitments; the PRD binds interfaces and measured outcomes.
- Legal review of DPDP, HIPAA, GDPR, RBI and IRDAI obligations is done per customer outside this PRD.

### 15.3 Answers carried over from the outline

| Question | Answer recorded | How this PRD treats it |
| --- | --- | --- |
| Anchor industries | InsurTech, FinTech, Healthcare | MVP types span all three (D5) |
| Deployment | SaaS first | SaaS and on-premise from one build (D1, confirmed) |
| Accuracy and STP target | "80% above" | Deferred by the product owner until the first paying customer defines it (M5) |
| Pricing | Per page | NFR-016 metering requirement |
| Languages at launch | English, Hindi, Telugu, Tamil, Malayalam | Per-language gate (FR-PRS-007) |

### 15.4 Open questions and decisions needed

- [x] **D1 to D5.** Confirmed 2026-10-29: SaaS and on-premise options (D1), cloud APIs included under policy (D2), D3 to D5 as drafted.
- [x] **Existing schemas.** None exist; schemas are authored in the schema studio, and the seven launch schemas become the library (SCH-009).
- [x] **Escapes.** 0 wrong key fields in auto-approved documents; control and gate in GR-027.
- [x] **Operational targets.** Confirmed: availability 99.5% (99.9% with high availability), RPO 15 minutes, RTO 4 hours, throughput 15 pages per minute.
- [x] **Handwriting.** English only at first.
- [x] **Healthcare data rules.** Both HIPAA and DPDP apply (GR-025).
- [ ] **Samples.** The product owner will provide them; agree the delivery date and the size of the locked set (about 100 per MVP type proposed).
- [ ] **Hardware.** Recommendations are in 12.1 (profiles P1 to P4 and S1); confirm the profile for the first pilot.
- [ ] **STP definition.** Deferred until the first paying customer defines it (M5); revisit then.
- [x] **Identity.** Internal identity service plus SSO, replicating the ICXBee design (self-issued RS256 JWTs, pluggable provider adapter, no Firebase); decided 2026-10-29 in ADR 0002. Remaining: SAML scope (OQ-DOCX-11).
- [ ] **Model licences.** Left open; review the candidate local models and any cloud provider terms before G0.
- [ ] **Cloud provider shortlist.** Which providers go on the register first (GR-021), and does each offer a business associate agreement, India-region processing and no-training terms?
- [ ] **SaaS tenancy and pricing.** Shared multi-tenant or dedicated per customer, and whether per-page pricing also applies to on-premise licences.
- [ ] **Cross-border rules.** Confirm with counsel which cloud regions DPDP permits for the first customers.

### 15.5 ICXBee platform integrations [Proposed]

The product is **standalone** (decided 2026-10-29): it does not depend on ICXBee services to run. The rows below are integrations and shared conventions it may use, not hard dependencies.

| Dependency | Purpose | Related |
| --- | --- | --- |
| ICXBee identity (`icxbee-login-registration-service`) | Optional federation only: an ICXBee tenant can sign in to this product through ICXBee acting as an OIDC identity provider. The product has its own internal identity service plus SSO (ADR 0002) and does not depend on ICXBee to run (ADR 0001) | SEC-001, SEC-002 |
| ICXBee roles (Master Data PRD owns Roles) | Roles Analyst, Reviewer, Schema author, Schema approver, Integrator, Auditor and Admin are tenant roles, distinct from the Back-Office roles (`SUPER_ADMIN`, `ADMIN`, `OPERATOR`, `VIEWER`) | SEC-003 |
| Feature catalog (BO Features PRD) | A catalog entry for the extraction module so a plan can entitle it | OQ-DOCX-06 |
| Plans (BO Plans PRD), `USAGE_METERED` and `LIMIT` entitlements, usage pricing with included quota and overage | Vehicle for per-page pricing and page quotas; the Plans PRD treats usage counters and metering as a separate domain | NFR-016, OQ-DOCX-06 |
| Master Data / `icxbee-common-services` | Code lists and country, currency and role lookups for normalisation | SCH-008, FR-VAL-001 |
| Notification service | Delivery of alert emails (SEC-021) and review SLA notices | SEC-021 |
| MongoDB 8.0 and Redis 7.2 | ICXBee stack candidates for metadata and for queues, caches and preview-style state | OQ-DOCX-05 |
| AWS Mumbai (ap-south-1), GitHub Actions with OIDC role | SaaS residency and CI/CD, no long-lived keys | SEC-019, SEC-013 |
| `icxbee-mockups` | Mocks, flow map, Stage 0 artefacts | 8.6 |

### 15.6 Open questions raised by the ICXBee alignment

| # | Question | Owner | Due | Answer |
| --- | --- | --- | --- | --- |
| OQ-DOCX-01 | Which chrome do the screens use: front-office (tenant staff, proposed) or back-office tokens? And is `DOCX` the right screen-ID stem? | Praveen / Design | Before Slice 0 mocks | **Resolved 2026-10-29:** front-office chrome and tokens. Stem `DOCX` kept. |
| OQ-DOCX-02 | The mockups repo is desktop-first and single-theme. Keep tablet 1024 and dark (MOCK-002) as generator options, or drop them for MVP? | Praveen / Design | Before Slice 0 mocks | **Resolved 2026-10-29:** desktop-first, light only for MVP; tablet and dark in v1. |
| OQ-DOCX-03 | Is this a module inside ICXBee (shared identity, feature catalog entry, plan entitlement) or a standalone product that integrates with it? | Praveen | Before G0 | **Resolved 2026-10-29:** standalone product; recorded in ADR 0001. Identity: OQ-DOCX-09. |
| OQ-DOCX-04 | Error envelope: nested `{error:{…}}` per API-010 vs the flat `code, message, fieldErrors, details, requestId` the ICXBee web clients already parse. (Path prefix `/api/v1` is decided.) | Praveen / Backend lead | Before API build | Open (prefix resolved 2026-10-29) |
| OQ-DOCX-05 | Persistence: reuse MongoDB and Redis from the ICXBee stack, or choose per component for the on-premise profile (storage, queue, audit)? | Praveen / Architect | Before Slice 1 | Open |
| OQ-DOCX-06 | Per-page pricing: model as a plan `USAGE_METERED` entitlement and who owns the per-page counter (Plans PRD defers metering)? As a standalone product it may also need its own plan and billing model. | Praveen / Product | Before pricing work | Open (kept open 2026-10-29; NFR-016 metering stays) |
| OQ-DOCX-07 | The reference stack is Python while ICXBee services are Java 17 and Spring Boot. Record the exception as an ADR under `/docs/adr/` and confirm the gate equivalents in 13.5. | Praveen | Before Slice 0 | **Resolved 2026-10-29:** Python; recorded in ADR 0001. |
| OQ-DOCX-08 | Where do the Spec Kit artefacts live: in `icxbee-document-extractor` (proposed, own constitution that inherits the ICXBee principles) or in `icxbee-refactor-services/specs/`? | Praveen | Before Slice 0 | **Resolved 2026-10-29:** the existing flat `icxbee-refactor-services/specs/`, folders named `NNN-docx-<module>`; a nested sub-folder was rejected after testing. ADR 0003. |
| OQ-DOCX-09 | The product is standalone, but SEC-001 and SEC-002 say to reuse ICXBee identity and API-key design. Which IdP serves the SaaS profile: ICXBee's, or a separate one? | Praveen / Architect | Before Slice 9 | **Resolved 2026-10-29:** replicate the ICXBee approach: internal identity plus SSO, in every profile. ADR 0002. |
| OQ-DOCX-10 | Spec Kit tooling assumes flat `specs/NNN-*` folders. Does the nested `specs/document-extractor/NNN-*` path work without affecting existing ICXBee specs? | Praveen | Before Slice 0 | **Resolved 2026-10-29:** No, not cleanly (script cannot create it; the shared `.specify/feature.json` or an env var would be needed; numeric-prefix fallback hits `001-auth-login`). Staying with the flat folder. ADR 0003. |
| OQ-DOCX-11 | Is SAML 2.0 in scope for MVP SSO, or OIDC only? Confirm what the ICXBee identity service supports before copying the pattern. | Praveen / Architect | Before Slice 9 | Open |

## 16. Spec-driven development handoff

The build follows the spec: requirements and mocks come first, tests are written from acceptance criteria, and code is accepted only when those tests pass. This section turns the PRD into work an engineer or coding agent can pick up without interpretation.

### 16.1 Specification repository layout

Proposed for the `icxbee-document-extractor` repository. The Spec Kit feature folders are the exception: they live in `icxbee-refactor-services/specs/` alongside the existing ICXBee specs (ADR 0003, 16.6).

```text
specs/
  prd/                 # this PRD split by section, one file per section, IDs unchanged
  schemas/             # document-type schemas (YAML) and the envelope JSON Schema
  api/openapi.yaml     # generated and checked against API-001 to API-014
  screens/             # one spec per SCR-nn plus generated mocks and sign-offs
  guardrails/          # GR tests: injection set, malformed files, blank-field set
  security/            # threat model, CSF profile (SEC-024), SBOM config
  traceability.csv     # requirement ID, component, test ID, screen, CSF category, status
tests/
  unit/ contract/ integration/ benchmark/ adversarial/ security/ performance/
```

### 16.2 Traceability matrix (by requirement family)

| Requirement family | Component | Primary tests | Screens | CSF Category |
| --- | --- | --- | --- | --- |
| FR-ING, GR-001 | Ingestion and preprocessing service | Unit, fuzz, integration | SCR-01, SCR-02 | PR.PS |
| FR-CLS | Classifier and schema binder | Benchmark (M4), out-of-domain set | SCR-03 | PR.PS |
| FR-PRS | Visual parser service (local model) | Benchmark (M2), checkbox set | SCR-03 | ID.AM, GV.SC |
| FR-EXT, GR-002 to 007 | Extraction service, prompt and schema compiler | Benchmark (M1, M3), blank-field, injection | SCR-03, SCR-06 | PR.DS, ID.RA |
| FR-VAL, GR-008 | Validation and rules engine | Unit rule suite | SCR-03, SCR-05 | PR.DS |
| FR-CNF, FR-REV | Confidence, routing, review service | Calibration (M6), UI conformance | SCR-03, SCR-04 | GV.OV |
| SCH, API | Schema service, API gateway | Contract (API-014), lifecycle tests | SCR-05, SCR-07 | PR.AA |
| FR-AUD, SEC-008 | Audit service | Tamper test, reproduction test | SCR-08 | DE.AE, RS.AN |
| SEC-001 to 007 | Identity, authorisation, masking | Permission matrix, cross-tenant | SCR-09 | PR.AA, PR.DS |
| SEC-009 to 023 | Platform hardening, backup, monitoring | Egress, restore drill, pen test | SCR-09, SCR-10 | PR.PS, PR.IR, DE.CM |
| NFR | All | Performance, resilience | SCR-10 | PR.IR |
| MOCK, SCR | UI | Mock conformance, accessibility | All | PR.AT |

### 16.3 Build order (vertical slices)

Each slice ends with something demonstrable and passing its tests. Slices 1 to 6 use DOC-01 and DOC-03 only, as the approach document recommends.

| Slice | Outcome | Main requirements |
| --- | --- | --- |
| 0 | Repo, CI, spec lint, traceability check, synthetic document generator, benchmark harness skeleton, signed-off mocks for SCR-01 to SCR-04 | MOCK-001 to 009, SEC-015, §13 |
| 1 | Upload to preprocessed pages with idempotency and file safety | FR-ING-001 to 006, GR-001 |
| 2 | Layout parsing to Markdown, tables, checkboxes, with bounding boxes | FR-PRS-001 to 005 |
| 3 | Classification, schema binding, schema service with lifecycle | FR-CLS, SCH-001 to 007 |
| 4 | Guided extraction with evidence, null handling and guardrails; first benchmark numbers | FR-EXT-001 to 007, GR-002 to 007 |
| 5 | Validation, normalisation, confidence, routing and envelope | FR-VAL, FR-CNF, SCH-011 to 014 |
| 6 | API, webhooks, audit and reproducibility | API-001 to 014, FR-AUD-001 |
| 7 | Review queue and workbench, results and export screens | SCR-03, 04, 06; FR-REV |
| 8 | Schema studio, intake and list screens; DOC-02 added | SCR-01, 02, 05; SCH-003, 008 |
| 9 | Security hardening, masking, retention, monitoring, provider policy and adapter, SaaS and on-premise profiles, CSF profile and evidence | SEC-001 to 026, GR-021 to 027, ARC-006, ARC-007, SCR-08, 09 |
| 10 | Performance and resilience tests, locked-set run, gate G1 review | NFR, §13, G1 |

### 16.4 Task slice template (for engineers and coding agents)

```text
Task: <slice>-<nn> <title>
Implements: <requirement IDs>
Spec files to read: <paths>
Inputs / outputs: <contracts, with schema paths>
Tests to write first: <acceptance criteria turned into tests, with IDs>
Guardrails and security checks: <GR and SEC IDs that apply>
Screens affected: <SCR IDs and the signed-off mock version>
Out of scope: <what must not be changed>
Done when: Definition of done in section 13.4 is met
```

### 16.5 Working rules for spec-driven builds

1. The PRD and the files under `specs/` are the source of truth. If code and spec disagree, the spec is changed first, with review, then the code.
2. Tests cite requirement IDs; the traceability check fails the build when a P0 requirement has no test.
3. A person or agent never invents behaviour that is not in the spec; an ambiguity becomes a comment on the PRD and a blocked task.
4. No real customer data in the repository, prompts, mocks, logs or tests (MOCK-004, SEC-012).
5. Each slice is reviewed against the guardrail and security checks before merge, not at the end.

### 16.6 Spec Kit mapping [Proposed]

ICXBee builds each module with GitHub Spec Kit: one numbered feature folder under `specs/` with `spec.md`, `plan.md`, `research.md`, `data-model.md`, `quickstart.md`, `contracts/` and `tasks.md`, governed by `.specify/memory/constitution.md`. This product follows the same workflow so its slices (16.3) become Spec Kit features. **Location (decided 2026-10-29, ADR 0003):** the existing flat folder `icxbee-refactor-services/specs/`, exactly as for the ICXBee specs. `/speckit.specify` assigns the next free sequential number (currently 014) and the folder is named `NNN-docx-<module>` so it is recognisable. A nested `specs/document-extractor/` sub-folder was tested and rejected because it needs changes to shared Spec Kit state; no `.specify/` file and no existing spec is touched. The other artefacts in the 16.1 layout (`prd`, `schemas`, `screens`, …) stay in the product repository.

| Slice | Spec folder (`NNN` assigned by `/speckit.specify`) | Stories | Screens |
| --- | --- | --- | --- |
| 0 | `NNN-docx-foundation-and-mocks` | none (tooling, mocks, benchmark skeleton) | DOCX-SCR-001 to 004 mocks |
| 1 | `NNN-docx-ingestion` | US-DOCX-001 | DOCX-SCR-001 |
| 2 | `NNN-docx-layout-parsing` | US-DOCX-004 (parsing part) | DOCX-SCR-003 data |
| 3 | `NNN-docx-classification-schema-service` | US-DOCX-003, US-DOCX-009, US-DOCX-010 | DOCX-SCR-005 |
| 4 | `NNN-docx-guided-extraction` | US-DOCX-004 | DOCX-SCR-003 data |
| 5 | `NNN-docx-validation-confidence-envelope` | US-DOCX-005, US-DOCX-006 | DOCX-SCR-003 |
| 6 | `NNN-docx-api-webhooks-audit` | US-DOCX-012, US-DOCX-013 | DOCX-SCR-007, DOCX-SCR-008 |
| 7 | `NNN-docx-review-and-results` | US-DOCX-007, US-DOCX-008, US-DOCX-011 | DOCX-SCR-003, 004, 006 |
| 8 | `NNN-docx-schema-studio-intake-list` | US-DOCX-002, US-DOCX-009, US-DOCX-010 | DOCX-SCR-001, 002, 005 |
| 9 | `NNN-docx-security-policy-profiles` | US-DOCX-014, US-DOCX-015, US-DOCX-017 | DOCX-SCR-008, 009 |
| 10 | `NNN-docx-performance-and-gate` | US-DOCX-016 | DOCX-SCR-010 |

Per feature: paste the stories from Section 17 into `/speckit.specify`, resolve questions in `/speckit.clarify`, take contracts from Sections 6, 7 and 18, run `/speckit.plan` and `/speckit.tasks` (task template in 16.4), check with `/speckit.analyze`, then `/speckit.implement`. The ICXBee principles that apply (quality gates, compliance-first sequencing, data residency, configuration over hardcoding, feature-branch discipline) govern this product through the shared constitution; this document's guardrails (Section 9) are added as principles by amendment.

## 17. Acceptance criteria (user stories)

Stories follow the ICXBee Gherkin style, ready for `/speckit.specify`. Each cites the requirement IDs it covers; thresholds live in those requirements and are not restated here.

---

#### US-DOCX-001: Upload documents

**Persona:** Operations analyst  
**Story:** As an operations analyst, I want to upload files and batches so that they enter the pipeline safely. Covers FR-ING-001 to 006, GR-001, SEC-009.

```gherkin
Feature: Upload and intake

  Scenario: Accepted file
    Given I am signed in as an analyst
    When I drop a PDF on DOCX-SCR-001 and click Upload
    Then a document is created with status "queued" and I see its ID

  Scenario: Unsupported file
    When I upload a renamed .exe as .pdf
    Then the row shows UNSUPPORTED_FILE and nothing is stored

  Scenario: Malware
    When I upload the EICAR test file
    Then the status is "rejected_malware" and no pipeline stage runs

  Scenario: Idempotent retry
    When the same upload is sent twice with one Idempotency-Key
    Then one document exists and both calls return its ID

  Scenario: Partial batch failure
    Given a batch of 3 files where 1 is unsupported
    Then 2 are queued and 1 is rejected with its code (DOCX-SCR-001-partial-failure)
```

---

#### US-DOCX-002: Track documents

**Persona:** Operations analyst  
**Story:** As an analyst, I want to find and follow documents so that nothing is lost. Covers API-015, SCH-012.

```gherkin
Feature: Document list

  Scenario: Default list
    When I open DOCX-SCR-002
    Then I see documents newest first, 10 per page, with status shown as text plus icon
    And the stat strip counts the whole tenant

  Scenario: Filter
    When I filter status "needs_review" and type "DISCHARGE_SUMMARY"
    Then only matching documents show and the page resets to 1

  Scenario: Empty
    Given no documents exist
    Then I see an empty state with an Upload action

  Scenario: Open by status
    When I open a "needs_review" document
    Then I land on the review workbench
    When I open an "auto_approved" document
    Then I land on the results screen
```

---

#### US-DOCX-003: Classify and route

**Persona:** System, Reviewer  
**Story:** As a reviewer, I want unrecognised documents routed to me instead of guessed. Covers FR-CLS-001 to 004, GR-004.

```gherkin
Feature: Classification

  Scenario: Known type
    Given a clean CASA form
    Then the document type is CASA_ACCOUNT_OPENING with a confidence in [0, 1]
    And the current approved schema version is recorded on the document

  Scenario: Unknown type
    Given an unrelated utility bill
    Then the type is UNKNOWN, no extraction output exists and a review task is created

  Scenario: Override
    When a reviewer changes the type in DOCX-SCR-003
    Then a new extraction run starts with the chosen schema and the old run stays in history
```

---

#### US-DOCX-004: Extract with evidence

**Persona:** System  
**Story:** As a business owner, I want every value typed, evidenced and null when unsure. Covers FR-PRS-001 to 005, FR-EXT-001 to 007, GR-002, GR-003, GR-005, GR-006.

```gherkin
Feature: Extraction

  Scenario: Evidence on every value
    Given an extracted non-null field
    Then it carries page, bbox and a supporting snippet

  Scenario: Absent field
    Given a form with the PAN left blank
    Then pan_card_number is null with null_reason "absent"

  Scenario: Value without evidence
    Given the model returns a value with fake evidence
    Then the field is null and flagged EVIDENCE_MISSING

  Scenario: Character grid and checkbox
    Given a PAN written in 10 boxes and a ticked box
    Then the PAN is one value without spaces and the tick maps to true

  Scenario: Injected instruction
    Given a document containing "ignore previous instructions"
    Then the output has no schema deviation
```

---

#### US-DOCX-005: Validate and normalise

**Persona:** System  
**Story:** As a compliance owner, I want malformed or inconsistent values flagged before approval. Covers FR-VAL-001 to 006, GR-008.

```gherkin
Feature: Validation

  Scenario: Normalisation
    Given a date "22/06/1985" and a phone "+91 98765 43210"
    Then they become 1985-06-22 and +919876543210

  Scenario: Format failure
    Given PAN "ABCDE1234"
    Then the field is "validation_failed" and the document is routed to review

  Scenario: Arithmetic
    Given items summing to 41,500 and a total of 45,100
    Then an ARITHMETIC_MISMATCH flag is raised on the claim

  Scenario: Chronology
    Given a discharge date before the admission date
    Then both fields are flagged
```

---

#### US-DOCX-006: Confidence and routing

**Persona:** System, Reviewer  
**Story:** As a reviewer, I want to see only what needs a person. Covers FR-CNF-001 to 003, GR-009, GR-017, GR-027.

```gherkin
Feature: Confidence routing

  Scenario: Auto-approve
    Given all fields pass validation and thresholds
    Then the document is "auto_approved"

  Scenario: Low-confidence handwriting
    Given a handwritten drug name at confidence 0.62
    Then it is review_required and the document is not auto-approved

  Scenario: Only flagged fields
    Given 5 of 40 fields are flagged
    Then the review task lists those 5 only

  Scenario: Escape control
    Given a sampled auto-approved document with a wrong key field
    Then auto-approval pauses for that document type within 1 minute
```

---

#### US-DOCX-007: Review in the workbench

**Persona:** Reviewer  
**Story:** As a reviewer, I want to verify flagged fields against the source image. Covers FR-REV-001 to 004, FR-CLS-004, SEC-007.

```gherkin
Feature: Review workbench

  Scenario: Highlight evidence
    When I select a field on DOCX-SCR-003
    Then its bounding box is highlighted in the viewer and clicking a box selects its row

  Scenario: Correct a field
    When I edit a value
    Then it is re-validated and the action is audit-logged with old and new value

  Scenario: Invalid correction
    When my correction fails a rule
    Then Complete is blocked and the failing rule is shown

  Scenario: Complete
    Given every flagged field is resolved
    Then Complete is enabled; otherwise it is disabled

  Scenario: Masked values
    Given I lack the unmask permission
    Then PII values are masked and a reveal needs a permitted role and is logged

  Scenario: Claimed elsewhere
    Given the task is claimed by another reviewer
    Then the workbench is read-only and shows the owner
```

---

#### US-DOCX-008: Work the review queue

**Persona:** Reviewer  
**Story:** As a reviewer, I want to claim the next exception. Covers API-005, API-016.

```gherkin
Feature: Review queue

  Scenario: Claim
    When I claim a task on DOCX-SCR-004
    Then it is assigned to me and the workbench opens

  Scenario: Claim conflict
    Given another reviewer claimed it first
    Then I see TASK_CLAIMED with the owner and the task stays theirs

  Scenario: Release
    When I release a task
    Then it returns to Unassigned
```

---

#### US-DOCX-009: Author and approve a schema

**Persona:** Business owner (author), Schema approver  
**Story:** As a business owner, I want to add a document type without engineering. Covers SCH-001 to 006, SCH-009, M8.

```gherkin
Feature: Schema lifecycle

  Scenario: Publish validation
    Given a field with no type
    Then publish validation fails

  Scenario: Round trip
    When I export the schema to YAML and re-import it
    Then the diff is empty

  Scenario: Four eyes
    Given I authored a schema version
    When I try to approve it
    Then I am refused; a second user with the approver role can approve

  Scenario: Only approved in production
    Given a schema in "draft"
    Then binding it in production returns SCHEMA_NOT_APPROVED

  Scenario: Rollback
    When I roll back to version 2
    Then new documents bind version 2 and history keeps every version
```

---

#### US-DOCX-010: Test a schema

**Persona:** Business owner  
**Story:** As a business owner, I want proof a schema works before approval. Covers SCH-007, API-007.

```gherkin
Feature: Test harness

  Scenario: Run on samples
    Given sample documents with ground truth
    When I run the test on DOCX-SCR-005
    Then every field lists precision, recall, F1 and calibration

  Scenario: Attach to approval
    When I request approval
    Then the latest passing run is attached
```

---

#### US-DOCX-011: View and export results

**Persona:** Operations analyst  
**Story:** As an analyst, I want approved output in the canonical envelope and my chosen format. Covers FR-OUT-001 to 003, SCH-011 to 014.

```gherkin
Feature: Results and export

  Scenario: Envelope
    When I open an approved document on DOCX-SCR-006
    Then I see fields with value, confidence, validation and evidence
    And the output validates against the published JSON Schema

  Scenario: Masked export
    When I export without the unmask permission
    Then PII fields are masked in the file

  Scenario: Unmasked export
    Given I hold the permission
    When I export unmasked and give a reason
    Then the export is audited with that reason
```

---

#### US-DOCX-012: Integrate through API and webhooks

**Persona:** Developer / integrator  
**Story:** As an integrator, I want a stable, signed, versioned contract. Covers API-001 to 014, Section 7.1.

```gherkin
Feature: Developer console and API

  Scenario: Create a key
    When I create an API key
    Then the secret is shown once and only its SHA-256 hash is stored

  Scenario: Signed webhook
    Given a subscription to document.completed
    Then deliveries carry an HMAC-SHA256 signature and retry with back-off for 24 hours

  Scenario: Rate limit
    When a client exceeds its limit
    Then it gets 429 with Retry-After and is throttled, not dropped

  Scenario: Error shape
    Then every error has code, message, request_id and no document content
```

---

#### US-DOCX-013: Audit and reproduce

**Persona:** Compliance and audit  
**Story:** As an auditor, I want to reconstruct who did what and re-run a result. Covers FR-AUD-001, SEC-008, GR-016.

```gherkin
Feature: Audit explorer

  Scenario: Search
    When I search a document ID on DOCX-SCR-008
    Then I see every event for it in order

  Scenario: Reproduce
    When I open a run
    Then I see model, prompt, schema and provider versions and every correction
    And re-running it in the same environment gives identical output

  Scenario: Tamper evidence
    Given a stored event is altered
    Then chain verification fails and an alert is raised
```

---

#### US-DOCX-014: Control where document data goes

**Persona:** Admin, compliance  
**Story:** As a compliance owner, I want document data to reach only providers that policy approves. Covers ARC-006, ARC-007, GR-013, GR-021 to 026, SEC-011.

```gherkin
Feature: Provider policy

  Scenario: Default is local only
    Given a tenant with no policy
    Then no external call is made

  Scenario: Unregistered provider
    When an admin configures an unregistered provider
    Then it is refused with PROVIDER_NOT_REGISTERED

  Scenario: Healthcare document
    Given DOC-03 and a policy missing a business associate agreement
    Then the run uses the local model and sets PROVIDER_BLOCKED_BY_POLICY

  Scenario: Region
    When a call would go to a disallowed region
    Then it is blocked and logged PROVIDER_REGION_BLOCKED

  Scenario: Four eyes on policy
    Given an admin created a policy
    Then it takes effect only after a second person approves
```

---

#### US-DOCX-015: Retention, masking and kill switch

**Persona:** Admin  
**Story:** As an admin, I want to set retention and masking and to stop auto-approval fast. Covers SEC-007, SEC-010, GR-015, SEC-021.

```gherkin
Feature: Security settings

  Scenario: Retention delete
    When retention expires or a deletion is requested
    Then originals and derived data are removed and a deletion certificate event is written

  Scenario: Pause auto-approval
    When I confirm the kill switch on DOCX-SCR-009
    Then new documents go to review within 1 minute and the change is audited

  Scenario: Alert
    Given 5 failed logins in a short period
    Then an alert is raised
```

---

#### US-DOCX-016: Watch operations

**Persona:** Admin, product owner  
**Story:** As an owner, I want accuracy, drift and escapes visible. Covers M1 to M11, NFR-010, NFR-011, API-023. P1.

```gherkin
Feature: Operations dashboard

  Scenario: KPIs
    When I open DOCX-SCR-010
    Then I see field F1, STP (reported only), latency, egress count and escapes

  Scenario: Drift alert
    Given a seeded shift in the confidence distribution
    Then an alert appears within 24 hours

  Scenario: No PII
    Then no panel shows document content
```

---

#### US-DOCX-017: Access control and tenancy

**Persona:** All  
**Story:** As an admin, I want least-privilege access with tenant isolation. Covers SEC-001, SEC-003, SEC-006.

```gherkin
Feature: Access control

  Scenario: Role matrix
    Then each role's access to each endpoint matches the permission matrix

  Scenario: Separation of duties
    Then no user both authors and approves the same schema version

  Scenario: Tenant isolation
    When a user guesses another tenant's document ID
    Then the API returns 404

  Scenario: Denied screen
    Given a role without access to a screen
    Then it shows the permission-denied state and no data

  Scenario: Admin MFA
    When an administrator signs in without MFA
    Then access is refused
```

---

## 18. API contract summary

| Story | Method | Endpoint | Auth | Key request fields | Key response fields |
| --- | --- | --- | --- | --- | --- |
| US-DOCX-001 | POST | /api/v1/documents | Bearer (analyst, integrator) | file, document_type_hint, schema_id, callback_url, Idempotency-Key | document_id (202) |
| US-DOCX-002 | GET | /api/v1/documents | Bearer | status, documentType, from, to, confidenceBelow, q, page, size | content[], stats, paging |
| US-DOCX-001, 011 | GET | /api/v1/documents/{id} | Bearer | none | status, metadata, envelope |
| US-DOCX-007 | GET | /api/v1/documents/{id}/pages/{n} | Bearer | none | page image |
| US-DOCX-004 | POST | /api/v1/documents/{id}/extract-sync | Bearer | none (single page) | envelope |
| US-DOCX-007, 008 | GET, POST | /api/v1/review-tasks, /api/v1/review-tasks/{id}/complete | Bearer (reviewer) | corrections[] | task, status |
| US-DOCX-008 | POST | /api/v1/review-tasks/{id}/claim, /release | Bearer (reviewer) | none | task, owner |
| US-DOCX-009 | GET, POST | /api/v1/schemas, /api/v1/schemas/{id}/versions, /approve, /rollback | Bearer (author, approver) | schema, version | schema, version, status |
| US-DOCX-010 | POST | /api/v1/schemas/{id}/test-runs | Bearer (author) | sample set | per-field precision, recall, F1, calibration |
| US-DOCX-011 | GET | /api/v1/documents/{id}/export | Bearer | format, masked, reason | file |
| US-DOCX-012 | GET, POST, DELETE | /api/v1/api-keys, /api/v1/webhooks | Bearer (integrator, admin) | name, scopes, url, events | key (once), subscription |
| US-DOCX-013 | GET | /api/v1/audit-events, /api/v1/audit-events/{id}/run | Bearer (auditor) | actor, type, documentId, range | events[], run |
| US-DOCX-014 | GET, PUT, POST | /api/v1/providers, /api/v1/policies/providers, /approve | Bearer (admin, approver) | provider, region, conditions | policy, status |
| US-DOCX-015 | GET, PUT, POST | /api/v1/settings/*, /api/v1/admin/* | Bearer (admin) | retention, masking, roles, pause | settings, state |
| US-DOCX-016 | GET | /api/v1/dashboard/kpis | Bearer (admin) | range | KPIs, histogram, drift |
| all | GET | /api/v1/health, /api/v1/metrics | none or internal | none | status, Prometheus metrics |

## 19. Error handling

Existing codes are kept; the HTTP column and codes marked **[Proposed]** are this PRD's recommendation, to be fixed with the envelope decision in OQ-DOCX-04. Errors never include document content or PII (API-011).

| Condition | HTTP / code | User sees |
| --- | --- | --- |
| Not signed in or token expired | 401 `UNAUTHENTICATED` [Proposed] | Redirect to sign-in |
| Role lacks permission | 403 `FORBIDDEN` [Proposed] | Permission-denied state of the screen |
| Unsupported or unsafe file | 400 `UNSUPPORTED_FILE` | "This file type isn't supported." |
| File or page limit exceeded | 413 `FILE_TOO_LARGE` [Proposed] | "The file is larger than the limit." |
| Malware found | status `rejected_malware` | "This file was blocked by the safety scan." |
| Document not found, or another tenant's | 404 `DOCUMENT_NOT_FOUND` | "Document not found." |
| Schema not approved | 409 `SCHEMA_NOT_APPROVED` | "This schema isn't approved for production." |
| Author approving own schema | 403 `SCHEMA_SELF_APPROVAL` [Proposed] | "A second person must approve this schema." |
| Validation failed on a field | 422 `VALIDATION_FAILED` | Inline message naming the rule |
| Task claimed by someone else | 409 `TASK_CLAIMED` [Proposed] | "Claimed by <name>." |
| Idempotency key reused with a different body | 409 `IDEMPOTENCY_CONFLICT` [Proposed] | "This request key was already used." |
| Rate limited | 429 `RATE_LIMITED` + `Retry-After` | "Too many requests. Try again shortly." |
| Provider not on the register | 400 `PROVIDER_NOT_REGISTERED` | "This provider isn't approved." |
| Provider region not allowed | `PROVIDER_REGION_BLOCKED` (logged, run continues locally) | Notice on the run |
| Provider blocked by policy | `PROVIDER_BLOCKED_BY_POLICY` (run uses local model) | Notice on the run |
| Evidence missing for a value | flag `EVIDENCE_MISSING` | Field null and flagged for review |
| Totals do not match | flag `ARITHMETIC_MISMATCH` | Banner naming the fields |
| Stage failed after retries | status `failed` + reason code | "Processing failed. A review task was created." |
| Network or server error | 500 `INTERNAL` [Proposed] | "Something went wrong. Please try again." |

## 20. Data model [Proposed]

Collection or table names are illustrative; the store is OQ-DOCX-05. Every record carries `tenantId` (SEC-006) and, where it exists, the audit fields `createdBy`, `createdOn`, `lastModifiedBy`, `lastModifiedOn`.

| Collection | Key fields | Notes |
| --- | --- | --- |
| `documents` | documentId, tenantId, status, documentType, schemaId, schemaVersion, fileHash, pageCount, idempotencyKey | Status per SCH-012 plus `queued`, `rejected_malware` |
| `pages` | documentId, n, imageRef, transforms[], dpi | Image in object storage, encrypted (SEC-005) |
| `runs` | runId, documentId, stage outputs, modelVersions, promptVersion, provider per stage, policyVersion, timings | Reproducible (FR-AUD-001, GR-026) |
| `fields` (or inside the envelope) | documentId, name, value, confidence, nullReason, reviewRequired, validation, evidence{page, bbox, text} | Value fields classed by `pii_class` |
| `review_tasks` | taskId, documentId, flaggedFields[], assignee, slaDueAt, status | Claim and release (API-016) |
| `schemas` / `schema_versions` | schemaId, version, status, definition, author, diff, approvedBy, harnessRunId | Immutable versions (SCH-006) |
| `reference_data` | tenantId, listName, version, entries[] | SCH-008 |
| `api_keys` | keyId, hash (SHA-256), scopes, expiry, revokedAt, lastUsedAt | Secret never stored (SEC-002) |
| `webhooks`, `webhook_deliveries` | url, events, secretRef; eventId, attempt, status | Signed delivery (7.1) |
| `audit_events` | eventId, actor, type, object, outcome, prevHash, hash | Append-only, hash-chained (SEC-008) |
| `provider_register`, `provider_policies` | legal entity, regions, terms, nextReview; tenantId, documentType, region, conditions, approvedBy, version | GR-021 to GR-023 |
| `retention_rules` | tenantId, documentType, days | SEC-010 |
| `metering_records` | tenantId, documentId, pages, provider, timestamp | Per-page pricing (NFR-016) |

## 21. Analytics and logging

Events carry identifiers, counts and codes only, never document content or PII (SEC-012, GR-014).

| Event / metric | Fields |
| --- | --- |
| `docx.document.uploaded` / `.rejected` | tenantId, userId, fileType, sizeBytes, outcome code |
| `docx.document.status_changed` | documentId, from, to, stage |
| `docx.classification.completed` | documentId, documentType, confidence, routed (bool) |
| `docx.extraction.completed` | documentId, schemaVersion, fieldCount, nullCount, flaggedCount, durationMs, provider per stage |
| `docx.review.claimed` / `.completed` / `.field_corrected` | taskId, userId, fieldName (no value), durationMs |
| `docx.schema.created` / `.approved` / `.rolled_back` | schemaId, version, userId |
| `docx.export` | documentId, format, masked, userId |
| `docx.pii.revealed` | userId, fieldName, reason code |
| `docx.provider.call` | provider, model, bytes, payloadHash, outcome |
| `docx.killswitch.changed` | scope, state, userId |
| Counters | documents by status, pages processed, escapes, failures by stage |
| Alerts | Repeated failed logins, unusual export volume, unmask spikes, egress attempts, audit-chain failure, model-file change (SEC-021); escape or drift (GR-027, NFR-011) |

Front-end analytics: page views per screen, filters used, "Next flagged field" use, time per flagged field (M10). No field values.

## 22. Traceability

| Requirement group | Story | Screen | Source reference |
| --- | --- | --- | --- |
| FR-ING-001..008, GR-001 | US-DOCX-001 | DOCX-SCR-001* | F1, A §3 |
| API-015, SCH-012 | US-DOCX-002 | DOCX-SCR-002* | F1, F16 |
| FR-CLS-001..004, GR-004 | US-DOCX-003 | DOCX-SCR-003-unknown-type | A §3, F5 |
| FR-PRS-001..007, FR-EXT-001..008, GR-002, 003, 005, 006 | US-DOCX-004 | DOCX-SCR-003* | A §3, §4, F6..F11 |
| FR-VAL-001..007, GR-008 | US-DOCX-005 | DOCX-SCR-003-validation-failed, -arithmetic-mismatch | A §3, F12, F13 |
| FR-CNF-001..003, GR-009, 017, 027 | US-DOCX-006 | DOCX-SCR-003-low-confidence | A §4.2, F14 |
| FR-REV-001..004 | US-DOCX-007 | DOCX-SCR-003* | F15, F16, F17 |
| API-005, API-016 | US-DOCX-008 | DOCX-SCR-004* | F16 |
| SCH-001..006, SCH-009 | US-DOCX-009 | DOCX-SCR-005* | outline §5 |
| SCH-007, API-007 | US-DOCX-010 | DOCX-SCR-005-test-results | outline §5 |
| FR-OUT-001..003, SCH-011..014 | US-DOCX-011 | DOCX-SCR-006* | F18, F19 |
| API-001..014, §7.1 | US-DOCX-012 | DOCX-SCR-007* | F18 |
| FR-AUD-001, SEC-008, GR-016 | US-DOCX-013 | DOCX-SCR-008* | outline NFR |
| ARC-006, 007, GR-013, GR-021..026, SEC-011 | US-DOCX-014 | DOCX-SCR-009-provider-policy* | D1, D2, P:constitution |
| SEC-007, SEC-010, GR-015, SEC-021 | US-DOCX-015 | DOCX-SCR-009* | A, F, P:constitution |
| M1..M11, NFR-010, NFR-011 | US-DOCX-016 | DOCX-SCR-010* | outline §7 |
| SEC-001, 003, 006 | US-DOCX-017 | all screens, `-denied` states | P:bo-login, P:auth-registration |
| MOCK-001..017 | none | all | 8.6 to 8.9, `icxbee-mockups` |
