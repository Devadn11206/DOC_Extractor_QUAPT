"""
The API's deployment posture is read from the environment (``DOC_API_*``) and development-only documentation is off in
production. Failing closed is the point: an unknown ``DOC_API_`` name, an empty value, an invalid value or a forbidden
combination is a ConfigurationError, never a silent fall-back to development.
"""

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[dev,api]')")
pytest.importorskip("multipart", reason="optional API extra not installed (pip install -e '.[dev,api]')")

from fastapi.testclient import TestClient

from doc_extractor.api import config as api_config
from doc_extractor.api.app import create_app
from doc_extractor.api.config import ENV_VARIABLES, ApiSettings, load_api_settings, settings_from_environment
from doc_extractor.exceptions import ConfigurationError


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    import os
    for name in [n for n in os.environ if n.startswith("DOC_API_")]:
        monkeypatch.delenv(name, raising=False)
    api_config.get_api_settings.cache_clear()
    yield
    api_config.get_api_settings.cache_clear()


def doc_urls(client: TestClient):
    return tuple(client.get(path).status_code for path in ("/docs", "/redoc", "/openapi.json"))


# -- reading the environment ------------------------------------------------------------------------

def test_every_documented_variable_maps_to_an_api_settings_field():
    assert set(ENV_VARIABLES.values()) <= set(ApiSettings.model_fields)
    assert all(name.startswith("DOC_API_") for name in ENV_VARIABLES)


def test_values_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "staging")
    monkeypatch.setenv("DOC_API_DEBUG", "false")
    monkeypatch.setenv("DOC_API_CORS_ORIGINS", "https://a.example, https://b.example ,")
    monkeypatch.setenv("DOC_API_CORS_ALLOW_CREDENTIALS", "no")
    monkeypatch.setenv("DOC_API_PREFIX", "/api/v2")
    monkeypatch.setenv("DOC_API_LOG_LEVEL", "WARNING")
    settings = load_api_settings()
    assert settings.app_env == "staging" and settings.debug is False
    assert settings.cors_origins == ["https://a.example", "https://b.example"] and settings.cors_allow_credentials is False
    assert settings.api_prefix == "/api/v2" and settings.log_level == "WARNING"


def test_an_explicit_development_environment_keeps_the_development_defaults(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "development")
    settings = load_api_settings()
    assert settings.app_env == "development" and settings.docs_url == "/docs" and settings.cors_origins == []


def test_explicit_values_passed_in_code_win_over_the_environment(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "staging")
    assert load_api_settings({"app_env": "development"}).app_env == "development"


def test_unrelated_doc_variables_are_ignored(monkeypatch):
    for name in ("DOC_MAX_PAGES", "DOC_DEFAULT_MODEL", "DOC_APP_ENV", "DOC_DEBUG"):
        monkeypatch.setenv(name, "production" if "ENV" in name else "1")
    monkeypatch.setenv("DOC_API_ENV", "staging")
    assert load_api_settings().app_env == "staging"            # only the DOC_API_ allow-list is read: DOC_APP_ENV=production is ignored


# -- failing closed ---------------------------------------------------------------------------------

def test_an_unknown_doc_api_variable_is_refused_so_a_typo_cannot_leave_the_api_in_development(monkeypatch):
    monkeypatch.setenv("DOC_API_EVN", "production")              # a typo of DOC_API_ENV
    with pytest.raises(ConfigurationError) as exc:
        load_api_settings()
    assert "DOC_API_EVN" in exc.value.message and exc.value.details == {"unknown_variables": ["DOC_API_EVN"]}


def test_create_app_refuses_to_start_on_a_typo_too(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "development")
    monkeypatch.setenv("DOC_API_DOCS_ENABELD", "false")
    with pytest.raises(ConfigurationError):
        create_app()


@pytest.mark.parametrize("variable", sorted(ENV_VARIABLES))
def test_a_variable_that_is_set_but_empty_is_refused(monkeypatch, variable):
    monkeypatch.setenv(variable, "   ")
    with pytest.raises(ConfigurationError) as exc:
        load_api_settings()
    assert exc.value.details == {"variable": variable}


@pytest.mark.parametrize("variable,value", [
    ("DOC_API_ENV", "prod"), ("DOC_API_ENV", "Production"), ("DOC_API_DEBUG", "maybe"), ("DOC_API_DOCS_ENABLED", "perhaps"),
    ("DOC_API_LOG_LEVEL", "LOUD"), ("DOC_API_CORS_ALLOW_CREDENTIALS", "2"),
])
def test_an_invalid_value_is_refused(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ConfigurationError):
        load_api_settings()


def test_production_with_debug_is_refused_even_from_the_environment(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "production")
    monkeypatch.setenv("DOC_API_DEBUG", "true")
    with pytest.raises(ConfigurationError):
        load_api_settings()


def test_production_with_a_wildcard_cors_origin_is_refused_from_the_environment(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "production")
    monkeypatch.setenv("DOC_API_CORS_ORIGINS", "*")
    monkeypatch.setenv("DOC_API_CORS_ALLOW_CREDENTIALS", "false")
    with pytest.raises(ConfigurationError):
        load_api_settings()


# -- development-only documentation -----------------------------------------------------------------

def test_documentation_is_served_in_development():
    assert doc_urls(TestClient(create_app(ApiSettings(app_env="development")))) == (200, 200, 200)


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_documentation_is_off_by_default_outside_development(environment):
    settings = ApiSettings(app_env=environment)
    assert settings.docs_url is None and settings.redoc_url is None and settings.openapi_url is None
    assert doc_urls(TestClient(create_app(settings))) == (404, 404, 404)


def test_a_production_app_built_from_the_environment_alone_serves_no_documentation(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "production")
    app = create_app()                                             # no settings passed: the environment decides
    client = TestClient(app)
    assert app.state.settings.app_env == "production"
    assert doc_urls(client) == (404, 404, 404)
    assert client.get("/api/v1/health").json()["environment"] == "production"      # the API itself still works


def test_documentation_cannot_be_enabled_in_production(monkeypatch):
    with pytest.raises(ValueError):
        ApiSettings(app_env="production", docs_enabled=True)
    monkeypatch.setenv("DOC_API_ENV", "production")
    monkeypatch.setenv("DOC_API_DOCS_ENABLED", "true")
    with pytest.raises(ConfigurationError):
        create_app()


def test_staging_can_opt_in_explicitly(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "staging")
    monkeypatch.setenv("DOC_API_DOCS_ENABLED", "true")
    assert doc_urls(TestClient(create_app())) == (200, 200, 200)


def test_development_can_switch_documentation_off(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "development")
    monkeypatch.setenv("DOC_API_DOCS_ENABLED", "false")
    assert doc_urls(TestClient(create_app())) == (404, 404, 404)


def test_the_openapi_document_is_still_available_in_process_for_contract_tests():
    app = create_app(ApiSettings(app_env="production"))
    assert "/api/v1/health" in app.openapi()["paths"]              # API-014 contract tests do not need the HTTP route


def test_the_cached_settings_provider_reads_the_environment_once(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "staging")
    assert api_config.get_api_settings().app_env == "staging"
    monkeypatch.setenv("DOC_API_ENV", "production")
    assert api_config.get_api_settings().app_env == "staging"      # cached for the process, as designed


def test_settings_from_environment_accepts_an_explicit_mapping():
    assert settings_from_environment({"DOC_API_ENV": "production", "PATH": "/bin"}) == {"app_env": "production"}


def test_env_example_documents_every_variable_the_api_reads_and_no_others():
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#? ?(DOC_API_[A-Z_]+)=", text, flags=re.MULTILINE))
    assert documented == set(ENV_VARIABLES)


def test_readme_names_the_production_switch_and_the_default_posture():
    from pathlib import Path
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "DOC_API_ENV=production uvicorn --factory" in readme and "development" in readme


# -- the environment is never assumed (review finding R8) -----------------------------------------------------------------

def test_an_unset_environment_is_refused_instead_of_defaulting_to_development():
    with pytest.raises(ConfigurationError) as exc:
        load_api_settings()
    assert "DOC_API_ENV" in exc.value.message and "never assumes" in exc.value.message
    assert exc.value.details == {"variable": "DOC_API_ENV", "allowed": ["development", "staging", "production"]}


def test_create_app_without_settings_and_without_an_environment_refuses_to_start():
    with pytest.raises(ConfigurationError):
        create_app()


def test_other_api_variables_do_not_substitute_for_the_environment(monkeypatch):
    monkeypatch.setenv("DOC_API_DEBUG", "false")
    monkeypatch.setenv("DOC_API_DOCS_ENABLED", "true")             # would be the dangerous combination if development were assumed
    with pytest.raises(ConfigurationError):
        load_api_settings()


def test_the_settings_dependency_fails_closed_too_when_no_environment_is_set():
    with pytest.raises(ConfigurationError):
        api_config.get_api_settings()


def test_an_explicit_app_env_passed_in_code_is_a_sufficient_declaration():
    assert load_api_settings({"app_env": "production"}).app_env == "production"
    assert ApiSettings(app_env="development").docs_url == "/docs"   # direct construction (tests) stays explicit and unaffected


def test_a_failed_environment_load_is_not_cached(monkeypatch):
    with pytest.raises(ConfigurationError):
        api_config.get_api_settings()
    monkeypatch.setenv("DOC_API_ENV", "production")
    assert api_config.get_api_settings().app_env == "production"


# -- development documentation is off in production, however it is asked for ---------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {}, {"docs_url": "/docs"}, {"redoc_url": "/redoc"}, {"openapi_url": "/openapi.json"},
    {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}, {"docs_enabled": None, "docs_url": "/api-docs"},
    {"docs_enabled": False, "docs_url": "/docs"},
])
def test_production_serves_no_documentation_whatever_the_url_settings(kwargs):
    settings = ApiSettings(app_env="production", **kwargs)
    assert (settings.docs_url, settings.redoc_url, settings.openapi_url) == (None, None, None)
    client = TestClient(create_app(settings))
    assert doc_urls(client) == (404, 404, 404) and client.get("/api-docs").status_code == 404


def test_production_from_the_environment_serves_no_documentation_even_with_every_other_variable_set(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "production")
    monkeypatch.setenv("DOC_API_DEBUG", "false")
    monkeypatch.setenv("DOC_API_PREFIX", "/api/v1")
    monkeypatch.setenv("DOC_API_CORS_ORIGINS", "https://app.example.com")
    assert doc_urls(TestClient(create_app())) == (404, 404, 404)


# -- upload body timeouts (N3, R4): finite, positive, bounded ------------------------------------------

IDLE = "upload_body_idle_timeout_seconds"
TOTAL = "upload_body_total_timeout_seconds"
IDLE_ENV = "DOC_API_UPLOAD_BODY_IDLE_TIMEOUT_SECONDS"
TOTAL_ENV = "DOC_API_UPLOAD_BODY_TOTAL_TIMEOUT_SECONDS"


def test_the_upload_timeout_defaults_are_unchanged():
    settings = ApiSettings(app_env="development")
    assert (getattr(settings, IDLE), getattr(settings, TOTAL)) == (30.0, 300.0)


@pytest.mark.parametrize("overrides", [
    {IDLE: float("inf")}, {IDLE: float("-inf")}, {IDLE: float("nan")}, {IDLE: 1e308}, {IDLE: 0}, {IDLE: -1}, {IDLE: -0.001},
    {IDLE: 300.001}, {IDLE: 301},
    {IDLE: 301, TOTAL: 3600}, {IDLE: 1000, TOTAL: 3600}, {IDLE: 3600, TOTAL: 3600},       # idle above its own maximum, total fine
    {IDLE: float("inf"), TOTAL: float("inf")},
    {TOTAL: float("inf")}, {TOTAL: float("-inf")}, {TOTAL: float("nan")}, {TOTAL: 1e308}, {TOTAL: 0}, {TOTAL: -1},
    {TOTAL: 3600.001}, {TOTAL: 3601},
    {IDLE: 60, TOTAL: 59.999}, {IDLE: 300, TOTAL: 299}, {TOTAL: 29.9},            # total below idle (the default idle is 30)
])
def test_invalid_upload_timeouts_are_refused_when_constructing_the_settings(overrides):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ApiSettings(app_env="development", **overrides)


@pytest.mark.parametrize("overrides", [
    {IDLE: 0.001, TOTAL: 0.001},            # smallest sensible: positive, total == idle
    {IDLE: 1, TOTAL: 1}, {IDLE: 30, TOTAL: 30}, {IDLE: 30, TOTAL: 300}, {IDLE: 300, TOTAL: 300},
    {IDLE: 300, TOTAL: 3600},               # both maxima
    {IDLE: 299.999, TOTAL: 3599.999}, {TOTAL: 3600}, {IDLE: 5},
])
def test_valid_upload_timeout_boundaries_are_accepted(overrides):
    settings = ApiSettings(app_env="development", **overrides)
    assert getattr(settings, IDLE) == overrides.get(IDLE, 30.0) and getattr(settings, TOTAL) == overrides.get(TOTAL, 300.0)


@pytest.mark.parametrize("variable, value", [
    (IDLE_ENV, "inf"), (IDLE_ENV, "-inf"), (IDLE_ENV, "Infinity"), (IDLE_ENV, "-Infinity"), (IDLE_ENV, "INF"), (IDLE_ENV, "nan"),
    (IDLE_ENV, "NaN"), (IDLE_ENV, "1e308"), (IDLE_ENV, "0"), (IDLE_ENV, "0.0"), (IDLE_ENV, "-5"), (IDLE_ENV, "301"),
    (IDLE_ENV, "300.5"), (IDLE_ENV, "abc"),
    (TOTAL_ENV, "inf"), (TOTAL_ENV, "-inf"), (TOTAL_ENV, "Infinity"), (TOTAL_ENV, "nan"), (TOTAL_ENV, "1e308"), (TOTAL_ENV, "0"),
    (TOTAL_ENV, "-1"), (TOTAL_ENV, "3601"), (TOTAL_ENV, "1e309"), (TOTAL_ENV, "abc"),
    (TOTAL_ENV, "29"),                      # below the default idle timeout (30)
])
def test_invalid_upload_timeouts_from_the_environment_are_refused(monkeypatch, variable, value):
    monkeypatch.setenv("DOC_API_ENV", "development")
    monkeypatch.setenv(variable, value)
    with pytest.raises(ConfigurationError):
        load_api_settings()


@pytest.mark.parametrize("idle", ["301", "1000", "3600"])
def test_an_idle_timeout_above_its_maximum_is_refused_from_the_environment_even_with_a_large_total(monkeypatch, idle):
    monkeypatch.setenv("DOC_API_ENV", "development")
    monkeypatch.setenv(IDLE_ENV, idle)
    monkeypatch.setenv(TOTAL_ENV, "3600")
    with pytest.raises(ConfigurationError):
        load_api_settings()


def test_a_total_timeout_below_the_idle_timeout_is_refused_from_the_environment(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "development")
    monkeypatch.setenv(IDLE_ENV, "120")
    monkeypatch.setenv(TOTAL_ENV, "60")
    with pytest.raises(ConfigurationError):
        load_api_settings()


@pytest.mark.parametrize("idle, total", [("0.5", "0.5"), ("1", "10"), ("30", "300"), ("300", "300"), ("300", "3600"), ("299.5", "3599.5")])
def test_valid_upload_timeout_boundaries_are_accepted_from_the_environment(monkeypatch, idle, total):
    monkeypatch.setenv("DOC_API_ENV", "development")
    monkeypatch.setenv(IDLE_ENV, idle)
    monkeypatch.setenv(TOTAL_ENV, total)
    settings = load_api_settings()
    assert (getattr(settings, IDLE), getattr(settings, TOTAL)) == (float(idle), float(total))


def test_the_bounds_are_the_documented_constants():
    assert api_config.MAX_UPLOAD_BODY_IDLE_TIMEOUT_SECONDS == 300.0
    assert api_config.MAX_UPLOAD_BODY_TOTAL_TIMEOUT_SECONDS == 3600.0
