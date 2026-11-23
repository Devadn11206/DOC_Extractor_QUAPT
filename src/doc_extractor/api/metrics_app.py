"""
Internal metrics endpoint (PRD NFR-010).

Serve it on an internal port, separate from the public API, for example:

    uvicorn doc_extractor.api.metrics_app:app --host 0.0.0.0 --port 9100

Only ``GET /metrics`` answers; everything else is 404. The network policy should admit this port from the monitoring
namespace only.
"""

from __future__ import annotations

from doc_extractor.metrics import render_metrics

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


async def app(scope, receive, send):
    if scope.get("type") != "http":
        return
    method = scope.get("method", "GET")
    path = scope.get("path", "")
    if method == "GET" and path == "/metrics":
        body = render_metrics().encode("utf-8")
        status, content_type = 200, CONTENT_TYPE
    else:
        body, status, content_type = b"not found", 404, "text/plain; charset=utf-8"
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", content_type.encode("ascii")), (b"content-length", str(len(body)).encode("ascii"))],
    })
    await send({"type": "http.response.body", "body": body})
