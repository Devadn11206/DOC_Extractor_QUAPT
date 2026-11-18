"""
Tenant isolation for local storage (PRD SEC-006: no cross-tenant access).

A tenant identifier decides which directory a tenant's documents and idempotency records live in. This module
makes that mapping collision-proof and verifiable, WITHOUT defining a tenant-ID format and WITHOUT silently
case-folding or normalizing an identity (that policy belongs to identity provisioning, coordinated with
Team Member 3). Three layers:

1. ``require_path_safe_tenant_id``: rejects identifiers that can leave their own directory (separators,
   ``.``/``..``, control characters, a Windows drive prefix), that are not valid Unicode, or that exceed
   ``MAX_TENANT_ID_BYTES``. Typed ``InvalidTenantIdentifierError`` (a ``ValueError``); the value is never echoed.
2. ``tenant_storage_key``: a deterministic, filesystem-safe directory name derived from the EXACT identity.
   An identifier made only of ``[a-z0-9_-]`` (characters no filesystem folds or normalizes) keeps its own
   name, so existing data stays where it is. Any other identifier (upper case, Unicode, ``:``, spaces, ...)
   maps to ``h+<32 hex of SHA-256>``, which is case-stable, fixed-length, and can never equal a plain key
   because plain keys contain no ``+``. Two different identities therefore never share a key.
3. ``TenantDirectoryResolver``: turns the key into a verified directory. A NEW directory carries an identity
   marker (the exact identifier, written atomically) that is checked on every access. A legacy directory
   named exactly like the identifier is used as it is and never modified or migrated. A case/Unicode alias of
   an existing different directory is refused, detected from real filesystem behaviour (so a case-sensitive
   filesystem is not penalised). Symlinked tenant directories are refused.

``ensure_inside_tenant`` additionally refuses any path below the tenant directory that passes through a
symlink or resolves outside it.

Residual risk (documented, not solved here): a process that can write to the storage root can race the checks
between verification and use (TOCTOU). Prevent that with filesystem permissions and per-tenant encryption keys
(SEC-005); it is infrastructure owned by Team Member 3.
"""

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path, PureWindowsPath
from typing import Dict, Optional, Set

from doc_extractor.exceptions import (
    InvalidTenantIdentifierError,
    TenantStorageIsolationError,
    TenantStorageUnavailableError,
)

# A safety bound, not a format: the common limit of one path component, in UTF-8 bytes.
MAX_TENANT_ID_BYTES = 255
HASHED_KEY_PREFIX = "h+"
IDENTITY_MARKER = ".tenant-identity.json"

# Identifiers made only of these characters are their own key: nothing folds or normalizes them.
_PLAIN_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


def tenant_id_path_problem(tenant_id: object, *, windows: Optional[bool] = None) -> Optional[str]:
    """
    A short machine-readable reason when ``tenant_id`` cannot be an isolation boundary, else None.
    The offending value is deliberately not echoed (it may be attacker controlled).

    ``windows`` selects Windows path semantics (a drive prefix such as ``C:`` escapes the base directory
    there); by default it follows the running platform.
    """
    if not isinstance(tenant_id, str):
        return "not_a_string"
    if not tenant_id:
        return "empty"
    try:
        encoded = tenant_id.encode("utf-8")
    except UnicodeEncodeError:
        return "invalid_unicode"
    if len(encoded) > MAX_TENANT_ID_BYTES:
        return "too_long"
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
    """Return ``tenant_id`` unchanged when usable, else raise ``InvalidTenantIdentifierError`` (a ``ValueError``)."""
    problem = tenant_id_path_problem(tenant_id, windows=windows)
    if problem is not None:
        raise InvalidTenantIdentifierError(problem)
    return tenant_id  # type: ignore[return-value]


def tenant_storage_key(tenant_id: str) -> str:
    """The deterministic directory name for the EXACT tenant identity (no case folding, no normalization)."""
    require_path_safe_tenant_id(tenant_id)
    if _PLAIN_KEY.fullmatch(tenant_id) and tenant_id not in _WINDOWS_DEVICE_NAMES:
        return tenant_id
    return HASHED_KEY_PREFIX + hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:32]


def _isolation(reason: str) -> TenantStorageIsolationError:
    return TenantStorageIsolationError(reason)


def _unavailable(reason: str) -> TenantStorageUnavailableError:
    return TenantStorageUnavailableError(reason)


class TenantDirectoryResolver:
    """Maps a tenant identity to its verified directory under ``base_dir`` (see the module docstring)."""

    def __init__(self, base_dir) -> None:
        self.base_dir = Path(base_dir).resolve()
        self._names: Dict[str, str] = {}      # tenant_id -> directory NAME decided once; every use is re-verified

    # -- public ---------------------------------------------------------------------------------------
    def resolve(self, tenant_id: str, *, create: bool = False) -> Path:
        """The tenant directory. READS never create anything: without ``create`` a missing directory is just returned
        as the path where it will be. Only an explicit write/provisioning call passes ``create=True``, and then the
        directory is created marker first. A filesystem failure is a typed ``TenantStorageUnavailableError``."""
        require_path_safe_tenant_id(tenant_id)
        name = self._names.get(tenant_id)
        if name is None:
            name = self._decide(tenant_id)
            self._names[tenant_id] = name
        path = self.base_dir / name
        if os.path.lexists(path):
            self._verify_existing(path, tenant_id, name)
        elif create:
            self._create(path, tenant_id, name)
        return path

    # -- decision -------------------------------------------------------------------------------------
    def _entries(self) -> Set[str]:
        try:
            with os.scandir(self.base_dir) as it:
                return {entry.name for entry in it}
        except FileNotFoundError:
            return set()
        except OSError as exc:                                   # for example PermissionError on an unreadable root
            raise _unavailable("storage_root_unreadable") from exc

    def _decide(self, tenant_id: str) -> str:
        key = tenant_storage_key(tenant_id)
        entries = self._entries()
        if key == tenant_id:                                   # plain key: the directory is named exactly like the tenant
            if key not in entries and os.path.lexists(self.base_dir / key):
                # The name exists although no entry is spelled exactly like it: a case/Unicode alias of
                # ANOTHER directory on a case- or normalization-insensitive filesystem. Never share it.
                raise _isolation("directory_alias_collision")
            return key
        legacy_ok = (
            tenant_id in entries
            and not tenant_id.startswith((HASHED_KEY_PREFIX, "."))     # never adopt another tenant's hashed dir or a temp dir
            and os.path.isdir(self.base_dir / tenant_id)
            and not os.path.islink(self.base_dir / tenant_id)
        )
        return tenant_id if legacy_ok else key                  # existing data is used where it already is

    # -- verification / creation ----------------------------------------------------------------------
    def _verify_existing(self, path: Path, tenant_id: str, name: str) -> None:
        if os.path.islink(path):
            raise _isolation("tenant_directory_is_a_symlink")
        if not os.path.isdir(path):
            raise _isolation("tenant_path_is_not_a_directory")
        if Path(os.path.realpath(path)) != self.base_dir / name:
            raise _isolation("tenant_directory_escapes_storage_root")
        marker = self._read_marker(path)
        hashed = name.startswith(HASHED_KEY_PREFIX)
        if marker is None:
            if hashed:
                raise _isolation("identity_marker_missing")     # a hashed directory is always created with its marker
            return                                              # a legacy or plain directory: the exact name is the identity
        if marker.get("tenant_id") != tenant_id or marker.get("key") != name:
            raise _isolation("identity_marker_mismatch")

    @staticmethod
    def _read_marker(path: Path) -> Optional[dict]:
        marker_path = path / IDENTITY_MARKER
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(marker_path, flags)
        except FileNotFoundError:
            return None
        except OSError:
            raise _isolation("identity_marker_unreadable")      # for example a symlinked marker (ELOOP)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise _isolation("identity_marker_not_a_regular_file")
            raw = os.read(fd, 4096)
        finally:
            os.close(fd)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _isolation("identity_marker_corrupt")
        if not isinstance(data, dict):
            raise _isolation("identity_marker_corrupt")
        return data

    def _create(self, path: Path, tenant_id: str, name: str) -> None:
        """Create the directory WITH its marker in one atomic rename, so no markerless directory ever exists."""
        staging = self.base_dir / f".tmp-tenant-{uuid.uuid4().hex}"
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            os.mkdir(staging)
        except OSError as exc:                                   # read-only or missing root, no permission, disk full
            raise _unavailable("tenant_directory_not_creatable") from exc
        try:
            with open(staging / IDENTITY_MARKER, "w", encoding="utf-8") as handle:
                json.dump({"schema": 1, "tenant_id": tenant_id, "key": name}, handle)
            os.rename(staging, path)
        except OSError as exc:
            # Lost a race with a concurrent creator (or the name now exists): verify what is there instead.
            try:
                for child in staging.iterdir():
                    child.unlink()
                os.rmdir(staging)
            except OSError:
                pass                                             # a leftover staging directory is harmless
            if not os.path.lexists(path):
                raise _unavailable("tenant_directory_not_creatable") from exc
            self._verify_existing(path, tenant_id, name)


def ensure_inside_tenant(tenant_dir: Path, path: Path) -> Path:
    """
    ``path`` must be ``tenant_dir`` or lie below it, no component below ``tenant_dir`` may be a symlink, and the
    resolved path must stay inside the resolved ``tenant_dir``. Returns ``path`` unchanged.
    """
    try:
        relative = path.relative_to(tenant_dir)
    except ValueError:
        raise _isolation("path_outside_tenant_directory")
    current = tenant_dir
    for part in relative.parts:
        current = current / part
        if os.path.islink(current):
            raise _isolation("symlink_inside_tenant_storage")
    try:
        path.resolve().relative_to(tenant_dir.resolve())
    except ValueError:
        raise _isolation("path_resolves_outside_tenant_directory")
    return path
