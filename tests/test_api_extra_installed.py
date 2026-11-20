"""
The ``api`` extra is really installed and importable (the mandatory dependency and import check).

Without FastAPI this SKIPS (the extra is optional for a developer); in a ``DOC_FAIL_ON_SKIP=1`` job it FAILS, so
the dedicated API job cannot pass with the dependency missing. The same check runs as a CI step via
``scripts/ci/check_api_extra.py``.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("check_api_extra", ROOT / "scripts" / "ci" / "check_api_extra.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_check_targets_the_whole_api_extra_and_httpx():
    names = {r.split(">")[0].split("<")[0].split("=")[0].strip().lower() for r in _script().declared_requirements()}
    assert names == {"fastapi", "starlette", "anyio", "uvicorn", "python-multipart", "httpx"}


def test_the_api_extra_is_installed_importable_and_the_app_answers_health():
    if importlib.util.find_spec("fastapi") is None:
        pytest.skip("optional API extra not installed (pip install -e '.[dev,api]')")
    assert _script().check() == []


def test_the_check_reports_a_missing_dependency_instead_of_passing(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n[project.optional-dependencies]\n'
        'api = ["no-such-distribution-xyz>=1.0"]\ndev = ["httpx>=0.24.0"]\n'
    )
    problems = _script().check(tmp_path)
    assert any("no-such-distribution-xyz: not installed" in p for p in problems)


def test_the_check_reports_a_version_below_the_declared_floor(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n[project.optional-dependencies]\n'
        'api = ["pytest>=999.0"]\ndev = ["httpx>=0.24.0"]\n'
    )
    assert any("pytest: installed" in p and "does not satisfy" in p for p in _script().check(tmp_path))
