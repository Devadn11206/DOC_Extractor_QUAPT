"""
HTTP protocol headers on error responses.

- ``Retry-After`` (API-012) is preserved on every error path and VALIDATED: a negative value is clamped to 0 and anything
  that is not a number of seconds or an HTTP-date is dropped (never sent malformed).
- ``WWW-Authenticate`` (401) and ``Allow`` (405) set by Starlette or a route are preserved; a 401 without one gets
  ``Bearer`` (RFC 9110 requires it); nothing else is invented and other exception headers never leak.
- Checked through TestClient AND over a real socket against a running uvicorn server.
"""

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[dev,api]')")

import threading
import time

import httpx
import uvicorn
from fastapi import HTTPException, Query
from fastapi.testclient import TestClient

from doc_extractor.api.app import create_app
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.exceptions import (
    AuthenticationRequiredError,
    RateLimitExceededError,
    preserved_headers,
    sanitize_retry_after,
)

HTTP_DATE = "Wed, 21 Oct 2026 07:28:00 GMT"


def build_app():
    app = create_app(ApiSettings(app_env="development"))

    @app.get("/p/retry")
    def retry(v: str = Query("")):
        raise HTTPException(429, "slow down", headers={"Retry-After": v})

    @app.get("/p/domain-retry")
    def domain_retry(v: float = Query(0)):
        raise RateLimitExceededError(message="x", retry_after_seconds=v)

    @app.get("/p/http401")
    def http401():
        raise HTTPException(401, "no", headers={"WWW-Authenticate": 'Bearer realm="docs", error="invalid_token"'})

    @app.get("/p/http401-plain")
    def http401_plain():
        raise HTTPException(401, "no")

    @app.get("/p/domain401")
    def domain401():
        raise AuthenticationRequiredError(message="Authorization header is required.")

    @app.get("/p/http403")
    def http403():
        raise HTTPException(403, "no")

    @app.post("/p/post-only")
    def post_only():
        return {}

    @app.get("/p/leaky")
    def leaky():
        raise HTTPException(400, "bad", headers={"X-Internal-Debug": "secret", "Set-Cookie": "a=b", "WWW-Authenticate": "x\r\ninjected: 1"})

    return app


@pytest.fixture(scope="module")
def client():
    return TestClient(build_app(), raise_server_exceptions=False)


# -- Retry-After validation -----------------------------------------------------------------------

@pytest.mark.parametrize("sent,expected", [
    ("7", "7"), ("0", "0"), ("-3", "0"), (" 12 ", "12"), ("999", "999"), (HTTP_DATE, HTTP_DATE),
    ("1.5", None), ("abc", None), ("", None), ("   ", None), ("7 seconds", None), ("7\r\nX-Evil: 1", None),
])
def test_retry_after_from_an_http_exception_is_validated(client, sent, expected):
    response = client.get("/p/retry", params={"v": sent})
    assert response.status_code == 429 and response.json()["error"]["code"] == "RATE_LIMITED"
    assert response.headers.get("Retry-After") == expected


@pytest.mark.parametrize("sent,expected", [(5, "5"), (5.9, "5"), (-5, "0"), (0, "0")])
def test_the_domain_rate_limit_path_applies_the_same_validation(client, sent, expected):
    response = client.get("/p/domain-retry", params={"v": sent})
    assert response.status_code == 429 and response.headers["Retry-After"] == expected


@pytest.mark.parametrize("value,expected", [
    (7, "7"), (-1, "0"), (0, "0"), (True, None), (False, None), (None, None), (float("nan"), None), (float("inf"), None),
    (3.9, "3"), (-0.5, "0"), ("12", "12"), ("-12", "0"), ("x", None), (HTTP_DATE, HTTP_DATE), ("a\nb", None), ("1" * 600, None), ("1" * 500, "86400"),     # over the 512-character cap: dropped (int() never sees it); under it: clamped to the maximum
])
def test_sanitize_retry_after_unit(value, expected):
    assert sanitize_retry_after(value) == expected


# -- WWW-Authenticate and Allow --------------------------------------------------------------------

def test_a_route_supplied_www_authenticate_is_preserved_verbatim(client):
    response = client.get("/p/http401")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer realm="docs", error="invalid_token"'


def test_a_401_without_the_header_gets_bearer_on_both_error_paths(client):
    for path in ("/p/http401-plain", "/p/domain401"):
        response = client.get(path)
        assert response.status_code == 401 and response.headers["WWW-Authenticate"] == "Bearer", path


def test_no_www_authenticate_is_invented_for_other_statuses(client):
    for path, status in (("/p/http403", 403), ("/p/retry", 429)):
        response = client.get(path)
        assert response.status_code == status and "WWW-Authenticate" not in response.headers


def test_allow_is_preserved_on_405_from_the_router(client):
    response = client.get("/p/post-only")
    assert response.status_code == 405 and response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert "POST" in response.headers["Allow"]


def test_no_allow_is_invented_on_a_404(client):
    response = client.get("/p/does-not-exist")
    assert response.status_code == 404 and "Allow" not in response.headers


def test_other_exception_headers_never_leak_and_a_malformed_value_is_dropped(client):
    response = client.get("/p/leaky")
    assert response.status_code == 400
    assert "X-Internal-Debug" not in response.headers and "Set-Cookie" not in response.headers
    assert "injected" not in str(dict(response.headers)).lower()


def test_error_envelope_and_request_id_are_unchanged_by_header_handling(client):
    response = client.get("/p/http401")
    error = response.json()["error"]
    assert set(error) == {"code", "message", "request_id", "details"} and response.headers["X-Request-ID"] == error["request_id"]


def test_preserved_headers_unit():
    assert preserved_headers({"retry-after": "4", "ALLOW": "GET, POST", "X-Other": "1", "WWW-Authenticate": "Basic"}) == {
        "Retry-After": "4", "Allow": "GET, POST", "WWW-Authenticate": "Basic"}
    assert preserved_headers(None) == {} and preserved_headers({}) == {}
    assert preserved_headers({"Allow": ""}) == {} and preserved_headers({"Allow": "x" * 600}) == {}


# -- over a real socket ---------------------------------------------------------------------------

class RunningServer:
    """A real uvicorn server on an ephemeral localhost port, for HTTP-level assertions."""

    def __init__(self, app):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline or not self.thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=15)


def test_headers_survive_over_a_real_http_socket():
    with RunningServer(build_app()) as running:
        with httpx.Client(base_url=running.base, timeout=10) as http:
            rate = http.get("/p/retry", params={"v": "7"})
            negative = http.get("/p/retry", params={"v": "-3"})
            junk = http.get("/p/retry", params={"v": "soon"})
            unauthorised = http.get("/p/http401-plain")
            wrong_method = http.get("/p/post-only")
    assert rate.status_code == 429 and rate.headers["retry-after"] == "7"
    assert negative.headers["retry-after"] == "0" and "retry-after" not in junk.headers
    assert unauthorised.status_code == 401 and unauthorised.headers["www-authenticate"] == "Bearer"
    assert wrong_method.status_code == 405 and "POST" in wrong_method.headers["allow"]
    assert all(r.headers["x-request-id"] for r in (rate, negative, junk, unauthorised, wrong_method))


# -- the documented maximum: 86,400 seconds (24 hours) -------------------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402
from email.utils import format_datetime  # noqa: E402

from doc_extractor.api.exceptions import MAX_RETRY_AFTER_SECONDS  # noqa: E402

NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)


def test_the_documented_maximum_is_one_day():
    assert MAX_RETRY_AFTER_SECONDS == 86_400


@pytest.mark.parametrize("value,expected", [
    (86_399, "86399"), (86_400, "86400"), (86_401, "86400"), (10**30, "86400"), (float(10**30), "86400"), (1e300, "86400"),
    ("86400", "86400"), ("86401", "86400"), ("9" * 40, "86400"), ("-86401", "0"), (-(10**30), "0"),
])
def test_retry_after_seconds_are_bounded(value, expected):
    assert sanitize_retry_after(value) == expected


def test_an_http_date_is_bounded_too():
    within = format_datetime(NOW + timedelta(hours=23), usegmt=True)
    exactly = format_datetime(NOW + timedelta(seconds=MAX_RETRY_AFTER_SECONDS), usegmt=True)
    beyond = format_datetime(NOW + timedelta(seconds=MAX_RETRY_AFTER_SECONDS + 1), usegmt=True)
    far = format_datetime(NOW + timedelta(days=3650), usegmt=True)
    past = format_datetime(NOW - timedelta(days=1), usegmt=True)
    assert sanitize_retry_after(within, now=NOW) == within            # a valid date inside the window is passed through
    assert sanitize_retry_after(exactly, now=NOW) == exactly
    assert sanitize_retry_after(beyond, now=NOW) == "86400"           # sent as seconds, clamped
    assert sanitize_retry_after(far, now=NOW) == "86400"
    assert sanitize_retry_after(past, now=NOW) == past                # "retry now": still a valid date


def test_an_oversized_value_is_clamped_on_every_response_path(client):
    assert client.get("/p/retry", params={"v": "999999999"}).headers["Retry-After"] == "86400"
    assert client.get("/p/domain-retry", params={"v": 10**12}).headers["Retry-After"] == "86400"
