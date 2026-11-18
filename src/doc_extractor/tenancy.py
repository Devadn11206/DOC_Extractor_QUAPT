"""
Path-safety guard for tenant identifiers (PRD SEC-006: no cross-tenant access).

A tenant identifier is used as a directory name by the local storage and idempotency stores
(``{base_dir}/{tenant_id}/...``). Whoever supplies it - an authentication provider, a stored key
record, a hand-off payload - must not be able to name a different tenant's directory, for example
``tenant_a/../tenant_b``.

This module does NOT define a tenant-ID format: which identifiers a deployment issues is decided by
identity provisioning. It rejects only values that can leave their own directory, so ordinary
identifiers such as ``tenant_alpha``, ``org:123``, ``Acme Corp`` or ``a..b`` stay valid.
"""

import os
from pathlib import PureWindowsPath
from typing import Optional


def tenant_id_path_problem(tenant_id: object, *, windows: Optional[bool] = None) -> Optional[str]:
    """
    A short machine-readable reason when ``tenant_id`` is unsafe as a directory name, else None.
    The offending value is deliberately not echoed (it may be attacker controlled).

    ``windows`` selects Windows path semantics (a drive prefix such as ``C:`` escapes the base
    directory there); by default it follows the running platform.
    """
    if not isinstance(tenant_id, str):
        return "not_a_string"
    if not tenant_id:
        return "empty"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in tenant_id):
        return "control_character"
    if "/" in tenant_id or "\\" in tenant_id:
        return "path_separator"
    if tenant_id in (".", ".."):
        return "relative_path_component"
    on_windows = os.name == "nt" if windows is None else windows
    if on_windows and PureWindowsPath(tenant_id).drive:
        return "drive_prefix"
    return None


def require_path_safe_tenant_id(tenant_id: object, *, windows: Optional[bool] = None) -> str:
    """Return ``tenant_id`` unchanged when it is path-safe, else raise ``ValueError`` (value not echoed)."""
    problem = tenant_id_path_problem(tenant_id, windows=windows)
    if problem is not None:
        raise ValueError(f"Unsafe tenant identifier ({problem}).")
    return tenant_id  # type: ignore[return-value]
