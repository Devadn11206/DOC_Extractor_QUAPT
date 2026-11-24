#!/usr/bin/env python3
"""
Production install check (Team Member 3, Phase 2A-7).

Run it in an environment built with ONLY the production extras, for example
``pip install ".[api,security,mongo,queue]"`` (no ``dev``, no ``test``). It verifies that

1. every requirement of the base installation and of the selected extras is installed at an allowed version (a requirement
   whose environment marker does not apply, such as ``bitsandbytes`` on macOS, is skipped);
2. no TEST-ONLY package is installed: ``pytest``, ``pytest-cov`` and ``fakeredis`` must be absent outright, and any other package
   that only the ``dev`` or ``test`` extras declare (``httpx``, ``packaging``, ``tomli``) may be present only because an
   installed non-test-only package REQUIRES it (``httpx`` comes with ``litellm``, ``packaging`` with ``transformers``);
3. the production entry points import, and importing them does not load a test-only module;
4. with ``--require-site-packages``, the first-party packages were imported from the INSTALLED distribution and not from a source
   checkout, which proves that the built package ships every package it needs (use a non-editable install).

Exit status 1 and a list of problems otherwise. The test suite exercises the decision functions with synthetic data and checks
that this script reports the leaks of a test environment; it is meant to run in the dedicated CI job.
"""

import argparse
import importlib
import importlib.metadata as metadata
import importlib.util
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set

try:
    import tomllib
except ImportError:                                      # Python 3.10
    import tomli as tomllib                              # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[2]

# Extras that are part of a production deployment. 'ui' (the demo) is optional and not selected by default.
PRODUCTION_EXTRAS = ("api", "security", "mongo", "queue", "storage", "ui")
DEFAULT_EXTRAS = ("api", "security", "mongo", "queue", "storage")
# The extras that hold test-only packages.
TEST_EXTRAS = ("dev", "test")
# Must be ABSENT from a production environment, whatever else is installed.
ABSOLUTELY_FORBIDDEN = ("pytest", "pytest-cov", "fakeredis")
FORBIDDEN_MODULES = ("pytest", "_pytest", "fakeredis")
# First-party packages shipped by the distribution (src/).
FIRST_PARTY = ("doc_extractor", "schema_lifecycle", "engine_errors")
# The production entry points: imported with only production packages installed.
PRODUCTION_IMPORTS = (
    "doc_extractor",
    "doc_extractor.api",
    "doc_extractor.api.app",
    "doc_extractor.api.providers.api_key_auth",
    "doc_extractor.api.providers.jwt_auth",
    "doc_extractor.distributed_state",
    "doc_extractor.infrastructure.mongo",
    "doc_extractor.infrastructure.mongo.review_tasks",
    "doc_extractor.infrastructure.mongo.api_keys",
    "doc_extractor.infrastructure.mongo.audit",
    "doc_extractor.infrastructure.mongo.processing",
    "doc_extractor.infrastructure.mongo.idempotency",
    "doc_extractor.infrastructure.queue",
    "doc_extractor.infrastructure.queue.rabbitmq",
    "doc_extractor.infrastructure.queue.worker",
    "doc_extractor.infrastructure.storage",
    "doc_extractor.infrastructure.storage.adapter",
    "doc_extractor.infrastructure.storage.envelope",
    "doc_extractor.infrastructure.storage.kms",
    "doc_extractor.infrastructure.audit",
    "doc_extractor.infrastructure.identity",
    "doc_extractor.api.ports.processing",
    "schema_lifecycle",
    "engine_errors",
)


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load_project(root: Path = ROOT) -> dict:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def requirement_name(spec: str) -> str:
    return normalise(re.split(r"[<>=!~\[; ]", spec.strip(), maxsplit=1)[0])


def production_requirements(project: Mapping, extras: Sequence[str] = DEFAULT_EXTRAS) -> List[str]:
    """The base requirements plus those of the selected production extras."""
    declared = project["optional-dependencies"]
    unknown = [e for e in extras if e not in PRODUCTION_EXTRAS or e not in declared]
    if unknown:
        raise ValueError(f"not production extras: {unknown}")
    specs = list(project["dependencies"])
    for extra in extras:
        specs.extend(declared[extra])
    return specs


def declared_only_by_tests(project: Mapping) -> Set[str]:
    """Packages declared by a test extra and by NO production group (the base or any production extra)."""
    declared = project["optional-dependencies"]
    production = {requirement_name(s) for s in project["dependencies"]}
    for extra in PRODUCTION_EXTRAS:
        production |= {requirement_name(s) for s in declared.get(extra, [])}
    tests: Set[str] = set()
    for extra in TEST_EXTRAS:
        tests |= {requirement_name(s) for s in declared.get(extra, [])}
    return tests - production


def unsatisfied_requirements(specs: Iterable[str], installed: Mapping[str, str]) -> List[str]:
    """Requirements (whose marker applies) that are missing from ``installed`` (name -> version) or at a disallowed version."""
    from packaging.requirements import Requirement

    problems: List[str] = []
    for spec in specs:
        requirement = Requirement(spec)
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = normalise(requirement.name)
        if name not in installed:
            problems.append(f"{name}: not installed (declared as {spec!r})")
        elif not requirement.specifier.contains(installed[name], prereleases=True):
            problems.append(f"{name}: installed {installed[name]} does not satisfy {spec!r}")
    return problems


def required_by(installed_requires: Mapping[str, Sequence[str]]) -> Dict[str, Set[str]]:
    """name -> the installed distributions that require it unconditionally (a marker that does not apply is ignored)."""
    from packaging.requirements import Requirement

    dependents: Dict[str, Set[str]] = {}
    for dist, specs in installed_requires.items():
        for spec in specs:
            requirement = Requirement(spec)
            if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
                continue
            dependents.setdefault(normalise(requirement.name), set()).add(dist)
    return dependents


def leaked_test_packages(installed_requires: Mapping[str, Sequence[str]], test_only: Set[str]) -> List[str]:
    """
    Test-only packages that are installed without a production reason. ``installed_requires`` maps every installed distribution
    to its requirement strings. A test-only package is tolerated only when a non-test-only installed package requires it.
    """
    problems: List[str] = []
    dependents = required_by(installed_requires)
    for name in sorted(set(ABSOLUTELY_FORBIDDEN) & set(installed_requires)):
        problems.append(f"{name}: a test-only package is installed in a production environment")
    for name in sorted((test_only - set(ABSOLUTELY_FORBIDDEN)) & set(installed_requires)):
        reasons = {d for d in dependents.get(name, set()) if d not in test_only and d not in ABSOLUTELY_FORBIDDEN}
        if not reasons:
            problems.append(f"{name}: declared only by a test extra and installed, but no production package requires it")
    return problems


def installed_versions() -> Dict[str, str]:
    return {normalise(d.metadata["Name"]): d.version for d in metadata.distributions() if d.metadata["Name"]}


def installed_requirements() -> Dict[str, List[str]]:
    return {normalise(d.metadata["Name"]): list(d.requires or []) for d in metadata.distributions() if d.metadata["Name"]}


def loaded_test_modules(loaded: Optional[Iterable[str]] = None) -> List[str]:
    """The test-only modules among ``loaded`` (default: ``sys.modules``)."""
    names = set(sys.modules if loaded is None else loaded)
    return sorted(m for m in FORBIDDEN_MODULES if m in names)


def import_problems(modules: Sequence[str]) -> List[str]:
    problems: List[str] = []
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception as exc:                          # noqa: BLE001 - reported with its type only
            problems.append(f"import {module}: {type(exc).__name__}: {str(exc)[:160]}")
    loaded = loaded_test_modules()
    if loaded and not problems:
        problems.append(f"importing the production modules loaded test-only modules: {loaded}")
    return problems


def location_problems(packages: Sequence[str]) -> List[str]:
    """First-party packages must come from site-packages (an installed distribution), not from a source checkout."""
    problems: List[str] = []
    for package in packages:
        spec = importlib.util.find_spec(package)
        origin = (spec.origin or "") if spec else ""
        if spec is None:
            problems.append(f"{package}: not importable (the built package does not ship it)")
        elif "site-packages" not in origin and "dist-packages" not in origin:
            problems.append(f"{package}: imported from a source checkout, not an installed distribution")
    return problems


def check(root: Path = ROOT, extras: Sequence[str] = DEFAULT_EXTRAS, require_site_packages: bool = False,
          imports: Optional[Sequence[str]] = PRODUCTION_IMPORTS) -> List[str]:
    project = load_project(root)
    problems = unsatisfied_requirements(production_requirements(project, extras), installed_versions())
    problems += leaked_test_packages(installed_requirements(), declared_only_by_tests(project))
    if require_site_packages:
        problems += location_problems(FIRST_PARTY)
    if imports:
        problems += import_problems(imports)
    return problems


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Check that a production install is complete and free of test-only packages.")
    parser.add_argument("--extras", default=",".join(DEFAULT_EXTRAS), help="production extras that were installed (default: %(default)s)")
    parser.add_argument("--require-site-packages", action="store_true", help="the code must come from an installed (non-editable) distribution")
    args = parser.parse_args(argv)
    extras = tuple(e for e in args.extras.split(",") if e)
    try:
        problems = check(extras=extras, require_site_packages=args.require_site_packages)
    except ValueError as exc:
        print(f"Production install check cannot run: {exc}")
        return 2
    if problems:
        print("Production install check FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"Production install check passed (extras: {', '.join(extras) or 'none'}): requirements satisfied, no test-only package, "
          f"{len(PRODUCTION_IMPORTS)} production modules import cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
