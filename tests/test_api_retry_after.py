"""
API-012: ``429`` responses carry ``Retry-After``. The header must survive on every error path, including
a plain ``HTTPException`` raised by a route or a dependency (it used to be computed and then dropped).
"""

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from doc_extractor.api.app import create_app
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.exceptions import RateLimitExceededError


@pytest.fixture
def client() -> TestClient:
    app = create_app(ApiSettings(app_env="development"))

    @app.get("/probe/http-429")
    def http_429():
        raise HTTPException(status_code=429, detail="slow down", headers={"Retry-After": "7"})

    @app.get("/probe/http-429-lowercase")
    def http_429_lower():
        raise HTTPException(status_code=429, detail="slow down", headers={"retry-after": "11"})

    @app.get("/probe/http-503")
    def http_503():
        raise HTTPException(status_code=503, detail="warming up", headers={"Retry-After": "30"})

    @app.get("/probe/http-404-no-header")
    def http_404():
        raise HTTPException(status_code=404, detail="gone")

    @app.get("/probe/http-400-other-header")
    def http_400():
        raise HTTPException(status_code=400, detail="bad", headers={"X-Internal": "secret"})

    @app.get("/probe/domain-429")
    def domain_429():
        raise RateLimitExceededError(message="Too many requests.", retry_after_seconds=5)

    return TestClient(app, raise_server_exceptions=False)


def test_retry_after_is_preserved_on_an_http_exception_429(client):
    response = client.get("/probe/http-429")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7"
    assert response.json()["error"]["code"] == "RATE_LIMITED"       # the existing public code is unchanged


def test_retry_after_name_matching_is_case_insensitive(client):
    assert client.get("/probe/http-429-lowercase").headers["Retry-After"] == "11"


def test_retry_after_is_preserved_on_other_statuses_too(client):
    response = client.get("/probe/http-503")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"


def test_no_retry_after_is_invented_when_the_exception_has_none(client):
    assert "Retry-After" not in client.get("/probe/http-404-no-header").headers


def test_only_retry_after_is_forwarded_not_arbitrary_exception_headers(client):
    response = client.get("/probe/http-400-other-header")
    assert response.status_code == 400
    assert "X-Internal" not in response.headers


def test_the_domain_rate_limit_path_still_sends_retry_after(client):
    response = client.get("/probe/domain-429")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"


def test_the_error_envelope_and_request_id_header_are_still_present(client):
    response = client.get("/probe/http-429")
    error = response.json()["error"]
    assert set(error) == {"code", "message", "request_id", "details"}
    assert response.headers["X-Request-ID"] == error["request_id"]
