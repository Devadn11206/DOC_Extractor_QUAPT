"""
The M4 roles are imported directly (review finding R4).

``doc_extractor.api.ports.authentication`` used to wrap the import of ``schema_lifecycle.roles`` in ``except ImportError``
and fall back to ``None``. M4 ships in this same distribution, so a failure there is a packaging or import-order bug; swallowing
it silently dropped every role-derived permission (the exact pattern behind the M1 import-order defect). It must now fail
loudly, in any import order.
"""

import ast
import subprocess
import sys
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[dev,api]')")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SAME_DISTRIBUTION = ("schema_lifecycle", "doc_extractor", "engine_errors")


def swallowed_internal_imports(source: str):
    """(line, module) of imports of this distribution inside a ``try`` whose handlers catch ImportError."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Try):
            continue
        catches = any(
            h.type is None
            or any(isinstance(n, ast.Name) and n.id in ("ImportError", "ModuleNotFoundError", "Exception") for n in ast.walk(h.type))
            for h in node.handlers
        )
        if not catches:
            continue
        for stmt in node.body:
            for sub in ast.walk(stmt):
                module = sub.module if isinstance(sub, ast.ImportFrom) else None
                names = [module] if module else ([a.name for a in sub.names] if isinstance(sub, ast.Import) else [])
                for name in names:
                    if name and name.split(".")[0] in SAME_DISTRIBUTION:
                        found.append((sub.lineno, name))
    return found


def test_the_scanner_sees_the_original_pattern():
    old = "try:\n    from schema_lifecycle.roles import Role as M4Role\nexcept ImportError:  # pragma: no cover\n    M4Role = None\n"
    assert swallowed_internal_imports(old) == [(2, "schema_lifecycle.roles")]
    assert swallowed_internal_imports("try:\n    import gradio\nexcept ImportError:\n    gradio = None\n") == []   # a third-party optional is fine
    assert swallowed_internal_imports("from schema_lifecycle.roles import Role\n") == []


def test_no_module_of_this_distribution_swallows_the_import_of_another():
    offenders = {}
    for path in sorted(SRC.rglob("*.py")):
        hits = swallowed_internal_imports(path.read_text(encoding="utf-8"))
        if hits:
            offenders[str(path.relative_to(ROOT))] = hits
    assert offenders == {}, f"silent fallback around an import of this distribution: {offenders}"


def test_the_m4_names_are_bound_and_drive_role_permissions():
    from doc_extractor.api.ports import authentication as auth
    from schema_lifecycle.roles import ROLE_PERMISSIONS, AuthenticatedPrincipal, Role, SchemaPermission

    assert auth.M4Principal is AuthenticatedPrincipal and auth.M4Role is Role
    assert auth.M4_ROLE_PERMISSIONS is ROLE_PERMISSIONS and auth.M4Permission is SchemaPermission
    author = auth.ApiPrincipal(principal_id="u", tenant_id="tenant_a", roles={"schema_author"})
    assert author.has_permission("schema:create") and not author.has_permission("schema:approve")
    assert isinstance(author.to_m4_principal(), AuthenticatedPrincipal)


@pytest.mark.parametrize("order", [
    ["doc_extractor.api.ports.authentication"],
    ["schema_lifecycle", "doc_extractor.api.ports.authentication"],
    ["schema_lifecycle.roles", "doc_extractor.api"],
    ["doc_extractor.exceptions", "doc_extractor.api.ports.authentication"],
])
def test_the_roles_are_bound_in_every_import_order_in_a_fresh_interpreter(order):
    code = (
        "import importlib, sys\n"
        f"for m in {order!r}: importlib.import_module(m)\n"
        "from doc_extractor.api.ports import authentication as a\n"
        "assert a.M4Role is not None and a.M4Principal is not None and a.M4_ROLE_PERMISSIONS and a.M4Permission is not None\n"
        "p = a.ApiPrincipal(principal_id='u', tenant_id='tenant_a', roles={'schema_author'})\n"
        "assert p.has_permission('schema:create')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert run.returncode == 0, run.stderr[-600:]


def test_a_broken_m4_import_fails_loudly_instead_of_silently_dropping_permissions():
    code = (
        "import sys\n"
        "from importlib.abc import MetaPathFinder\n"
        "class Block(MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'schema_lifecycle.roles':\n"
        "            raise ImportError('simulated broken import')\n"
        "sys.meta_path.insert(0, Block())\n"
        "try:\n"
        "    import doc_extractor.api.ports.authentication\n"
        "except ImportError as exc:\n"
        "    print('LOUD', exc)\n"
        "    raise SystemExit(0)\n"
        "print('SILENT')\n"
        "raise SystemExit(3)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert run.returncode == 0 and "LOUD" in run.stdout, run.stdout + run.stderr[-400:]
