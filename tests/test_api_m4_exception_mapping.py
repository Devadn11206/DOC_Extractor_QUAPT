"""
Every M4 (schema lifecycle) exception must answer with its correct HTTP status and its EXISTING domain
error code. Before this fix only three classes were mapped and the rest (self-approval, forbidden,
unauthenticated, invalid transition, ...) answered HTTP 500. Public error codes are not renamed here:
that is PRD decision D1.
"""

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[api]')")

import inspect

from fastapi.testclient import TestClient

from doc_extractor.api import exceptions as api_exceptions
from doc_extractor.api.app import create_app
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.exceptions import get_status_code_for_exception
from schema_lifecycle import exceptions as m4


def _all_m4_exception_classes():
    found, stack = [], [m4.SchemaLifecycleError]
    while stack:
        cls = stack.pop()
        found.append(cls)
        stack.extend(cls.__subclasses__())
    return sorted(set(found), key=lambda c: c.__name__)


M4_CLASSES = _all_m4_exception_classes()

# Expected (status, domain code). The 401/403/409 rows are the PRD section 19 names, already emitted by M4.
EXPECTED = {
    "SchemaLifecycleError": (400, "SCHEMA_LIFECYCLE_ERROR"),
    "SchemaValidationError": (422, "SCHEMA_VALIDATION_ERROR"),
    "SchemaSerializationError": (422, "SCHEMA_SERIALIZATION_ERROR"),
    "SchemaVersionError": (422, "SCHEMA_VERSION_ERROR"),
    "InvalidStateTransitionError": (409, "INVALID_STATE_TRANSITION"),
    "SchemaPromotionNotAllowedError": (409, "SCHEMA_PROMOTION_NOT_ALLOWED"),
    "SchemaNotApprovedError": (409, "SCHEMA_NOT_APPROVED"),
    "SchemaNotFoundError": (404, "SCHEMA_NOT_FOUND"),
    "SchemaRollbackError": (422, "SCHEMA_ROLLBACK_ERROR"),
    "TenantAccessError": (403, "TENANT_ACCESS_ERROR"),
    "UnauthenticatedError": (401, "UNAUTHENTICATED"),
    "ForbiddenActionError": (403, "FORBIDDEN"),
    "SchemaSelfApprovalError": (403, "SCHEMA_SELF_APPROVAL"),
    "BenchmarkVerificationError": (422, "HARNESS_RESULT_INVALID"),
}


def _make(cls):
    """Instantiate an M4 exception with a placeholder for each required constructor argument."""
    kwargs = {}
    for name, param in inspect.signature(cls.__init__).parameters.items():
        if name == "self" or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD) or param.default is not param.empty:
            continue
        kwargs[name] = "x"
    return cls(**kwargs)


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = create_app(ApiSettings(app_env="development"))
    for cls in M4_CLASSES:
        def _raiser(cls=cls):
            def route():
                raise _make(cls)
            return route
        app.get(f"/probe/{cls.__name__}")(_raiser())
    return TestClient(app, raise_server_exceptions=False)


def test_the_expectation_table_covers_every_m4_exception_class():
    """When M4 gains an exception this fails until its status is chosen on purpose."""
    assert {c.__name__ for c in M4_CLASSES} == set(EXPECTED)


@pytest.mark.parametrize("cls", M4_CLASSES, ids=lambda c: c.__name__)
def test_every_m4_exception_has_an_explicit_status_not_the_500_fallback(cls):
    assert cls in api_exceptions._STATUS_CODE_MAP, f"{cls.__name__} relies on the fallback"
    assert get_status_code_for_exception(_make(cls)) == EXPECTED[cls.__name__][0] != 500


@pytest.mark.parametrize("cls", M4_CLASSES, ids=lambda c: c.__name__)
def test_every_m4_exception_answers_with_its_status_and_its_own_domain_code(client, cls):
    status, code = EXPECTED[cls.__name__]
    response = client.get(f"/probe/{cls.__name__}")
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code == _make(cls).code           # the existing code, never renamed
    assert set(error) == {"code", "message", "request_id", "details"}
    assert response.headers["X-Request-ID"] == error["request_id"]


def test_a_subclass_is_not_shadowed_by_its_parent():
    assert get_status_code_for_exception(_make(m4.SchemaSelfApprovalError)) == 403   # parent ForbiddenActionError is also 403
    assert get_status_code_for_exception(_make(m4.SchemaPromotionNotAllowedError)) == 409


def test_prd_section_19_rows_are_honoured():
    assert EXPECTED["UnauthenticatedError"] == (401, "UNAUTHENTICATED")
    assert EXPECTED["SchemaSelfApprovalError"] == (403, "SCHEMA_SELF_APPROVAL")
    assert EXPECTED["SchemaNotApprovedError"] == (409, "SCHEMA_NOT_APPROVED")


def test_existing_non_m4_mappings_are_unchanged():
    from doc_extractor.exceptions import (
        MalwareDetectedError, ScannerUnavailableError, StorageError, UnsupportedFileError,
    )
    assert get_status_code_for_exception(UnsupportedFileError("x")) == 400
    assert get_status_code_for_exception(MalwareDetectedError("x")) == 400
    assert get_status_code_for_exception(ScannerUnavailableError("x")) == 503
    assert get_status_code_for_exception(StorageError("x")) == 500
    assert get_status_code_for_exception(ValueError("not a domain error")) == 500


# -- M1: the mapping must not depend on which package is imported first -----------------------------

import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# The six cases that answered 500 INTERNAL_ERROR when schema_lifecycle was imported before doc_extractor.
PREVIOUSLY_FAILING_SIX = {
    "SchemaSelfApprovalError": (403, "SCHEMA_SELF_APPROVAL"),
    "ForbiddenActionError": (403, "FORBIDDEN"),
    "UnauthenticatedError": (401, "UNAUTHENTICATED"),
    "InvalidStateTransitionError": (409, "INVALID_STATE_TRANSITION"),
    "SchemaNotApprovedError": (409, "SCHEMA_NOT_APPROVED"),
    "SchemaNotFoundError": (404, "SCHEMA_NOT_FOUND"),
}

_PROBE = """
import inspect, json
from fastapi.testclient import TestClient
from doc_extractor.api.app import create_app
from doc_extractor.api.config import ApiSettings
from schema_lifecycle import exceptions as m4

def classes():
    out, stack = [], [m4.SchemaLifecycleError]
    while stack:
        c = stack.pop(); out.append(c); stack.extend(c.__subclasses__())
    return sorted(set(out), key=lambda c: c.__name__)

def make(cls):
    kw = {n: "x" for n, p in inspect.signature(cls.__init__).parameters.items()
          if n != "self" and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD) and p.default is p.empty}
    return cls(**kw)

app = create_app(ApiSettings(app_env="development"))
for cls in classes():
    def route(cls=cls):
        def handler(): raise make(cls)
        return handler
    app.get(f"/p/{cls.__name__}")(route())
client = TestClient(app, raise_server_exceptions=False)
print(json.dumps({c.__name__: [client.get(f"/p/{c.__name__}").status_code, client.get(f"/p/{c.__name__}").json()["error"]["code"]] for c in classes()}))
"""

_ORDERS = [
    ("doc_extractor",),
    ("schema_lifecycle",),
    ("schema_lifecycle.exceptions",),
    ("schema_lifecycle.metamodel",),
    ("schema_lifecycle.manager", "doc_extractor.api"),
]


def _probe(order):
    prelude = "".join(f"import {name}\n" for name in order)
    env = {**os.environ, "PYTHONPATH": str(_ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    run = subprocess.run([sys.executable, "-c", prelude + _PROBE], capture_output=True, text=True, env=env, cwd=str(_ROOT))
    assert run.returncode == 0, run.stderr[-800:]
    return {name: tuple(value) for name, value in json.loads(run.stdout.strip().splitlines()[-1]).items()}


@pytest.mark.parametrize("order", _ORDERS, ids=lambda o: ">".join(o))
def test_every_m4_exception_maps_identically_in_every_import_order(order):
    assert _probe(order) == EXPECTED


@pytest.mark.parametrize("order", [("schema_lifecycle",), ("schema_lifecycle.exceptions",)], ids=lambda o: ">".join(o))
def test_the_six_previously_failing_cases_now_return_their_status_and_domain_code(order):
    seen = _probe(order)
    for name, expected in PREVIOUSLY_FAILING_SIX.items():
        assert seen[name] == expected, f"{name}: {seen[name]} != {expected} (schema_lifecycle imported first)"
