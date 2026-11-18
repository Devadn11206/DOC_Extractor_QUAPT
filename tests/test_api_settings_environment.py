"""
The API's deployment posture is read from the environment (``DOC_API_*``) and development-only documentation is off in
production. Failing closed is the point: an unknown ``DOC_API_`` name, an empty value, an invalid value or a forbidden
combination is a ConfigurationError, never a silent fall-back to development.
"""

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[dev,api]')")

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


def test_nothing_set_keeps_the_development_defaults():
    settings = load_api_settings()
    assert settings.app_env == "development" and settings.docs_url == "/docs" and settings.cors_origins == []


def test_explicit_values_passed_in_code_win_over_the_environment(monkeypatch):
    monkeypatch.setenv("DOC_API_ENV", "staging")
    assert load_api_settings({"app_env": "development"}).app_env == "development"


def test_unrelated_doc_variables_are_ignored(monkeypatch):
    for name in ("DOC_MAX_PAGES", "DOC_DEFAULT_MODEL", "DOC_APP_ENV", "DOC_DEBUG"):
        monkeypatch.setenv(name, "production" if "ENV" in name else "1")
    assert load_api_settings().app_env == "development"        # only the DOC_API_ allow-list is read


# -- failing closed ---------------------------------------------------------------------------------

def test_an_unknown_doc_api_variable_is_refused_so_a_typo_cannot_leave_the_api_in_development(monkeypatch):
    monkeypatch.setenv("DOC_API_EVN", "production")              # a typo of DOC_API_ENV
    with pytest.raises(ConfigurationError) as exc:
        load_api_settings()
    assert "DOC_API_EVN" in exc.value.message and exc.value.details == {"unknown_variables": ["DOC_API_EVN"]}


def test_create_app_refuses_to_start_on_a_typo_too(monkeypatch):
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
