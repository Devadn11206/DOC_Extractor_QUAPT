# PRD versions

**The only PRD to read and build against is `Document_Extraction_Engine_PRD.md` at the repo root (current: v1.5).**

The files in this folder are old versions, kept for history only. Do not build against them.

| File | Version | Date | What it was | Status |
| --- | --- | --- | --- | --- |
| PRD_v1.0_original.pdf | 1.0 | 2026-10-28 | Original draft | Superseded |
| PRD_v1.3.md | 1.3 | 2026-11-07 | Single-model design (Qwen3-VL-8B for every document) | Superseded by v1.4 |
| PRD_v1.4.md | 1.4 | 2026-11-12 | Router plus specialist models by category (structured, unstructured, handwritten), cost workbook, training plan, demo UI | Superseded by v1.5 |
| `../../Document_Extraction_Engine_PRD.md` | **1.5** | 2026-11-16 | v1.4 plus alignment with the six-layer IDP capability list (section 3.5 shows coverage) | **Current** |

## When the PRD changes

1. Copy the current root file into this folder as `PRD_v<old version>.md`.
2. Edit the root file and bump its version, date and the changelog (section 1.x).
3. Add a row to the table above and mark the old row "Superseded".
4. Keep the root file name unchanged, so links to it never break.
