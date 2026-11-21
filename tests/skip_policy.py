"""
Skip policy for mandatory CI jobs.

By default a test that needs an OPTIONAL component (the ``api`` extra, the OCR extra, a live model server) skips
with a visible reason: a developer without that component still gets a green run of everything else.

In a job that must prove those tests really ran, set ``DOC_FAIL_ON_SKIP=1``: any skip then FAILS, whether it comes
from ``pytest.skip``, ``pytest.mark.skip``/``skipif``, ``pytest.importorskip`` in a fixture, or a module-level
``pytest.importorskip`` during collection (a missing dependency can therefore never turn the job green by skipping).
``xfail`` is not a skip and is left alone.

The hooks are imported into ``tests/conftest.py``; they read the environment when they run, so a subprocess can
switch the policy on.
"""

import os

import pytest

ENV_VAR = "DOC_FAIL_ON_SKIP"


def strict() -> bool:
    return os.environ.get(ENV_VAR) == "1"


def _reason(report) -> str:
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


def _fail(report) -> None:
    reason = _reason(report)
    report.outcome = "failed"
    report.longrepr = f"{ENV_VAR}=1: a skipped test is not allowed in this job. Original skip reason: {reason}"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if strict() and report.skipped and not hasattr(report, "wasxfail"):
        _fail(report)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    report = outcome.get_result()
    if strict() and report.skipped:
        _fail(report)
