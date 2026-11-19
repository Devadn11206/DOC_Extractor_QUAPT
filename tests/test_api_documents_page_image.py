"""
Tests for API-003: GET /api/v1/documents/{document_id}/pages/{page_number} (Milestone 2D Slice 1 Remediation).

Comprehensive security, race-prevention, and correctness tests verifying:
- F1: Route is synchronous off the event loop, leaf file uses O_NONBLOCK, FIFOs refused promptly without hanging.
- F2: Race-safe descriptor-relative path traversal (openat) with O_NOFOLLOW preventing static and dynamic symlink escapes.
- F3: Strict canonical storage URI enforcement ({tenant}/{doc}/pages/page_{page:04d}.png) and full PNG decodability verification.
- F4: Neutralized app.state service bypass; tenant context originates strictly from authenticated principal.
- Unconfigured container/storage returns typed 503 SERVICE_UNAVAILABLE.
- Verification that reading does not create RoutedExtractor, Module B, or tenant processing components.
- Zero path disclosure across all HTTP status codes.
- R1: descriptor ownership (os.fdopen transfer), no double close, no leak, on every success and failure path.
- R2: strict PNG chunk structure; nothing may follow IEND.
- R3: valid-PNG decoys in symlink tests, deterministic swap-at-open races, restored handoff document-ID mismatch test.
- R4: no raw exception text, URI, tenant or document id in this module's log records.
- R5: required platform capabilities fail closed before any file access.
"""

import ast
import errno
import inspect
import io
import json
import logging
import os
import shutil
import stat
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Dict, Optional
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, PngImagePlugin

from doc_extractor.api.app import create_app
from doc_extractor.api.composition import ApplicationContainer
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.contracts.documents import SCOPE_DOCUMENTS_READ, validate_document_id
from doc_extractor.api.exceptions import TenantResourceNotFoundError
from doc_extractor.api.ports.authentication import (
    ApiPrincipal,
    AuthenticationProvider,
    PrincipalType,
)
from doc_extractor.api.routers.documents import get_document_page
from doc_extractor.api.services import document_read_service as read_service_module
from doc_extractor.api.services.document_read_service import DocumentReadService, validate_png_structure
from doc_extractor.exceptions import (
    ComponentConfigurationError,
    CrossTenantStorageAccessError,
    StorageError,
    TenantStorageIsolationError,
)
from doc_extractor.handoff import MODULE_A_CONTRACT_VERSION, ModuleAHandoffPayload, PageHandoffRecord
from doc_extractor.storage import LocalDiskStorage


class MockAuthProvider(AuthenticationProvider):
    """Test authentication provider mapping bearer tokens to predefined principals."""

    def __init__(self) -> None:
        self.principals: Dict[str, ApiPrincipal] = {
            "token-alpha-reader": ApiPrincipal(
                principal_id="user_alpha",
                tenant_id="tenant_alpha",
                principal_type=PrincipalType.USER,
                scopes={SCOPE_DOCUMENTS_READ},
                is_active=True,
            ),
            "token-alpha-writer-only": ApiPrincipal(
                principal_id="user_alpha_writer",
                tenant_id="tenant_alpha",
                principal_type=PrincipalType.USER,
                scopes={"documents:write"},
                is_active=True,
            ),
            "token-beta-reader": ApiPrincipal(
                principal_id="user_beta",
                tenant_id="tenant_beta",
                principal_type=PrincipalType.USER,
                scopes={SCOPE_DOCUMENTS_READ},
                is_active=True,
            ),
            "token-fresh-tenant": ApiPrincipal(
                principal_id="user_fresh",
                tenant_id="tenant_never_provisioned",
                principal_type=PrincipalType.USER,
                scopes={SCOPE_DOCUMENTS_READ},
                is_active=True,
            ),
        }

    async def authenticate(self, credentials: str, request=None) -> Optional[ApiPrincipal]:
        if not credentials:
            return None
        token = credentials
        if credentials.startswith("Bearer "):
            token = credentials[len("Bearer ") :].strip()
        return self.principals.get(token)


def _create_test_document(
    storage: LocalDiskStorage,
    document_id: str,
    tenant_id: str,
    num_pages: int = 2,
) -> Dict[int, bytes]:
    """Helper creating real PNG page images and canonical handoff payload in storage."""
    pages = []
    page_bytes: Dict[int, bytes] = {}

    for p in range(1, num_pages + 1):
        img = np.full((100, 100, 3), fill_value=p * 60, dtype=np.uint8)
        _, png_buf = cv2.imencode(".png", img)
        raw_bytes = png_buf.tobytes()
        page_bytes[p] = raw_bytes

        art = storage.store_page_image(document_id, p, raw_bytes)
        pages.append(
            PageHandoffRecord(
                page_number=p,
                original_artifact_id=art.artifact_id,
                preprocessed_artifact_id=f"prep_{p}",
                original_storage_uri=f"{tenant_id}/{document_id}/pages/page_{p:04d}.png",
                preprocessed_storage_uri=f"{tenant_id}/{document_id}/preprocessed/page_{p:04d}_preprocessed.png",
                original_width_px=100,
                original_height_px=100,
                preprocessed_width_px=100,
                preprocessed_height_px=100,
                dpi=200,
                applied_rotation_deg=0,
                applied_deskew_deg=0.0,
                residual_skew_deg=0.0,
                contrast_method="none",
                metadata_storage_uri=f"{tenant_id}/{document_id}/preprocessed/page_{p:04d}_metadata.json",
            )
        )

    handoff = ModuleAHandoffPayload(
        contract_version=MODULE_A_CONTRACT_VERSION,
        run_id="run_test_1",
        document_id=document_id,
        tenant_id=tenant_id,
        status="completed",
        internal_state="PROCESSED",
        claimed_filename="sample.pdf",
        file_hash_sha256="0" * 64,
        file_size_bytes=sum(len(b) for b in page_bytes.values()),
        page_count=num_pages,
        mime_type="application/pdf",
        pages=pages,
        created_at="2026-11-19T00:00:00Z",
        completed_at="2026-11-19T00:01:00Z",
    )
    storage.save_handoff_payload(handoff)
    return page_bytes


@pytest.fixture
def auth_provider() -> MockAuthProvider:
    return MockAuthProvider()


@pytest.fixture
def setup_api(tmp_path: Path, auth_provider: MockAuthProvider):
    settings = ApiSettings(
        app_env="development",
        storage_base_dir=str(tmp_path),
    )
    container = ApplicationContainer(api_settings=settings, auth_provider=auth_provider)
    app = create_app(settings=settings, container=container)
    client = TestClient(app)

    # Populate tenant_alpha with a 2-page document
    storage_alpha = LocalDiskStorage(tmp_path, tenant_id="tenant_alpha")
    doc_id = "doc_valid_123"
    page_bytes = _create_test_document(storage_alpha, doc_id, "tenant_alpha", num_pages=2)

    return {
        "app": app,
        "client": client,
        "container": container,
        "tmp_path": tmp_path,
        "storage_alpha": storage_alpha,
        "doc_id": doc_id,
        "page_bytes": page_bytes,
    }


# ============================================================================
# Shared helpers for the F1/F2 and R1-R5 regression tests
# ============================================================================

ALPHA_HEADERS = {"Authorization": "Bearer token-alpha-reader"}
APPENDED_SECRET = b"TOP-SECRET-APPENDED-TEXT"


def _png_bytes(shade: int, size: int = 16) -> bytes:
    """An independently generated (Pillow, not the OpenCV encoder of the fixtures) valid PNG, used as a decoy."""
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (shade, 255 - shade, shade // 2)).save(buffer, format="PNG")
    return buffer.getvalue()


DECOY_PNG = _png_bytes(200)


def _png_chunks(png: bytes):
    """Split a PNG into [(type, data)] chunks (test helper; assumes a well-formed input)."""
    chunks, position = [], 8
    while position < len(png):
        length = struct.unpack(">I", png[position:position + 4])[0]
        chunks.append((png[position + 4:position + 8], png[position + 8:position + 8 + length]))
        position += 12 + length
    return chunks


def _png_assemble(chunks) -> bytes:
    """Build a PNG from [(type, data)] with CORRECT lengths and CRCs, so a test breaks exactly one thing."""
    out = b"\x89PNG\r\n\x1a\n"
    for chunk_type, data in chunks:
        out += struct.pack(">I", len(data)) + chunk_type + data
        out += struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    return out


def _open_fd_count() -> int:
    return len(os.listdir("/dev/fd"))


class _FailingReadFile:
    """File object whose ``read`` fails after the descriptor was transferred to it (it still closes correctly)."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> bool:
        self._inner.close()
        return False

    def read(self, *args, **kwargs):
        raise OSError(errno.EIO, "simulated read failure")

    def close(self) -> None:
        self._inner.close()

    @property
    def closed(self) -> bool:
        return self._inner.closed


class _OsProxy:
    """
    Stand-in for the ``os`` module inside ``document_read_service``. It forwards every call to the real ``os`` but
    records descriptor ownership events (open / transfer to a file object / close), can run a hook immediately
    BEFORE a chosen ``os.open`` (a deterministic swap-at-open race, no sleeps), can hide attributes (platform
    capability tests) and can make page reads fail after the descriptor was handed to the file object.
    """

    def __init__(self, *, before_open=None, fail_page_reads=False, hidden=(), dir_fd_open_supported=True) -> None:
        self._hidden = set(hidden)
        self._before_open = before_open
        self._fail_page_reads = fail_page_reads
        self.events = []
        self.transferred_files = []
        self.open_calls = 0
        self.open = self._open
        self.close = self._close
        self.fdopen = self._fdopen
        supported = set(os.supports_dir_fd) - {os.open}
        if dir_fd_open_supported:
            supported.add(self.open)
        self.supports_dir_fd = supported
        if "supports_dir_fd" in self._hidden:
            del self.supports_dir_fd

    def __getattr__(self, name):
        if name in self.__dict__.get("_hidden", ()):
            raise AttributeError(name)
        return getattr(os, name)

    def _open(self, path, flags, mode=0o777, *, dir_fd=None):
        self.open_calls += 1
        if self._before_open is not None:
            self._before_open(path, dir_fd)
        fd = os.open(path, flags, mode, dir_fd=dir_fd)
        self.events.append(("open", fd))
        return fd

    def _fdopen(self, fd, *args, **kwargs):
        file_obj = os.fdopen(fd, *args, **kwargs)
        self.events.append(("transfer", fd))
        mode = args[0] if args else kwargs.get("mode", "r")
        if self._fail_page_reads and "b" in mode:
            file_obj = _FailingReadFile(file_obj)
        self.transferred_files.append(file_obj)
        return file_obj

    def _close(self, fd):
        self.events.append(("close", fd))
        return os.close(fd)

    def ownership_report(self):
        """(problems, leaked descriptors, file objects left open) derived from the recorded events."""
        state, problems = {}, []
        for kind, fd in self.events:
            current = state.get(fd)
            if kind == "open":
                if current == "open":
                    problems.append(f"fd {fd} opened again while still owned")
                state[fd] = "open"
            elif kind == "transfer":
                if current != "open":
                    problems.append(f"fd {fd} transferred while {current}")
                state[fd] = "transferred"
            else:
                if current != "open":
                    problems.append(f"fd {fd} closed by the service while {current} (double close or close after transfer)")
                state[fd] = "closed"
        leaked = [fd for fd, value in state.items() if value == "open"]
        unclosed = [f for f in self.transferred_files if not f.closed]
        return problems, leaked, unclosed


def _install_proxy(monkeypatch, **kwargs) -> _OsProxy:
    proxy = _OsProxy(**kwargs)
    monkeypatch.setattr(read_service_module, "os", proxy)
    return proxy


def _assert_descriptors_clean(proxy: _OsProxy, baseline_fd_count: int) -> None:
    problems, leaked, unclosed = proxy.ownership_report()
    assert problems == [], problems
    assert leaked == [], f"descriptors opened by the service were never closed or transferred: {leaked}"
    assert unclosed == [], "a file object that owns a descriptor was left open"
    assert _open_fd_count() == baseline_fd_count, "open descriptor count did not return to the baseline"


def _call_with_timeout(fn, *, fifo_paths=(), timeout=5.0):
    """Run ``fn`` in a thread; a blocked FIFO open fails the test instead of hanging the whole run."""
    outcome = {}

    def runner():
        try:
            outcome["value"] = fn()
        except BaseException as exc:  # re-raised in the calling thread
            outcome["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        for fifo in fifo_paths:                      # release the blocked open so the thread can end
            try:
                os.close(os.open(str(fifo), os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        thread.join(2.0)
        pytest.fail(f"read blocked for more than {timeout}s (FIFO must be refused without blocking)")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _warm_up(setup_api) -> None:
    """One cheap request so lazily created, long-lived client/server resources exist before a baseline is taken."""
    setup_api["client"].get("/api/v1/documents/doc_warm_up/pages/1", headers=ALPHA_HEADERS)


def _service_for(setup_api) -> DocumentReadService:
    return DocumentReadService(setup_api["storage_alpha"], "tenant_alpha")


def _new_document(setup_api, doc_id: str, pages: int = 1) -> Path:
    storage = setup_api["storage_alpha"]
    _create_test_document(storage, doc_id, "tenant_alpha", num_pages=pages)
    return storage._get_document_dir(doc_id)


def _rewrite_handoff(doc_dir: Path, mutate) -> None:
    handoff_file = doc_dir / "module_a_handoff.json"
    data = json.loads(handoff_file.read_text(encoding="utf-8"))
    mutate(data)
    handoff_file.write_text(json.dumps(data), encoding="utf-8")


def _make_outside_document(root: Path, tenant_id: str, doc_id: str) -> Path:
    """
    A fully valid document (valid handoff, valid PNG page) living OUTSIDE the tenant's real storage. Its page is
    overwritten with an independently generated decoy PNG, so if a protection is removed and the symlink followed,
    the decoy bytes are served and the test fails on the bytes, not merely on PNG validation.
    """
    root.mkdir(parents=True, exist_ok=True)
    outside_storage = LocalDiskStorage(root, tenant_id=tenant_id)
    _create_test_document(outside_storage, doc_id, tenant_id, num_pages=1)
    doc_dir = outside_storage._get_document_dir(doc_id)
    (doc_dir / "pages" / "page_0001.png").write_bytes(DECOY_PNG)
    return doc_dir


def _swap_for_symlink(path: Path, target: Path):
    """Replace ``path`` with a symlink to ``target`` and return a function restoring the original."""
    moved = path.with_name(path.name + "_moved")
    path.rename(moved)
    path.symlink_to(target)

    def restore() -> None:
        if path.is_symlink():
            path.unlink()
        if moved.exists() or moved.is_symlink():
            moved.rename(path)

    return restore


def _swap_for_fifo(path: Path):
    moved = path.with_name(path.name + "_moved")
    path.rename(moved)
    os.mkfifo(str(path))

    def restore() -> None:
        if path.exists() or path.is_symlink():
            path.unlink()
        if moved.exists():
            moved.rename(path)

    return restore


# ============================================================================
# F1: Event loop non-blocking & FIFO prompt refusal
# ============================================================================

def test_api003_route_is_synchronous_off_event_loop():
    """F1: Route function is synchronous (def, not async def) so FastAPI executes it in threadpool."""
    assert not inspect.iscoroutinefunction(get_document_page), (
        "get_document_page must be synchronous (def) to avoid blocking the asyncio event loop on file I/O."
    )


def test_api003_fifo_leaf_and_handoff_refused_without_blocking(setup_api):
    """F1: A FIFO at the page leaf or at the handoff is refused promptly (O_NONBLOCK + fstat), never blocking."""
    client = setup_api["client"]
    for doc_id, victim in (("doc_fifo_leaf", "pages/page_0001.png"), ("doc_fifo_handoff", "module_a_handoff.json")):
        doc_dir = _new_document(setup_api, doc_id)
        target = doc_dir / victim
        target.unlink()
        os.mkfifo(str(target))

        response = _call_with_timeout(
            lambda: client.get(f"/api/v1/documents/{doc_id}/pages/1", headers=ALPHA_HEADERS), fifo_paths=[target]
        )
        assert response.status_code == 500
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert str(setup_api["tmp_path"]) not in response.text


# ============================================================================
# F2 / R3-B: Symlinks at every component, with VALID PNG decoys
# ============================================================================

def test_api003_tenant_directory_symlink_refused_by_descriptor_walk(setup_api, monkeypatch):
    """
    F2: the descriptor walk ALONE refuses a tenant directory that is a symlink. The path-based tenant resolver is
    bypassed here on purpose, so removing O_NOFOLLOW on the tenant component would expose the decoy bytes.
    """
    tmp_path = setup_api["tmp_path"]
    outside_root = tmp_path.parent / (tmp_path.name + "_outside_tenant")
    _make_outside_document(outside_root, "tenant_symlinked", "doc_victim")
    (tmp_path / "tenant_symlinked").symlink_to(outside_root / "tenant_symlinked")

    monkeypatch.setattr(LocalDiskStorage, "_tenant_dir", lambda self, create=False: self.base_dir / self.tenant_id)
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_symlinked")
    service = DocumentReadService(storage, "tenant_symlinked")

    with pytest.raises(TenantStorageIsolationError) as excinfo:
        service.get_page_image("doc_victim", 1)
    assert excinfo.value.details["reason"] == "tenant_directory_is_a_symlink"


def test_api003_tenant_directory_symlink_refused_through_api(setup_api):
    """F2: Statically planted symlink at the tenant directory never serves the decoy (API level, real resolver)."""
    client, tmp_path = setup_api["client"], setup_api["tmp_path"]
    outside_root = tmp_path.parent / (tmp_path.name + "_outside_tenant_api")
    _make_outside_document(outside_root, "tenant_symlink_attack", "doc_123")
    (tmp_path / "tenant_symlink_attack").symlink_to(outside_root / "tenant_symlink_attack")
    setup_api["app"].state.auth_provider.principals["token-attack"] = ApiPrincipal(
        principal_id="attacker", tenant_id="tenant_symlink_attack", scopes={SCOPE_DOCUMENTS_READ}, is_active=True
    )

    response = client.get("/api/v1/documents/doc_123/pages/1", headers={"Authorization": "Bearer token-attack"})
    assert response.status_code == 500
    assert response.content != DECOY_PNG
    assert str(tmp_path) not in response.text


def test_api003_document_directory_symlink_refused(setup_api):
    """F2: A document directory that is a symlink to a valid outside document serves nothing."""
    client, tmp_path, storage = setup_api["client"], setup_api["tmp_path"], setup_api["storage_alpha"]
    outside_doc = _make_outside_document(tmp_path.parent / (tmp_path.name + "_outside_doc"), "tenant_alpha", "doc_symlink")
    (storage._tenant_dir(create=True) / "doc_symlink").symlink_to(outside_doc)

    response = client.get("/api/v1/documents/doc_symlink/pages/1", headers=ALPHA_HEADERS)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.content != DECOY_PNG
    assert str(tmp_path) not in response.text


def test_api003_pages_directory_symlink_refused(setup_api):
    """F2: A `pages` directory that is a symlink to a directory holding a valid decoy PNG serves nothing."""
    client, tmp_path = setup_api["client"], setup_api["tmp_path"]
    doc_dir = _new_document(setup_api, "doc_pages_symlink")
    outside_pages = tmp_path.parent / (tmp_path.name + "_outside_pages")
    outside_pages.mkdir()
    (outside_pages / "page_0001.png").write_bytes(DECOY_PNG)
    shutil.rmtree(doc_dir / "pages")
    (doc_dir / "pages").symlink_to(outside_pages)

    response = client.get("/api/v1/documents/doc_pages_symlink/pages/1", headers=ALPHA_HEADERS)
    assert response.status_code == 500
    assert response.content != DECOY_PNG
    assert str(tmp_path) not in response.text


def test_api003_final_file_symlink_refused(setup_api):
    """F2: A page file that is a symlink to a valid decoy PNG outside the tenant serves nothing."""
    client, tmp_path = setup_api["client"], setup_api["tmp_path"]
    doc_dir = _new_document(setup_api, "doc_leaf_symlink")
    outside_png = tmp_path.parent / (tmp_path.name + "_outside_leaf.png")
    outside_png.write_bytes(DECOY_PNG)
    (doc_dir / "pages" / "page_0001.png").unlink()
    (doc_dir / "pages" / "page_0001.png").symlink_to(outside_png)

    response = client.get("/api/v1/documents/doc_leaf_symlink/pages/1", headers=ALPHA_HEADERS)
    assert response.status_code == 500
    assert response.content != DECOY_PNG
    assert str(tmp_path) not in response.text


def test_api003_handoff_file_symlink_refused(setup_api):
    """F2: A handoff that is a symlink to a valid handoff is refused (it is never followed)."""
    client, tmp_path = setup_api["client"], setup_api["tmp_path"]
    doc_dir = _new_document(setup_api, "doc_handoff_symlink")
    outside_handoff = tmp_path.parent / (tmp_path.name + "_outside_handoff.json")
    outside_handoff.write_text((doc_dir / "module_a_handoff.json").read_text(encoding="utf-8"), encoding="utf-8")
    (doc_dir / "module_a_handoff.json").unlink()
    (doc_dir / "module_a_handoff.json").symlink_to(outside_handoff)

    response = client.get("/api/v1/documents/doc_handoff_symlink/pages/1", headers=ALPHA_HEADERS)
    assert response.status_code == 500          # a followed (valid) handoff would answer 200
    assert str(tmp_path) not in response.text


# ============================================================================
# R3-C: Genuine swap-at-open races (deterministic hook, no sleeps)
# ============================================================================

def _race_environment(setup_api, doc_id: str):
    """(doc_dir, outside_doc_dir) where the outside tree mirrors tenant/doc layout with decoy bytes."""
    tmp_path = setup_api["tmp_path"]
    doc_dir = _new_document(setup_api, doc_id, pages=1)
    outside_root = tmp_path.parent / (tmp_path.name + "_race_outside_" + doc_id)
    outside_doc = _make_outside_document(outside_root, "tenant_alpha", doc_id)
    return doc_dir, outside_doc, outside_root


def _once(trigger_name: str, action):
    """Hook for ``_OsProxy``: run ``action`` once, immediately before the descriptor-relative open of ``trigger_name``."""
    state = {"done": False}

    def hook(path, dir_fd):
        if dir_fd is not None and path == trigger_name and not state["done"]:
            state["done"] = True
            action()

    hook.state = state
    return hook


@pytest.mark.parametrize("component", ["tenant", "document", "pages", "leaf", "handoff"])
def test_api003_swap_at_open_is_refused(setup_api, monkeypatch, component):
    """
    R3-C: the component is replaced by a symlink to valid decoy data in the instant between the service's earlier
    steps and the relative open of that very component. The O_NOFOLLOW open must refuse it; the decoy is never served.
    """
    client, tmp_path = setup_api["client"], setup_api["tmp_path"]
    doc_id = "doc_race_" + component
    doc_dir, outside_doc, outside_root = _race_environment(setup_api, doc_id)

    plans = {
        "tenant": ("tenant_alpha", lambda: _swap_for_symlink(tmp_path / "tenant_alpha", outside_root / "tenant_alpha")),
        "document": (doc_id, lambda: _swap_for_symlink(doc_dir, outside_doc)),
        "pages": ("pages", lambda: _swap_for_symlink(doc_dir / "pages", outside_doc / "pages")),
        "leaf": ("page_0001.png", lambda: _swap_for_symlink(doc_dir / "pages" / "page_0001.png", outside_doc / "pages" / "page_0001.png")),
        "handoff": ("module_a_handoff.json", lambda: _swap_for_symlink(doc_dir / "module_a_handoff.json", outside_doc / "module_a_handoff.json")),
    }
    trigger, make_swap = plans[component]
    restore = {}
    hook = _once(trigger, lambda: restore.setdefault("fn", make_swap()))
    _warm_up(setup_api)
    proxy = _install_proxy(monkeypatch, before_open=hook)
    baseline = _open_fd_count()
    try:
        response = client.get(f"/api/v1/documents/{doc_id}/pages/1", headers=ALPHA_HEADERS)
    finally:
        monkeypatch.undo()
        if "fn" in restore:
            restore["fn"]()

    assert hook.state["done"], "the swap hook never fired: the test did not exercise the race"
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.content != DECOY_PNG
    assert str(tmp_path) not in response.text
    _assert_descriptors_clean(proxy, baseline)


def test_api003_swap_leaf_for_fifo_at_open_is_refused_without_blocking(setup_api, monkeypatch):
    """R3-C: a regular file replaced by a FIFO right before the open: O_NONBLOCK + fstat refuse it, no hang."""
    doc_dir = _new_document(setup_api, "doc_race_fifo")
    leaf = doc_dir / "pages" / "page_0001.png"
    restore = {}
    hook = _once("page_0001.png", lambda: restore.setdefault("fn", _swap_for_fifo(leaf)))
    proxy = _install_proxy(monkeypatch, before_open=hook)
    baseline = _open_fd_count()
    try:
        with pytest.raises(TenantStorageIsolationError) as excinfo:
            _call_with_timeout(lambda: _service_for(setup_api).get_page_image("doc_race_fifo", 1), fifo_paths=[leaf])
    finally:
        monkeypatch.undo()
        if "fn" in restore:
            restore["fn"]()
    assert hook.state["done"]
    assert excinfo.value.details["reason"] == "not_a_regular_file"
    _assert_descriptors_clean(proxy, baseline)


@pytest.mark.parametrize("replaced", ["document", "pages"])
def test_api003_parent_replaced_after_descriptor_acquired_still_reads_original(setup_api, monkeypatch, replaced):
    """
    R3-C: once a parent directory descriptor is held, replacing that parent's PATH by a symlink to decoy data does
    not change what is read: the child is opened relative to the descriptor, so the ORIGINAL bytes are returned and
    the decoy never is. A path-based implementation would serve the decoy here.
    """
    doc_id = "doc_race_after_" + replaced
    doc_dir, outside_doc, _ = _race_environment(setup_api, doc_id)
    original = (doc_dir / "pages" / "page_0001.png").read_bytes()
    restore = {}
    if replaced == "document":      # swap the document directory path right before `pages` is opened relative to it
        trigger, make_swap = "pages", lambda: _swap_for_symlink(doc_dir, outside_doc)
    else:                           # swap the pages directory path right before the leaf is opened relative to it
        trigger, make_swap = "page_0001.png", lambda: _swap_for_symlink(doc_dir / "pages", outside_doc / "pages")
    hook = _once(trigger, lambda: restore.setdefault("fn", make_swap()))
    proxy = _install_proxy(monkeypatch, before_open=hook)
    baseline = _open_fd_count()
    try:
        image_bytes, media_type = _service_for(setup_api).get_page_image(doc_id, 1)
    finally:
        monkeypatch.undo()
        if "fn" in restore:
            restore["fn"]()
    assert hook.state["done"]
    assert image_bytes == original
    assert image_bytes != DECOY_PNG
    assert media_type == "image/png"
    _assert_descriptors_clean(proxy, baseline)


# ============================================================================
# R1 / R3-D: Descriptor ownership, double close, leaks
# ============================================================================

def _prep_invalid_utf8(doc_dir):
    (doc_dir / "module_a_handoff.json").write_bytes(b"\xff\xfe\x00not-utf8")


def _prep_invalid_json(doc_dir):
    (doc_dir / "module_a_handoff.json").write_text("{not json", encoding="utf-8")


def _prep_not_an_object(doc_dir):
    (doc_dir / "module_a_handoff.json").write_text("[1, 2]", encoding="utf-8")


def _prep_tenant_mismatch(doc_dir):
    _rewrite_handoff(doc_dir, lambda d: d.__setitem__("tenant_id", "tenant_beta"))


def _prep_document_mismatch(doc_dir):
    _rewrite_handoff(doc_dir, lambda d: d.__setitem__("document_id", "doc_someone_else"))


def _prep_non_canonical(doc_dir):
    _rewrite_handoff(
        doc_dir, lambda d: d["pages"][0].__setitem__("original_storage_uri", f"tenant_alpha/{doc_dir.name}/pages/extra.png")
    )


def _prep_pages_missing(doc_dir):
    shutil.rmtree(doc_dir / "pages")


def _prep_page_missing(doc_dir):
    (doc_dir / "pages" / "page_0001.png").unlink()


def _prep_pages_is_a_file(doc_dir):
    shutil.rmtree(doc_dir / "pages")
    (doc_dir / "pages").write_bytes(DECOY_PNG)


def _prep_pages_symlink(doc_dir):
    outside = doc_dir.parent.parent / "outside_pages_for_fd_test"
    outside.mkdir(exist_ok=True)
    (outside / "page_0001.png").write_bytes(DECOY_PNG)
    shutil.rmtree(doc_dir / "pages")
    (doc_dir / "pages").symlink_to(outside)


def _prep_leaf_symlink(doc_dir):
    outside = doc_dir.parent.parent / "outside_leaf_for_fd_test.png"
    outside.write_bytes(DECOY_PNG)
    (doc_dir / "pages" / "page_0001.png").unlink()
    (doc_dir / "pages" / "page_0001.png").symlink_to(outside)


def _prep_leaf_fifo(doc_dir):
    (doc_dir / "pages" / "page_0001.png").unlink()
    os.mkfifo(str(doc_dir / "pages" / "page_0001.png"))


def _prep_corrupt_png(doc_dir):
    (doc_dir / "pages" / "page_0001.png").write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-png")


def _prep_appended(doc_dir):
    leaf = doc_dir / "pages" / "page_0001.png"
    leaf.write_bytes(leaf.read_bytes() + APPENDED_SECRET)


_FAILURE_SCENARIOS = [
    pytest.param(None, None, 1, None, id="success"),
    pytest.param(_prep_invalid_utf8, StorageError, 1, None, id="handoff-invalid-utf8"),
    pytest.param(_prep_invalid_json, StorageError, 1, None, id="handoff-invalid-json"),
    pytest.param(_prep_not_an_object, StorageError, 1, None, id="handoff-not-an-object"),
    pytest.param(_prep_tenant_mismatch, CrossTenantStorageAccessError, 1, None, id="handoff-tenant-mismatch"),
    pytest.param(_prep_document_mismatch, TenantResourceNotFoundError, 1, None, id="handoff-document-mismatch"),
    pytest.param(None, TenantResourceNotFoundError, 9, None, id="page-not-in-handoff"),
    pytest.param(_prep_non_canonical, StorageError, 1, None, id="non-canonical-uri"),
    pytest.param(_prep_pages_missing, TenantResourceNotFoundError, 1, None, id="pages-directory-missing"),
    pytest.param(_prep_page_missing, TenantResourceNotFoundError, 1, None, id="page-file-missing"),
    pytest.param(_prep_pages_is_a_file, TenantStorageIsolationError, 1, None, id="pages-is-a-regular-file"),
    pytest.param(_prep_pages_symlink, TenantStorageIsolationError, 1, None, id="pages-symlink"),
    pytest.param(_prep_leaf_symlink, TenantStorageIsolationError, 1, None, id="leaf-symlink"),
    pytest.param(_prep_leaf_fifo, TenantStorageIsolationError, 1, None, id="leaf-fifo"),
    pytest.param(_prep_corrupt_png, StorageError, 1, None, id="png-corrupt"),
    pytest.param(_prep_appended, StorageError, 1, None, id="png-appended-data"),
    pytest.param(None, StorageError, 1, {"fail_page_reads": True}, id="page-read-error-after-transfer"),
]


@pytest.mark.parametrize("prepare, expected_error, page, proxy_kwargs", _FAILURE_SCENARIOS)
def test_api003_descriptor_ownership_on_every_path(setup_api, monkeypatch, prepare, expected_error, page, proxy_kwargs):
    """
    R1/R3-D: on the success path and on every failure path, no descriptor is closed twice, none is closed after
    ownership moved to a file object, every descriptor the service opened is closed or transferred, every file
    object that owns one is closed, and the process's open-descriptor count returns to its baseline.
    """
    doc_id = "doc_fd_" + (expected_error.__name__ if expected_error else "ok").lower() + f"_{page}_{id(prepare) % 9973}"
    doc_dir = _new_document(setup_api, doc_id)
    if prepare is not None:
        prepare(doc_dir)
    proxy = _install_proxy(monkeypatch, **(proxy_kwargs or {}))
    baseline = _open_fd_count()
    fifo_paths = [doc_dir / "pages" / "page_0001.png"]

    try:
        if expected_error is None:
            image_bytes, _ = _call_with_timeout(lambda: _service_for(setup_api).get_page_image(doc_id, page), fifo_paths=fifo_paths)
            assert image_bytes.startswith(b"\x89PNG")
            transfers = [e for e in proxy.events if e[0] == "transfer"]
            assert len(transfers) == 2, "handoff and page descriptors must both be handed to file objects"
        else:
            with pytest.raises(expected_error):
                _call_with_timeout(lambda: _service_for(setup_api).get_page_image(doc_id, page), fifo_paths=fifo_paths)
    finally:
        monkeypatch.undo()
    _assert_descriptors_clean(proxy, baseline)


def test_api003_invalid_utf8_handoff_does_not_double_close_through_api(setup_api, monkeypatch):
    """R1: the exact reported failure (invalid UTF-8 handoff) through the real route: sanitized 500, no double close."""
    doc_dir = _new_document(setup_api, "doc_utf8_http")
    _prep_invalid_utf8(doc_dir)
    _warm_up(setup_api)
    proxy = _install_proxy(monkeypatch)
    baseline = _open_fd_count()
    try:
        response = setup_api["client"].get("/api/v1/documents/doc_utf8_http/pages/1", headers=ALPHA_HEADERS)
    finally:
        monkeypatch.undo()
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["details"] == {}
    assert str(setup_api["tmp_path"]) not in response.text
    _assert_descriptors_clean(proxy, baseline)


def test_api003_page_read_failure_does_not_double_close_through_api(setup_api, monkeypatch):
    """R1: a read error after the page descriptor was transferred to its file object: sanitized 500, no double close."""
    _new_document(setup_api, "doc_read_error_http")
    _warm_up(setup_api)
    proxy = _install_proxy(monkeypatch, fail_page_reads=True)
    baseline = _open_fd_count()
    try:
        response = setup_api["client"].get("/api/v1/documents/doc_read_error_http/pages/1", headers=ALPHA_HEADERS)
    finally:
        monkeypatch.undo()
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "simulated" not in response.text and str(setup_api["tmp_path"]) not in response.text
    _assert_descriptors_clean(proxy, baseline)


def test_api003_no_descriptor_leak_after_repeated_mixed_requests(setup_api):
    """R1/R3-D: repeated successful and failing requests (through the route) leave the descriptor count unchanged."""
    client = setup_api["client"]
    cases = {"doc_loop_ok": None, "doc_loop_utf8": _prep_invalid_utf8, "doc_loop_png": _prep_corrupt_png,
             "doc_loop_tail": _prep_appended, "doc_loop_nopages": _prep_pages_missing}
    for doc_id, prepare in cases.items():
        doc_dir = _new_document(setup_api, doc_id)
        if prepare is not None:
            prepare(doc_dir)

    def sweep():
        return [client.get(f"/api/v1/documents/{d}/pages/1", headers=ALPHA_HEADERS).status_code for d in cases]

    assert sweep() == [200, 500, 500, 500, 404]           # warm-up, also pins the expected outcome per case
    baseline = _open_fd_count()
    for _ in range(40):
        assert sweep() == [200, 500, 500, 500, 404]
    assert _open_fd_count() == baseline


def test_api003_close_failures_are_not_swallowed(monkeypatch):
    """R1: a failing close raises on the success path and never masks an already propagating error."""
    real_close = os.close

    class FailingCloseOs(_OsProxy):
        def _close(self, fd):
            real_close(fd)
            raise OSError(errno.EIO, "simulated close failure")

    r1, w1 = os.pipe()
    r2, w2 = os.pipe()
    try:
        monkeypatch.setattr(read_service_module, "os", FailingCloseOs())
        with pytest.raises(StorageError) as excinfo:               # success path: the failure is surfaced
            read_service_module._close_owned_descriptors((r1, None, None), strict=True)
        assert excinfo.value.details["reason"] == "fd_close_failed"
        # another error already propagating: the close failure is logged but must not raise or replace it
        read_service_module._close_owned_descriptors((None, r2, None), strict=False)
    finally:
        monkeypatch.undo()
        for fd in (r1, w1, r2, w2):
            try:
                real_close(fd)
            except OSError:
                pass


# ============================================================================
# R2: strict PNG structure (nothing may follow IEND)
# ============================================================================

def test_api003_png_with_appended_secret_after_iend_rejected_through_api(setup_api):
    """R2: a valid PNG followed by secret text is refused by the real route; no secret byte reaches the response."""
    doc_dir = _new_document(setup_api, "doc_png_tail")
    leaf = doc_dir / "pages" / "page_0001.png"
    valid = leaf.read_bytes()
    leaf.write_bytes(valid + APPENDED_SECRET)

    response = setup_api["client"].get("/api/v1/documents/doc_png_tail/pages/1", headers=ALPHA_HEADERS)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["details"] == {}
    assert APPENDED_SECRET not in response.content
    assert b"SECRET" not in response.content
    assert str(setup_api["tmp_path"]) not in response.text
    # control: the unmodified valid PNG is served byte-for-byte
    leaf.write_bytes(valid)
    ok = setup_api["client"].get("/api/v1/documents/doc_png_tail/pages/1", headers=ALPHA_HEADERS)
    assert ok.status_code == 200 and ok.content == valid


def test_api003_png_with_appended_secret_reports_trailing_data_reason(setup_api):
    """R2: the service refuses with the fixed machine-readable reason (logged server-side only)."""
    doc_dir = _new_document(setup_api, "doc_png_tail_reason")
    leaf = doc_dir / "pages" / "page_0001.png"
    leaf.write_bytes(leaf.read_bytes() + APPENDED_SECRET)
    with pytest.raises(StorageError) as excinfo:
        _service_for(setup_api).get_page_image("doc_png_tail_reason", 1)
    assert excinfo.value.details["reason"] == "png_structure_trailing_data"


def _valid_png_with_ancillary_chunks() -> bytes:
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "legitimate ancillary text chunk")
    buffer = io.BytesIO()
    Image.new("RGB", (24, 24), (10, 120, 200)).save(buffer, format="PNG", pnginfo=info, dpi=(200, 200), optimize=True)
    return buffer.getvalue()


@pytest.mark.parametrize("png", [_png_bytes(10), _png_bytes(240, size=64), _valid_png_with_ancillary_chunks()])
def test_validate_png_structure_accepts_valid_pngs(png):
    """R2: valid PNGs, including ones with legitimate ancillary chunks (tEXt, pHYs), pass the structure check."""
    validate_png_structure(png)


def test_api003_valid_png_with_ancillary_chunks_served_byte_for_byte(setup_api):
    """R2: the stricter check does not reject a legitimate canonical PNG; the exact bytes are served."""
    doc_dir = _new_document(setup_api, "doc_png_ancillary")
    legit = _valid_png_with_ancillary_chunks()
    (doc_dir / "pages" / "page_0001.png").write_bytes(legit)
    response = setup_api["client"].get("/api/v1/documents/doc_png_ancillary/pages/1", headers=ALPHA_HEADERS)
    assert response.status_code == 200
    assert response.content == legit


def test_api003_structurally_valid_but_undecodable_png_is_rejected(setup_api):
    """
    R2: the retained Pillow decoding step still matters: a PNG whose chunk structure and CRCs are all correct, but
    whose IHDR declares an impossible colour type, passes the structure check and is refused by decoding.
    """
    doc_dir = _new_document(setup_api, "doc_png_bad_header")
    chunks = _png_chunks(_png_bytes(42))
    bad_ihdr = struct.pack(">IIBBBBB", 16, 16, 8, 9, 0, 0, 0)          # colour type 9 does not exist
    forged = _png_assemble([(b"IHDR", bad_ihdr)] + chunks[1:])
    validate_png_structure(forged)                                       # structurally valid on purpose
    (doc_dir / "pages" / "page_0001.png").write_bytes(forged)

    with pytest.raises(StorageError) as excinfo:
        _service_for(setup_api).get_page_image("doc_png_bad_header", 1)
    assert excinfo.value.details["reason"] == "png_decode_failure"
    response = setup_api["client"].get("/api/v1/documents/doc_png_bad_header/pages/1", headers=ALPHA_HEADERS)
    assert response.status_code == 500 and response.content != forged


def _png_cases():
    base = _png_bytes(77)
    chunks = _png_chunks(base)
    ihdr, rest = chunks[0], chunks[1:]
    idat_index = next(i for i, (t, _) in enumerate(chunks) if t == b"IDAT")
    flipped = bytearray(base)
    assert len(base) > 60
    flipped[8 + 25 + 8 + 2] ^= 0xFF                       # a byte inside the first IDAT's data: CRC no longer matches
    overrun = bytearray(base)
    overrun[8 + 25:8 + 29] = struct.pack(">I", 0x7FFFFFF0)  # first chunk after IHDR claims to be huge
    bad_type = bytearray(base)
    bad_type[8 + 25 + 4] = ord("1")                        # chunk type must be ASCII letters
    iend_chunk = _png_assemble([(b"IEND", b"")])[8:]
    return [
        pytest.param(base + b"X", "trailing_data", id="one-trailing-byte"),
        pytest.param(base + APPENDED_SECRET, "trailing_data", id="appended-secret-text"),
        pytest.param(base + iend_chunk, "trailing_data", id="second-iend-chunk"),
        pytest.param(base + _png_assemble([(b"tEXt", b"k\x00v")])[8:], "trailing_data", id="chunk-after-iend"),
        pytest.param(base[:-4], "truncated", id="missing-iend-crc"),
        pytest.param(base[:-12], "truncated", id="iend-chunk-removed"),
        pytest.param(base[:50], "chunk_overruns_buffer", id="cut-inside-stream"),
        pytest.param(_png_assemble(rest), "ihdr_missing", id="ihdr-not-first"),
        pytest.param(_png_assemble([ihdr, ihdr] + rest), "ihdr_duplicate", id="ihdr-duplicated"),
        pytest.param(_png_assemble([(b"IHDR", b"\x00" * 12)] + rest), "ihdr_missing", id="ihdr-wrong-length"),
        pytest.param(_png_assemble(chunks[:-1] + [(b"IEND", b"data")]), "iend_not_empty", id="iend-with-data"),
        pytest.param(bytes(flipped), "crc_mismatch", id="idat-crc-mismatch"),
        pytest.param(bytes(overrun), "chunk_overruns_buffer", id="chunk-overruns-buffer"),
        pytest.param(bytes(bad_type), "chunk_type_invalid", id="chunk-type-corrupted"),
        pytest.param(b"\x00" + base[1:], "bad_signature", id="bad-signature"),
        pytest.param(b"\x89PNG\r\n\x1a\n", "too_short", id="signature-only"),
        pytest.param(b"", "too_short", id="empty"),
        pytest.param(b"\x89PNG\r\n\x1a\n" + b"SECRET_DATA_CANNOT_BE_EXFILTRATED_12345" * 2, "chunk_type_invalid", id="signature-plus-text"),
    ]


@pytest.mark.parametrize("data, reason", _png_cases())
def test_validate_png_structure_rejects_malformed_structure(data, reason):
    """R2: every structural defect (trailing data, truncation, bad IHDR/IEND/CRC/length/type) is refused."""
    with pytest.raises(StorageError) as excinfo:
        validate_png_structure(data)
    assert excinfo.value.details["reason"] == f"png_structure_{reason}"


# ============================================================================
# R4: log hygiene
# ============================================================================

class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages = []

    def emit(self, record) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def captured_logs():
    handler = _LogCapture()
    attached = []
    for name in ("doc_extractor.api.read_service", "doc_extractor.api.errors"):
        target = logging.getLogger(name)
        attached.append((target, target.level))
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        for target, level in attached:
            target.removeHandler(handler)
            target.setLevel(level)


def test_api003_newline_in_handoff_uri_cannot_forge_log_entries(setup_api, captured_logs):
    """R4: a URI containing a newline and a forged log line never reaches a log record."""
    doc_dir = _new_document(setup_api, "doc_log_uri")
    forged = "FORGED LOG LINE level=CRITICAL"
    _rewrite_handoff(doc_dir, lambda d: d["pages"][0].__setitem__("original_storage_uri", f"tenant_alpha/doc_log_uri/x\n{forged}"))

    response = setup_api["client"].get("/api/v1/documents/doc_log_uri/pages/1", headers=ALPHA_HEADERS)
    assert response.status_code == 500
    assert captured_logs.messages, "the refusal must be logged"
    assert all("\n" not in m and "\r" not in m for m in captured_logs.messages), captured_logs.messages
    assert not any("FORGED" in m for m in captured_logs.messages)
    assert not any(forged in m for m in captured_logs.messages)


def test_api003_untrusted_handoff_key_names_and_values_are_not_logged(setup_api, captured_logs):
    """R4: unknown handoff keys (the exception text names them), foreign tenant and document ids never get logged."""
    doc_dir = _new_document(setup_api, "doc_log_key")
    _rewrite_handoff(doc_dir, lambda d: d.__setitem__("evil\nFORGED LOG LINE level=CRITICAL", 1))
    r1 = setup_api["client"].get("/api/v1/documents/doc_log_key/pages/1", headers=ALPHA_HEADERS)

    doc_dir2 = _new_document(setup_api, "doc_log_tenant")
    _rewrite_handoff(doc_dir2, lambda d: d.__setitem__("tenant_id", "tenant_secret_value_xyz"))
    r2 = setup_api["client"].get("/api/v1/documents/doc_log_tenant/pages/1", headers=ALPHA_HEADERS)

    doc_dir3 = _new_document(setup_api, "doc_log_docid")
    _rewrite_handoff(doc_dir3, lambda d: d.__setitem__("document_id", "doc_foreign_secret_abc"))
    r3 = setup_api["client"].get("/api/v1/documents/doc_log_docid/pages/1", headers=ALPHA_HEADERS)

    assert (r1.status_code, r2.status_code, r3.status_code) == (500, 404, 404)
    joined = "\n".join(captured_logs.messages)
    for forbidden in ("FORGED", "evil", "tenant_secret_value_xyz", "doc_foreign_secret_abc", "doc_log_key", "doc_log_docid"):
        assert forbidden not in joined, f"{forbidden!r} leaked into the logs"
    assert all("\n" not in m for m in captured_logs.messages)


def test_api003_invalid_utf8_decode_text_is_not_logged(setup_api, captured_logs):
    """R4: the text of a UnicodeDecodeError (it quotes offending bytes) is not logged, only its class name."""
    _prep_invalid_utf8(_new_document(setup_api, "doc_log_utf8"))
    setup_api["client"].get("/api/v1/documents/doc_log_utf8/pages/1", headers=ALPHA_HEADERS)
    joined = "\n".join(captured_logs.messages)
    assert "reason=handoff_unreadable" in joined
    assert "0xff" not in joined and "invalid start byte" not in joined


def test_api003_read_service_log_calls_use_only_fixed_reasons_and_safe_arguments():
    """
    R4 regression guard (static): in document_read_service every ``logger.*`` call takes a constant message and
    only ``type(exc).__name__`` / ``exc.errno`` / constants as arguments: no raw exception, URI, tenant or id.
    """
    tree = ast.parse(inspect.getsource(read_service_module))
    forbidden_names = {"exc", "storage_uri", "handoff_tenant", "safe_doc_id", "document_id", "handoff_data",
                       "handoff_raw", "page_record", "canonical_uri", "tenant_id", "tenant_name", "image_bytes"}
    checked = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "logger"):
            continue
        checked += 1
        assert isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str), \
            f"line {node.lineno}: log message must be a constant string (no f-string)"
        for arg in node.args[1:]:
            is_errno = (isinstance(arg, ast.Attribute) and arg.attr == "errno"
                        and isinstance(arg.value, ast.Name) and arg.value.id == "exc")
            is_type_name = (isinstance(arg, ast.Attribute) and arg.attr == "__name__" and isinstance(arg.value, ast.Call)
                            and isinstance(arg.value.func, ast.Name) and arg.value.func.id == "type")
            if is_errno or is_type_name:
                continue
            for sub in ast.walk(arg):
                assert not (isinstance(sub, ast.Name) and sub.id in forbidden_names), \
                    f"line {node.lineno}: argument {sub.id!r} may carry untrusted data into a log record"
                assert not (isinstance(sub, ast.Attribute) and sub.attr == "tenant_id"), \
                    f"line {node.lineno}: tenant identifier must not be logged"
    assert checked >= 20, "the static guard found suspiciously few logger calls"


# ============================================================================
# R5: required platform capabilities fail closed
# ============================================================================

@pytest.mark.parametrize("flag", ["O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"])
def test_api003_missing_required_open_flag_fails_closed(setup_api, monkeypatch, flag):
    """R5: a platform without a required open flag refuses before touching any file (typed error, generic 500)."""
    _new_document(setup_api, "doc_platform_" + flag.lower())
    proxy = _install_proxy(monkeypatch, hidden=(flag,))
    try:
        with pytest.raises(ComponentConfigurationError) as excinfo:
            _service_for(setup_api).get_page_image("doc_platform_" + flag.lower(), 1)
        response = setup_api["client"].get(f"/api/v1/documents/doc_platform_{flag.lower()}/pages/1", headers=ALPHA_HEADERS)
    finally:
        monkeypatch.undo()
    assert excinfo.value.details["reason"] == "platform_unsupported"
    assert proxy.open_calls == 0, "no stored file may be opened when a required capability is missing"
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["details"] == {}
    assert flag not in response.text and str(setup_api["tmp_path"]) not in response.text


@pytest.mark.parametrize("variant", ["open_without_dir_fd", "supports_dir_fd_missing"])
def test_api003_missing_dir_fd_support_fails_closed(setup_api, monkeypatch, variant):
    """R5: no dir_fd support for os.open (or no os.supports_dir_fd at all) refuses before touching any file."""
    _new_document(setup_api, "doc_platform_dirfd")
    kwargs = {"dir_fd_open_supported": False} if variant == "open_without_dir_fd" else {"hidden": ("supports_dir_fd",)}
    proxy = _install_proxy(monkeypatch, **kwargs)
    try:
        with pytest.raises(ComponentConfigurationError):
            _service_for(setup_api).get_page_image("doc_platform_dirfd", 1)
    finally:
        monkeypatch.undo()
    assert proxy.open_calls == 0


def test_api003_platform_guard_happens_after_input_validation_only(setup_api, monkeypatch):
    """R5: malformed identifiers are still the concealed 404 (input validation precedes the platform guard)."""
    _install_proxy(monkeypatch, hidden=("O_NOFOLLOW",))
    try:
        with pytest.raises(TenantResourceNotFoundError):
            _service_for(setup_api).get_page_image("doc@bad", 1)
    finally:
        monkeypatch.undo()


# ============================================================================
# F3: Canonical page artifact URI & decodable PNG enforcement
# ============================================================================

def test_api003_extra_notes_png_in_same_document_rejected(setup_api):
    """F3: Non-canonical URI in same document (e.g. extra_notes.png) is rejected with 500."""
    client = setup_api["client"]
    storage = setup_api["storage_alpha"]
    doc_id = "doc_extra_notes"

    _create_test_document(storage, doc_id, "tenant_alpha", num_pages=1)
    doc_dir = storage._get_document_dir(doc_id)
    (doc_dir / "pages" / "extra_notes.png").write_bytes(setup_api["page_bytes"][1])

    handoff_dict = storage.get_handoff_payload(doc_id)
    handoff_dict["pages"][0]["original_storage_uri"] = f"tenant_alpha/{doc_id}/pages/extra_notes.png"

    with open(doc_dir / "module_a_handoff.json", "w", encoding="utf-8") as f:
        json.dump(handoff_dict, f)

    response = client.get(
        f"/api/v1/documents/{doc_id}/pages/1",
        headers={"Authorization": "Bearer token-alpha-reader"},
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


def test_api003_same_tenant_different_document_uri_rejected(setup_api):
    """F3: Storage URI pointing to another document's page within same tenant returns 404."""
    client = setup_api["client"]
    storage = setup_api["storage_alpha"]
    doc_id = "doc_cross_doc_uri"

    _create_test_document(storage, doc_id, "tenant_alpha", num_pages=1)
    handoff_dict = storage.get_handoff_payload(doc_id)
    handoff_dict["pages"][0]["original_storage_uri"] = "tenant_alpha/doc_other/pages/page_0001.png"

    with open(storage._get_document_dir(doc_id) / "module_a_handoff.json", "w", encoding="utf-8") as f:
        json.dump(handoff_dict, f)

    response = client.get(
        f"/api/v1/documents/{doc_id}/pages/1",
        headers={"Authorization": "Bearer token-alpha-reader"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_api003_png_signature_with_arbitrary_text_rejected(setup_api):
    """F3: Payload with 8 PNG signature bytes followed by arbitrary text is rejected."""
    client = setup_api["client"]
    storage = setup_api["storage_alpha"]
    doc_id = "doc_fake_png"

    _create_test_document(storage, doc_id, "tenant_alpha", num_pages=1)
    doc_dir = storage._get_document_dir(doc_id)
    page_file = doc_dir / "pages" / "page_0001.png"

    # 8-byte PNG header + arbitrary secret text
    fake_png_payload = b"\x89PNG\r\n\x1a\nSECRET_DATA_CANNOT_BE_EXFILTRATED_12345"
    page_file.write_bytes(fake_png_payload)

    response = client.get(
        f"/api/v1/documents/{doc_id}/pages/1",
        headers={"Authorization": "Bearer token-alpha-reader"},
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "SECRET_DATA" not in response.text


def test_api003_handoff_tenant_id_mismatch_rejected(setup_api):
    """F3: Handoff payload with mismatched tenant_id is rejected with concealed 404."""
    client = setup_api["client"]
    storage = setup_api["storage_alpha"]
    doc_id = "doc_tenant_mismatch"

    _create_test_document(storage, doc_id, "tenant_alpha", num_pages=1)
    handoff_dict = storage.get_handoff_payload(doc_id)
    handoff_dict["tenant_id"] = "tenant_beta"

    with open(storage._get_document_dir(doc_id) / "module_a_handoff.json", "w", encoding="utf-8") as f:
        json.dump(handoff_dict, f)

    response = client.get(
        f"/api/v1/documents/{doc_id}/pages/1",
        headers={"Authorization": "Bearer token-alpha-reader"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_api003_handoff_document_id_mismatch_returns_concealed_404(setup_api):
    """
    R3-A: document A is requested but its handoff file is document B's handoff. The answer is the same concealed
    DOCUMENT_NOT_FOUND as an unknown document, and neither B's identifier nor any storage path is disclosed.
    """
    client, tmp_path = setup_api["client"], setup_api["tmp_path"]
    doc_a_dir = _new_document(setup_api, "doc_mismatch_a")
    doc_b_dir = _new_document(setup_api, "doc_mismatch_b_private")
    (doc_a_dir / "module_a_handoff.json").write_text(
        (doc_b_dir / "module_a_handoff.json").read_text(encoding="utf-8"), encoding="utf-8"
    )

    response = client.get("/api/v1/documents/doc_mismatch_a/pages/1", headers=ALPHA_HEADERS)
    unknown = client.get("/api/v1/documents/doc_never_existed/pages/1", headers=ALPHA_HEADERS)
    assert response.status_code == 404
    body, unknown_body = response.json()["error"], unknown.json()["error"]
    assert body["code"] == "DOCUMENT_NOT_FOUND"
    assert body["message"] == "Document not found."
    assert body["details"] == {}
    assert {k: v for k, v in body.items() if k != "request_id"} == {k: v for k, v in unknown_body.items() if k != "request_id"}
    assert "doc_mismatch_b_private" not in response.text
    assert str(tmp_path) not in response.text
    assert b"\x89PNG" not in response.content


def test_api003_malformed_traversal_storage_uri_standardized_response(setup_api):
    """F3: Malformed storage URI with traversal returns standardized 500 response without path leak."""
    client = setup_api["client"]
    storage = setup_api["storage_alpha"]
    doc_id = "doc_traversal_check"

    _create_test_document(storage, doc_id, "tenant_alpha", num_pages=1)
    handoff_dict = storage.get_handoff_payload(doc_id)
    handoff_dict["pages"][0]["original_storage_uri"] = "tenant_alpha/doc_1/../../etc/passwd"

    with open(storage._get_document_dir(doc_id) / "module_a_handoff.json", "w", encoding="utf-8") as f:
        json.dump(handoff_dict, f)

    response = client.get(
        f"/api/v1/documents/{doc_id}/pages/1",
        headers={"Authorization": "Bearer token-alpha-reader"},
    )
    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["message"] == "An unexpected internal server error occurred."
    assert payload["error"]["details"] == {}
    assert str(setup_api["tmp_path"]) not in response.text
    assert "passwd" not in response.text


# ============================================================================
# F4: Neutralize app.state service bypass
# ============================================================================

def test_api003_app_state_service_bypass_neutralized(setup_api):
    """F4: Injecting read service on app.state cannot bypass authenticated principal tenant context."""
    client = setup_api["client"]
    app = setup_api["app"]
    storage_alpha = setup_api["storage_alpha"]
    doc_id = setup_api["doc_id"]  # doc_valid_123 in tenant_alpha

    # Attacker injects a preconfigured tenant_alpha service on app.state
    alpha_service = DocumentReadService(storage_alpha, "tenant_alpha")
    app.state.document_read_service = alpha_service

    # Caller authenticates as tenant_beta
    response = client.get(
        f"/api/v1/documents/{doc_id}/pages/1",
        headers={"Authorization": "Bearer token-beta-reader"},
    )
    # Must fail closed with 404 DOCUMENT_NOT_FOUND, NOT return tenant_alpha's bytes
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


# ============================================================================
# Heavy components test: Prove read does NOT construct pipelines
# ============================================================================

def test_api003_read_does_not_create_heavy_components(setup_api):
    """Proves read request does NOT instantiate Module A, Module B, or tenant processing components."""
    client = setup_api["client"]
    container = setup_api["container"]
    doc_id = setup_api["doc_id"]

    # Verify tenant cache is empty initially
    assert len(container.tenant_cache) == 0
    assert not container.tenant_cache.contains("tenant_alpha")

    with patch.object(container, "get_tenant_components", wraps=container.get_tenant_components) as spy_get_components, \
         patch("doc_extractor.pipeline_b.ModuleBPipeline.__init__") as spy_mod_b, \
         patch("doc_extractor.processing_service.DocumentProcessingService.__init__") as spy_proc:

        response = client.get(
            f"/api/v1/documents/{doc_id}/pages/1",
            headers={"Authorization": "Bearer token-alpha-reader"},
        )
        assert response.status_code == 200

        # Prove get_tenant_components was never invoked
        assert spy_get_components.call_count == 0
        # Prove tenant cache remains empty (no tenant components cached)
        assert len(container.tenant_cache) == 0
        assert not container.tenant_cache.contains("tenant_alpha")
        # Prove ModuleBPipeline and DocumentProcessingService were never instantiated
        assert spy_mod_b.call_count == 0
        assert spy_proc.call_count == 0


# ============================================================================
# Unconfigured storage / container shutdown returns 503
# ============================================================================

def test_api003_unconfigured_container_returns_typed_503(setup_api):
    """Unconfigured container or container shutdown returns typed 503 SERVICE_UNAVAILABLE."""
    client = setup_api["client"]
    container = setup_api["container"]
    doc_id = setup_api["doc_id"]

    # Shut down application container
    container.shutdown()

    response = client.get(
        f"/api/v1/documents/{doc_id}/pages/1",
        headers={"Authorization": "Bearer token-alpha-reader"},
    )
    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert payload["error"]["details"].get("port") == "ApplicationContainer"


# ============================================================================
# Standard scenarios: Success, Auth, Validation, Concealment
# ============================================================================

def test_api003_successful_authenticated_page_retrieval(setup_api):
    """Successful authenticated page-2 retrieval returns exact page-2 bytes."""
    client = setup_api["client"]
    doc_id = setup_api["doc_id"]
    page_bytes = setup_api["page_bytes"]

    response = client.get(
        f"/api/v1/documents/{doc_id}/pages/2",
        headers={"Authorization": "Bearer token-alpha-reader"},
    )
    assert response.status_code == 200
    assert response.content == page_bytes[2]
    assert response.content != page_bytes[1]


def test_api003_response_headers_and_media_type(setup_api):
    """Verifies exact required headers: nosniff, cache-control, and disposition."""
    client = setup_api["client"]
    doc_id = setup_api["doc_id"]

    response = client.get(
        f"/api/v1/documents/{doc_id}/pages/2",
        headers={"Authorization": "Bearer token-alpha-reader"},
    )
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/png"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Content-Disposition"] == 'inline; filename="page-2.png"'
    assert "X-Request-ID" in response.headers
    assert len(response.headers["X-Request-ID"]) > 0


def test_api003_missing_authentication_returns_401(setup_api):
    """Unauthenticated request returns 401 AUTHENTICATION_REQUIRED / INVALID_CREDENTIALS."""
    client = setup_api["client"]
    doc_id = setup_api["doc_id"]

    # No Authorization header
    response = client.get(f"/api/v1/documents/{doc_id}/pages/1")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert "WWW-Authenticate" in response.headers

    # Invalid token
    response_invalid = client.get(
        f"/api/v1/documents/{doc_id}/pages/1",
        headers={"Authorization": "Bearer unknown-token"},
    )
    assert response_invalid.status_code == 401
    assert response_invalid.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_api003_missing_scope_returns_403(setup_api):
    """Principal lacking documents:read scope returns 403 FORBIDDEN_SCOPE."""
    client = setup_api["client"]
    doc_id = setup_api["doc_id"]

    response = client.get(
        f"/api/v1/documents/{doc_id}/pages/1",
        headers={"Authorization": "Bearer token-alpha-writer-only"},
    )
    assert response.status_code == 403
    payload = response.json()
    assert payload["error"]["code"] == "FORBIDDEN_SCOPE"
    assert payload["error"]["details"].get("required_scope") == SCOPE_DOCUMENTS_READ


def test_api003_unknown_document_id_returns_concealed_404(setup_api):
    """Unknown document ID returns 404 DOCUMENT_NOT_FOUND."""
    client = setup_api["client"]

    response = client.get(
        "/api/v1/documents/doc_nonexistent_999/pages/1",
        headers={"Authorization": "Bearer token-alpha-reader"},
    )
    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "DOCUMENT_NOT_FOUND"
    assert payload["error"]["message"] == "Document not found."
    assert payload["error"]["details"] == {}


def test_api003_malformed_document_id_returns_concealed_404(setup_api):
    """Malformed document ID returns concealed 404 DOCUMENT_NOT_FOUND."""
    client = setup_api["client"]

    for malformed_id in (
        "..traversal",
        "doc..traversal",
        "doc@bad",
        "doc!invalid",
        "doc%20with%20space",
        "a" * 129,
    ):
        response = client.get(
            f"/api/v1/documents/{malformed_id}/pages/1",
            headers={"Authorization": "Bearer token-alpha-reader"},
        )
        assert response.status_code == 404
        payload = response.json()
        assert payload["error"]["code"] == "DOCUMENT_NOT_FOUND"
        assert payload["error"]["message"] == "Document not found."
        assert payload["error"]["details"] == {}


def test_api003_foreign_tenant_returns_concealed_404(setup_api):
    """Accessing another tenant's document returns 404 DOCUMENT_NOT_FOUND."""
    client = setup_api["client"]
    doc_id = setup_api["doc_id"]  # Belongs to tenant_alpha

    # Principal belongs to tenant_beta
    response = client.get(
        f"/api/v1/documents/{doc_id}/pages/1",
        headers={"Authorization": "Bearer token-beta-reader"},
    )
    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "DOCUMENT_NOT_FOUND"
    assert payload["error"]["message"] == "Document not found."
    assert payload["error"]["details"] == {}


def test_api003_identical_concealed_404_bodies(setup_api):
    """Verifies identical 404 bodies across unknown, malformed, foreign, and out-of-range IDs."""
    client = setup_api["client"]
    doc_id = setup_api["doc_id"]
    headers = {"Authorization": "Bearer token-alpha-reader"}

    res_unknown = client.get("/api/v1/documents/doc_missing/pages/1", headers=headers)
    res_malformed = client.get("/api/v1/documents/doc@bad/pages/1", headers=headers)
    res_foreign = client.get(
        f"/api/v1/documents/{doc_id}/pages/1",
        headers={"Authorization": "Bearer token-beta-reader"},
    )
    res_out_of_range = client.get(f"/api/v1/documents/{doc_id}/pages/99", headers=headers)

    responses = [res_unknown, res_malformed, res_foreign, res_out_of_range]
    for r in responses:
        assert r.status_code == 404
        body = r.json()["error"]
        assert body["code"] == "DOCUMENT_NOT_FOUND"
        assert body["message"] == "Document not found."
        assert body["details"] == {}
        assert "request_id" in body and body["request_id"]


def test_api003_page_number_zero_and_non_integer_returns_422(setup_api):
    """Non-integer or non-positive page numbers return 422 VALIDATION_ERROR."""
    client = setup_api["client"]
    doc_id = setup_api["doc_id"]
    headers = {"Authorization": "Bearer token-alpha-reader"}

    r_zero = client.get(f"/api/v1/documents/{doc_id}/pages/0", headers=headers)
    assert r_zero.status_code == 422
    assert r_zero.json()["error"]["code"] == "VALIDATION_ERROR"

    r_neg = client.get(f"/api/v1/documents/{doc_id}/pages/-1", headers=headers)
    assert r_neg.status_code == 422
    assert r_neg.json()["error"]["code"] == "VALIDATION_ERROR"

    r_str = client.get(f"/api/v1/documents/{doc_id}/pages/first", headers=headers)
    assert r_str.status_code == 422
    assert r_str.json()["error"]["code"] == "VALIDATION_ERROR"


def test_api003_out_of_range_or_unrendered_page_returns_concealed_404(setup_api):
    """Out-of-range page number returns concealed 404 DOCUMENT_NOT_FOUND."""
    client = setup_api["client"]
    doc_id = setup_api["doc_id"]
    headers = {"Authorization": "Bearer token-alpha-reader"}

    response = client.get(f"/api/v1/documents/{doc_id}/pages/3", headers=headers)
    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "DOCUMENT_NOT_FOUND"
    assert payload["error"]["message"] == "Document not found."
    assert payload["error"]["details"] == {}


def test_api003_never_provisioned_tenant_does_not_create_directory(setup_api):
    """Read operation for never-provisioned tenant does not create directory on disk."""
    client = setup_api["client"]
    tmp_path = setup_api["tmp_path"]

    fresh_tenant_dir = tmp_path / "tenant_never_provisioned"
    assert not fresh_tenant_dir.exists()

    response = client.get(
        "/api/v1/documents/doc_does_not_matter/pages/1",
        headers={"Authorization": "Bearer token-fresh-tenant"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
    assert not fresh_tenant_dir.exists()


def test_api003_hashed_tenant_directory_is_served_through_the_route(setup_api):
    """
    A tenant id outside [a-z0-9_-] (here "Tenant.A") is stored by the REAL storage mapper under a hashed directory
    (`h+<hash>`), never under its raw name. The page is created through that mapper and read back through the actual
    route; a read service that used the raw tenant id as the directory name would answer 404 instead of the bytes.
    """
    tmp_path, client = setup_api["tmp_path"], setup_api["client"]
    tenant_id, doc_id = "Tenant.A", "doc_hashed_tenant"

    storage = LocalDiskStorage(tmp_path, tenant_id=tenant_id)
    page_bytes = _create_test_document(storage, doc_id, tenant_id, num_pages=1)
    setup_api["app"].state.auth_provider.principals["token-hashed-tenant"] = ApiPrincipal(
        principal_id="user_hashed",
        tenant_id=tenant_id,
        principal_type=PrincipalType.USER,
        scopes={SCOPE_DOCUMENTS_READ},
        is_active=True,
    )

    # The mapping is the storage layer's own: the data lives in the mapper's directory, not under the raw id.
    mapped_dir_name = storage._tenant_dir(create=False).name
    assert mapped_dir_name != tenant_id and mapped_dir_name.startswith("h+")
    assert (tmp_path / mapped_dir_name / doc_id / "module_a_handoff.json").is_file()
    assert not (tmp_path / tenant_id).exists()

    response = client.get(
        f"/api/v1/documents/{doc_id}/pages/1",
        headers={"Authorization": "Bearer token-hashed-tenant"},
    )
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "image/png"
    assert response.content == page_bytes[1]
    assert str(tmp_path) not in response.text and mapped_dir_name not in "".join(response.headers.values())

    # Tenant binding is unchanged: another tenant cannot read it, and it cannot read another tenant's document.
    foreign = client.get(f"/api/v1/documents/{doc_id}/pages/1", headers={"Authorization": "Bearer token-beta-reader"})
    assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
    other = client.get(
        f"/api/v1/documents/{setup_api['doc_id']}/pages/1", headers={"Authorization": "Bearer token-hashed-tenant"}
    )
    assert other.status_code == 404 and other.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_api003_no_filesystem_path_appears_in_responses(setup_api):
    """Server filesystem paths never appear in 200, 401, 403, 404, 422, or 500 responses."""
    client = setup_api["client"]
    tmp_path_str = str(setup_api["tmp_path"])
    doc_id = setup_api["doc_id"]
    headers = {"Authorization": "Bearer token-alpha-reader"}

    test_endpoints = [
        client.get(f"/api/v1/documents/{doc_id}/pages/1", headers=headers),
        client.get(f"/api/v1/documents/{doc_id}/pages/1"),
        client.get(
            f"/api/v1/documents/{doc_id}/pages/1",
            headers={"Authorization": "Bearer token-alpha-writer-only"},
        ),
        client.get(f"/api/v1/documents/{doc_id}/pages/99", headers=headers),
        client.get(f"/api/v1/documents/{doc_id}/pages/0", headers=headers),
    ]

    for resp in test_endpoints:
        assert tmp_path_str not in resp.text
        for h_key, h_val in resp.headers.items():
            assert tmp_path_str not in h_val


def test_validate_document_id_contract():
    """Unit test for validate_document_id safety contract."""
    assert validate_document_id("doc_123") == "doc_123"
    assert validate_document_id("doc-ABC_456") == "doc-ABC_456"
    assert validate_document_id("a" * 128) == "a" * 128

    for invalid in (
        "",
        "   ",
        None,
        123,
        True,
        False,
        "../traversal",
        "/absolute",
        "doc/sub",
        "doc\\sub",
        "doc\x00null",
        "doc\nnewline",
        "a" * 129,
    ):
        with pytest.raises(TenantResourceNotFoundError):
            validate_document_id(invalid)
