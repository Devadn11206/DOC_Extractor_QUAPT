"""
M1 regression: ``schema_lifecycle.exceptions`` and ``doc_extractor.exceptions`` must share ONE
``DocumentExtractorError`` whatever is imported first.

History: importing ``schema_lifecycle`` before ``doc_extractor`` hit a circular import
(schema_lifecycle.exceptions -> doc_extractor/__init__ -> rule_evaluator -> schema_lifecycle.metamodel ->
schema_lifecycle.exceptions, still half-initialised). A silent ``except ImportError`` then gave M4 a private
copy of the base class, so the REST API's handlers no longer recognised any M4 error.

Every check runs in a FRESH interpreter, because import order cannot be exercised inside a process in which
both packages are already loaded. Needs no FastAPI.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

IMPORT_ORDERS = [
    ("doc_extractor",),                                   # the order the app and the old tests used
    ("schema_lifecycle",),                                # the order that used to fall back silently
    ("schema_lifecycle.exceptions",),
    ("schema_lifecycle.metamodel",),
    ("schema_lifecycle.manager", "doc_extractor"),
    ("doc_extractor.exceptions", "schema_lifecycle"),
    ("engine_errors", "schema_lifecycle", "doc_extractor"),
]


def run_fresh(code: str, *, imports=()):
    """Run ``code`` in a new interpreter after importing ``imports`` in the given order."""
    prelude = "".join(f"import {name}\n" for name in imports)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-c", prelude + code], capture_output=True, text=True, env=env, cwd=str(ROOT))


CHECK_IDENTITY = """
import engine_errors, doc_extractor.exceptions as a, schema_lifecycle.exceptions as m
assert a.DocumentExtractorError is engine_errors.DocumentExtractorError is m.DocumentExtractorError, "three different base classes"
assert issubclass(m.SchemaLifecycleError, a.DocumentExtractorError)
assert issubclass(m.SchemaSelfApprovalError, a.DocumentExtractorError)
assert m.DocumentExtractorError.__module__ == "engine_errors", "a private fallback copy is in use"
print("ok")
"""


@pytest.mark.parametrize("order", IMPORT_ORDERS, ids=lambda o: ">".join(o))
def test_every_import_order_shares_one_base_class(order):
    result = run_fresh(CHECK_IDENTITY, imports=order)
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr[-800:]


def test_the_shared_base_is_a_leaf_that_imports_neither_engine_package():
    result = run_fresh(
        "import sys, engine_errors\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in ('doc_extractor', 'schema_lifecycle'))\n"
        "assert not loaded, loaded\nprint('ok')"
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr[-800:]


def test_importing_schema_lifecycle_first_no_longer_raises_or_swallows_an_import_error():
    """The old code swallowed the circular ImportError; assert the import chain simply succeeds."""
    result = run_fresh(
        "import warnings; warnings.simplefilter('error')\n"
        "import schema_lifecycle, schema_lifecycle.exceptions as m\n"
        "assert not any('fallback' in (c.__doc__ or '').lower() for c in m.SchemaLifecycleError.__mro__), 'fallback base in use'\n"
        "print('ok')"
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr[-800:]


def test_the_envelope_behaviour_of_the_base_class_is_unchanged():
    from doc_extractor.exceptions import DocumentExtractorError, UnsupportedFileError
    err = DocumentExtractorError("boom", code="X_CODE", request_id="r1", details={"a": 1})
    assert err.to_envelope() == {"error": {"code": "X_CODE", "message": "boom", "request_id": "r1", "details": {"a": 1}}}
    assert DocumentExtractorError("m").code == "INTERNAL_ERROR"
    assert UnsupportedFileError("x").code == "UNSUPPORTED_FILE"
