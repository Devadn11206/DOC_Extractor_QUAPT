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
    assert allowed == {"test_live_qwen_smoke_against_local_ollama", "test_mongo_integration", "test_queue_integration", "test_live_services"}
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
    scripts = re.findall(r"scripts/ci/\w+\.py", "\n".join(steps_text(job) for job in JOBS.values()))
    assert scripts
    for script in scripts:
        assert (ROOT / script).is_file(), script


# -- MongoDB integration job (Team Member 3, Phase 2A) ---------------------------------------------------------------------

MONGO_JOB = JOBS["mongo-integration"]


def test_the_mongo_integration_job_runs_a_real_replica_set_and_fails_on_any_skip():
    text = steps_text(MONGO_JOB)
    assert MONGO_JOB["env"]["DOC_FAIL_ON_SKIP"] == "1"
    assert "mongo:8.0" in text and "--replSet rs0" in text and "rs.initiate" in text
    assert "tests/test_mongo_integration.py" in text
    assert "--max-skipped 0" in text and "--min-tests" in text
    assert MONGO_JOB["env"]["DOC_TEST_MONGO_URI"].startswith("mongodb://localhost:27017/") and "replicaSet=rs0" in MONGO_JOB["env"]["DOC_TEST_MONGO_URI"]
    assert MONGO_JOB["env"]["DOC_TEST_MONGO_STANDALONE_URI"].startswith("mongodb://localhost:27018")
    assert MONGO_JOB["strategy"]["matrix"]["python-version"] == ["3.11", "3.14"]


def test_the_mongo_job_expects_every_integration_test_to_run():
    import re as _re

    minimum = int(_re.search(r"--min-tests (\d+)", steps_text(MONGO_JOB)).group(1))
    source = (ROOT / "tests" / "test_mongo_integration.py").read_text(encoding="utf-8")
    assert minimum == len(_re.findall(r"(?m)^def test_", source))


def test_the_test_only_extra_is_installed_wherever_the_tests_need_it_and_never_in_the_dependency_audit():
    for job in (API_JOB, FULL_JOB):
        assert 'pip install -e ".[security,mongo,queue,test]"' in steps_text(job)
    audit = steps_text(JOBS["supply-chain"])
    assert "requirements-mongo.txt" in audit and "requirements-security.txt" in audit and "requirements-queue.txt" in audit
    assert "requirements-test.txt" not in audit


def test_the_mongo_job_installs_the_driver_through_the_mongo_extra():
    assert 'pip install -e ".[dev,api,security,mongo]"' in steps_text(MONGO_JOB)


# -- Dependency groups per job (Team Member 3, Phase 2A-7): production and test-only packages stay separate ------------------

PROD_JOB, LIVE_JOB = JOBS["production-install"], JOBS["live-services"]
_INSTALL = re.compile(r'pip install (-e )?"\.\[([\w,]+)\]"')


def installed_extras(job):
    """The extras a job installs through ``pip install [-e] ".[a,b]"``, and whether any of those installs is editable."""
    groups, editable = set(), False
    for step in job["steps"]:
        for flag, names in _INSTALL.findall(str(step.get("run", ""))):
            groups |= set(names.split(","))
            editable = editable or bool(flag)
    return groups, editable


def test_the_unit_and_regression_jobs_install_the_runner_every_production_extra_and_the_test_only_extra():
    for job in (API_JOB, FULL_JOB):
        assert installed_extras(job)[0] == {"dev", "api", "security", "mongo", "queue", "test"}


def test_the_mongo_job_installs_the_production_extras_it_exercises_but_not_the_test_only_extra():
    """The API package imports redis at import time, so 'security' is required; fakeredis is not, and the production bound is tested."""
    assert installed_extras(MONGO_JOB)[0] == {"dev", "api", "security", "mongo"}


def test_the_production_install_job_installs_only_production_extras_non_editable_and_checks_it_from_outside_the_checkout():
    groups, editable = installed_extras(PROD_JOB)
    assert groups == {"api", "security", "mongo", "queue"} and editable is False
    assert not groups & {"dev", "test"}
    text = steps_text(PROD_JOB)
    assert "scripts/ci/check_production_install.py" in text and "--require-site-packages" in text
    assert '"$RUNNER_TEMP"' in text                                          # not run from the repository root
    assert PROD_JOB["strategy"]["matrix"]["python-version"] == ["3.11", "3.14"] and PROD_JOB["strategy"]["fail-fast"] is False
    assert "pytest" not in text and "fakeredis" not in text and "requirements-test.txt" not in text and "requirements-dev.txt" not in text


def test_the_live_service_job_uses_real_services_the_security_extra_and_no_test_only_extra():
    groups, _ = installed_extras(LIVE_JOB)
    assert groups == {"dev", "security"}                                    # the real redis client, at the production bound
    assert LIVE_JOB["env"]["DOC_FAIL_ON_SKIP"] == "1"
    assert LIVE_JOB["env"]["DOC_TEST_REDIS_URL"].startswith("redis://127.0.0.1:") and LIVE_JOB["env"]["DOC_TEST_CLAMD_ADDRESS"].startswith("127.0.0.1:")
    assert LIVE_JOB["services"]["redis"]["image"].startswith("redis:")
    text = steps_text(LIVE_JOB)
    assert "clamav/clamav:" in text and "zPING" in text                      # clamd is started and awaited
    assert "tests/test_live_services.py" in text and "--max-skipped 0" in text
    assert LIVE_JOB["strategy"]["matrix"]["python-version"] == ["3.11", "3.14"]


def test_the_live_job_expects_every_live_test_to_run():
    minimum = int(re.search(r"--min-tests (\d+)", steps_text(LIVE_JOB)).group(1))
    source = (ROOT / "tests" / "test_live_services.py").read_text(encoding="utf-8")
    assert minimum == len(re.findall(r"(?m)^def test_", source))


def test_only_the_unit_and_regression_jobs_install_the_test_only_extra():
    for name, job in JOBS.items():
        groups, _ = installed_extras(job)
        if "test" in groups:
            assert name in ("api-and-security", "full-suite"), name
        if name in ("production-install", "supply-chain"):
            assert not groups & {"dev", "test"}, name


def test_the_dependency_audit_covers_exactly_the_production_requirement_files():
    audited = set(re.findall(r"-r (requirements[\w-]*\.txt)", steps_text(JOBS["supply-chain"])))
    assert audited == {"requirements.txt", "requirements-api.txt", "requirements-security.txt", "requirements-mongo.txt", "requirements-queue.txt"}


def test_every_job_installs_cpu_only_torch_before_the_project():
    """The multi-GB CUDA wheels must never be pulled by 'accelerate': the CPU build goes first in every job that installs the project."""
    for name, job in JOBS.items():
        if installed_extras(job)[0] or name == "full-suite":
            runs = [str(step.get("run", "")) for step in job["steps"]]
            torch_at = next(i for i, run in enumerate(runs) if "download.pytorch.org/whl/cpu" in run)
            project_at = next(i for i, run in enumerate(runs) if _INSTALL.search(run))
            assert torch_at < project_at, name


# -- Durable queue integration job (Team Member 3, Phase 3) ---------------------------------------------------------------------

QUEUE_JOB = JOBS["queue-integration"]


def test_the_queue_integration_job_runs_a_real_broker_database_and_redis_and_fails_on_any_skip():
    text = steps_text(QUEUE_JOB)
    assert QUEUE_JOB["env"]["DOC_FAIL_ON_SKIP"] == "1"
    assert "rabbitmq:4.0" in text and "redis:7" in text and "mongo:8.0" in text and "--replSet rs0" in text and "rs.initiate" in text
    assert "tests/test_queue_integration.py" in text and "--max-skipped 0" in text and "--min-tests" in text
    env = QUEUE_JOB["env"]
    assert env["DOC_TEST_MONGO_URI"].startswith("mongodb://localhost:27017/") and env["DOC_TEST_RABBITMQ_URL"].startswith("amqp://")
    assert env["DOC_TEST_REDIS_URL"].startswith("redis://")
    assert QUEUE_JOB["strategy"]["matrix"]["python-version"] == ["3.11", "3.14"]


def test_the_queue_job_expects_every_integration_test_to_run():
    minimum = int(re.search(r"--min-tests (\d+)", steps_text(QUEUE_JOB)).group(1))
    source = (ROOT / "tests" / "test_queue_integration.py").read_text(encoding="utf-8")
    assert minimum == len(re.findall(r"(?m)^def test_", source))


def test_the_queue_job_installs_the_production_extras_it_exercises_but_not_the_test_only_extra():
    assert installed_extras(QUEUE_JOB)[0] == {"dev", "api", "security", "mongo", "queue"}
