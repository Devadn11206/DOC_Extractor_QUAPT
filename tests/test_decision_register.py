"""
The M5 open-decisions register (review finding R2).

Decisions were referred to as "PRD decision D1/D4/D8/D11" while the PRD's own decision table uses the same labels for
different decisions (D1 Deployment, D4 Latency, D8 Document categories, D11 Domain variation), and no register existed in
the repository. Now ``docs/m5-open-decisions.md`` defines every decision with a unique ``M5-Dnn`` ID. Needs no FastAPI.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "docs" / "m5-open-decisions.md"
TEXT = REGISTER.read_text(encoding="utf-8")

# The decisions of the integration plan (its D1-D12, D13 and D-A) plus those raised by the independent reviews.
EXPECTED_IDS = [f"M5-D{n:02d}" for n in range(1, 19)]
REQUIRED_FIELDS = ["**Description.**", "**Owner.**", "**Blocks.**", "**Status.**", "**Resolution.**"]
ALLOWED_STATUS = re.compile(r"\*\*Status\.\*\* (Open|Resolved)")

# Files that talk about M5 decisions. The PRD, the PRD versions and the Phase 1 task headers use PRD labels legitimately.
SCANNED = (
    [ROOT / "README.md", ROOT / "docs" / "m5-api-ownership.md", ROOT / ".env.example"]
    + sorted((ROOT / "src" / "doc_extractor" / "api").rglob("*.py"))
    + sorted((ROOT / "tests").glob("test_api_*.py"))
    + sorted((ROOT / "tests").glob("test_tenant_isolation.py"))
    + sorted((ROOT / "tests").glob("test_storage_*.py"))
    + sorted((ROOT / "scripts").rglob("*.py"))
)
AMBIGUOUS = re.compile(r"(?i)(?:security |prd )?decision D\d+\b|\(D\d{1,2}\)|\bD-A\b")


def sections():
    parts = re.split(r"(?m)^### (M5-D\d{2}) ", TEXT)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def test_the_register_defines_every_decision_exactly_once():
    headings = re.findall(r"(?m)^### (M5-D\d{2}) ", TEXT)
    assert headings == EXPECTED_IDS


def test_every_decision_has_the_required_fields_and_a_prd_reference():
    for decision_id, body in sections().items():
        for field in REQUIRED_FIELDS:
            assert field in body, f"{decision_id} lacks {field}"
        assert "**PRD reference.**" in body or decision_id in ("M5-D13",), f"{decision_id} lacks a PRD reference"
        assert ALLOWED_STATUS.search(body), f"{decision_id} has no valid status"


def test_nothing_is_marked_resolved_without_an_owner_approval_on_record():
    for decision_id, body in sections().items():
        status = ALLOWED_STATUS.search(body).group(1)
        resolution = re.search(r"\*\*Resolution\.\*\* (.+)", body).group(1).strip()
        if status == "Open":
            assert resolution.startswith("none"), f"{decision_id} is open but records a resolution"
        else:
            assert re.search(r"approved by .+ on \d{4}-\d{2}-\d{2}", resolution), f"{decision_id} is resolved without an approval record"


def test_the_summary_table_and_the_sections_agree():
    summary = re.findall(r"(?m)^\| \[(M5-D\d{2})\]\(#([^)]+)\) \|", TEXT)
    assert [i for i, _ in summary] == EXPECTED_IDS
    for decision_id, anchor in summary:
        heading = re.search(rf"(?m)^### {decision_id} (.+)$", TEXT).group(1)
        github_anchor = re.sub(r"[^a-z0-9_ -]", "", f"{decision_id} {heading}".lower()).replace(" ", "-")
        assert anchor == github_anchor, f"{decision_id}: anchor {anchor!r} does not match its heading ({github_anchor!r})"


def test_every_stage_a_decision_blocks_is_defined():
    stages = set(re.findall(r"(?m)^\| \*\*(S\d+)\*\*", TEXT))
    assert stages == {f"S{n}" for n in range(1, 11)}
    for decision_id, body in sections().items():
        blocks = re.search(r"\*\*Blocks\.\*\* ([^.\n]+)", body).group(1)
        named = set(re.findall(r"\bS\d+\b", blocks))
        assert named <= stages, f"{decision_id} blocks an undefined stage {named - stages}"
        assert named or "none" in blocks.lower(), f"{decision_id} names no blocking stage"


def test_every_reference_to_a_decision_in_the_repository_is_defined_in_the_register():
    referenced = {}
    for path in SCANNED + [REGISTER]:
        for match in re.finditer(r"M5-D\d{2}\b", path.read_text(encoding="utf-8")):
            referenced.setdefault(match.group(0), set()).add(path.name)
    assert set(referenced) <= set(EXPECTED_IDS), f"undefined decision IDs: {set(referenced) - set(EXPECTED_IDS)}"
    for needed in ("M5-D01", "M5-D04", "M5-D08", "M5-D11"):
        assert needed in referenced, f"{needed} is not referenced from the code or documents it concerns"


def test_no_m5_file_uses_an_ambiguous_prd_style_decision_label():
    offenders = {}
    for path in SCANNED:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if AMBIGUOUS.search(line):
                offenders[f"{path.relative_to(ROOT)}:{number}"] = line.strip()[:100]
    assert offenders == {}, f"ambiguous decision labels (use M5-Dnn): {offenders}"


def test_the_scanner_would_catch_the_old_labels():
    for old in ("security decision D8 is open", "PRD decision D1", "task states (decision D11)", "(D7)", "split (D-A)"):
        assert AMBIGUOUS.search(old), old
    assert not AMBIGUOUS.search("decision M5-D08 is open")


def test_the_readme_and_the_ownership_doc_point_to_the_register():
    assert "docs/m5-open-decisions.md" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "m5-open-decisions.md" in (ROOT / "docs" / "m5-api-ownership.md").read_text(encoding="utf-8")
