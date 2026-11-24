"""
Request metrics (PRD NFR-010). A pure ASGI middleware, so it times the whole request, including errors.

The route label is the route template from ``scope["route"]`` (set by the router once the request has matched), so a
path such as ``/api/v1/documents/doc_123`` is recorded as ``/api/v1/documents/{document_id}``. A request that matched no
route is recorded as ``unmatched``. Raw paths, identifiers and bodies are never recorded.
"""

from __future__ import annotations

import time

from doc_extractor.metrics import HTTP_DURATION, HTTP_REQUESTS, route_label


class RequestMetricsMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = {"code": 500}

        async def record_status(message):
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, record_status)
        finally:
            label = route_label(_template(scope))
            HTTP_REQUESTS.inc(label, str(status["code"]))
            HTTP_DURATION.observe(time.perf_counter() - started, label)


def _template(scope) -> "str | None":
    """The full path with each path-parameter value replaced by its name. The prefix is kept; no identifier is kept."""
    if scope.get("route") is None:
        return None
    names_by_value = {str(value): name for name, value in (scope.get("path_params") or {}).items()}
    segments = str(scope.get("path", "")).split("/")
    return "/".join("{" + names_by_value[s] + "}" if s in names_by_value else s for s in segments)
