#!/usr/bin/env python3
"""
Mandatory API dependency and import check.

Verifies that the optional ``api`` extra (and ``httpx`` from ``dev``) is REALLY installed: every requirement
declared in ``pyproject.toml`` is present at a version its specifier allows, its module imports, and the API
package imports and answers its health endpoint. Exit status 1 and a list of problems otherwise.

Run it right after ``pip install -e ".[dev,api]"``; it is also exercised by ``tests/test_api_extra_installed.py``.
"""

import importlib
import importlib.metadata as metadata
import importlib.util
import sys
from pathlib import Path
from typing import List

try:
    import tomllib
except ImportError:                                      # Python 3.10
    import tomli as tomllib                              # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[2]
# distribution name -> candidate import names (python-multipart changed its module name over time)
IMPORT_NAMES = {
    "fastapi": ["fastapi"],
    "starlette": ["starlette"],
    "anyio": ["anyio"],
    "uvicorn": ["uvicorn"],
    "python-multipart": ["python_multipart", "multipart"],
    "httpx": ["httpx"],
}


def declared_requirements(root: Path = ROOT) -> List[str]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    wanted = list(extras["api"]) + [d for d in extras["dev"] if d.lower().startswith("httpx")]
    return wanted


def check(root: Path = ROOT) -> List[str]:
    from packaging.requirements import Requirement

    problems: List[str] = []
    for spec in declared_requirements(root):
        requirement = Requirement(spec)
        name = requirement.name.lower().replace("_", "-")
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            problems.append(f"{name}: not installed (declared as {spec!r})")
            continue
        if not requirement.specifier.contains(installed, prereleases=True):
            problems.append(f"{name}: installed {installed} does not satisfy {spec!r}")
        if not any(importlib.util.find_spec(m) for m in IMPORT_NAMES.get(name, [name.replace("-", "_")])):
            problems.append(f"{name}: installed but no importable module among {IMPORT_NAMES.get(name)}")
    if problems:
        return problems
    try:
        for module in ("doc_extractor.api", "doc_extractor.api.app", "doc_extractor.api.dependencies", "doc_extractor.api.exceptions"):
            importlib.import_module(module)
        from fastapi.testclient import TestClient
        from doc_extractor.api.app import create_app
        from doc_extractor.api.config import ApiSettings

        response = TestClient(create_app(ApiSettings(app_env="development"))).get("/api/v1/health")
        if response.status_code != 200:
            problems.append(f"GET /api/v1/health answered {response.status_code}")
    except Exception as exc:                              # noqa: BLE001 - report any failure as a problem
        problems.append(f"API import/health check failed: {type(exc).__name__}: {exc}")
    return problems


def main() -> int:
    problems = check()
    if problems:
        print("API extra check FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    versions = ", ".join(f"{Requirement_name}=={metadata.version(Requirement_name)}" for Requirement_name in
                         ("fastapi", "starlette", "anyio", "uvicorn", "python-multipart", "httpx"))
    print(f"API extra check OK on Python {sys.version.split()[0]}: {versions}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
