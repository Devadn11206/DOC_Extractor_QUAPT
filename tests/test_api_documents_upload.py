"""
Tests for API-001: POST /api/v1/documents (Milestone 2D, upload only, PROVISIONAL).

What is verified (see docs/m5-open-decisions.md, "M2D API-001 Provisional Behavior and Limitations"):

- The endpoint is a thin adapter over the EXISTING Module A pipeline: the real ``ModuleAPipeline`` runs (real validation,
  hashing, idempotency store, malware scanner double, record write, page rendering, preprocessing, handoff). The accepted
  response is HTTP 202 with ``document_id`` ONLY.
- Errors follow the PRD and are built from fixed text: nothing Module A puts in an error (filename, header dump, threat
  name, path, key, exception text) reaches a response, and the client's filename never reaches a log record.
- A pure ASGI guard refuses an over-limit body BEFORE the multipart parser spools it. It is tested at the ASGI level with
  chunked bodies, understated ``Content-Length`` and a counting ``receive``.
- Authentication, the provisional ``documents:write`` scope, tenant isolation and idempotency (including concurrency).
- Compatibility: an uploaded document is served by API-002, API-003 and API-015.
"""

import asyncio
import ast
import inspect
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import starlette.formparsers
from fastapi.testclient import TestClient

from doc_extractor import ingestion as ingestion_module
from doc_extractor.api import middleware as middleware_module
from doc_extractor.api.app import create_app
from doc_extractor.api.composition import ApplicationContainer
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.contracts.documents import (
    SCOPE_DOCUMENTS_READ,
    SCOPE_DOCUMENTS_WRITE,
    DocumentUploadResponse,
)
from doc_extractor.api.exceptions import FileTooLargeError, get_status_code_for_exception
from doc_extractor.api.middleware import UploadSizeLimitMiddleware
from doc_extractor.api.ports.authentication import ApiPrincipal
from doc_extractor.api.services import document_upload_service as upload_service_module
from doc_extractor.api.services.document_upload_service import DocumentUploadService, public_upload_error
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.exceptions import (
    ConflictError,
    DocumentProcessingError,
    MalwareDetectedError,
    ScannerUnavailableError,
    StorageError,
    TenantMismatchError,
    UnsupportedFileError,
)
from doc_extractor.malware import (
    EICAR_SIGNATURE,
    MalwareScanResult,
    MockMalwareScanner,
    ScanStatus,
    UnconfiguredScanner,
)
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.renderer import PdfRenderer
from doc_extractor.storage import LocalDiskStorage
from tests.conftest import create_minimal_jpeg, create_minimal_pdf, create_minimal_png, create_minimal_tiff
from tests.test_api_documents_page_image import MockAuthProvider

URL = "/api/v1/documents"
WRITER = {"Authorization": "Bearer token-alpha-writer-only"}
READER = {"Authorization": "Bearer token-alpha-reader"}
BETA_WRITER = {"Authorization": "Bearer token-beta-writer"}
ALPHA_RW = {"Authorization": "Bearer token-alpha-rw"}
DOC_ID = re.compile(r"^doc_[0-9a-f]{12}$")
SECRET_NAME = "SECRET-NAME-123.pdf"


# ---------------------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------------------

def _make_api(tmp_path, *, scanner=None, **settings_overrides):
    settings = ApiSettings(app_env="development", storage_base_dir=str(tmp_path), **settings_overrides)
    container = ApplicationContainer(
        api_settings=settings,
        auth_provider=MockAuthProvider(),
        malware_scanner=scanner if scanner is not None else MockMalwareScanner(is_ready=True),
    )
    app = create_app(settings=settings, container=container)
    principals = app.state.auth_provider.principals
    principals["token-beta-writer"] = ApiPrincipal(
        principal_id="beta_writer", tenant_id="tenant_beta", scopes={SCOPE_DOCUMENTS_WRITE}, is_active=True
    )
    principals["token-alpha-rw"] = ApiPrincipal(
        principal_id="alpha_rw", tenant_id="tenant_alpha",
        scopes={SCOPE_DOCUMENTS_READ, SCOPE_DOCUMENTS_WRITE}, is_active=True,
    )
    return {"app": app, "client": TestClient(app), "container": container, "tmp_path": tmp_path, "settings": settings}


@pytest.fixture
def api(tmp_path):
    return _make_api(tmp_path)


def _real_jpeg(seed: int = 0) -> bytes:
    """A JPEG Pillow can really decode (the conftest one is structure-only)."""
    import io
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (64 + seed, 48), (200, 30, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _real_tiff(pages: int = 1) -> bytes:
    """A (multi-page) TIFF Pillow can really decode (the conftest one is structure-only)."""
    import io
    from PIL import Image
    frames = [Image.new("RGB", (40 + i, 30), (10 * i, 80, 120)) for i in range(pages)]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="TIFF", save_all=True, append_images=frames[1:])
    return buffer.getvalue()


def _pdf(marker: str = "m0", pages: int = 1) -> bytes:
    """A valid PDF made unique (hash) by a trailing comment."""
    return create_minimal_pdf(pages) + b"\n%" + marker.encode() + b"\n"


def _post(api, content: bytes = None, *, name: str = "scan.pdf", content_type: str = "application/pdf", headers=WRITER,
          key: str = None, data: dict = None, params: dict = None):
    content = _pdf() if content is None else content
    send_headers = dict(headers)
    if key is not None:
        send_headers["Idempotency-Key"] = key
    return api["client"].post(
        URL, files={"file": (name, content, content_type)}, data=data or {}, headers=send_headers, params=params
    )


def _storage(api, tenant: str = "tenant_alpha") -> LocalDiskStorage:
    return LocalDiskStorage(api["tmp_path"], tenant_id=tenant)


def _doc_dirs(api, tenant: str = "tenant_alpha") -> list:
    tenant_dir = api["tmp_path"] / tenant
    if not tenant_dir.exists():
        return []
    return sorted(p.name for p in tenant_dir.iterdir() if p.is_dir() and p.name.startswith("doc_"))


def _all_doc_dirs(api) -> list:
    return sorted(p.name for p in api["tmp_path"].rglob("doc_*") if p.is_dir())


def _error(response) -> dict:
    return response.json()["error"]


def _assert_public_error(response, status: int, code: str, *forbidden: str) -> dict:
    assert response.status_code == status, response.text
    body = _error(response)
    assert body["code"] == code
    assert body["details"] == {}, "an upload error never carries details"
    assert body["request_id"] and response.headers["X-Request-ID"] == body["request_id"]
    text = response.text
    for leak in (*forbidden, SECRET_NAME, "tenant_alpha", "tenant_beta"):
        assert leak not in text, f"{leak!r} leaked into an error response"
    return body


def _multipart(file_bytes: bytes, *, filename: str = "scan.pdf", fields: dict = None, boundary: str = "----apiboundary") -> bytes:
    parts = []
    for name, value in (fields or {}).items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n".encode() + file_bytes + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


def _asgi_post(app, chunks, *, content_length=None, token: str = "token-alpha-writer-only", path: str = URL,
               boundary: str = "----apiboundary", root_path: str = ""):
    """
    Drive the REAL ASGI app with a hand-made, lazily produced body (no ``Content-Length`` unless given) and a counting
    ``receive``. Returns (status, body bytes, {"pulled": bytes read by the app, "calls": receive calls}, sent messages).
    """
    headers = [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {token}".encode()),
        (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
    ]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    iterator = iter(chunks)
    pending = {"next": next(iterator, None)}
    counters = {"pulled": 0, "calls": 0}
    sent = []

    async def receive():
        counters["calls"] += 1
        chunk = pending["next"]
        if chunk is None:
            return {"type": "http.disconnect"}
        pending["next"] = next(iterator, None)
        counters["pulled"] += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": pending["next"] is not None}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "path": path,
        "raw_path": path.encode(), "query_string": b"", "headers": headers, "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80), "scheme": "http", "root_path": root_path,
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, counters, sent


def _streamed_body(file_bytes_total: int, *, chunk: int = 65536, filename: str = "big.pdf", boundary: str = "----apiboundary"):
    """A multipart body whose file part is ``file_bytes_total`` bytes, produced lazily chunk by chunk (never held whole)."""
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/pdf\r\n\r\n".encode() + b"%PDF-1.4\n"
    )
    tail = f"\r\n--{boundary}--\r\n".encode()

    def generate():
        yield head
        remaining = file_bytes_total
        while remaining > 0:
            size = min(chunk, remaining)
            yield b"0" * size
            remaining -= size
        yield tail

    return generate()


class _CaptureAll(logging.Handler):
    """Every log record, serialized WITH its extra attributes (the message alone would hide structured fields)."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(json.dumps(record.__dict__, default=str))

    @property
    def text(self) -> str:
        return "\n".join(self.records)


@pytest.fixture
def logs():
    handler = _CaptureAll()
    targets = [logging.getLogger(), logging.getLogger("doc_extractor")]
    saved = [(t, t.level) for t in targets]
    for target in targets:
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        for target, level in saved:
            target.removeHandler(handler)
            target.setLevel(level)


@pytest.fixture
def pipeline_spy(monkeypatch):
    """Counts Module A runs and PDF renders; both still run for real."""
    calls = {"process": 0, "render": 0}
    original_process = ModuleAPipeline.process_document
    original_render = PdfRenderer.render_pdf

    def process(self, *args, **kwargs):
        calls["process"] += 1
        return original_process(self, *args, **kwargs)

    def render(self, *args, **kwargs):
        calls["render"] += 1
        return original_render(self, *args, **kwargs)

    monkeypatch.setattr(ModuleAPipeline, "process_document", process)
    monkeypatch.setattr(PdfRenderer, "render_pdf", render)
    return calls


# ============================================================================
# Acceptance: 202, document_id only, record and handoff created by the real Module A
# ============================================================================

_ACCEPTED = [
    pytest.param(lambda: _pdf("acc-pdf", 3), "scan.pdf", "application/pdf", 3, "application/pdf", id="pdf-3-pages"),
    pytest.param(lambda: create_minimal_png(), "photo.png", "image/png", 1, "image/png", id="png"),
    pytest.param(lambda: _real_jpeg(), "photo.jpg", "image/jpeg", 1, "image/jpeg", id="jpeg"),
    pytest.param(lambda: _real_tiff(2), "scan.tif", "image/tiff", 2, "image/tiff", id="tiff-2-pages"),
]


@pytest.mark.parametrize("make, name, content_type, pages, mime", _ACCEPTED)
def test_upload_accepted_formats_return_202_with_document_id_only(api, make, name, content_type, pages, mime):
    response = _post(api, make(), name=name, content_type=content_type)

    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"document_id"}, "the 202 body carries the document id and nothing else"
    assert DOC_ID.match(body["document_id"])
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Request-ID"]
    assert set(DocumentUploadResponse.model_fields) == {"document_id"}

    doc_dir = _storage(api)._get_document_dir(body["document_id"])
    record = json.loads((doc_dir / "document_record.json").read_text(encoding="utf-8"))
    assert record["document_id"] == body["document_id"] and record["tenant_id"] == "tenant_alpha"
    assert record["mime_type"] == mime and record["page_count"] == pages
    assert (record["status"], record["internal_state"]) == ("processing", "queued")
    handoff = json.loads((doc_dir / "module_a_handoff.json").read_text(encoding="utf-8"))
    assert handoff["document_id"] == body["document_id"] and handoff["page_count"] == pages and handoff["error"] is None
    assert len(handoff["pages"]) == pages
    for page in range(1, pages + 1):
        assert (doc_dir / "pages" / f"page_{page:04d}.png").is_file()
        assert (doc_dir / "preprocessed" / f"page_{page:04d}_preprocessed.png").is_file()
    assert _doc_dirs(api) == [body["document_id"]]


def test_upload_pdf_original_is_stored_and_the_record_is_the_only_status_source(api):
    document_id = _post(api, _pdf("orig")).json()["document_id"]
    doc_dir = _storage(api)._get_document_dir(document_id)
    handoff = json.loads((doc_dir / "module_a_handoff.json").read_text(encoding="utf-8"))
    assert handoff["original_document_storage_uri"]
    assert handoff["status"] == "processing"            # nothing continues in the background: it stays processing


def test_upload_filename_and_media_type_are_untrusted(api):
    # a path-traversal filename is reduced to its basename; nothing is written outside the storage root
    outside_before = sorted(p.name for p in api["tmp_path"].parent.iterdir())
    response = _post(api, _pdf("trav"), name="../../../../etc/passwd.pdf")
    assert response.status_code == 202
    record = json.loads((_storage(api)._get_document_dir(response.json()["document_id"]) / "document_record.json").read_text())
    assert record["claimed_filename"] == "passwd.pdf"
    assert sorted(p.name for p in api["tmp_path"].parent.iterdir()) == outside_before
    # the client's media type is never consulted: the magic bytes decide, in both directions
    assert _post(api, _pdf("ct1"), name="a.pdf", content_type="application/x-msdownload").status_code == 202
    mismatch = _post(api, create_minimal_png(), name="a.png", content_type="application/pdf")
    assert mismatch.status_code == 202
    assert json.loads((_storage(api)._get_document_dir(mismatch.json()["document_id"]) / "document_record.json").read_text())["mime_type"] == "image/png"


def test_upload_openapi_documents_the_multipart_contract(api):
    schema = api["client"].get("/openapi.json").json()
    operation = schema["paths"][URL]["post"]
    assert "202" in operation["responses"] and {"400", "401", "403", "409", "413", "422", "503"} <= set(operation["responses"])
    body_ref = operation["requestBody"]["content"]["multipart/form-data"]["schema"]["$ref"].split("/")[-1]
    properties = schema["components"]["schemas"][body_ref]["properties"]
    assert set(properties) == {"file", "document_type_hint", "schema_id", "callback_url"}
    assert any(p["name"] == "Idempotency-Key" and p["in"] == "header" for p in operation["parameters"])
    assert operation["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith("/DocumentUploadResponse")
    assert "get" in schema["paths"][URL]                                  # API-015 is still there next to it


def test_upload_is_a_synchronous_route():
    from doc_extractor.api.routers.documents import upload_document
    assert not inspect.iscoroutinefunction(upload_document)


# ============================================================================
# Compatibility with API-002, API-003 and API-015
# ============================================================================

def test_uploaded_document_is_served_by_api002_api003_and_api015(api):
    document_id = _post(api, _pdf("compat", 2), headers=ALPHA_RW, key="compat-key").json()["document_id"]

    detail = api["client"].get(f"{URL}/{document_id}", headers=ALPHA_RW)
    assert detail.status_code == 200
    body = detail.json()
    assert body["document_id"] == document_id and body["status"] == "processing" and body["page_count"] == 2
    assert body["result"] is None and body["filename"] == "scan.pdf" and body["mime_type"] == "application/pdf"

    for page in (1, 2):
        image = api["client"].get(f"{URL}/{document_id}/pages/{page}", headers=ALPHA_RW)
        assert image.status_code == 200 and image.headers["Content-Type"] == "image/png"
        assert image.content.startswith(b"\x89PNG")
    assert api["client"].get(f"{URL}/{document_id}/pages/3", headers=ALPHA_RW).status_code == 404

    listing = api["client"].get(URL, headers=ALPHA_RW)
    assert listing.status_code == 200
    assert [row["document_id"] for row in listing.json()["content"]] == [document_id]
    assert listing.json()["paging"]["total_items"] == 1 and listing.json()["stats"]["processing"] == 1
    assert listing.json()["content"][0] == {k: v for k, v in body.items() if k != "result"}


# ============================================================================
# Rejections: unsupported, spoofed, empty, corrupt, limits
# ============================================================================

_UNSUPPORTED = [
    pytest.param(b"MZ\x90\x00\x03 this is an executable " * 20, "payload.pdf", id="renamed-exe-as-pdf"),
    pytest.param(create_minimal_png(), "photo.pdf", id="png-bytes-named-pdf"),
    pytest.param(_pdf("spoof"), "document.png", id="pdf-bytes-named-png"),
    pytest.param(create_minimal_jpeg(), "photo.png", id="jpeg-bytes-named-png"),
    pytest.param(b"plain text, not a document at all", "notes.pdf", id="plain-text"),
    pytest.param(b"", "empty.pdf", id="empty-file"),
    pytest.param(b"%PDF-1.4\n", "truncated.pdf", id="corrupt-truncated-pdf"),
    pytest.param(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "bad.png", id="corrupt-png"),
    pytest.param(b"\xff\xd8\xff\xe0" + b"\x00" * 10, "bad.jpg", id="corrupt-jpeg"),
    pytest.param(b"II*\x00" + b"\x00" * 6, "bad.tif", id="corrupt-tiff"),
]


@pytest.mark.parametrize("content, name", _UNSUPPORTED)
def test_upload_unsupported_spoofed_empty_and_corrupt_files_are_400_and_nothing_is_stored(api, content, name, logs):
    response = _post(api, content, name=name)
    body = _assert_public_error(response, 400, "UNSUPPORTED_FILE", "detected_header", "MAGIC", "reason")
    assert body["message"] == "This file type isn't supported."
    assert _all_doc_dirs(api) == [], "a rejected upload stores nothing"


def test_upload_rejection_error_never_echoes_the_filename_or_header_bytes(api, logs):
    response = _post(api, b"MZ\x90\x00 secret-header-bytes", name=SECRET_NAME)
    _assert_public_error(response, 400, "UNSUPPORTED_FILE", "4d5a9000", "secret-header-bytes", "filename")
    assert SECRET_NAME not in logs.text and "4d5a9000" not in logs.text


def test_upload_page_limit_overflow_is_413_file_too_large(tmp_path):
    api = _make_api(tmp_path, max_page_count=2)
    pdf = _post(api, _pdf("pages", 3))
    body = _assert_public_error(pdf, 413, "FILE_TOO_LARGE", "page_count", "max_pages")
    assert body["message"] == "The file is larger than the limit."
    tiff = _post(api, _real_tiff(3), name="scan.tif", content_type="image/tiff")
    _assert_public_error(tiff, 413, "FILE_TOO_LARGE", "page_count", "max_pages")
    assert _all_doc_dirs(api) == []
    assert _post(api, _pdf("pages-ok", 2)).status_code == 202            # exactly at the limit is accepted


def test_upload_file_over_the_byte_limit_but_inside_the_body_headroom_is_413_from_module_a(tmp_path, pipeline_spy):
    api = _make_api(tmp_path, max_file_size_bytes=50 * 1024)
    big = create_minimal_pdf(1) + b"\n%" + b"x" * (60 * 1024) + b"\n"             # 60 KiB > 50 KiB, far below limit + 1 MiB
    response = _post(api, big)
    body = _assert_public_error(response, 413, "FILE_TOO_LARGE", "FILE_SIZE_LIMIT_EXCEEDED", "51200", "bytes")
    assert body["message"] == "The file is larger than the limit."
    assert pipeline_spy["process"] == 1, "this one passed the body guard and was judged by Module A's own size check"
    assert pipeline_spy["render"] == 0 and _all_doc_dirs(api) == []


# ============================================================================
# Malware and scanner availability
# ============================================================================

def test_upload_malware_is_rejected_before_any_processing_stage(api, pipeline_spy, monkeypatch, logs):
    def forbidden(*args, **kwargs):
        raise AssertionError("a processing stage ran for an infected file")

    monkeypatch.setattr("doc_extractor.pipeline.PagePreprocessor.preprocess_document_pages", forbidden)
    infected = create_minimal_pdf(1) + b"\n%" + EICAR_SIGNATURE + b"\n"

    response = _post(api, infected, name=SECRET_NAME)

    body = _assert_public_error(response, 400, "MALWARE_DETECTED", "EICAR", "threat", "signature")
    assert body["message"] == "This file was blocked by the safety scan."
    assert pipeline_spy["render"] == 0
    assert _all_doc_dirs(api) == [], "no record, page, preprocessed image or handoff exists for an infected file"
    assert "EICAR" not in logs.text and SECRET_NAME not in logs.text


@pytest.mark.parametrize("scanner", [
    pytest.param(UnconfiguredScanner(), id="unconfigured-scanner"),
    pytest.param(MockMalwareScanner(is_ready=False), id="scanner-not-ready"),
    pytest.param(MockMalwareScanner(simulate_error=True), id="scanner-simulated-failure"),
])
def test_upload_with_an_unavailable_scanner_fails_closed_with_a_typed_503(tmp_path, scanner, pipeline_spy):
    api = _make_api(tmp_path, scanner=scanner)
    response = _post(api, _pdf("scan-down"), name=SECRET_NAME)
    body = _assert_public_error(response, 503, "SCANNER_UNAVAILABLE", "Scanner", "scanner_type", "Mock", "Unconfigured")
    assert "unavailable" in body["message"].lower()
    assert pipeline_spy["render"] == 0 and _all_doc_dirs(api) == []


def test_upload_when_the_scan_itself_errors_is_also_a_503(tmp_path):
    class ErroringScanner(MockMalwareScanner):
        def scan_stream(self, stream):
            return MalwareScanResult(status=ScanStatus.ERROR, engine_name="SECRET-ENGINE", details="SECRET-DETAILS")

    api = _make_api(tmp_path, scanner=ErroringScanner(is_ready=True))
    response = _post(api, _pdf("scan-error"))
    _assert_public_error(response, 503, "SCANNER_UNAVAILABLE", "SECRET-ENGINE", "SECRET-DETAILS")
    assert _all_doc_dirs(api) == []


# ============================================================================
# Request-size guard: refused BEFORE the multipart body is parsed or spooled
# ============================================================================

def test_oversize_with_content_length_is_413_before_parsing_or_ingestion(tmp_path, monkeypatch, pipeline_spy):
    monkeypatch.setattr(middleware_module, "UPLOAD_MULTIPART_OVERHEAD_BYTES", 1024)
    api = _make_api(tmp_path, max_file_size_bytes=10 * 1024)               # body limit = 11 KiB
    parses = []
    original_parse = starlette.formparsers.MultiPartParser.parse

    async def spy_parse(self):
        parses.append(1)
        return await original_parse(self)

    monkeypatch.setattr(starlette.formparsers.MultiPartParser, "parse", spy_parse)

    response = _post(api, b"%PDF-1.4\n" + b"0" * (200 * 1024), name=SECRET_NAME)

    body = _assert_public_error(response, 413, "FILE_TOO_LARGE")
    assert body["message"] == "The file is larger than the limit."
    assert response.headers["Connection"] == "close"
    assert parses == [], "the multipart parser must not run for an over-limit body"
    assert pipeline_spy["process"] == 0 and _all_doc_dirs(api) == []


def test_oversize_without_content_length_is_stopped_while_streaming(tmp_path, monkeypatch, pipeline_spy):
    monkeypatch.setattr(middleware_module, "UPLOAD_MULTIPART_OVERHEAD_BYTES", 1024)
    api = _make_api(tmp_path, max_file_size_bytes=10 * 1024)
    limit = 10 * 1024 + 1024
    chunk = 8192
    total = 50 * 1024 * 1024                                              # 50 MiB offered, never produced in one piece

    status, body, counters, sent = _asgi_post(api["app"], _streamed_body(total, chunk=chunk))

    assert status == 413
    error = json.loads(body)["error"]
    assert error["code"] == "FILE_TOO_LARGE" and error["details"] == {} and error["request_id"]
    assert counters["pulled"] <= limit + chunk + 1024, f"the app pulled {counters['pulled']} bytes of {total}"
    assert counters["pulled"] > limit, "the guard trips only once the running total exceeds the limit"
    assert pipeline_spy["process"] == 0 and _all_doc_dirs(api) == []
    started = [m for m in sent if m["type"] == "http.response.start"]
    assert len(started) == 1, "exactly one response, even though the application was cut off mid-parse"


def test_oversize_with_an_understated_content_length_is_still_stopped(tmp_path, monkeypatch, pipeline_spy):
    monkeypatch.setattr(middleware_module, "UPLOAD_MULTIPART_OVERHEAD_BYTES", 1024)
    api = _make_api(tmp_path, max_file_size_bytes=10 * 1024)
    limit = 10 * 1024 + 1024
    status, body, counters, _ = _asgi_post(
        api["app"], _streamed_body(20 * 1024 * 1024, chunk=8192), content_length=100          # the client lies
    )
    assert status == 413 and json.loads(body)["error"]["code"] == "FILE_TOO_LARGE"
    assert counters["pulled"] <= limit + 8192 + 1024
    assert pipeline_spy["process"] == 0


def test_oversize_with_a_declared_length_reads_no_body_at_all(tmp_path, monkeypatch, pipeline_spy):
    monkeypatch.setattr(middleware_module, "UPLOAD_MULTIPART_OVERHEAD_BYTES", 1024)
    api = _make_api(tmp_path, max_file_size_bytes=10 * 1024)
    status, body, counters, _ = _asgi_post(api["app"], _streamed_body(1024 * 1024), content_length=5 * 1024 * 1024)
    assert status == 413 and json.loads(body)["error"]["code"] == "FILE_TOO_LARGE"
    assert counters == {"pulled": 0, "calls": 0}, "a declared over-limit length is refused without touching the body"
    assert pipeline_spy["process"] == 0


def test_a_chunked_upload_within_the_limit_is_parsed_and_accepted(api):
    pdf = _pdf("chunked", 1)
    body = _multipart(pdf)
    chunks = [body[i:i + 700] for i in range(0, len(body), 700)]                 # many small chunks, no Content-Length
    status, response_body, counters, _ = _asgi_post(api["app"], chunks)
    assert status == 202, response_body
    assert set(json.loads(response_body)) == {"document_id"}
    assert counters["pulled"] == len(body)


def test_the_guard_runs_before_authentication_but_an_in_limit_unauthenticated_upload_is_401(tmp_path, monkeypatch):
    monkeypatch.setattr(middleware_module, "UPLOAD_MULTIPART_OVERHEAD_BYTES", 1024)
    api = _make_api(tmp_path, max_file_size_bytes=10 * 1024)
    oversize = api["client"].post(URL, files={"file": ("a.pdf", b"%PDF-1.4\n" + b"0" * 100_000, "application/pdf")})
    assert oversize.status_code == 413                                   # decided from the length alone, no credential needed
    small = api["client"].post(URL, files={"file": ("a.pdf", _pdf("auth"), "application/pdf")})
    assert small.status_code == 401


def test_documented_limitation_the_body_is_parsed_before_authentication_is_decided(api, monkeypatch):
    """
    PINS A LIMITATION (docs/m5-open-decisions.md, API-001): the framework parses the multipart body BEFORE it evaluates the
    route dependencies, so an unauthenticated or under-scoped caller still makes the server parse (and spool) a body of up to
    the guard's limit before the 401 or 403. The size guard bounds this; rate limiting (API-012) does not exist yet.
    """
    parses = []
    original_parse = starlette.formparsers.MultiPartParser.parse

    async def spy_parse(self):
        parses.append(1)
        return await original_parse(self)

    monkeypatch.setattr(starlette.formparsers.MultiPartParser, "parse", spy_parse)
    assert api["client"].post(URL, files={"file": ("a.pdf", _pdf("lim"), "application/pdf")}).status_code == 401
    assert _post(api, headers=READER).status_code == 403
    assert len(parses) == 2


def test_the_413_carries_the_request_id_and_a_safe_client_supplied_one_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(middleware_module, "UPLOAD_MULTIPART_OVERHEAD_BYTES", 1024)
    api = _make_api(tmp_path, max_file_size_bytes=10 * 1024)
    response = api["client"].post(
        URL, files={"file": ("a.pdf", b"0" * 100_000, "application/pdf")}, headers={**WRITER, "X-Request-ID": "client-req-0001"}
    )
    assert response.status_code == 413
    assert response.headers["X-Request-ID"] == "client-req-0001" == _error(response)["request_id"]


# ---- the middleware in isolation ------------------------------------------------------------------------------------------

def _drive(middleware, *, method="POST", path="/up", headers=(), chunks=(b"",), scope_type="http", root_path=""):
    seen = {"app_called": False, "body": 0}
    counters = {"calls": 0}
    iterator = iter(chunks)
    pending = {"next": next(iterator, None)}
    sent = []

    async def receive():
        counters["calls"] += 1
        chunk = pending["next"]
        if chunk is None:
            return {"type": "http.disconnect"}
        pending["next"] = next(iterator, None)
        return {"type": "http.request", "body": chunk, "more_body": pending["next"] is not None}

    async def send(message):
        sent.append(message)

    scope = {
        "type": scope_type,
        "method": method,
        "path": path,
        "headers": list(headers),
        "state": {"request_id": "req-test-0001"},
        "root_path": root_path,
    }
    asyncio.run(middleware(scope, receive, send))
    return sent, counters


def _echo_app(record):
    async def app(scope, receive, send):
        record["called"] = record.get("called", 0) + 1
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            total += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        record["bytes"] = total
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(total).encode()})
    return app


def _status(sent):
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_guard_boundary_is_exact_for_one_big_chunk_and_for_one_byte_chunks():
    record = {}
    guard = UploadSizeLimitMiddleware(_echo_app(record), path="/up", max_body_bytes=100)
    sent, _ = _drive(guard, chunks=[b"x" * 100])
    assert _status(sent) == 200 and record["bytes"] == 100                      # exactly at the limit passes
    record.clear()
    sent, _ = _drive(guard, chunks=[b"x" * 101])
    assert _status(sent) == 413 and record.get("bytes") == 0, "the app is cut off: it sees a disconnect, never the body"
    record.clear()
    sent, counters = _drive(guard, chunks=[b"x"] * 500)
    assert _status(sent) == 413
    assert counters["calls"] <= 102, "byte-by-byte input is cut off at limit + 1 bytes, not read to the end"


def test_guard_declared_length_exactly_at_the_limit_is_accepted_and_one_over_is_refused():
    record = {}
    guard = UploadSizeLimitMiddleware(_echo_app(record), path="/up", max_body_bytes=100)
    sent, _ = _drive(guard, headers=[(b"content-length", b"100")], chunks=[b"x" * 100])
    assert _status(sent) == 200 and record["bytes"] == 100
    record.clear()
    sent, counters = _drive(guard, headers=[(b"content-length", b"101")], chunks=[b"x" * 101])
    assert _status(sent) == 413 and "called" not in record and counters["calls"] == 0


def test_guard_is_scoped_to_one_method_and_one_exact_path():
    record = {}
    guard = UploadSizeLimitMiddleware(_echo_app(record), path="/up", max_body_bytes=10)
    huge = [(b"content-length", b"999999")]
    for kwargs in ({"method": "GET"}, {"method": "PUT"}, {"path": "/other"}, {"path": "/up/more"}, {"path": "/UP"}):
        record.clear()
        sent, _ = _drive(guard, headers=huge, chunks=[b"x" * 50], **kwargs)
        assert _status(sent) == 200 and record["bytes"] == 50, f"{kwargs} must pass through untouched"
    record.clear()
    sent, _ = _drive(guard, headers=huge, chunks=[b"x"])
    assert _status(sent) == 413 and "called" not in record, "the guarded request is refused without calling the app"


def test_guard_handles_root_path_correctly():
    record = {}
    guard = UploadSizeLimitMiddleware(_echo_app(record), path="/up", max_body_bytes=10)
    huge = [(b"content-length", b"999999")]

    # Behind /svc root_path, /svc/up resolves to route /up and is guarded
    record.clear()
    sent, _ = _drive(guard, headers=huge, chunks=[b"x"], path="/svc/up", root_path="/svc")
    assert _status(sent) == 413 and "called" not in record

    # Behind /svc root_path, an unrelated route passes through
    record.clear()
    sent, _ = _drive(guard, headers=huge, chunks=[b"x" * 50], path="/svc/other", root_path="/svc")
    assert _status(sent) == 200 and record["bytes"] == 50

    # In-limit body behind root_path is accepted
    record.clear()
    sent, _ = _drive(guard, chunks=[b"x" * 10], path="/svc/up", root_path="/svc")
    assert _status(sent) == 200 and record["bytes"] == 10


def test_app_upload_size_limit_with_root_path(tmp_path, monkeypatch):
    monkeypatch.setattr(middleware_module, "UPLOAD_MULTIPART_OVERHEAD_BYTES", 1024)
    api = _make_api(tmp_path, max_file_size_bytes=10 * 1024)  # limit 11 KiB

    # 1. Declared oversize behind root_path is refused with 413 before reading body
    status, body, counters, _ = _asgi_post(
        api["app"],
        _streamed_body(1024 * 1024),
        content_length=5 * 1024 * 1024,
        path="/svc/api/v1/documents",
        root_path="/svc",
    )
    assert status == 413
    assert json.loads(body)["error"]["code"] == "FILE_TOO_LARGE"
    assert counters["pulled"] == 0

    # 2. Chunked oversize behind root_path is stopped while streaming
    status, body, counters, _ = _asgi_post(
        api["app"],
        _streamed_body(50 * 1024 * 1024, chunk=8192),
        path="/svc/api/v1/documents",
        root_path="/svc",
    )
    assert status == 413
    assert json.loads(body)["error"]["code"] == "FILE_TOO_LARGE"

    # 3. In-limit upload behind root_path is accepted
    pdf = _pdf("root-chunked", 1)
    boundary = "----apiboundary"
    body_bytes = _multipart(pdf, boundary=boundary)
    chunks = [body_bytes[i:i + 700] for i in range(0, len(body_bytes), 700)]
    status, response_body, counters, _ = _asgi_post(
        api["app"], chunks, path="/svc/api/v1/documents", root_path="/svc", boundary=boundary
    )
    assert status == 202
    assert set(json.loads(response_body)) == {"document_id"}


def test_guard_passes_non_http_scopes_through_unchanged():
    called = {}

    async def app(scope, receive, send):
        called["type"] = scope["type"]

    guard = UploadSizeLimitMiddleware(app, path="/up", max_body_bytes=1)
    asyncio.run(guard({"type": "lifespan"}, None, None))
    assert called == {"type": "lifespan"}


@pytest.mark.parametrize("headers", [
    [(b"content-length", b"abc")], [(b"content-length", b"-5")], [(b"content-length", b"")],
    [(b"content-length", b"5"), (b"content-length", b"999999")],
])
def test_guard_ignores_a_malformed_or_repeated_content_length_and_still_counts_the_stream(headers):
    record = {}
    guard = UploadSizeLimitMiddleware(_echo_app(record), path="/up", max_body_bytes=100)
    sent, _ = _drive(guard, headers=headers, chunks=[b"x" * 50])
    assert _status(sent) == 200 and record["bytes"] == 50
    sent, _ = _drive(guard, headers=headers, chunks=[b"x" * 60, b"x" * 60])
    assert _status(sent) == 413


def test_guard_response_is_the_sanitized_api010_envelope():
    guard = UploadSizeLimitMiddleware(_echo_app({}), path="/up", max_body_bytes=1)
    sent, _ = _drive(guard, chunks=[b"xx"])
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))
    assert start["status"] == 413
    assert body == {"error": {"code": "FILE_TOO_LARGE", "message": "The file is larger than the limit.",
                              "request_id": "req-test-0001", "details": {}}}
    assert (b"connection", b"close") in [(k.lower(), v) for k, v in start["headers"]]


def test_guard_rejects_a_non_positive_limit_and_swallows_the_app_response_after_cutting_it_off():
    with pytest.raises(ValueError):
        UploadSizeLimitMiddleware(_echo_app({}), path="/up", max_body_bytes=0)

    async def greedy(scope, receive, send):                    # keeps reading, then tries to answer 200 after the cut-off
        while (await receive())["type"] == "http.request":
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"late"})

    guard = UploadSizeLimitMiddleware(greedy, path="/up", max_body_bytes=3)
    sent, _ = _drive(guard, chunks=[b"xx", b"xx", b"xx"])
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [413]
    assert b"late" not in b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def test_the_guard_is_installed_only_on_the_upload_path_and_sized_from_the_engine_settings(api):
    layers = [m for m in api["app"].user_middleware if m.cls is UploadSizeLimitMiddleware]
    assert len(layers) == 1
    options = layers[0].kwargs
    assert options["path"] == URL
    assert options["max_body_bytes"] == api["settings"].max_file_size_bytes + middleware_module.UPLOAD_MULTIPART_OVERHEAD_BYTES
    # other routes accept bodies of any size through the guard (GET /documents is untouched)
    assert api["client"].get(URL, headers=ALPHA_RW).status_code == 200


# ============================================================================
# Authentication, scope and tenant isolation
# ============================================================================

def test_upload_requires_authentication_and_the_provisional_write_scope(api):
    assert SCOPE_DOCUMENTS_WRITE == "documents:write"
    assert api["client"].post(URL, files={"file": ("a.pdf", _pdf("a"), "application/pdf")}).status_code == 401
    assert _post(api, headers={"Authorization": "Bearer not-a-token"}).status_code == 401
    read_only = _post(api, headers=READER)
    assert read_only.status_code == 403 and _error(read_only)["code"] == "FORBIDDEN_SCOPE"
    assert _all_doc_dirs(api) == [], "a read-only token must not create a document"
    assert _post(api, headers=WRITER).status_code == 202


def test_upload_tenant_comes_only_from_the_principal_never_from_input(api):
    for kwargs in (
        {"data": {"tenant_id": "tenant_beta"}},
        {"data": {"tenant": "tenant_beta"}},
        {"params": {"tenant_id": "tenant_beta"}},
        {"params": {"tenantId": "tenant_beta"}},
    ):
        response = _post(api, _pdf("tenant"), **kwargs)
        assert response.status_code == 422, kwargs
        assert _error(response)["details"]["validation_errors"][0]["type"] == "unknown_parameter"
        assert "tenant_beta" not in response.text
    assert _all_doc_dirs(api) == []
    # a tenant header is not an input at all: ignored, the document belongs to the authenticated tenant
    response = api["client"].post(
        URL, files={"file": ("a.pdf", _pdf("hdr"), "application/pdf")}, headers={**WRITER, "X-Tenant-ID": "tenant_beta"}
    )
    assert response.status_code == 202
    assert _doc_dirs(api, "tenant_alpha") == [response.json()["document_id"]] and _doc_dirs(api, "tenant_beta") == []


def test_upload_documents_of_two_tenants_stay_apart_and_are_invisible_to_each_other(api):
    alpha = _post(api, _pdf("iso-a"), headers=ALPHA_RW).json()["document_id"]
    beta = _post(api, _pdf("iso-b"), headers=BETA_WRITER).json()["document_id"]
    assert _doc_dirs(api, "tenant_alpha") == [alpha] and _doc_dirs(api, "tenant_beta") == [beta]
    assert api["client"].get(f"{URL}/{beta}", headers=ALPHA_RW).status_code == 404
    assert [r["document_id"] for r in api["client"].get(URL, headers=ALPHA_RW).json()["content"]] == [alpha]


# ============================================================================
# Idempotency (the EXISTING store and fingerprint)
# ============================================================================

def test_idempotent_retry_returns_the_same_document_and_creates_one(api, pipeline_spy):
    pdf = _pdf("idem", 2)
    first = _post(api, pdf, key="key-one")
    second = _post(api, pdf, key="key-one")
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert _doc_dirs(api) == [first.json()["document_id"]]
    assert pipeline_spy["render"] == 1, "the replay must not render the document a second time"


def test_idempotency_key_with_a_different_file_is_409_and_leaves_the_first_document_alone(api):
    first = _post(api, _pdf("conf-a"), key="key-conflict")
    conflict = _post(api, _pdf("conf-b"), key="key-conflict", name=SECRET_NAME)
    body = _assert_public_error(conflict, 409, "IDEMPOTENCY_KEY_CONFLICT", "key-conflict", "idempotency_key")
    assert body["message"] == "This request key was already used."
    assert _doc_dirs(api) == [first.json()["document_id"]]


def test_idempotency_key_with_a_different_filename_is_a_conflict_too(api):
    pdf = _pdf("conf-name")
    assert _post(api, pdf, key="key-name", name="a.pdf").status_code == 202
    _assert_public_error(_post(api, pdf, key="key-name", name="b.pdf"), 409, "IDEMPOTENCY_KEY_CONFLICT", "key-name")



def test_without_a_key_the_same_file_is_two_documents(api):
    pdf = _pdf("nokey")
    one, two = _post(api, pdf), _post(api, pdf)
    assert one.status_code == two.status_code == 202
    assert one.json()["document_id"] != two.json()["document_id"] and len(_doc_dirs(api)) == 2


def test_concurrent_identical_requests_create_exactly_one_document(tmp_path, pipeline_spy):
    api = _make_api(tmp_path, max_concurrent_uploads=10)
    pdf = _pdf("race", 2)

    def attempt(_):
        return _post(api, pdf, key="key-race")

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(attempt, range(8)))

    assert {r.status_code for r in responses} == {202}
    ids = {r.json()["document_id"] for r in responses}
    assert len(ids) == 1, ids
    assert _doc_dirs(api) == sorted(ids)
    assert pipeline_spy["render"] == 1, "concurrent identical retries must wait and replay, not render the document again"
    handoff = json.loads((_storage(api)._get_document_dir(ids.pop()) / "module_a_handoff.json").read_text())
    assert handoff["page_count"] == 2 and len(handoff["pages"]) == 2


def test_concurrent_different_uploads_all_succeed_and_are_distinct(tmp_path):
    api = _make_api(tmp_path, max_concurrent_uploads=10)

    def attempt(i):
        return _post(api, _pdf(f"par-{i}"), key=f"key-par-{i}")

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(attempt, range(6)))
    assert {r.status_code for r in responses} == {202}
    assert len({r.json()["document_id"] for r in responses}) == 6 and len(_doc_dirs(api)) == 6


def test_idempotency_keys_are_scoped_to_the_tenant(api):
    pdf = _pdf("tenant-key")
    alpha = _post(api, pdf, headers=WRITER, key="shared-key")
    beta = _post(api, pdf, headers=BETA_WRITER, key="shared-key")
    assert alpha.status_code == beta.status_code == 202
    assert alpha.json()["document_id"] != beta.json()["document_id"]
    # beta reusing alpha's key with a DIFFERENT file is not a conflict: the key space is per tenant
    assert _post(api, _pdf("tenant-key-2"), headers=BETA_WRITER, key="other-key").status_code == 202
    assert _post(api, _pdf("tenant-key-3"), headers=BETA_WRITER, key="shared-key").status_code == 409
    assert _post(api, _pdf("tenant-key-4"), headers=WRITER, key="other-key").status_code == 202
    assert _doc_dirs(api, "tenant_alpha") and _doc_dirs(api, "tenant_beta")


@pytest.mark.parametrize("key", ["", " leading", "has space", "tab\there", "ünïcode", "x" * 256, "a\x7fb"])
def test_malformed_idempotency_keys_are_422_without_echo(api, key, pipeline_spy):
    response = api["client"].post(
        URL, files={"file": ("a.pdf", _pdf("badkey"), "application/pdf")}, headers={**WRITER, "Idempotency-Key": key.encode("utf-8")}
    )
    assert response.status_code == 422
    detail = _error(response)["details"]["validation_errors"][0]
    assert detail["location"] == ["header", "Idempotency-Key"]
    if key.strip():
        assert key not in response.text
    assert pipeline_spy["process"] == 0 and _all_doc_dirs(api) == []


@pytest.mark.parametrize("key", ["k", "a" * 255, "uuid-1b4e28ba-2fa1-11d2-883f-0016d3cca427", "A.b_c:d~e!f"])
def test_well_formed_idempotency_keys_are_accepted(api, key):
    assert _post(api, _pdf("goodkey-" + key[:8]), key=key).status_code == 202


# ============================================================================
# Optional parameters
# ============================================================================

@pytest.mark.parametrize("hint", [
    "DOC-01", "DOC-02", "DOC-03", "doc-01", "DOC-99", "", "Auto", "auto-detect", "' OR 1=1 --", "DOC-01 ", "../DOC-01"
])
def test_document_type_hint_is_rejected_as_unsupported_parameter(api, hint, pipeline_spy):
    response = _post(api, _pdf("badhint"), data={"document_type_hint": hint})
    assert response.status_code == 422
    assert _error(response)["code"] == "VALIDATION_ERROR"
    detail = _error(response)["details"]["validation_errors"][0]
    assert detail["location"] == ["body", "document_type_hint"] and detail["type"] == "unsupported_parameter"
    assert detail["message"] == "This parameter is not supported yet."
    if hint.strip() and hint not in ("Auto",):
        assert hint not in response.text
    assert pipeline_spy["process"] == 0 and _all_doc_dirs(api) == []


def test_the_allowed_hints_are_exactly_the_fixed_extraction_choices():
    from doc_extractor.extractor import SCHEMA_MAP
    assert sorted(SCHEMA_MAP) == ["DOC-01", "DOC-02", "DOC-03"]


@pytest.mark.parametrize("field, value", [
    ("schema_id", "casa_account_opening"), ("schema_id", ""), ("schema_id", "SECRET-SCHEMA"),
    ("callback_url", "https://attacker.example/hook"), ("callback_url", ""), ("callback_url", "http://169.254.169.254/latest"),
    ("callback_url", "file:///etc/passwd"),
])
def test_schema_id_and_callback_url_are_rejected_not_ignored(api, field, value, pipeline_spy):
    response = _post(api, _pdf("unsupported"), data={field: value})
    assert response.status_code == 422
    assert _error(response)["code"] == "VALIDATION_ERROR"
    detail = _error(response)["details"]["validation_errors"][0]
    assert detail["location"] == ["body", field] and detail["type"] == "unsupported_parameter"
    for text in ("attacker", "169.254", "passwd", "SECRET-SCHEMA", "casa_account_opening"):
        assert text not in response.text
    assert pipeline_spy["process"] == 0 and _all_doc_dirs(api) == [], "validated before Module A runs"


def test_unknown_form_fields_extra_files_and_query_parameters_are_422(api, pipeline_spy):
    for data in ({"document_type_hnt": "DOC-01"}, {"file2": "x"}, {"Document_Type_Hint": "DOC-01"}):
        response = _post(api, _pdf("unk"), data=data)
        assert response.status_code == 422 and _error(response)["details"]["validation_errors"][0]["type"] == "unknown_parameter"
    assert _post(api, _pdf("unk"), params={"x": "1"}).status_code == 422
    two_files = api["client"].post(
        URL, files=[("file", ("a.pdf", _pdf("two-a"), "application/pdf")), ("file", ("b.pdf", _pdf("two-b"), "application/pdf"))],
        headers=WRITER,
    )
    assert two_files.status_code == 422
    assert _error(two_files)["details"]["validation_errors"][0]["type"] == "duplicate_parameter"
    assert pipeline_spy["process"] == 0 and _all_doc_dirs(api) == []


@pytest.mark.parametrize("field, val1, val2", [
    ("document_type_hint", "DOC-01", "DOC-02"),
    ("schema_id", "s1", "s2"),
    ("callback_url", "https://a.com", "https://b.com"),
])
def test_duplicate_form_fields_are_rejected_with_422(api, field, val1, val2):
    response = api["client"].post(
        URL,
        files=[
            ("file", ("scan.pdf", _pdf("dup"), "application/pdf")),
            (field, (None, val1)),
            (field, (None, val2)),
        ],
        headers=WRITER,
    )
    assert response.status_code == 422
    assert _error(response)["code"] == "VALIDATION_ERROR"
    detail = _error(response)["details"]["validation_errors"][0]
    assert detail["location"] == ["body", field]
    assert detail["type"] == "duplicate_parameter"
    assert detail["message"] == "Multiple values are not permitted for this field."


def test_duplicate_file_field_is_rejected_with_422(api):
    response = api["client"].post(
        URL,
        files=[("file", ("a.pdf", _pdf("dup-a"), "application/pdf")), ("file", ("b.pdf", _pdf("dup-b"), "application/pdf"))],
        headers=WRITER,
    )
    assert response.status_code == 422
    assert _error(response)["code"] == "VALIDATION_ERROR"
    detail = _error(response)["details"]["validation_errors"][0]
    assert detail["location"] == ["body", "file"]
    assert detail["type"] == "duplicate_parameter"
    assert detail["message"] == "Multiple values are not permitted for this field."


def test_duplicate_unknown_field_rejected_without_leak(api):
    response = api["client"].post(
        URL,
        files=[
            ("file", ("scan.pdf", _pdf("dup-unk"), "application/pdf")),
            ("tenant_id", (None, "t1")),
            ("tenant_id", (None, "t2")),
        ],
        headers=WRITER,
    )
    assert response.status_code == 422
    assert _error(response)["code"] == "VALIDATION_ERROR"
    detail = _error(response)["details"]["validation_errors"][0]
    assert detail["type"] == "unknown_parameter"
    assert "t1" not in response.text and "t2" not in response.text


def test_missing_file_or_a_non_multipart_body_is_422(api, pipeline_spy):
    assert api["client"].post(URL, data={"document_type_hint": "DOC-01"}, headers=WRITER).status_code == 422
    assert api["client"].post(URL, json={"file": "x"}, headers=WRITER).status_code == 422
    assert api["client"].post(URL, content=b"raw bytes", headers={**WRITER, "Content-Type": "application/pdf"}).status_code == 422
    assert pipeline_spy["process"] == 0


# ============================================================================
# Sanitization and logging
# ============================================================================

def test_the_client_filename_and_file_content_never_reach_a_log_record(api, logs):
    marker = "SECRET-CONTENT-MARKER"
    ok = _post(api, _pdf(marker), name=SECRET_NAME, key="log-key")
    replay = _post(api, _pdf(marker), name=SECRET_NAME, key="log-key")
    rejected = _post(api, b"MZ nope " + marker.encode(), name=SECRET_NAME)
    infected = _post(api, create_minimal_pdf(1) + b"\n%" + EICAR_SIGNATURE, name=SECRET_NAME)
    oversize = api["client"].post(URL, files={"file": (SECRET_NAME, b"0" * (api["settings"].max_file_size_bytes + 2 * 1024 * 1024), "application/pdf")}, headers=WRITER)
    assert [ok.status_code, replay.status_code, rejected.status_code, infected.status_code, oversize.status_code] == [202, 202, 400, 400, 413]
    assert logs.records, "the capture must actually see Module A's log records"
    for secret in (SECRET_NAME, "SECRET-NAME", marker, "EICAR"):
        assert secret not in logs.text, f"{secret!r} reached a log record"


def test_the_request_id_reaches_module_a_for_correlation(api, logs):
    response = api["client"].post(
        URL, files={"file": ("a.pdf", _pdf("corr"), "application/pdf")}, headers={**WRITER, "X-Request-ID": "client-req-0002"}
    )
    assert response.status_code == 202 and response.headers["X-Request-ID"] == "client-req-0002"
    received = [json.loads(r) for r in logs.records if "Ingestion request received" in r]
    assert received and all(record["request_id"] == "client-req-0002" for record in received)


def test_the_ingestion_request_log_carries_no_filename_field(api, logs):
    _post(api, _pdf("logcheck"), name=SECRET_NAME)
    received = [json.loads(r) for r in logs.records if "Ingestion request received" in r]
    assert received, "the ingestion request log record must exist"
    for record in received:
        assert "claimed_filename" not in json.dumps(record.get("extra_data", {}))
        assert record["extra_data"] == {"has_idempotency_key": False}
    source = inspect.getsource(ingestion_module.IngestionService.ingest_document)
    assert '"claimed_filename": safe_filename' not in source


def test_the_upload_service_module_has_exactly_one_constant_log_line_with_only_safe_arguments():
    """The only log call: a constant message, a fixed reason and two identifiers. Never a filename, a key or an exception."""
    source = inspect.getsource(upload_service_module)
    tree = ast.parse(source)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "logger"
    ]
    assert len(calls) == 1, "one log line only: an incomplete late-rejection rollback"
    call = calls[0]
    assert isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
    assert [a.id for a in call.args[1:] if isinstance(a, ast.Name)] == ["reason", "request_id", "doc_id"]
    assert len(call.args) == 4 and not call.keywords, "no other argument (no exception, no filename, no key)"
    assert "logging.getLogger" in source and "exc_info" not in source


def test_a_failure_after_the_record_was_saved_is_a_generic_500_and_leaves_a_processing_record(api, monkeypatch, logs):
    """Documented limitation: Module A saved the record before rendering failed; no terminal-status writer exists."""
    def explode(self, *args, **kwargs):
        raise RuntimeError("SECRET-EXCEPTION-TEXT /srv/secret/path")

    monkeypatch.setattr(PdfRenderer, "render_pdf", explode)
    response = _post(api, _pdf("fail-render"), name=SECRET_NAME)
    assert response.status_code == 500, response.text
    body = _error(response)
    assert body["code"] == "INTERNAL_ERROR" and body["details"] == {}
    for leak in ("SECRET-EXCEPTION-TEXT", "/srv/secret", SECRET_NAME, str(api["tmp_path"]), "RuntimeError", "PROCESSING_FAILED"):
        assert leak not in response.text
    assert SECRET_NAME not in logs.text
    (document_id,) = _doc_dirs(api)
    record = json.loads((_storage(api)._get_document_dir(document_id) / "document_record.json").read_text())
    assert record["status"] == "processing"                       # the limitation: a failed upload can leave a record
    assert not (_storage(api)._get_document_dir(document_id) / "module_a_handoff.json").exists()


@pytest.mark.parametrize("content, name", [
    pytest.param(create_minimal_jpeg(), "structure-only.jpg", id="jpeg"),
    pytest.param(create_minimal_tiff(1), "structure-only.tif", id="tiff"),
])
def test_a_structurally_valid_but_undecodable_image_is_a_generic_500_with_a_leftover_record(api, content, name, logs):
    """
    Documented limitation of Module A (not changed here): its validator checks structure, not decodability, so such a file
    is accepted, a record is saved, and rendering then fails. The caller gets a sanitized generic 500, never a 202.
    """
    response = _post(api, content, name=name, content_type="image/jpeg")
    assert response.status_code == 500
    assert _error(response)["code"] == "INTERNAL_ERROR" and _error(response)["details"] == {}
    for leak in ("UnidentifiedImageError", "cannot identify", "BytesIO", name, str(api["tmp_path"])):
        assert leak not in response.text
    (document_id,) = _doc_dirs(api)
    doc_dir = _storage(api)._get_document_dir(document_id)
    assert json.loads((doc_dir / "document_record.json").read_text())["status"] == "processing"
    assert not (doc_dir / "module_a_handoff.json").exists()


def test_a_record_save_failure_is_a_generic_500(api, monkeypatch):
    def fail(self, record):
        raise StorageError("disk exploded at /srv/secret", details={"document_id": "SECRET-DOC", "path": "/srv/secret"})

    monkeypatch.setattr(LocalDiskStorage, "save_document_record", fail)
    response = _post(api, _pdf("fail-save"), name=SECRET_NAME)
    assert response.status_code == 500
    for leak in ("disk exploded", "/srv/secret", "SECRET-DOC", SECRET_NAME):
        assert leak not in response.text
    assert _error(response)["code"] == "INTERNAL_ERROR" and _error(response)["details"] == {}


# ============================================================================
# The service and its error mapping, both paths (returned error dict AND raised exception)
# ============================================================================

def _fake_module_a(tenant_id="tenant_alpha", *, returns=None, raises=None):
    class Fake:
        def process_document(self, **kwargs):
            if raises is not None:
                raise raises
            return returns

    fake = Fake()
    fake.tenant_id = tenant_id
    return fake


def _payload(status="processing", error=None, document_id="doc_0123456789ab"):
    return SimpleNamespace(status=status, error=error, document_id=document_id)


@pytest.mark.parametrize("error, expected_type, expected_status", [
    ({"code": "UNSUPPORTED_FILE", "message": "SECRET", "details": {"reason": "MAGIC_BYTE_MISMATCH", "filename": "SECRET", "detected_header": "4d5a"}}, UnsupportedFileError, 400),
    ({"code": "UNSUPPORTED_FILE", "message": "SECRET", "details": {"reason": "FILE_SIZE_LIMIT_EXCEEDED", "size": 1, "limit": 2}}, FileTooLargeError, 413),
    ({"code": "UNSUPPORTED_FILE", "message": "SECRET", "details": {"reason": "PAGE_LIMIT_EXCEEDED", "page_count": 9}}, FileTooLargeError, 413),
    ({"code": "UNSUPPORTED_FILE"}, UnsupportedFileError, 400),
    ({"code": "SECURITY_CHECK_FAILED", "message": "SECRET"}, UnsupportedFileError, 400),
    ({"code": "DOCUMENT_ENCRYPTED", "message": "SECRET password-protected", "details": {"document_id": "SECRET"}}, UnsupportedFileError, 400),
    ({"code": "MALWARE_DETECTED", "message": "SECRET", "details": {"threat_name": "Eicar-Test-Signature"}}, MalwareDetectedError, 400),
    ({"code": "SCANNER_UNAVAILABLE", "message": "SECRET", "details": {"scanner_type": "UnconfiguredScanner"}}, ScannerUnavailableError, 503),
    ({"code": "IDEMPOTENCY_KEY_CONFLICT", "message": "SECRET", "details": {"idempotency_key": "SECRET-KEY"}}, ConflictError, 409),
    ({"code": "STORAGE_FAILED", "message": "SECRET /srv/x"}, StorageError, 500),
    ({"code": "PROCESSING_FAILED", "message": "SECRET /srv/x"}, StorageError, 500),
    ({"code": 12345, "message": "SECRET"}, StorageError, 500),
    ({"message": "SECRET"}, StorageError, 500),
    ("SECRET plain string error", StorageError, 500),
    (["SECRET"], StorageError, 500),
])
def test_service_maps_a_returned_error_dict_to_fixed_public_errors(error, expected_type, expected_status):
    service = DocumentUploadService(_fake_module_a(returns=_payload("rejected", error)), "tenant_alpha")
    with pytest.raises(Exception) as excinfo:
        service.upload(None, "x.pdf", request_id="req-1")
    exc = excinfo.value
    assert type(exc) is expected_type
    assert get_status_code_for_exception(exc) == expected_status
    assert "SECRET" not in str(exc) and "SECRET" not in json.dumps(getattr(exc, "details", {}) or {}, default=str)
    if expected_type is StorageError:
        assert exc.details == {"reason": "upload_processing_failed"}
    else:
        assert exc.details == {} or exc.details is None


@pytest.mark.parametrize("raised, expected_type", [
    (UnsupportedFileError(message="SECRET", details={"reason": "FILE_SIZE_LIMIT_EXCEEDED", "filename": "SECRET"}), FileTooLargeError),
    (UnsupportedFileError(message="SECRET", details={"reason": "EMPTY_FILE", "filename": "SECRET"}), UnsupportedFileError),
    (MalwareDetectedError(message="SECRET", details={"threat_name": "SECRET"}), MalwareDetectedError),
    (ScannerUnavailableError(message="SECRET", details={"scanner_type": "SECRET"}), ScannerUnavailableError),
    (ConflictError(message="SECRET", details={"idempotency_key": "SECRET"}), ConflictError),
])
def test_service_maps_a_raised_exception_to_the_same_fixed_public_errors(raised, expected_type):
    service = DocumentUploadService(_fake_module_a(raises=raised), "tenant_alpha")
    with pytest.raises(Exception) as excinfo:
        service.upload(None, "x.pdf")
    assert type(excinfo.value) is expected_type
    assert "SECRET" not in str(excinfo.value) and not (excinfo.value.details or {})
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


def test_service_untyped_exceptions_propagate_unchanged_and_a_non_processing_payload_is_not_accepted():
    boom = RuntimeError("untyped")
    with pytest.raises(RuntimeError):
        DocumentUploadService(_fake_module_a(raises=boom), "tenant_alpha").upload(None, "x.pdf")
    with pytest.raises(StorageError):
        DocumentUploadService(_fake_module_a(returns=_payload("failed", None)), "tenant_alpha").upload(None, "x.pdf")
    with pytest.raises(StorageError):
        DocumentUploadService(_fake_module_a(returns=_payload("processing", {"code": "X"})), "tenant_alpha").upload(None, "x.pdf")
    assert DocumentUploadService(_fake_module_a(returns=_payload()), "tenant_alpha").upload(None, "x.pdf") == "doc_0123456789ab"


def test_service_refuses_a_module_a_component_of_another_tenant():
    with pytest.raises(TenantMismatchError):
        DocumentUploadService(_fake_module_a("tenant_beta"), "tenant_alpha")


def test_public_upload_error_table_is_fixed_text_only():
    cases = [
        (("UNSUPPORTED_FILE", "MAGIC_MISMATCH"), UnsupportedFileError, "UNSUPPORTED_FILE", "This file type isn't supported.", {}, 400),
        (("UNSUPPORTED_FILE", "FILE_SIZE_LIMIT_EXCEEDED"), FileTooLargeError, "FILE_TOO_LARGE", "The file is larger than the limit.", {}, 413),
        (("UNSUPPORTED_FILE", "PAGE_LIMIT_EXCEEDED"), FileTooLargeError, "FILE_TOO_LARGE", "The file is larger than the limit.", {}, 413),
        (("UNSUPPORTED_FILE", None), UnsupportedFileError, "UNSUPPORTED_FILE", "This file type isn't supported.", {}, 400),
        (("SECURITY_CHECK_FAILED", None), UnsupportedFileError, "UNSUPPORTED_FILE", "This file type isn't supported.", {}, 400),
        (("DOCUMENT_ENCRYPTED", None), UnsupportedFileError, "UNSUPPORTED_FILE", "This file type isn't supported.", {}, 400),
        (("MALWARE_DETECTED", "EICAR"), MalwareDetectedError, "MALWARE_DETECTED", "This file was blocked by the safety scan.", {}, 400),
        (("SCANNER_UNAVAILABLE", None), ScannerUnavailableError, "SCANNER_UNAVAILABLE", "Malware scanning is currently unavailable. Try again later.", {}, 503),
        (("IDEMPOTENCY_KEY_CONFLICT", None), ConflictError, "IDEMPOTENCY_KEY_CONFLICT", "This request key was already used.", {}, 409),
        (("STORAGE_FAILED", None), StorageError, "STORAGE_FAILED", "Upload processing failed.", {"reason": "upload_processing_failed"}, 500),
        (("WHATEVER", None), StorageError, "STORAGE_FAILED", "Upload processing failed.", {"reason": "upload_processing_failed"}, 500),
        ((None, None), StorageError, "STORAGE_FAILED", "Upload processing failed.", {"reason": "upload_processing_failed"}, 500),
    ]
    for (code, reason), exc_cls, exp_code, exp_msg, exp_details, expected_status in cases:
        exc = public_upload_error(code, reason, "req-fixed-test")
        assert isinstance(exc, exc_cls)
        assert getattr(exc, "code", None) == exp_code
        assert getattr(exc, "message", None) == exp_msg
        assert getattr(exc, "details", None) == exp_details
        assert getattr(exc, "request_id", None) == "req-fixed-test"
        assert get_status_code_for_exception(exc) == expected_status      # the existing exception-to-status mapping


def test_service_uses_the_existing_module_a_pipeline():
    seen = {}

    class Recording:
        tenant_id = "tenant_alpha"

        def process_document(self, **kwargs):
            seen.update(kwargs)
            return _payload()

    stream = object()
    DocumentUploadService(Recording(), "tenant_alpha").upload(
        stream, "a.pdf", idempotency_key="k", request_id="req-9"
    )
    assert seen == {
        "file_path_or_stream": stream, "claimed_filename": "a.pdf", "request_id": "req-9", "idempotency_key": "k",
    }
    seen.clear()
    DocumentUploadService(Recording(), "tenant_alpha").upload(stream, "a.pdf")
    assert "extra_request_params" not in seen


def test_service_postcondition_missing_or_mismatched_record_raises_storage_error():
    class StorageMock:
        def __init__(self, record):
            self.record = record
        def get_document_record(self, doc_id):
            return self.record

    class PipelineMock:
        tenant_id = "tenant_alpha"
        def process_document(self, **kwargs):
            return _payload(document_id="doc_0123456789ab")

    # 1. Missing document record (None)
    service_missing = DocumentUploadService(PipelineMock(), "tenant_alpha", storage=StorageMock(None))
    with pytest.raises(StorageError) as excinfo:
        service_missing.upload(object(), "a.pdf")
    assert excinfo.value.details == {"reason": "missing_document_record"}

    # 2. Mismatched tenant
    wrong_tenant_rec = {"document_id": "doc_0123456789ab", "tenant_id": "tenant_beta"}
    service_wrong_tenant = DocumentUploadService(PipelineMock(), "tenant_alpha", storage=StorageMock(wrong_tenant_rec))
    with pytest.raises(StorageError) as excinfo:
        service_wrong_tenant.upload(object(), "a.pdf")
    assert excinfo.value.details == {"reason": "missing_document_record"}

    # 3. Mismatched document_id
    wrong_doc_rec = {"document_id": "doc_other", "tenant_id": "tenant_alpha"}
    service_wrong_doc = DocumentUploadService(PipelineMock(), "tenant_alpha", storage=StorageMock(wrong_doc_rec))
    with pytest.raises(StorageError) as excinfo:
        service_wrong_doc.upload(object(), "a.pdf")
    assert excinfo.value.details == {"reason": "missing_document_record"}

    # 4. Correct record succeeds
    good_rec = {"document_id": "doc_0123456789ab", "tenant_id": "tenant_alpha"}
    service_good = DocumentUploadService(PipelineMock(), "tenant_alpha", storage=StorageMock(good_rec))
    assert service_good.upload(object(), "a.pdf") == "doc_0123456789ab"


def test_renderer_rollback_cleans_idempotency_record_and_retry_succeeds(tmp_path):
    api = _make_api(tmp_path)
    pdf = _pdf("rollback-test", 2)
    key = "rollback-idem-key"

    orig_render_pdf = PdfRenderer.render_pdf
    attempts = {"count": 0}

    def failing_render_pdf(self, *args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            doc_id = kwargs.get("document_id")
            if doc_id:
                self.storage.cleanup_document(doc_id)
            raise DocumentProcessingError(
                message="Simulated rendering engine crash.",
                details={"document_id": doc_id},
            )
        return orig_render_pdf(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(PdfRenderer, "render_pdf", failing_render_pdf)
        resp1 = _post(api, pdf, key=key)
        assert resp1.status_code == 500
        assert _error(resp1)["code"] == "INTERNAL_ERROR"

    # Verify that the document directory was removed by rollback
    assert _all_doc_dirs(api) == []

    # Verify that the orphaned idempotency record was deleted
    idem_dir = tmp_path / "tenant_alpha" / "idempotency"
    if idem_dir.exists():
        assert list(idem_dir.glob("*.json")) == []

    # Second attempt with the SAME key succeeds!
    resp2 = _post(api, pdf, key=key)
    assert resp2.status_code == 202
    doc_id = resp2.json()["document_id"]
    assert DOC_ID.match(doc_id)

    # Verify document directory, document_record.json, and module_a_handoff.json exist
    doc_dir = _storage(api)._get_document_dir(doc_id)
    assert (doc_dir / "document_record.json").is_file()
    assert (doc_dir / "module_a_handoff.json").is_file()

    # Verify API-002 serves the document with 200
    detail = api["client"].get(f"{URL}/{doc_id}", headers=ALPHA_RW)
    assert detail.status_code == 200
    assert detail.json()["document_id"] == doc_id

    # Verify API-015 lists the document
    listing = api["client"].get(URL, headers=ALPHA_RW)
    assert listing.status_code == 200
    assert [d["document_id"] for d in listing.json()["content"]] == [doc_id]

    # Verify subsequent replay with same key returns same doc_id without re-rendering
    resp3 = _post(api, pdf, key=key)
    assert resp3.status_code == 202
    assert resp3.json()["document_id"] == doc_id


def test_upload_admission_control_prevents_threadpool_starvation(tmp_path):
    # Set max_concurrent_uploads=1 to test admission rejection cleanly with one in-flight request
    api = _make_api(tmp_path, max_file_size_bytes=100 * 1024, max_concurrent_uploads=1)
    upload_started = threading.Event()
    release_uploads = threading.Event()

    original_run = DocumentUploadService._run

    def slow_run(self, *args, **kwargs):
        upload_started.set()
        release_uploads.wait(timeout=5.0)
        return original_run(self, *args, **kwargs)

    pdf = _pdf("concur-test")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(DocumentUploadService, "_run", slow_run)

        def run_upload(i):
            return _post(api, pdf, key=f"key-slow-{i}")

        executor = ThreadPoolExecutor(max_workers=2)
        f1 = executor.submit(run_upload, 1)

        # Wait until upload 1 is actively running in Module A
        assert upload_started.wait(timeout=5.0), "Upload 1 did not start in time"

        # 2nd upload arrives while upload 1 is active: rejected immediately with 429 RATE_LIMITED
        resp2 = _post(api, pdf, key="key-slow-2")
        assert resp2.status_code == 429
        err = _error(resp2)
        assert err["code"] == "RATE_LIMITED"
        assert err["message"] == "Too many concurrent uploads. Try again shortly."
        assert resp2.headers.get("Retry-After") == "5"

        # Unrelated route GET /documents is NOT starved and responds 200
        get_resp = api["client"].get(URL, headers=ALPHA_RW)
        assert get_resp.status_code == 200

        # Release upload 1
        release_uploads.set()
        r1 = f1.result(timeout=5.0)
        executor.shutdown(wait=True)

        assert r1.status_code == 202

    # Capacity freed: new upload succeeds
    resp3 = _post(api, _pdf("after-release"), key="key-after")
    assert resp3.status_code == 202


# ============================================================================
# N1: admission never holds the blocking lock across an await (event-loop deadlock regression)
# ============================================================================

def _run_loop(factory, timeout: float = 20.0):
    """Run ``factory()`` on ONE event loop in a thread. A blocked loop fails this test instead of hanging the whole run."""
    box = {}

    def runner():
        try:
            box["result"] = asyncio.run(factory())
        except BaseException as exc:                       # noqa: BLE001 - re-raised below
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "the event loop is blocked (hung)"
    if "error" in box:
        raise box["error"]
    return box["result"]


class _SpyLock:
    """A ``threading.Lock`` stand-in that records whether it is currently held."""

    def __init__(self):
        self._lock = threading.Lock()
        self.held = False
        self.acquisitions = 0

    def __enter__(self):
        self._lock.acquire()
        self.held = True
        self.acquisitions += 1
        return self

    def __exit__(self, *exc_info):
        self.held = False
        self._lock.release()


def _guard_scope(path="/up"):
    return {"type": "http", "method": "POST", "path": path, "root_path": "", "headers": [], "state": {"request_id": "req-test-0001"}}


async def _empty_body():
    return {"type": "http.request", "body": b"", "more_body": False}


def test_two_concurrent_over_capacity_requests_finish_when_send_yields_and_no_lock_spans_a_send():
    """
    THE REGRESSION: one shared event loop, the only permit held, two concurrent over-capacity requests whose response
    ``send`` yields to the loop (as BaseHTTPMiddleware's stream or a server's drain() do). When the 429 was sent while
    holding the blocking admission lock, the second request blocked the loop thread on that lock and the server froze.
    """
    async def scenario():
        started, hold = asyncio.Event(), asyncio.Event()

        async def holder_app(scope, receive, send):
            started.set()
            await hold.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        guard = UploadSizeLimitMiddleware(holder_app, path="/up", max_body_bytes=100, max_concurrent_uploads=1)
        spy = _SpyLock()
        guard._admission_lock = spy
        held_during_send = []
        statuses = []

        async def yielding_send(message):
            held_during_send.append(spy.held)               # must never be True: no await while the lock is held
            await asyncio.sleep(0)
            held_during_send.append(spy.held)
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        async def quiet_send(message):
            return None

        holder = asyncio.create_task(guard(_guard_scope(), _empty_body, quiet_send))
        await started.wait()
        await asyncio.wait_for(
            asyncio.gather(*(guard(_guard_scope(), _empty_body, yielding_send) for _ in range(3))), timeout=5
        )
        hold.set()
        await holder
        return statuses, held_during_send, guard._active_uploads, spy.acquisitions

    statuses, held, active, acquisitions = _run_loop(scenario)
    assert statuses == [429, 429, 429]
    assert held and not any(held), "the admission lock was held while a response was being sent"
    assert active == 0 and acquisitions == 5          # 1 admit + 3 over-capacity checks + 1 release


def test_health_stays_responsive_while_over_capacity_uploads_are_rejected_on_the_real_app(tmp_path):
    api = _make_api(tmp_path, max_concurrent_uploads=1, upload_body_idle_timeout_seconds=30.0)

    async def scenario():
        stall = asyncio.Event()
        reached = asyncio.Event()

        async def stalled_receive():
            reached.set()
            await stall.wait()                               # the permit holder: a client that is still sending

        holder = asyncio.create_task(_call_app(api["app"], "POST", URL, _upload_headers(), stalled_receive))
        await reached.wait()
        rejected = await asyncio.wait_for(asyncio.gather(*(
            _call_app(api["app"], "POST", URL, _upload_headers(), _empty_body, yielding=True) for _ in range(3)
        )), timeout=5)
        health = await asyncio.wait_for(_call_app(api["app"], "GET", "/api/v1/health"), timeout=5)
        holder.cancel()
        try:
            await holder
        except asyncio.CancelledError:
            pass
        return [r[0] for r in rejected], health[0]

    statuses, health_status = _run_loop(scenario)
    assert statuses == [429, 429, 429] and health_status == 200


async def _call_app(app, method, path, headers=(), receive=None, *, yielding=False):
    """One request to the REAL ASGI app on the running loop. Returns (status, body bytes, response headers dict)."""
    sent = []

    async def send(message):
        if yielding:
            await asyncio.sleep(0)
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
        "raw_path": path.encode(), "query_string": b"", "headers": list(headers) + [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1234), "server": ("testserver", 80), "scheme": "http", "root_path": "",
    }
    await app(scope, receive or _empty_body, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body, {k.decode().lower(): v.decode() for k, v in start["headers"]}


def _upload_headers(token="token-alpha-writer-only", boundary="----apiboundary"):
    return [
        (b"authorization", f"Bearer {token}".encode()),
        (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
    ]


async def _noop_send(message):
    return None


def _permit_scenarios():
    """(label, coroutine factory taking the guard) for every way a request can end."""
    async def ok_app(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def raising_app(scope, receive, send):
        raise RuntimeError("boom")

    async def disconnect_app(scope, receive, send):
        await receive()

    return ok_app, raising_app, disconnect_app


def test_admission_capacity_is_returned_after_success_and_every_error_and_cancellation_path():
    ok_app, raising_app, disconnect_app = _permit_scenarios()

    async def scenario():
        results = {}

        def guard_for(app, **kwargs):
            return UploadSizeLimitMiddleware(app, path="/up", max_body_bytes=5, max_concurrent_uploads=1, **kwargs)

        # success
        g = guard_for(ok_app)
        await g(_guard_scope(), _empty_body, _noop_send)
        results["success"] = g._active_uploads
        # the application raises
        g = guard_for(raising_app)
        with pytest.raises(RuntimeError):
            await g(_guard_scope(), _empty_body, _noop_send)
        results["app raises"] = g._active_uploads
        # declared oversize -> 413
        g = guard_for(ok_app)
        scope = _guard_scope()
        scope["headers"] = [(b"content-length", b"999")]
        await g(scope, _empty_body, _noop_send)
        results["declared oversize"] = g._active_uploads
        # streamed oversize -> 413
        g = guard_for(ok_app)

        async def big():
            return {"type": "http.request", "body": b"x" * 10, "more_body": True}

        async def reading_app(scope, receive, send):
            while (await receive())["type"] == "http.request":
                pass

        g.app = reading_app
        await g(_guard_scope(), big, _noop_send)
        results["streamed oversize"] = g._active_uploads
        # the client disconnects
        g = guard_for(disconnect_app)

        async def gone():
            return {"type": "http.disconnect"}

        await g(_guard_scope(), gone, _noop_send)
        results["disconnect"] = g._active_uploads
        # body idle timeout -> 408
        g = guard_for(reading_app, body_idle_timeout=0.1, body_total_timeout=1.0)

        async def silent():
            await asyncio.Event().wait()

        await g(_guard_scope(), silent, _noop_send)
        results["idle timeout"] = g._active_uploads
        # cancellation while waiting for the body
        g = guard_for(reading_app, body_idle_timeout=30, body_total_timeout=60)
        task = asyncio.create_task(g(_guard_scope(), silent, _noop_send))
        await asyncio.sleep(0.05)
        assert g._active_uploads == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        results["cancelled while receiving"] = g._active_uploads
        # cancellation while the application runs (Module A time)
        running = asyncio.Event()

        async def slow_app(scope, receive, send):
            await receive()
            running.set()
            await asyncio.sleep(60)

        g = guard_for(slow_app)
        task = asyncio.create_task(g(_guard_scope(), _empty_body, _noop_send))
        await running.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        results["cancelled while processing"] = g._active_uploads
        # a rejection (429) never consumes a permit, and the permit is reusable afterwards
        release, held = asyncio.Event(), asyncio.Event()

        async def hold_app(scope, receive, send):
            held.set()
            await release.wait()

        g = guard_for(hold_app)
        holder = asyncio.create_task(g(_guard_scope(), _empty_body, _noop_send))
        await held.wait()
        sent = []

        async def collect(message):
            sent.append(message)

        await g(_guard_scope(), _empty_body, collect)
        assert sent[0]["status"] == 429 and g._active_uploads == 1
        release.set()
        await holder
        results["after 429 and holder finished"] = g._active_uploads
        await g(_guard_scope(), _empty_body, _noop_send)                       # capacity is usable again
        results["reused"] = g._active_uploads
        return results

    results = _run_loop(scenario)
    assert all(count == 0 for count in results.values()), results


# ============================================================================
# N2: the guard's path matching does not depend on starlette.routing.get_route_path (absent before Starlette 0.35)
# ============================================================================

_ROUTE_PATH_CASES = [
    ({"path": "/api/v1/documents", "root_path": ""}, "/api/v1/documents"),            # no root_path
    ({"path": "/api/v1/documents"}, "/api/v1/documents"),                             # no root_path key at all
    ({"path": "/svc/api/v1/documents", "root_path": "/svc"}, "/api/v1/documents"),    # matching root_path
    ({"path": "/api/v1/documents", "root_path": "/svc"}, "/api/v1/documents"),        # the server did not prefix the path
    ({"path": "/svcx/api/v1/documents", "root_path": "/svc"}, "/svcx/api/v1/documents"),   # prefix of a different segment
    ({"path": "/svc", "root_path": "/svc"}, ""),                                      # exactly the root_path
    ({"path": "/a.c/api/v1/documents", "root_path": "/a.c"}, "/api/v1/documents"),    # regex metacharacters are literal
    ({"path": "/abc/api/v1/documents", "root_path": "/a.c"}, "/abc/api/v1/documents"),
    ({"path": "/svc/api/v1/documents", "root_path": "/svc/"}, "/svc/api/v1/documents"),   # trailing slash: as Starlette
    ({"path": "/api/v1/documents/", "root_path": ""}, "/api/v1/documents/"),
]


@pytest.mark.parametrize("scope, expected", _ROUTE_PATH_CASES)
def test_route_path_helper_reproduces_the_root_path_aware_route_path(scope, expected):
    assert middleware_module.route_path_of(scope) == expected


@pytest.mark.parametrize("scope, expected", _ROUTE_PATH_CASES)
def test_route_path_helper_agrees_with_starlettes_own_when_it_exists(scope, expected):
    starlette_routing = pytest.importorskip("starlette.routing")
    reference = getattr(starlette_routing, "get_route_path", None)
    if reference is None:
        pytest.skip("this Starlette predates get_route_path (0.35): the local helper is the only implementation")
    assert middleware_module.route_path_of(scope) == reference(scope)


def test_the_middleware_module_does_not_import_get_route_path():
    tree = ast.parse(inspect.getsource(middleware_module))
    imported = [alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names]
    assert "get_route_path" not in imported


# ============================================================================
# N3: body idle and total timeouts
# ============================================================================

def _timed_guard(app, **kwargs):
    kwargs.setdefault("max_body_bytes", 10_000)
    kwargs.setdefault("max_concurrent_uploads", 1)
    return UploadSizeLimitMiddleware(app, path="/up", **kwargs)


def _reading_app(record):
    async def app(scope, receive, send):
        record["called"] = True
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                record["saw"] = message["type"]
                # like the framework after a ClientDisconnect: the application still tries to answer; that must be swallowed
                await send({"type": "http.response.start", "status": 400, "headers": []})
                await send({"type": "http.response.body", "body": b"app-answer"})
                return
            total += len(message.get("body", b""))
            if not message.get("more_body", False):
                break
        record["bytes"] = total
        record["finished"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
    return app


async def _run_guard(guard, receive):
    sent = []

    async def collect(message):
        sent.append(message)

    started = time.monotonic()
    await guard(_guard_scope(), receive, collect)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = {k.decode().lower(): v.decode() for k, v in next(m for m in sent if m["type"] == "http.response.start")["headers"]}
    return status, body, headers, time.monotonic() - started, sum(1 for m in sent if m["type"] == "http.response.start")


def _assert_timeout_response(status, body, headers, responses):
    assert status == 408 and responses == 1
    error = json.loads(body)["error"]
    assert error == {"code": "REQUEST_TIMEOUT", "message": "The upload was not received in time.", "details": {}, "request_id": "req-test-0001"}
    assert headers["connection"] == "close"


def test_a_client_that_sends_some_bytes_and_stalls_gets_408_and_the_permit_is_released():
    record = {}

    async def scenario():
        guard = _timed_guard(_reading_app(record), body_idle_timeout=0.2, body_total_timeout=5.0)
        state = {"sent": False}

        async def stalling():
            if not state["sent"]:
                state["sent"] = True
                return {"type": "http.request", "body": b"x" * 10, "more_body": True}
            await asyncio.Event().wait()

        result = await _run_guard(guard, stalling)
        return result, guard._active_uploads

    (status, body, headers, elapsed, responses), active = _run_loop(scenario)
    _assert_timeout_response(status, body, headers, responses)
    assert 0.15 <= elapsed < 2.0, f"timed out after {elapsed:.2f}s (idle timeout 0.2s)"
    assert active == 0
    assert record.get("saw") == "http.disconnect" and "finished" not in record, "the application never saw a complete body"


def test_a_client_that_never_sends_a_byte_after_the_headers_also_times_out():
    async def scenario():
        guard = _timed_guard(_reading_app({}), body_idle_timeout=0.2, body_total_timeout=5.0)

        async def silent():
            await asyncio.Event().wait()

        return await _run_guard(guard, silent), guard._active_uploads

    (status, body, headers, elapsed, responses), active = _run_loop(scenario)
    _assert_timeout_response(status, body, headers, responses)
    assert active == 0 and elapsed < 2.0


def test_the_total_timeout_covers_the_whole_body_even_when_bytes_keep_trickling_in():
    record = {}

    async def scenario():
        guard = _timed_guard(_reading_app(record), body_idle_timeout=0.5, body_total_timeout=0.6)

        async def trickle():
            await asyncio.sleep(0.05)                      # a byte every 50 ms: the idle timeout (0.5 s) never trips
            return {"type": "http.request", "body": b"x", "more_body": True}

        return await _run_guard(guard, trickle), guard._active_uploads

    (status, body, headers, elapsed, responses), active = _run_loop(scenario)
    _assert_timeout_response(status, body, headers, responses)
    assert 0.5 <= elapsed < 2.0, f"the total timeout (0.6 s) must end the trickle, took {elapsed:.2f}s"
    assert active == 0 and "finished" not in record


def test_the_idle_timer_resets_only_on_real_body_progress():
    async def scenario():
        guard = _timed_guard(_reading_app({}), body_idle_timeout=0.25, body_total_timeout=10.0)

        async def empty_chunks():
            await asyncio.sleep(0.05)                      # frames arrive, but carry no body bytes
            return {"type": "http.request", "body": b"", "more_body": True}

        return await _run_guard(guard, empty_chunks), guard._active_uploads

    (status, body, headers, elapsed, responses), active = _run_loop(scenario)
    _assert_timeout_response(status, body, headers, responses)
    assert elapsed < 1.5, f"empty frames must not keep the request alive (took {elapsed:.2f}s)"
    assert active == 0


def test_a_slow_but_progressing_upload_within_both_timeouts_is_accepted():
    record = {}

    async def scenario():
        guard = _timed_guard(_reading_app(record), body_idle_timeout=0.3, body_total_timeout=3.0)
        count = {"n": 0}

        async def receive():
            await asyncio.sleep(0.05)
            count["n"] += 1
            return {"type": "http.request", "body": b"x" * 10, "more_body": count["n"] < 6}

        return await _run_guard(guard, receive)

    status, body, headers, elapsed, responses = _run_loop(scenario)
    assert status == 200 and record["bytes"] == 60


def test_processing_time_after_the_body_arrived_is_not_counted_against_the_body_timeouts():
    """
    After the body is complete the framework keeps calling ``receive()`` to watch for a disconnect while Module A runs
    (BaseHTTPMiddleware, StreamingResponse). That wait must NOT be timed: a timer still running would send a 408 in the
    middle of a healthy upload and swallow its real response.
    """
    async def scenario():
        async def slow_processing_app(scope, receive, send):
            while (await receive()).get("more_body"):
                pass
            watcher = asyncio.create_task(receive())          # the disconnect watcher: waits as long as the client is quiet
            await asyncio.sleep(0.6)                           # "Module A": longer than both body timeouts
            watcher.cancel()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        guard = _timed_guard(slow_processing_app, body_idle_timeout=0.1, body_total_timeout=0.2)
        state = {"delivered": False}

        async def receive():
            if not state["delivered"]:
                state["delivered"] = True
                return {"type": "http.request", "body": b"abc", "more_body": False}
            await asyncio.Event().wait()                       # the client is idle but connected

        return await _run_guard(guard, receive), guard._active_uploads

    (status, body, headers, elapsed, responses), active = _run_loop(scenario)
    assert status == 200 and body == b"ok" and responses == 1 and active == 0 and elapsed >= 0.55


def test_the_timeout_values_must_be_positive_and_the_settings_have_finite_documented_defaults():
    for kwargs in ({"body_idle_timeout": 0}, {"body_total_timeout": -1}):
        with pytest.raises(ValueError):
            UploadSizeLimitMiddleware(_reading_app({}), path="/up", max_body_bytes=1, **kwargs)
    defaults = ApiSettings(app_env="development")
    assert (defaults.upload_body_idle_timeout_seconds, defaults.upload_body_total_timeout_seconds) == (30.0, 300.0)
    assert defaults.upload_body_total_timeout_seconds >= defaults.max_file_size_bytes / (256 * 1024)     # 50 MiB in <= 300 s
    for bad in ({"upload_body_idle_timeout_seconds": 0}, {"upload_body_total_timeout_seconds": -5},
                {"upload_body_idle_timeout_seconds": 60, "upload_body_total_timeout_seconds": 30}):
        with pytest.raises(Exception):
            ApiSettings(app_env="development", **bad)


def test_the_app_installs_the_configured_timeouts(tmp_path):
    api = _make_api(tmp_path, upload_body_idle_timeout_seconds=7.5, upload_body_total_timeout_seconds=70.0, max_concurrent_uploads=3)
    middleware = next(m for m in api["app"].user_middleware if m.cls is UploadSizeLimitMiddleware)
    assert middleware.kwargs["body_idle_timeout"] == 7.5
    assert middleware.kwargs["body_total_timeout"] == 70.0
    assert middleware.kwargs["max_concurrent_uploads"] == 3


def test_a_stalled_upload_on_the_real_app_gets_408_without_reaching_module_a_and_nothing_else_is_blocked(tmp_path, pipeline_spy):
    api = _make_api(tmp_path, max_concurrent_uploads=1, upload_body_idle_timeout_seconds=0.5, upload_body_total_timeout_seconds=5.0)
    api["app"].state.auth_provider.principals["token-beta-reader"] = ApiPrincipal(
        principal_id="beta_reader", tenant_id="tenant_beta", scopes={SCOPE_DOCUMENTS_READ}, is_active=True
    )
    boundary = "----apiboundary"
    pdf = _pdf("after-stall")

    async def scenario():
        state = {"sent": False}
        stall_reached = asyncio.Event()

        async def stalled_receive():
            if not state["sent"]:
                state["sent"] = True
                return {"type": "http.request", "more_body": True, "body": (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n'
                    "Content-Type: application/pdf\r\n\r\n%PDF-1.4\n"
                ).encode()}
            stall_reached.set()
            await asyncio.Event().wait()                   # the client went silent

        stalled = asyncio.create_task(_call_app(api["app"], "POST", URL, _upload_headers(), stalled_receive))
        await stall_reached.wait()
        outcome = {}
        started = time.monotonic()
        # 2. another client tries to upload while the stalled one holds the only permit
        full = _multipart(pdf, boundary=boundary)

        async def one_chunk():
            return {"type": "http.request", "body": full, "more_body": False}

        outcome["other_upload"] = (await asyncio.wait_for(_call_app(api["app"], "POST", URL, _upload_headers(), one_chunk), 3))[0]
        # 6. health and another tenant's listing stay responsive
        outcome["health"] = (await asyncio.wait_for(_call_app(api["app"], "GET", "/api/v1/health"), 3))[0]
        outcome["other_tenant_list"] = (await asyncio.wait_for(
            _call_app(api["app"], "GET", URL, [(b"authorization", b"Bearer token-beta-reader")]), 3))[0]
        outcome["checked_in"] = time.monotonic() - started
        # 3. the stalled request is answered with a sanitized 408 (or the connection is closed)
        outcome["stalled"] = await asyncio.wait_for(stalled, 5)
        # 4/5. the permit is free again: a legitimate upload now succeeds
        async def legit_receive():
            return {"type": "http.request", "body": full, "more_body": False}

        outcome["legit"] = await asyncio.wait_for(_call_app(api["app"], "POST", URL, _upload_headers(), legit_receive), 10)
        return outcome

    outcome = _run_loop(scenario)
    assert outcome["other_upload"] == 429
    assert outcome["health"] == 200 and outcome["other_tenant_list"] == 200
    status, body, headers = outcome["stalled"]
    error = json.loads(body)["error"]
    assert status == 408 and error["code"] == "REQUEST_TIMEOUT" and error["details"] == {}
    assert error["request_id"] and headers["x-request-id"] == error["request_id"] and headers["connection"] == "close"
    assert outcome["legit"][0] == 202
    assert pipeline_spy["process"] == 1, "Module A ran only for the legitimate upload, never for the incomplete body"
    assert len(_doc_dirs(api)) == 1


# ============================================================================
# N4: a client rejection discovered AFTER the record was saved leaves nothing stored
# ============================================================================

CORRUPT_PDF = b"%PDF-1.4\n" + b"\x00garbage" * 50 + b"\n%%EOF\n"        # valid header, body the renderer cannot open


def _idempotency_files(api, tenant="tenant_alpha"):
    folder = api["tmp_path"] / tenant / "idempotency"
    return sorted(folder.glob("*.json")) if folder.exists() else []


def _capture_saved_ids(monkeypatch):
    saved = []
    original = LocalDiskStorage.save_document_record

    def spy(self, record):
        saved.append(record.document_id)
        return original(self, record)

    monkeypatch.setattr(LocalDiskStorage, "save_document_record", spy)
    return saved


def test_a_corrupt_pdf_is_a_400_and_leaves_nothing_stored(api, monkeypatch, logs):
    saved = _capture_saved_ids(monkeypatch)
    response = _post(api, CORRUPT_PDF, name=SECRET_NAME, key="corrupt-key")
    _assert_public_error(response, 400, "UNSUPPORTED_FILE")
    assert _error(response)["message"] == "This file type isn't supported."
    assert len(saved) == 1, "the case under test: Module A had saved the record before it rejected the file"
    document_id = saved[0]
    assert _all_doc_dirs(api) == [], "no document directory (record, original bytes) remains"
    assert not list(api["tmp_path"].rglob("document_record.json")) and not list(api["tmp_path"].rglob("*.pdf"))
    assert _idempotency_files(api) == [], "no matching idempotency artifact remains"
    assert api["client"].get(f"{URL}/{document_id}", headers=ALPHA_RW).status_code == 404       # concealed, as for any unknown id
    assert _error(api["client"].get(f"{URL}/{document_id}", headers=ALPHA_RW))["code"] == "DOCUMENT_NOT_FOUND"
    listing = api["client"].get(URL, headers=ALPHA_RW)
    assert listing.status_code == 200 and listing.json()["content"] == []
    assert SECRET_NAME not in logs.text


def test_a_corrupt_pdf_without_a_key_leaves_nothing_stored_either(api):
    assert _post(api, CORRUPT_PDF).status_code == 400
    assert _all_doc_dirs(api) == [] and _idempotency_files(api) == []


def test_after_a_corrupt_pdf_rollback_a_same_key_retry_is_a_fresh_ingestion(api, monkeypatch):
    saved = _capture_saved_ids(monkeypatch)
    assert _post(api, CORRUPT_PDF, key="retry-key").status_code == 400
    # the same key with the same file: ingested afresh (a new document id is tried) and rejected again, nothing stored
    assert _post(api, CORRUPT_PDF, key="retry-key").status_code == 400
    assert len(saved) == 2 and saved[0] != saved[1]
    assert _all_doc_dirs(api) == [] and _idempotency_files(api) == []
    # the key is not poisoned: the same key with a different, valid file is accepted (it used to be a 409 conflict)
    good = _post(api, _pdf("after-corrupt"), key="retry-key")
    assert good.status_code == 202
    detail = api["client"].get(f"{URL}/{good.json()['document_id']}", headers=ALPHA_RW)
    assert detail.status_code == 200
    assert [d["document_id"] for d in api["client"].get(URL, headers=ALPHA_RW).json()["content"]] == [good.json()["document_id"]]


def test_the_corrupt_pdf_rollback_never_touches_another_requests_document_or_idempotency_record(api, monkeypatch):
    first = _post(api, _pdf("survivor"), key="shared-key")
    assert first.status_code == 202
    survivor = first.json()["document_id"]
    before = [p.read_bytes() for p in _idempotency_files(api)]
    # the same key with a different file is a conflict (409), decided BEFORE anything is saved: nothing is rolled back
    assert _post(api, CORRUPT_PDF, key="shared-key").status_code == 409
    # another tenant's corrupt upload with the same key value touches only its own namespace
    assert _post(api, CORRUPT_PDF, headers=BETA_WRITER, key="shared-key").status_code == 400
    assert api["client"].get(f"{URL}/{survivor}", headers=ALPHA_RW).status_code == 200
    assert [p.read_bytes() for p in _idempotency_files(api)] == before
    assert _doc_dirs(api) == [survivor] and _doc_dirs(api, "tenant_beta") == []


def test_the_rollback_requires_the_matching_document_id_fingerprint_and_tenant(api):
    """Direct service-level checks of the ownership conditions (the route cannot produce these mismatches)."""
    components = api["container"].get_tenant_components("tenant_alpha")
    service = DocumentUploadService(module_a=components.module_a, tenant_id="tenant_alpha", storage=components.storage)
    store = components.module_a.idempotency_store

    def make(marker, key):
        pdf = _pdf(marker)
        response = _post(api, pdf, key=key, name="x.pdf")
        assert response.status_code == 202
        import hashlib
        return response.json()["document_id"], hashlib.sha256(pdf).hexdigest()

    doc_a, hash_a = make("owner-a", "key-a")
    doc_b, hash_b = make("owner-b", "key-b")
    # a rejection payload for doc B, delivered under key A: B's artifacts go (it is the rejected document), A's record stays
    service._roll_back_late_rejection(SimpleNamespace(document_id=doc_b, file_hash_sha256=hash_b, claimed_filename="x.pdf"), "key-a")
    assert doc_b not in _doc_dirs(api) and doc_a in _doc_dirs(api)
    assert store.get_record("tenant_alpha", "key-a").document_id == doc_a
    assert store.get_record("tenant_alpha", "key-b").document_id == doc_b, "B's own record is not named by key-a"
    # right document id, WRONG fingerprint (another file/name): the idempotency record is left alone
    doc_c, hash_c = make("owner-c", "key-c")
    service._roll_back_late_rejection(SimpleNamespace(document_id=doc_c, file_hash_sha256="0" * 64, claimed_filename="x.pdf"), "key-c")
    assert store.get_record("tenant_alpha", "key-c") is not None
    # right fingerprint and id: removed under the key lock
    doc_d, hash_d = make("owner-d", "key-d")
    service._roll_back_late_rejection(SimpleNamespace(document_id=doc_d, file_hash_sha256=hash_d, claimed_filename="x.pdf"), "key-d")
    assert store.get_record("tenant_alpha", "key-d") is None and doc_d not in _doc_dirs(api)
    # the idempotency record names ANOTHER document although the fingerprint is the same (same file and name): left alone
    doc_e = _post(api, _pdf("owner-a"), name="x.pdf").json()["document_id"]                  # same bytes as doc A, no key
    assert doc_e != doc_a
    service._roll_back_late_rejection(SimpleNamespace(document_id=doc_e, file_hash_sha256=hash_a, claimed_filename="x.pdf"), "key-a")
    assert doc_e not in _doc_dirs(api) and store.get_record("tenant_alpha", "key-a").document_id == doc_a
    # artifacts of an id for which this tenant has NO record are not this request's to delete
    stray = api["tmp_path"] / "tenant_alpha" / "doc_aaaaaaaaaaaa"
    stray.mkdir()
    (stray / "keep.txt").write_text("not mine")
    service._roll_back_late_rejection(SimpleNamespace(document_id="doc_aaaaaaaaaaaa", file_hash_sha256="", claimed_filename=""), None)
    assert (stray / "keep.txt").exists()
    # no record of that id in this tenant (a rejection before the record was saved, or another tenant's id): nothing happens
    beta_doc = _post(api, _pdf("beta"), headers=BETA_WRITER).json()["document_id"]
    service._roll_back_late_rejection(SimpleNamespace(document_id=beta_doc, file_hash_sha256="0" * 64, claimed_filename="x.pdf"), None)
    service._roll_back_late_rejection(SimpleNamespace(document_id="doc_rej_deadbeef", file_hash_sha256="", claimed_filename=""), "key-a")
    service._roll_back_late_rejection(SimpleNamespace(document_id="../../etc", file_hash_sha256="", claimed_filename=""), "key-a")
    assert beta_doc in _doc_dirs(api, "tenant_beta") and doc_a in _doc_dirs(api)


def _service_log_records(logs):
    """The upload service's log records. The ``logs`` fixture listens on two nested loggers, so one record is captured twice."""
    unique = {}
    for record in logs.records:
        if '"name": "doc_extractor.api.upload_service"' in record:
            unique.setdefault(json.loads(record)["created"], record)           # one record, one creation time
    return list(unique.values())


def _the_one_idempotency_artifact(api):
    files = _idempotency_files(api)
    assert len(files) == 1
    return json.loads(files[0].read_text())


def test_a_cleanup_that_silently_does_nothing_is_a_generic_500_and_never_a_400(api, monkeypatch, logs):
    """
    R1. The storage backend's ``cleanup_document`` swallows errors, so it can return without removing anything. The rollback
    must verify, and must not answer 400 ("nothing is stored") when the document is still there.
    """
    saved = _capture_saved_ids(monkeypatch)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(LocalDiskStorage, "cleanup_document", lambda self, document_id: None)
        response = _post(api, CORRUPT_PDF, name=SECRET_NAME, key="secret-idem-key-xyz")
    body = _assert_public_error(response, 500, "INTERNAL_ERROR", "late_rejection", "rollback", "UNSUPPORTED")
    assert len(saved) == 1
    document_id = saved[0]
    # the document really is still stored, and the idempotency record was NOT deleted prematurely
    assert _doc_dirs(api) == [document_id]
    assert _storage(api).get_document_record(document_id) is not None
    assert _the_one_idempotency_artifact(api)["document_id"] == document_id
    # API-002 and API-015 tell the truth about it: it exists (as processing), it is not presented as rejected-and-removed
    assert api["client"].get(f"{URL}/{document_id}", headers=ALPHA_RW).status_code == 200
    assert [(d["document_id"], d["status"]) for d in api["client"].get(URL, headers=ALPHA_RW).json()["content"]] == [(document_id, "processing")]
    # exactly one fixed-reason rollback log line with only safe identifiers
    records = _service_log_records(logs)
    assert len(records) == 1
    assert "late_rejection_rollback_incomplete" in records[0] and body["request_id"] in records[0] and document_id in records[0]
    for forbidden in (SECRET_NAME, "secret-idem-key-xyz", "OSError", str(api["tmp_path"])):
        assert forbidden not in logs.text, f"{forbidden!r} reached a log record"
    # recovery: once cleanup works again, a retry with the same key rejects the file and rolls the document back for real
    retry = _post(api, CORRUPT_PDF, name=SECRET_NAME, key="secret-idem-key-xyz")        # same file and name: same request
    assert retry.status_code == 400
    assert _all_doc_dirs(api) == [] and _idempotency_files(api) == []


def test_a_cleanup_that_raises_is_the_same_sanitized_500(api, monkeypatch, logs):
    saved = _capture_saved_ids(monkeypatch)

    def explode(self, document_id):
        raise OSError("cannot delete /srv/secret/path SECRET-EXC")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(LocalDiskStorage, "cleanup_document", explode)
        response = _post(api, CORRUPT_PDF, name=SECRET_NAME, key="secret-idem-key-xyz")
    body = _assert_public_error(response, 500, "INTERNAL_ERROR", "/srv/secret", "SECRET-EXC", "OSError", "late_rejection")
    document_id = saved[0]
    assert _doc_dirs(api) == [document_id] and _the_one_idempotency_artifact(api)["document_id"] == document_id
    records = _service_log_records(logs)
    assert len(records) == 1 and "late_rejection_rollback_incomplete" in records[0] and body["request_id"] in records[0]
    for forbidden in ("/srv/secret", "SECRET-EXC", SECRET_NAME, "secret-idem-key-xyz"):
        assert forbidden not in logs.text, f"{forbidden!r} reached a log record"


def test_a_partial_cleanup_that_removes_only_the_record_is_still_incomplete(api, monkeypatch, logs):
    """The record is gone but the original document artifact is not: 'nothing stored' is false, so the answer is a 500."""
    saved = _capture_saved_ids(monkeypatch)

    def remove_only_the_record(self, document_id):
        (self._get_document_dir(document_id) / "document_record.json").unlink()

    monkeypatch.setattr(LocalDiskStorage, "cleanup_document", remove_only_the_record)
    response = _post(api, CORRUPT_PDF, key="partial-key")
    _assert_public_error(response, 500, "INTERNAL_ERROR")
    document_id = saved[0]
    assert _storage(api).get_document_record(document_id) is None
    assert list((api["tmp_path"] / "tenant_alpha" / document_id).rglob("*.pdf")), "the original artifact really remains"
    assert len(_idempotency_files(api)) == 1, "the idempotency record is not deleted while the document is not gone"
    assert "late_rejection_rollback_incomplete" in _service_log_records(logs)[0]


def test_a_rollback_that_cannot_be_verified_without_a_key_is_also_a_500(api, monkeypatch, logs):
    monkeypatch.setattr(LocalDiskStorage, "cleanup_document", lambda self, document_id: None)
    response = _post(api, CORRUPT_PDF)
    _assert_public_error(response, 500, "INTERNAL_ERROR")
    assert len(_doc_dirs(api)) == 1 and len(_service_log_records(logs)) == 1


def test_a_failing_idempotency_deletion_after_the_document_is_gone_is_a_500_that_heals_on_retry(api, monkeypatch, logs):
    saved = _capture_saved_ids(monkeypatch)

    def refuse(self, tenant_id, idempotency_key):
        raise OSError("cannot delete /srv/idem-secret")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(LocalIdempotencyStore, "delete_record", refuse)
        response = _post(api, CORRUPT_PDF, name=SECRET_NAME, key="secret-idem-key-xyz")
    body = _assert_public_error(response, 500, "INTERNAL_ERROR", "/srv/idem-secret", "idempotency")
    document_id = saved[0]
    assert _all_doc_dirs(api) == [], "the document itself was removed and verified"
    assert api["client"].get(f"{URL}/{document_id}", headers=ALPHA_RW).status_code == 404
    assert len(_idempotency_files(api)) == 1, "the orphaned idempotency record could not be deleted"
    records = _service_log_records(logs)
    assert len(records) == 1 and "late_rejection_idempotency_cleanup_incomplete" in records[0] and body["request_id"] in records[0]
    for forbidden in ("/srv/idem-secret", SECRET_NAME, "secret-idem-key-xyz"):
        assert forbidden not in logs.text
    # safe recovery: Module A's orphan check treats the record as stale; the same-key retry is a fresh ingestion
    retry = _post(api, CORRUPT_PDF, name=SECRET_NAME, key="secret-idem-key-xyz")        # same file and name: same request
    assert retry.status_code == 400
    assert _all_doc_dirs(api) == [] and _idempotency_files(api) == []


def test_an_idempotency_deletion_that_silently_fails_is_detected(api, monkeypatch, logs):
    monkeypatch.setattr(LocalIdempotencyStore, "delete_record", lambda self, tenant_id, idempotency_key: None)
    response = _post(api, CORRUPT_PDF, key="k-silent")
    _assert_public_error(response, 500, "INTERNAL_ERROR")
    assert len(_idempotency_files(api)) == 1
    assert "late_rejection_idempotency_cleanup_incomplete" in _service_log_records(logs)[0]


def test_a_successful_verified_rollback_still_answers_400_and_logs_nothing(api, monkeypatch, logs):
    saved = _capture_saved_ids(monkeypatch)
    response = _post(api, CORRUPT_PDF, key="clean-key")
    _assert_public_error(response, 400, "UNSUPPORTED_FILE")
    assert len(saved) == 1 and _all_doc_dirs(api) == [] and _idempotency_files(api) == []
    assert _service_log_records(logs) == []


class _FakeStorage:
    """Scriptable storage: a record that disappears after cleanup, optionally leaving page artifacts or raising."""

    def __init__(self, *, pages=(), lookup_raises=False, record_survives=False):
        self.record = {"document_id": "doc_0123456789ab", "tenant_id": "tenant_alpha"}
        self.pages, self.lookup_raises, self.record_survives, self.cleaned = list(pages), lookup_raises, record_survives, 0

    def get_document_record(self, document_id):
        if self.lookup_raises:
            raise OSError("SECRET-LOOKUP")
        return self.record

    def cleanup_document(self, document_id):
        self.cleaned += 1
        if not self.record_survives:
            self.record = None

    def list_page_images(self, document_id):
        return self.pages


def _rollback_with(storage, logs):
    class Pipeline:
        tenant_id = "tenant_alpha"

    service = DocumentUploadService(Pipeline(), "tenant_alpha", storage=storage)
    payload = SimpleNamespace(document_id="doc_0123456789ab", file_hash_sha256="0" * 64, claimed_filename="x.pdf")
    return service._roll_back_late_rejection(payload, None, "req-unit-0001")


def test_rollback_verification_unit_cases(logs):
    assert _rollback_with(_FakeStorage(), logs) is None                                     # removed and verified
    assert _rollback_with(_FakeStorage(pages=["page_0001.png"]), logs) == "late_rejection_rollback_incomplete"   # artifacts left
    assert _rollback_with(_FakeStorage(lookup_raises=True), logs) == "late_rejection_rollback_incomplete"        # cannot be verified
    assert _rollback_with(_FakeStorage(record_survives=True), logs) == "late_rejection_rollback_incomplete"      # record still there
    assert len(_service_log_records(logs)) == 3
    assert "SECRET-LOOKUP" not in logs.text


# ---- R3: an orphaned idempotency record never answers 409 -------------------------------------------------------------------

def _orphan_through_the_route(api, key, pdf, tenant_headers=WRITER, tenant="tenant_alpha"):
    first = _post(api, pdf, key=key, headers=tenant_headers)
    assert first.status_code == 202
    document_id = first.json()["document_id"]
    _storage(api, tenant).cleanup_document(document_id)                       # the document is gone, its idempotency record stays
    assert _storage(api, tenant).get_document_record(document_id) is None
    return document_id


def test_an_orphaned_key_accepts_a_different_valid_file_as_fresh_ingestion(api):
    key = "orphan-key"
    gone = _orphan_through_the_route(api, key, _pdf("orphan-a"))
    assert len(_idempotency_files(api)) == 1
    different = _post(api, _pdf("orphan-b", 2), key=key)
    assert different.status_code == 202, different.text                       # it used to be 409
    fresh = different.json()["document_id"]
    assert fresh != gone and _doc_dirs(api) == [fresh]
    assert api["client"].get(f"{URL}/{fresh}", headers=ALPHA_RW).status_code == 200
    assert _the_one_idempotency_artifact(api)["document_id"] == fresh
    assert _post(api, _pdf("orphan-b", 2), key=key).json()["document_id"] == fresh        # a normal replay from here on
    assert _post(api, _pdf("orphan-a"), key=key).status_code == 409                       # and a real conflict: its document exists


def test_an_orphan_left_by_a_failed_idempotency_deletion_no_longer_poisons_the_key(api, monkeypatch):
    """The R1 failure path (document removed, idempotency delete failed) followed by a different valid file with that key."""
    def refuse(self, tenant_id, idempotency_key):
        raise OSError("cannot delete")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(LocalIdempotencyStore, "delete_record", refuse)
        assert _post(api, CORRUPT_PDF, key="poison-key").status_code == 500
    assert _all_doc_dirs(api) == [] and len(_idempotency_files(api)) == 1
    good = _post(api, _pdf("after-poison"), key="poison-key")
    assert good.status_code == 202, good.text                                  # was 409 until the same file was retried
    assert _doc_dirs(api) == [good.json()["document_id"]]


def test_a_live_document_with_a_different_file_still_conflicts_and_the_record_is_untouched(api):
    first = _post(api, _pdf("live-a"), key="live-key").json()["document_id"]
    before = _idempotency_files(api)[0].read_bytes()
    assert _post(api, _pdf("live-b", 2), key="live-key").status_code == 409
    assert _idempotency_files(api)[0].read_bytes() == before
    assert _doc_dirs(api) == [first] and api["client"].get(f"{URL}/{first}", headers=ALPHA_RW).status_code == 200


def test_recovering_an_orphan_leaves_another_tenants_record_and_document_alone(api):
    key = "same-key-value"
    beta_doc = _post(api, _pdf("beta-doc"), key=key, headers=BETA_WRITER).json()["document_id"]
    beta_file = (api["tmp_path"] / "tenant_beta" / "idempotency")
    before = [p.read_bytes() for p in sorted(beta_file.glob("*.json"))]
    _orphan_through_the_route(api, key, _pdf("alpha-doc"))
    assert _post(api, _pdf("alpha-other", 2), key=key).status_code == 202
    assert [p.read_bytes() for p in sorted(beta_file.glob("*.json"))] == before
    assert _doc_dirs(api, "tenant_beta") == [beta_doc]


@pytest.mark.parametrize("same_file", [True, False])
def test_concurrent_same_key_requests_after_an_orphan_stay_safe(api, same_file):
    key = "race-key"
    _orphan_through_the_route(api, key, _pdf("race-a"))
    files = [_pdf("race-a"), _pdf("race-a")] if same_file else [_pdf("race-a"), _pdf("race-b", 2)]
    barrier = threading.Barrier(2)

    def go(content):
        barrier.wait()
        return _post(api, content, key=key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(go, files))
    codes = sorted(r.status_code for r in responses)
    ids = {r.json()["document_id"] for r in responses if r.status_code == 202}
    assert codes == ([202, 202] if same_file else [202, 409])
    assert len(ids) == 1 and _doc_dirs(api) == sorted(ids), "one document, whichever request won"
    assert _the_one_idempotency_artifact(api)["document_id"] in ids


# ---- R2: a password-protected PDF is a client rejection ----------------------------------------------------------------------

# A real AES-256 password-protected PDF (user and owner passwords set), generated with PyMuPDF
# (``page.insert_text`` then ``doc.save(buf, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=..., owner_pw=...)``), embedded so
# the test does not depend on PyMuPDF. PDFium refuses it with "Incorrect password error".
ENCRYPTED_PDF = __import__("base64").b64decode(
    "JVBERi0xLjcKJcK1wrYKJSBXcml0dGVuIGJ5IE11UERGIDEuMjguMgoKMSAwIG9iago8PC9UeXBlL0NhdGFsb2cvUGFnZXMgMiAw"
    "IFIvSW5mbzw8L1Byb2R1Y2VyPEZCRTY0MTZEOTIzMjM1NzA5RDJCMzgwNTU2MTE5MzM3RDc5NUVGRDhFRTQwN0Y3REY1MUUxQURF"
    "QkYwQzgyNDQ+Pj4+PgplbmRvYmoKCjIgMCBvYmoKPDwvVHlwZS9QYWdlcy9Db3VudCAxL0tpZHNbNCAwIFJdPj4KZW5kb2JqCgoz"
    "IDAgb2JqCjw8L0ZvbnQ8PC9oZWx2IDUgMCBSPj4+PgplbmRvYmoKCjQgMCBvYmoKPDwvVHlwZS9QYWdlL01lZGlhQm94WzAgMCA1"
    "OTUgODQyXS9Sb3RhdGUgMC9SZXNvdXJjZXMgMyAwIFIvUGFyZW50IDIgMCBSL0NvbnRlbnRzWzYgMCBSXT4+CmVuZG9iagoKNSAw"
    "IG9iago8PC9UeXBlL0ZvbnQvU3VidHlwZS9UeXBlMS9CYXNlRm9udC9IZWx2ZXRpY2EvRW5jb2RpbmcvV2luQW5zaUVuY29kaW5n"
    "Pj4KZW5kb2JqCgo2IDAgb2JqCjw8L0xlbmd0aCAxMTIvRmlsdGVyL0ZsYXRlRGVjb2RlPj4Kc3RyZWFtCtHXS44p/1vBTKCTiGCJ"
    "DZCTYWqV6UKp+JtkBBMV3LC9D7Z2o4LZPF58WHQRGJRJRZQdVqSIE3uLOwotlXbUuQgU2VcNoMlzoYOKLEXHNcXA7KLi4PvO2kaJ"
    "8zS1bEh5nLTGoyA3dGbREjNjzy38rykKZW5kc3RyZWFtCmVuZG9iagoKeHJlZgowIDcKMDAwMDAwMDAwMCA2NTUzNSBmIAowMDAw"
    "MDAwMDQyIDAwMDAwIG4gCjAwMDAwMDAxNzIgMDAwMDAgbiAKMDAwMDAwMDIyNCAwMDAwMCBuIAowMDAwMDAwMjY1IDAwMDAwIG4g"
    "CjAwMDAwMDAzNzIgMDAwMDAgbiAKMDAwMDAwMDQ2MSAwMDAwMCBuIAoKdHJhaWxlcgo8PC9TaXplIDcvUm9vdCAxIDAgUi9JRFs8"
    "QzM5NDY1MTUwOTFGNUZDMjlDMkY3MTY4MjI0NEMyQUE+PDhBNDU3M0FENDA0NkZDMUU5MTUyRDVGREExQzYyRDRGPl0vRW5jcnlw"
    "dDw8L0ZpbHRlci9TdGFuZGFyZC9SIDYvViA1L0xlbmd0aCAyNTYvUCAtNC9FbmNyeXB0TWV0YWRhdGEgdHJ1ZS9TdG1GL1N0ZENG"
    "L1N0ckYvU3RkQ0YvQ0Y8PC9TdGRDRjw8L0F1dGhFdmVudC9Eb2NPcGVuL0NGTS9BRVNWMy9MZW5ndGggMzI+Pj4+L088RDA2QjQ5"
    "Q0E1OUVEMkNEOEVFOTcyMjdGNkIyNjUxRTQ2MjFCMzI4QTYwRTJFQUY3RDkxMTA1NTY1M0I0MEQwQzE0Q0IzMjQ4NDM4MDM2RTZC"
    "Q0M5RDM5RUZFNTMyMjE3Pi9VPDZDQUU1Qzg4QjM1MDI4NTdEQTcyMUI1N0RCM0RFMTZEOUUwMUFBQkIyOTAzN0FDRUQ4MzA4MzUy"
    "NjU1QkI4NUIzMDUzNTY2MjM5OUFEQUQ4RDE3MDNEQTJCMUZEMkYwRT4vT0U8QjY1QkI1QTFBREQzQ0MwRjU2RTNFRDY4Qjc5OTVE"
    "MjExM0MwQ0VFMDM1QUQwOEIxQ0U1MTVFQkQwMUQxM0U0RD4vVUU8NDQ1MjE1M0MyQjBCNTBDMEMxNDY5NEE2QjcxMzMxMzA2Mzk2"
    "RUNBRUQyQjg0MTdDQTIwOTQ0RTZFNzZDQTU1OT4vUGVybXM8NkVBMTBGMzZDODNGNzg1NzQ3NDVBRUVBQkM1REIwQjA+Pj4+Pgpz"
    "dGFydHhyZWYKNjQyCiUlRU9GCg=="
)


def _capture_encrypted_renderer_error(monkeypatch):
    seen = []
    original = PdfRenderer.render_pdf

    def spy(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except DocumentProcessingError as exc:
            seen.append(exc.code)
            raise

    monkeypatch.setattr(PdfRenderer, "render_pdf", spy)
    return seen


def test_a_password_protected_pdf_is_a_400_unsupported_file_and_is_rolled_back(api, monkeypatch, logs):
    assert ENCRYPTED_PDF.startswith(b"%PDF-") and b"/Encrypt" in ENCRYPTED_PDF
    saved = _capture_saved_ids(monkeypatch)
    renderer_codes = _capture_encrypted_renderer_error(monkeypatch)
    response = _post(api, ENCRYPTED_PDF, name=SECRET_NAME, key="enc-key")
    assert renderer_codes == ["DOCUMENT_ENCRYPTED"], "the renderer itself identified the file as encrypted"
    body = _assert_public_error(response, 400, "UNSUPPORTED_FILE", "password", "encrypt", "DOCUMENT_ENCRYPTED", "protected")
    assert body["message"] == "This file type isn't supported."
    assert len(saved) == 1, "the record had been saved before the renderer rejected the file"
    document_id = saved[0]
    assert _all_doc_dirs(api) == [] and not list(api["tmp_path"].rglob("document_record.json"))
    assert not list(api["tmp_path"].rglob("*.pdf")), "the original document artifact is removed"
    assert _idempotency_files(api) == [], "the matching idempotency record is removed"
    assert _error(api["client"].get(f"{URL}/{document_id}", headers=ALPHA_RW))["code"] == "DOCUMENT_NOT_FOUND"
    assert api["client"].get(URL, headers=ALPHA_RW).json()["content"] == []
    assert _service_log_records(logs) == []
    assert SECRET_NAME not in logs.text


def test_after_an_encrypted_pdf_rollback_a_same_key_retry_is_fresh_and_the_key_is_not_poisoned(api, monkeypatch):
    saved = _capture_saved_ids(monkeypatch)
    assert _post(api, ENCRYPTED_PDF, key="enc-retry").status_code == 400
    assert _post(api, ENCRYPTED_PDF, key="enc-retry").status_code == 400
    assert len(saved) == 2 and saved[0] != saved[1], "the retry is a fresh ingestion, not a replay"
    assert _all_doc_dirs(api) == [] and _idempotency_files(api) == []
    good = _post(api, _pdf("after-encrypted"), key="enc-retry")                       # it used to be a 409 conflict
    assert good.status_code == 202
    assert api["client"].get(f"{URL}/{good.json()['document_id']}", headers=ALPHA_RW).status_code == 200


def test_a_password_protected_pdf_without_a_key_leaves_nothing_either(api):
    assert _post(api, ENCRYPTED_PDF).status_code == 400
    assert _all_doc_dirs(api) == [] and _idempotency_files(api) == []


def test_an_encrypted_pdf_whose_cleanup_cannot_be_verified_is_a_500_not_a_400(api, monkeypatch, logs):
    monkeypatch.setattr(LocalDiskStorage, "cleanup_document", lambda self, document_id: None)
    response = _post(api, ENCRYPTED_PDF, key="enc-unverified")
    _assert_public_error(response, 500, "INTERNAL_ERROR")
    assert len(_doc_dirs(api)) == 1 and len(_service_log_records(logs)) == 1


@pytest.mark.parametrize("label, patcher", [
    ("preprocessing", lambda mp: mp.setattr(
        "doc_extractor.preprocessing.PagePreprocessor.preprocess_page", lambda self, page: (_ for _ in ()).throw(RuntimeError("cv2 SECRET")))),
    ("original-document store", lambda mp: mp.setattr(
        LocalDiskStorage, "store_original_document", lambda self, **kw: (_ for _ in ()).throw(OSError("disk SECRET")))),
])
def test_operational_post_save_failures_stay_500_and_keep_their_processing_record_documented_limitation(api, monkeypatch, label, patcher):
    patcher(monkeypatch)
    response = _post(api, _pdf("operational-" + label, 2), key="op-key")
    _assert_public_error(response, 500, "INTERNAL_ERROR", "SECRET")
    (document_id,) = _doc_dirs(api)
    record = json.loads((_storage(api)._get_document_dir(document_id) / "document_record.json").read_text())
    assert record["status"] == "processing"
    assert [d["document_id"] for d in api["client"].get(URL, headers=ALPHA_RW).json()["content"]] == [document_id]


def test_a_real_per_page_renderer_rollback_and_a_keyed_retry_through_the_unmocked_renderer(api, monkeypatch):
    """The renderer's own rollback (no render_pdf patch): record, pages and idempotency record are gone; the retry is fresh."""
    original = LocalDiskStorage.store_page_image
    state = {"fail": True}

    def flaky(self, document_id, page_number, *args, **kwargs):
        if state["fail"] and page_number == 2:
            raise OSError("disk full SECRET")
        return original(self, document_id, page_number, *args, **kwargs)

    monkeypatch.setattr(LocalDiskStorage, "store_page_image", flaky)
    pdf = _pdf("real-rollback", 3)
    failed = _post(api, pdf, key="rr-key")
    _assert_public_error(failed, 500, "INTERNAL_ERROR", "SECRET")
    assert _all_doc_dirs(api) == [] and _idempotency_files(api) == []
    state["fail"] = False
    retry = _post(api, pdf, key="rr-key")
    assert retry.status_code == 202
    document_id = retry.json()["document_id"]
    assert api["client"].get(f"{URL}/{document_id}", headers=ALPHA_RW).status_code == 200
    assert api["client"].get(f"{URL}/{document_id}/pages/1", headers=ALPHA_RW).status_code == 200
    assert _post(api, pdf, key="rr-key").json()["document_id"] == document_id               # replay, no second document
    assert _doc_dirs(api) == [document_id]


# ============================================================================
# N6: no unused extra_request_params passthrough in the pipeline (ingestion keeps its own, with a real caller)
# ============================================================================

def test_the_pipeline_has_no_extra_request_params_passthrough_and_ingestion_keeps_its_own():
    assert "extra_request_params" not in inspect.signature(ModuleAPipeline.process_document).parameters
    assert "extra_request_params" in inspect.signature(ingestion_module.IngestionService.ingest_document).parameters
    assert "extra_request_params" not in inspect.getsource(upload_service_module)


def test_the_route_depends_on_the_tenant_module_a_component_and_not_on_document_service():
    from doc_extractor.api import dependencies
    from doc_extractor.api.routers import documents as router_module

    dependency_source = inspect.getsource(dependencies.get_document_upload_service)
    assert "get_tenant_components" in dependency_source and ".module_a" in dependency_source
    route_source = inspect.getsource(router_module.upload_document)
    assert "process_document" not in route_source and "DocumentService(" not in route_source
    service_source = inspect.getsource(upload_service_module.DocumentUploadService)
    assert "self.module_a.process_document" in service_source        # the one entry point into Module A
    assert "DocumentService" not in inspect.getsource(upload_service_module).split('"""', 2)[2].replace("DocumentUploadService", "")
