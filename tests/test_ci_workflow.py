"""
Guards the CI definition against being weakened by accident (the workflow itself runs on GitHub, not here).
Needs no FastAPI, so it always runs.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
JOBS = WORKFLOW["jobs"]
API_JOB, FULL_JOB = JOBS["api-and-security"], JOBS["full-suite"]


def steps_text(job) -> str:
    return "\n".join(str(step.get("run", "")) + " " + str(step.get("uses", "")) + " " + str(step.get("name", "")) for step in job["steps"])


def test_the_workflow_runs_on_push_and_pull_request_with_read_only_permissions():
    triggers = WORKFLOW.get("on") if "on" in WORKFLOW else WORKFLOW[True]      # PyYAML reads the key "on" as True
    assert set(triggers) == {"push", "pull_request"}
    assert WORKFLOW["permissions"] == {"contents": "read"}


def test_both_jobs_test_python_3_11_and_3_14():
    for job in (API_JOB, FULL_JOB):
        assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.14"]
        assert job["strategy"]["fail-fast"] is False


def test_both_jobs_install_the_real_dev_and_api_extras():
    for job in (API_JOB, FULL_JOB):
        assert 'pip install -e ".[dev,api]"' in steps_text(job)
    assert "torch --index-url https://download.pytorch.org/whl/cpu" in steps_text(API_JOB)     # CPU wheels, not CUDA


def test_the_api_job_fails_on_any_skip_and_on_a_missing_dependency():
    assert API_JOB["env"]["DOC_FAIL_ON_SKIP"] == "1"
    text = steps_text(API_JOB)
    assert "scripts/ci/check_api_extra.py" in text                       # the mandatory dependency/import check
    assert "--max-skipped 0" in text and "--min-tests" in text           # and the junit re-check
    assert "continue-on-error" not in str(WORKFLOW)


def test_the_api_job_selects_every_security_relevant_test_module():
    text = steps_text(API_JOB)
    for selection in ("tests/test_api_*.py", "tests/test_tenant_isolation.py", "tests/test_exception_base_identity.py",
                      "tests/test_packaging_consistency.py", "tests/test_ci_policy.py"):
        assert selection in text


def test_the_api_selection_matches_real_files_and_meets_the_minimum_test_count():
    assert list((ROOT / "tests").glob("test_api_*.py"))
    for name in ("test_tenant_isolation.py", "test_exception_base_identity.py", "test_packaging_consistency.py", "test_ci_policy.py"):
        assert (ROOT / "tests" / name).is_file()
    minimum = int(re.search(r"--min-tests (\d+)", steps_text(API_JOB)).group(1))
    assert 100 <= minimum <= 2000


def test_only_live_model_tests_may_skip_in_the_full_job():
    text = steps_text(FULL_JOB)
    allowed = set(re.findall(r"--allow-skip (\S+)", text))
    assert allowed == {"test_live_qwen_smoke_against_local_ollama"}
    assert "DOC_FAIL_ON_SKIP" not in str(FULL_JOB.get("env", {}))       # optional tests are allowed to skip there
    assert not any("api" in a for a in allowed)


def test_actions_are_pinned_to_a_major_version_and_do_not_persist_credentials():
    for job in (API_JOB, FULL_JOB):
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w./-]+@v\d+", step["uses"]), step["uses"]
        checkout = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout"))
        assert checkout["with"]["persist-credentials"] is False


def test_the_helper_scripts_the_workflow_calls_exist():
    for script in re.findall(r"scripts/ci/\w+\.py", steps_text(API_JOB) + steps_text(FULL_JOB)):
        assert (ROOT / script).is_file(), script
