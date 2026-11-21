"""
The mandatory-CI machinery itself: the skip policy (``DOC_FAIL_ON_SKIP=1``) and ``scripts/ci/assert_junit.py``.
Needs no FastAPI, so it always runs. The policy is proven in a subprocess because it changes how pytest itself
reports skips.
"""

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(script: str):
    spec = importlib.util.spec_from_file_location(script.replace(".py", ""), ROOT / "scripts" / "ci" / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assert_junit = _load("assert_junit.py")


def run_pytest(tmp_path, source: str, *, strict: bool, plugin: str = "tests.conftest"):
    """Run a throw-away test file in a fresh pytest that uses the repository's conftest hooks."""
    target = tmp_path / "test_probe.py"
    target.write_text(textwrap.dedent(source))
    env = {k: v for k, v in os.environ.items() if k != "DOC_FAIL_ON_SKIP"}
    env.update(PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    if strict:
        env["DOC_FAIL_ON_SKIP"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", plugin, "-p", "no:cacheprovider", "-o", "addopts=", "-q", "-rs", str(target)],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )


SKIPPING_TEST = "import pytest\ndef test_a():\n    pytest.skip('optional thing missing')\n"
MARKED_SKIP = "import pytest\n@pytest.mark.skip(reason='marked')\ndef test_a():\n    pass\n"
SKIPIF = "import pytest\n@pytest.mark.skipif(True, reason='conditional')\ndef test_a():\n    pass\n"
MODULE_IMPORTORSKIP = "import pytest\npytest.importorskip('no_such_module_xyz', reason='dependency missing')\ndef test_a():\n    pass\n"
FIXTURE_IMPORTORSKIP = "import pytest\n@pytest.fixture\ndef dep():\n    return pytest.importorskip('no_such_module_xyz')\ndef test_a(dep):\n    pass\n"
XFAIL = "import pytest\n@pytest.mark.xfail(reason='known')\ndef test_a():\n    assert False\n"
PASSING = "def test_a():\n    assert True\n"


@pytest.mark.parametrize("source", [SKIPPING_TEST, MARKED_SKIP, SKIPIF, MODULE_IMPORTORSKIP, FIXTURE_IMPORTORSKIP], ids=[
    "pytest.skip", "mark.skip", "mark.skipif", "module importorskip", "fixture importorskip"])
def test_by_default_optional_tests_still_skip_and_the_run_is_green(tmp_path, source):
    result = run_pytest(tmp_path, source, strict=False)
    # 0 = green; 5 = pytest's own "no tests collected" when the only module skips at collection. Never 1 (failed) or 2 (error).
    assert result.returncode in (0, 5), result.stdout[-600:]
    assert "skipped" in result.stdout and "failed" not in result.stdout and "error" not in result.stdout.lower().replace("errors", "")


@pytest.mark.parametrize("source", [SKIPPING_TEST, MARKED_SKIP, SKIPIF, MODULE_IMPORTORSKIP, FIXTURE_IMPORTORSKIP], ids=[
    "pytest.skip", "mark.skip", "mark.skipif", "module importorskip", "fixture importorskip"])
def test_with_doc_fail_on_skip_every_kind_of_skip_fails_the_run(tmp_path, source):
    result = run_pytest(tmp_path, source, strict=True)
    assert result.returncode != 0, result.stdout[-600:]
    assert "DOC_FAIL_ON_SKIP=1" in result.stdout and "Original skip reason" in result.stdout
    assert "1 skipped" not in result.stdout


def test_a_missing_dependency_cannot_turn_a_strict_job_green_by_skipping_at_collection(tmp_path):
    result = run_pytest(tmp_path, MODULE_IMPORTORSKIP, strict=True)
    assert result.returncode == 2                                    # collection error: pytest interrupted
    assert "dependency missing" in result.stdout


def test_xfail_and_passing_tests_are_not_affected_by_the_strict_policy(tmp_path):
    assert run_pytest(tmp_path, XFAIL, strict=True).returncode == 0
    ok = run_pytest(tmp_path, PASSING, strict=True)
    assert ok.returncode == 0 and "1 passed" in ok.stdout


def test_the_repository_conftest_exposes_the_policy_hooks():
    import tests.conftest as conftest
    import tests.skip_policy as policy
    assert conftest.pytest_runtest_makereport is policy.pytest_runtest_makereport
    assert conftest.pytest_make_collect_report is policy.pytest_make_collect_report


def test_the_policy_reads_the_environment_when_it_runs(monkeypatch):
    import tests.skip_policy as policy
    monkeypatch.delenv("DOC_FAIL_ON_SKIP", raising=False)
    assert policy.strict() is False
    monkeypatch.setenv("DOC_FAIL_ON_SKIP", "1")
    assert policy.strict() is True
    monkeypatch.setenv("DOC_FAIL_ON_SKIP", "0")
    assert policy.strict() is False


# -- scripts/ci/assert_junit.py -------------------------------------------------------------------

def _junit(path, *, tests=10, failures=0, errors=0, skipped=()):
    cases = "".join(f'<testcase classname="{c}" name="{n}"><skipped message="why"/></testcase>' for c, n in skipped)
    path.write_text(
        f'<testsuites><testsuite name="pytest" tests="{tests}" failures="{failures}" errors="{errors}" '
        f'skipped="{len(skipped)}">{cases}<testcase classname="t" name="ok"/></testsuite></testsuites>'
    )
    return str(path)


def test_junit_passes_a_clean_report(tmp_path):
    problems, summary = assert_junit.evaluate(_junit(tmp_path / "r.xml"), max_skipped=0, min_tests=5)
    assert problems == [] and "10 tests" in summary


def test_junit_fails_on_any_skip_when_none_are_allowed(tmp_path):
    report = _junit(tmp_path / "r.xml", skipped=[("tests.test_api_x", "test_a")])
    problems, _ = assert_junit.evaluate(report, max_skipped=0)
    assert any("1 skipped" in p for p in problems)


def test_junit_fails_on_failures_and_errors(tmp_path):
    problems, _ = assert_junit.evaluate(_junit(tmp_path / "r.xml", failures=1, errors=2))
    assert any("1 failure(s) and 2 error(s)" in p for p in problems)


def test_junit_fails_when_too_few_tests_ran(tmp_path):
    problems, _ = assert_junit.evaluate(_junit(tmp_path / "r.xml", tests=3), min_tests=300)
    assert any("only 3 tests ran" in p for p in problems)


def test_junit_allows_skips_only_for_the_named_optional_components(tmp_path):
    report = _junit(tmp_path / "r.xml", skipped=[("tests.test_ocr_backend_selection", ""), ("tests.test_api_tenant", "test_x")])
    problems, _ = assert_junit.evaluate(report, allow_skip=("test_ocr_backend_selection",))
    assert len(problems) == 1 and "test_api_tenant" in problems[0]


def test_junit_command_line_exit_codes(tmp_path):
    clean, bad = _junit(tmp_path / "a.xml"), _junit(tmp_path / "b.xml", skipped=[("x", "y")])
    script = str(ROOT / "scripts" / "ci" / "assert_junit.py")
    assert subprocess.run([sys.executable, script, clean, "--max-skipped", "0"], capture_output=True).returncode == 0
    assert subprocess.run([sys.executable, script, bad, "--max-skipped", "0"], capture_output=True).returncode == 1
