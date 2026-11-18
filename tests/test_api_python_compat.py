"""
The package declares ``requires-python >= 3.10`` but the suite may run on Python 3.14, where annotations
are evaluated lazily. A name used in an annotation but never imported then works on 3.14 and raises
``NameError`` at import time on 3.10-3.13 (this happened: ``-> Any`` without ``import Any``).

These checks do not depend on the interpreter running them:
- a static scan for annotation names that are never bound at runtime;
- a parse with the Python 3.10 grammar;
- a fresh-interpreter import on every older CPython that happens to be available (skipped otherwise).
"""

import ast
import builtins
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "src" / "doc_extractor" / "api"
API_FILES = sorted(API_DIR.rglob("*.py"))


def _type_checking_imports(tree: ast.AST) -> set:
    """Import nodes under ``if TYPE_CHECKING:``: they do not exist at runtime."""
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            name = test.id if isinstance(test, ast.Name) else getattr(test, "attr", None)
            if name == "TYPE_CHECKING":
                for child in node.body:
                    for sub in ast.walk(child):
                        if isinstance(sub, (ast.Import, ast.ImportFrom)):
                            guarded.add(id(sub))
    return guarded


def unbound_annotation_names(source: str):
    """(line, name) for annotation names never bound at runtime, in a module that evaluates annotations eagerly."""
    tree = ast.parse(source)
    if any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    ):
        return []                                    # every annotation is a string there

    guarded = _type_checking_imports(tree)
    bound = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in guarded:
            for alias in node.names:
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)

    annotations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
                if arg is not None and arg.annotation is not None:
                    annotations.append(arg.annotation)
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)

    return sorted({(n.lineno, n.id) for ann in annotations for n in ast.walk(ann)
                   if isinstance(n, ast.Name) and n.id not in bound})


# -- the scanner itself must be able to see the defect it guards against -------------------------

def test_scanner_detects_the_original_missing_any_import():
    broken = "from typing import Callable\n\ndef get_service(request) -> Any:\n    ...\n"
    assert unbound_annotation_names(broken) == [(3, "Any")]


def test_scanner_detects_class_level_and_type_checking_only_names():
    assert unbound_annotation_names("class M:\n    value: Missing = None\n") == [(2, "Missing")]
    guarded = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from x import Only\n\ndef f(a: Only): ...\n"
    assert unbound_annotation_names(guarded) == [(5, "Only")]


def test_scanner_accepts_deferred_annotations_and_bound_names():
    assert unbound_annotation_names("from __future__ import annotations\n\ndef f(a: Anything) -> Whatever: ...\n") == []
    assert unbound_annotation_names("from typing import Any\n\ndef f(a: Any) -> Any: ...\n") == []


# -- the API package ------------------------------------------------------------------------------

def test_api_package_files_were_found():
    assert len(API_FILES) >= 15


@pytest.mark.parametrize("path", API_FILES, ids=lambda p: str(p.relative_to(API_DIR)))
def test_no_annotation_in_the_api_package_relies_on_deferred_evaluation(path):
    assert unbound_annotation_names(path.read_text(encoding="utf-8")) == [], (
        f"{path.name}: annotation names that are never imported work on Python 3.14 only"
    )


@pytest.mark.parametrize("path", API_FILES, ids=lambda p: str(p.relative_to(API_DIR)))
def test_api_package_parses_with_the_python_3_10_grammar(path):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))


def _interpreters():
    found = {sys.executable}
    for minor in (10, 11, 12, 13):
        exe = shutil.which(f"python3.{minor}")
        if exe:
            found.add(exe)
    return sorted(found)


@pytest.mark.parametrize("exe", _interpreters())
def test_api_package_imports_in_a_fresh_interpreter(exe):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    probe = subprocess.run(
        [exe, "-c", "import sys; print(sys.version_info[:2])"], capture_output=True, text=True, env=env
    )
    assert probe.returncode == 0
    run = subprocess.run(
        [exe, "-c", "import doc_extractor.api, doc_extractor.api.dependencies, doc_extractor.api.app"],
        capture_output=True, text=True, env=env, cwd=str(ROOT),
    )
    if run.returncode != 0 and "ModuleNotFoundError" in run.stderr and "doc_extractor" not in run.stderr.split("ModuleNotFoundError")[-1].split("\n")[0]:
        pytest.skip(f"{exe} does not have the API dependencies installed: {run.stderr.strip().splitlines()[-1]}")
    assert run.returncode == 0, run.stderr[-600:]
