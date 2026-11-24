"""
The production install check (``scripts/ci/check_production_install.py``, Team Member 3, Phase 2A-7).

The script runs in the dedicated CI job against an environment built with ONLY the production extras. Its decisions are pure
functions of the installed metadata, so they are tested here with synthetic environments; and because this test run IS a test
environment (pytest is installed), the real check must report exactly the leaks a production install would not have.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytest.importorskip("packaging", reason="the check uses packaging, which the 'test' extra declares")


def _load():
    spec = importlib.util.spec_from_file_location("check_production_install", ROOT / "scripts" / "ci" / "check_production_install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load()
PROJECT = check.load_project(ROOT)


# ---- which packages are test-only ----------------------------------------------------------------------------------------

def test_the_test_only_packages_are_those_declared_by_a_test_extra_and_no_production_group():
    names = check.declared_only_by_tests(PROJECT)
    assert {"pytest", "pytest-cov", "fakeredis", "httpx", "packaging"} <= names
    assert "redis" not in names                                               # shared: production declares it too
    assert not names & {"pydantic", "fastapi", "pymongo", "pyjwt", "cryptography", "gradio"}
    assert set(check.ABSOLUTELY_FORBIDDEN) <= names


def test_the_production_requirements_are_the_base_plus_the_selected_extras_only():
    specs = check.production_requirements(PROJECT, ("api", "security", "mongo", "queue"))
    names = {check.requirement_name(s) for s in specs}
    assert {"pydantic", "fastapi", "pyjwt", "redis", "pymongo", "aio-pika", "transformers"} <= names
    assert not names & {"pytest", "pytest-cov", "fakeredis", "httpx", "gradio"}
    assert "gradio" in {check.requirement_name(s) for s in check.production_requirements(PROJECT, ("ui",))}


@pytest.mark.parametrize("extras", [("dev",), ("test",), ("api", "test"), ("nonexistent",)])
def test_a_test_or_unknown_extra_is_refused_as_a_production_selection(extras):
    with pytest.raises(ValueError):
        check.production_requirements(PROJECT, extras)


def test_main_reports_a_refused_selection_without_a_traceback(capsys):
    assert check.main(["--extras", "api,test"]) == 2
    assert "cannot run" in capsys.readouterr().out


# ---- the leak decision, on synthetic environments ------------------------------------------------------------------------

BASE_ENV = {
    "litellm": ["httpx>=0.23", "pydantic"], "httpx": ["anyio"], "anyio": [], "pydantic": [], "transformers": ["packaging>=20"],
    "packaging": [], "redis": [],
}
TEST_ONLY = {"pytest", "pytest-cov", "fakeredis", "httpx", "packaging", "tomli"}


def test_a_clean_production_environment_has_no_leak_even_though_httpx_and_packaging_are_installed():
    """httpx and packaging are declared by the test extras but are REQUIRED by production packages: they are allowed."""
    assert check.leaked_test_packages(BASE_ENV, TEST_ONLY) == []


@pytest.mark.parametrize("name", ["pytest", "pytest-cov", "fakeredis"])
def test_the_forbidden_packages_are_a_leak_whatever_requires_them(name):
    environment = {**BASE_ENV, name: [], "something": [f"{name}>=1"]}
    problems = check.leaked_test_packages(environment, TEST_ONLY)
    assert any(problem.startswith(f"{name}:") for problem in problems)


def test_httpx_without_a_production_reason_is_a_leak():
    environment = {"pydantic": [], "httpx": ["anyio"], "anyio": []}
    assert any(p.startswith("httpx:") for p in check.leaked_test_packages(environment, TEST_ONLY))


def test_a_requirement_behind_an_extra_or_a_marker_that_does_not_apply_is_not_a_reason():
    environment = {"litellm": ["httpx>=0.23; extra == 'proxy'", "tomli; python_version < '2.0'"], "httpx": [], "tomli": []}
    problems = check.leaked_test_packages(environment, TEST_ONLY)
    assert any(p.startswith("httpx:") for p in problems) and any(p.startswith("tomli:") for p in problems)


def test_a_test_only_dependent_is_not_a_reason_for_another_test_only_package():
    environment = {"pytest-cov": ["httpx"], "httpx": []}
    problems = check.leaked_test_packages(environment, TEST_ONLY)
    assert any(p.startswith("httpx:") for p in problems) and any(p.startswith("pytest-cov:") for p in problems)


def test_names_are_compared_without_case_or_separator_differences():
    assert check.normalise("PyJWT") == "pyjwt" and check.normalise("python_multipart") == "python-multipart"
    assert check.normalise("Zope.Interface") == "zope-interface"
    assert check.leaked_test_packages({"pytest-cov": []}, TEST_ONLY)


# ---- requirements and imports ---------------------------------------------------------------------------------------------

def test_unsatisfied_requirements_report_a_missing_package_and_a_version_below_the_floor():
    problems = check.unsatisfied_requirements(["alpha>=2.0", "beta>=1.0", "gamma==1.0"], {"alpha": "1.5", "gamma": "1.0"})
    assert len(problems) == 2
    assert any(p.startswith("alpha: installed 1.5") for p in problems) and any(p.startswith("beta: not installed") for p in problems)


def test_a_requirement_whose_marker_does_not_apply_is_skipped():
    assert check.unsatisfied_requirements(["bitsandbytes>=0.43; sys_platform == 'no-such-platform'"], {}) == []
    assert check.unsatisfied_requirements(["alpha>=1; python_version >= '3.11'"], {"alpha": "1.0"}) == []


def test_only_test_modules_count_as_loaded_test_modules():
    assert check.loaded_test_modules(["json", "doc_extractor", "pydantic"]) == []
    assert check.loaded_test_modules(["json", "_pytest", "fakeredis"]) == ["_pytest", "fakeredis"]


def test_an_import_failure_is_reported_by_type_not_raised():
    problems = check.import_problems(["json", "no_such_module_for_the_check"])
    assert len(problems) == 1 and "no_such_module_for_the_check" in problems[0] and "ModuleNotFoundError" in problems[0]


def test_the_production_entry_points_named_by_the_check_exist_in_src():
    for module in check.PRODUCTION_IMPORTS:
        relative = Path(*module.split("."))
        assert (ROOT / "src" / relative).with_suffix(".py").is_file() or (ROOT / "src" / relative / "__init__.py").is_file(), module


def test_the_location_check_refuses_a_source_checkout_and_a_missing_package():
    """These tests import the first-party code from src/, which is exactly what a production install must NOT do."""
    problems = check.location_problems(["doc_extractor", "no_such_first_party_package"])
    assert any(p.startswith("doc_extractor:") and "source checkout" in p for p in problems)
    assert any(p.startswith("no_such_first_party_package:") and "not importable" in p for p in problems)


# ---- the real check, run in this TEST environment --------------------------------------------------------------------------

def test_the_real_check_reports_the_test_packages_that_this_environment_has_and_a_production_one_would_not():
    problems = check.check(ROOT, imports=None)
    leaked = {p.split(":")[0] for p in problems if "test-only package is installed" in p or "no production package requires" in p}
    assert "pytest" in leaked                                                  # pytest is running these very tests


def test_the_check_script_exits_non_zero_here_and_names_the_leak(capsys):
    assert check.main(["--extras", "api,security,mongo,queue"]) == 1
    output = capsys.readouterr().out
    assert "Production install check FAILED" in output and "pytest" in output
