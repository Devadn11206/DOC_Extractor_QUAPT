"""
Tests must not depend on the directory pytest is started from (one ported test opened ``pyproject.toml`` relative to
the working directory and failed with FileNotFoundError anywhere else, for example from an IDE or ``pytest /abs/path``).
Needs no FastAPI, so it always runs.
"""

import re
import subprocess
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# A file name used with open()/Path() as a bare relative literal (anchor on __file__ or tmp_path instead).
RELATIVE_LITERAL = re.compile(r"""(?:open|Path|read_text|read_bytes)\(\s*r?["'](?:\./)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:toml|txt|md|json|yaml|yml|cfg|ini|example)["']""")
RELATIVE_TREE = re.compile(r"""\(\s*r?["'](?:\./)?(?:pyproject|requirements|README|src|docs|scripts|\.github|\.env)[A-Za-z0-9_./-]*["']\s*[,)]""")


def test_no_test_opens_a_file_relative_to_the_working_directory():
    offenders = []
    for path in sorted(TESTS.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if "__file__" in code or "tmp_path" in code or "tmpdir" in code:
                continue
            if RELATIVE_LITERAL.search(code) and ("open(" in code or "read_text(" in code or "Path(" in code):
                offenders.append(f"{path.name}:{number}: {line.strip()[:90]}")
    assert offenders == [], "working-directory dependent file access:\n" + "\n".join(offenders)


def test_the_scanner_would_catch_the_original_offender():
    assert RELATIVE_LITERAL.search('    with open("pyproject.toml", "rb") as f:')
    assert RELATIVE_LITERAL.search("Path('requirements.txt').read_text()")
    assert not RELATIVE_LITERAL.search('open(Path(__file__).resolve().parents[1] / "pyproject.toml", "rb")')


def test_the_packaging_and_configuration_tests_pass_from_another_working_directory(tmp_path):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("DOC_FAIL_ON_SKIP", None)
    selection = [
        "tests/test_packaging_consistency.py",
        "tests/test_ci_workflow.py",
        "tests/test_exception_base_identity.py",
        "tests/test_api_foundation.py::test_runtime_dependencies_exclude_httpx",
    ]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "-q", "--rootdir", str(ROOT), "-c", str(ROOT / "pyproject.toml"),
         *[str(ROOT / item) for item in selection]],
        capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    assert tmp_path.resolve() != ROOT
    assert result.returncode == 0, result.stdout[-800:]
    assert " passed" in result.stdout and "failed" not in result.stdout
