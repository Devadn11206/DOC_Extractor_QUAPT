"""
Phase 1 / Task 8A (D3): the live vLLM integration test is opt-in and never silently suppressed.

Runs pytest in a subprocess against the single live test:
- default (RUN_VLLM_TESTS unset)  -> cleanly SKIPPED with the documented reason;
- RUN_VLLM_TESTS=1, server down   -> a genuine FAILURE (connection refused), not a skip or pass;
- the ``live_vllm`` marker is registered (the suite runs with --strict-markers).
No real vLLM server is needed: the enabled run is pointed at a closed local port.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIVE_TEST = "tests/test_local_qwen_integration.py::test_live_qwen_smoke_against_vllm"


def _run(extra_env, *args):
    env = {k: v for k, v in os.environ.items() if k not in ("RUN_VLLM_TESTS", "VLLM_TEXT_API_BASE")}
    env.update(extra_env)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "src"), env.get("PYTHONPATH", "")]))
    return subprocess.run(
        [sys.executable, "-m", "pytest", LIVE_TEST, "-p", "no:cacheprovider", "-p", "no:warnings", "-rs", *args],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
    )


def _summary(proc):
    return {k: int(n) for n, k in re.findall(r"(\d+) (passed|failed|skipped|error)", proc.stdout)}


def test_live_vllm_test_is_skipped_by_default_with_a_clear_reason():
    proc = _run({})
    assert proc.returncode == 0, proc.stdout
    assert _summary(proc) == {"skipped": 1}
    assert "RUN_VLLM_TESTS=1" in proc.stdout


def test_values_other_than_one_do_not_enable_the_live_test():
    proc = _run({"RUN_VLLM_TESTS": "true"})
    assert _summary(proc) == {"skipped": 1}


def test_enabled_live_test_with_unreachable_server_is_a_genuine_failure():
    proc = _run({"RUN_VLLM_TESTS": "1", "VLLM_TEXT_API_BASE": "http://127.0.0.1:9"})
    assert proc.returncode != 0, proc.stdout
    summary = _summary(proc)
    assert summary.get("failed") == 1 and "skipped" not in summary and "passed" not in summary
    assert "LLMExecutionError" in proc.stdout


def test_live_vllm_marker_is_registered_and_selectable():
    proc = _run({}, "-m", "live_vllm", "--strict-markers")
    assert proc.returncode == 0 and _summary(proc) == {"skipped": 1}
    other = _run({}, "-m", "not live_vllm", "--strict-markers")
    assert other.returncode == 5      # nothing selected: the marker really deselects it
