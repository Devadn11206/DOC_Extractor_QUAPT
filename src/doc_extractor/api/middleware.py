"""
Middleware components for the Document Extraction Engine REST API (Module M5).

Adheres to:
- PRD §7.2 API-010: Request ID correlation and tracing.
- Generation of unique request identifiers.
- ContextVar and request.state propagation across the entire request lifecycle.
- Inclusion of X-Request-ID in all HTTP response headers.
- Safe validation of client-supplied identifiers to prevent header injection or untrusted tracking IDs.
- A narrowly scoped, pre-spooling request-body limit for the document upload endpoint (API-001).
"""

import contextvars
import re
import threading
import time
from typing import Any, Callable, Dict, MutableMapping, Optional
from uuid import uuid4
import anyio
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# Context variable to make request_id accessible anywhere in async execution context
request_id_ctx_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "request_id", default=None
)

# Strict regex for client-supplied request IDs: alphanumeric, underscores, hyphens only, 8-64 chars
_SAFE_REQUEST_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]{8,64}$")


def get_request_id_from_context() -> Optional[str]:
    """Retrieve the current request ID from the async context variable."""
    return request_id_ctx_var.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Middleware managing request identifier generation, validation, propagation,
    and response header assignment.
    """

    def __init__(
        self,
        app: Callable,
        header_name: str = "X-Request-ID",
        allow_client_request_id: bool = True,
    ) -> None:
        super().__init__(app)
        self.header_name = header_name
        self.allow_client_request_id = allow_client_request_id

    def _resolve_request_id(self, incoming_header: Optional[str]) -> str:
        """
        Validates client-supplied header or generates a fresh authoritative ID.
        Untrusted or malformed headers are discarded to prevent injection.
        """
        if self.allow_client_request_id and incoming_header:
            candidate = incoming_header.strip()
            if _SAFE_REQUEST_ID_REGEX.match(candidate):
                return candidate

        # Fallback to server-generated authoritative UUID
        return f"req_{uuid4().hex}"

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        raw_header = request.headers.get(self.header_name)
        request_id = self._resolve_request_id(raw_header)

        # Store in request.state for Starlette/FastAPI route & error handlers
        request.state.request_id = request_id

        # Store in ContextVar for logging / auditing across coroutine boundaries
        token = request_id_ctx_var.set(request_id)

        try:
            response = await call_next(request)
            # Ensure response contains documented correlation header
            response.headers[self.header_name] = request_id
            return response
        finally:
            request_id_ctx_var.reset(token)


# ---------------------------------------------------------------------------------------------------------------------
# API-001 request-body limit (pure ASGI, runs BEFORE any multipart parsing)
# ---------------------------------------------------------------------------------------------------------------------

# Headroom for the multipart framing around the file (boundaries, part headers, the few small optional fields). It is NOT
# a second file-size limit: the per-file limit (``max_file_size_bytes``) is still enforced by Module A's validation, and a
# file between the two numbers reaches that check. PROVISIONAL: a placeholder, not a PRD figure.
UPLOAD_MULTIPART_OVERHEAD_BYTES = 1024 * 1024

_Message = MutableMapping[str, Any]


def _declared_content_length(scope: Dict[str, Any]) -> Optional[int]:
    """The single, well-formed ``Content-Length`` the client declared, or ``None`` (absent, repeated or malformed)."""
    values = [value for name, value in scope.get("headers", []) if name == b"content-length"]
    if len(values) != 1 or not values[0].isdigit():
        return None
    return int(values[0])


def route_path_of(scope: Dict[str, Any]) -> str:
    """
    The request path as the router sees it: ``scope["path"]`` without the ASGI ``root_path`` prefix.

    This reproduces ``starlette.routing.get_route_path`` (added in Starlette 0.35) so the upload guard keeps the same
    view of the path as the router at every Starlette version the project declares (``starlette>=0.27.0``): a server such
    as uvicorn with ``root_path="/svc"`` reports ``/svc/api/v1/documents``, and the guard must still recognise the route.
    """
    path: str = scope["path"]
    root_path = scope.get("root_path", "")
    if not root_path:
        return path
    if not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if path[len(root_path)] == "/":
        return path[len(root_path):]
    return path


class UploadSizeLimitMiddleware:
    """
    Admission control, a body-receive deadline and a request-body size limit for the upload endpoint, all applied BEFORE
    the multipart body is parsed.

    Starlette parses (and spools to memory or disk) the complete multipart body before the route handler runs, so a check
    inside the handler comes too late. This is a plain ASGI middleware, not ``BaseHTTPMiddleware``, so it sits in front of
    the body stream without buffering it:

    - Admission (process-local): at most ``max_concurrent_uploads`` requests are admitted at a time; any further request is
      answered at once with a sanitized ``429 RATE_LIMITED`` and nothing is queued. The blocking lock protects ONLY the
      synchronous check and increment/decrement of the counter: it is never held across an ``await``, because blocking
      the event-loop thread on it while another task is suspended holding it would freeze the whole server.
    - Body deadline: the body must keep arriving (``body_idle_timeout``: no more than this long without body bytes; it
      resets only when bytes actually arrive) and must be complete within ``body_total_timeout``. Otherwise the request is
      answered with a sanitized ``408 REQUEST_TIMEOUT`` and the application sees a disconnected client: it can neither
      finish parsing an incomplete body nor reach Module A. The deadline covers only the body-receive period; time spent
      afterwards (Module A processing) is not counted. A silent client therefore cannot hold a permit.
    - A declared ``Content-Length`` above the limit is refused (``413``) without reading a single body byte.
      ``Content-Length`` is never trusted on its own: the bytes that actually arrive are counted as they are pulled by
      the parser, so a chunked request or one that understates its size is stopped as soon as the running total exceeds
      the limit. At most one more chunk than the limit is ever read.
    - Nothing is accumulated here: only counters and timestamps are kept. The permit is returned in a ``finally``, so
      success, an exception, a disconnect, a timeout and a cancellation all release it.

    Scope: one method and one exact path (the upload route). Every other request is passed through untouched.
    """

    def __init__(
        self,
        app: Callable,
        *,
        path: str,
        max_body_bytes: int,
        method: str = "POST",
        max_concurrent_uploads: int = 2,
        body_idle_timeout: float = 30.0,
        body_total_timeout: float = 300.0,
    ) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        if max_concurrent_uploads <= 0:
            raise ValueError("max_concurrent_uploads must be positive")
        if body_idle_timeout <= 0 or body_total_timeout <= 0:
            raise ValueError("the body timeouts must be positive")
        self.app = app
        self.path = path
        self.method = method
        self.max_body_bytes = max_body_bytes
        self.max_concurrent_uploads = max_concurrent_uploads
        self.body_idle_timeout = body_idle_timeout
        self.body_total_timeout = body_total_timeout
        self._admission_lock = threading.Lock()
        self._active_uploads = 0

    async def _send_error(
        self, scope: Dict[str, Any], receive: Callable, send: Callable, *, code: str, message: str, status_code: int,
        headers: Dict[str, str],
    ) -> None:
        from doc_extractor.api.exceptions import build_error_response       # lazy: exceptions imports this module lazily too

        state = scope.get("state") or {}
        response = build_error_response(
            code=code,
            message=message,
            status_code=status_code,
            request_id=state.get("request_id"),
            details={},
            headers=headers,
        )
        await response(scope, receive, send)

    async def _send_too_large(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        await self._send_error(
            scope, receive, send, code="FILE_TOO_LARGE", message="The file is larger than the limit.", status_code=413,
            headers={"Connection": "close"},          # the rest of the body was not read: do not reuse this connection
        )

    async def _send_rate_limited(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        await self._send_error(
            scope, receive, send, code="RATE_LIMITED", message="Too many concurrent uploads. Try again shortly.",
            status_code=429, headers={"Retry-After": "5", "Connection": "close"},
        )

    async def _send_request_timeout(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        await self._send_error(
            scope, receive, send, code="REQUEST_TIMEOUT", message="The upload was not received in time.",
            status_code=408, headers={"Connection": "close"},
        )

    def _try_admit(self) -> bool:
        """Synchronous check-and-increment. The lock is held only inside this method, never across an await."""
        with self._admission_lock:
            if self._active_uploads >= self.max_concurrent_uploads:
                return False
            self._active_uploads += 1
            return True

    def _release(self) -> None:
        """Synchronous decrement, the counterpart of ``_try_admit``."""
        with self._admission_lock:
            self._active_uploads -= 1

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or scope.get("method") != self.method:
            await self.app(scope, receive, send)
            return

        if route_path_of(scope) != self.path:
            await self.app(scope, receive, send)
            return

        if not self._try_admit():
            await self._send_rate_limited(scope, receive, send)          # outside the lock: this await may yield
            return

        try:
            declared = _declared_content_length(scope)
            if declared is not None and declared > self.max_body_bytes:
                await self._send_too_large(scope, receive, send)
                return

            started = time.monotonic()
            last_progress = started
            seen = 0
            rejected = False
            body_complete = False
            response_started = False

            async def limited_receive() -> _Message:
                nonlocal seen, rejected, body_complete, last_progress
                if rejected:
                    return {"type": "http.disconnect"}
                if body_complete:
                    return await receive()          # the body is in: nothing here is timed (Module A time is not counted)

                delay = min(
                    last_progress + self.body_idle_timeout - time.monotonic(),
                    started + self.body_total_timeout - time.monotonic(),
                )
                message: Optional[_Message] = None
                if delay > 0:
                    with anyio.move_on_after(delay) as deadline:
                        message = await receive()
                    if deadline.cancelled_caught:
                        message = None
                if message is None:
                    rejected = True
                    if not response_started:
                        await self._send_request_timeout(scope, receive, send)
                    return {"type": "http.disconnect"}

                if message["type"] == "http.request":
                    chunk_size = len(message.get("body", b""))
                    seen += chunk_size
                    if seen > self.max_body_bytes:
                        rejected = True
                        if not response_started:
                            await self._send_too_large(scope, receive, send)
                        # Stop the parser: from here on the application sees a disconnected client, and anything it tries
                        # to send is swallowed (the 413 above is the response).
                        return {"type": "http.disconnect"}
                    if chunk_size:
                        last_progress = time.monotonic()           # only real body progress resets the idle timer
                    if not message.get("more_body", False):
                        body_complete = True
                return message

            async def guarded_send(message: _Message) -> None:
                nonlocal response_started
                if rejected:
                    return
                if message["type"] == "http.response.start":
                    response_started = True
                await send(message)

            await self.app(scope, limited_receive, guarded_send)
        finally:
            self._release()
