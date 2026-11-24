"""
Broker settings and URL-policy tests (Team Member 3, Phase 3). Pure: no broker, no network, no client library.
"""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from doc_extractor.infrastructure.queue.errors import BrokerConfigurationError
from doc_extractor.infrastructure.queue.settings import (
    BrokerSettings,
    check_url_policy,
    parse_url_shape,
)

SECRET_URL = "amqps://svc:Sup3rS3cretPW@broker.internal:5671/vh"


def api(**values):
    base = {"broker_url": None, "broker_url_file": None, "app_env": "development"}
    base.update(values)
    return SimpleNamespace(**base)


# ---- the URL policy -------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("env", ["staging", "production"])
@pytest.mark.parametrize(
    "url, reason",
    [
        ("amqp://svc:pw@host/vh", "tls_is_required_outside_development"),
        ("amqps://host/vh", "authentication_is_required_outside_development"),
        ("amqps://svc:pw@host/vh?no_verify_ssl=1", "insecure_tls_options_are_refused_outside_development"),
        ("amqps://svc:pw@host/vh?verify=verify_none", "insecure_tls_options_are_refused_outside_development"),
        ("amqps://svc:pw@host/vh?tlsAllowInvalidCertificates=true", "insecure_tls_options_are_refused_outside_development"),
    ],
)
def test_hardened_environments_require_tls_credentials_and_verification(env, url, reason):
    with pytest.raises(BrokerConfigurationError) as raised:
        check_url_policy(url, env)
    assert raised.value.details["reason"] == reason and "pw" not in repr(raised.value)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_a_proper_tls_url_with_credentials_is_accepted_in_hardened_environments(env):
    assert check_url_policy(SECRET_URL, env).scheme == "amqps"


def test_development_accepts_a_plain_url_without_credentials():
    assert check_url_policy("amqp://localhost:5672/", "development").scheme == "amqp"


@pytest.mark.parametrize(
    "url, reason",
    [
        ("", "url_empty"),
        ("amqp://host\n/vh", "url_contains_whitespace_or_control_characters"),
        ("http://host/", "url_scheme_must_be_amqp_or_amqps"),
        ("amqp:///vh", "url_has_no_host"),
        ("amqp://host:notaport/", "url_malformed"),
    ],
)
def test_malformed_urls_are_refused_with_a_fixed_reason(url, reason):
    with pytest.raises(BrokerConfigurationError) as raised:
        parse_url_shape(url)
    assert raised.value.details["reason"] == reason


def test_an_unknown_environment_is_refused():
    with pytest.raises(BrokerConfigurationError):
        check_url_policy("amqp://h/", "qa")


# ---- the settings -----------------------------------------------------------------------------------------------------------------

def test_no_url_means_no_broker():
    assert BrokerSettings.from_api_settings(api()) is None


def test_settings_carry_the_api_values_and_defaults():
    settings = BrokerSettings.from_api_settings(
        api(broker_url=SecretStr(SECRET_URL), app_env="production", broker_queue_prefix="docs", broker_max_in_flight=7,
            broker_connect_timeout_seconds=3.0, broker_publish_timeout_seconds=4.0)
    )
    assert settings.queue_prefix == "docs" and settings.max_in_flight == 7 and settings.connect_timeout_seconds == 3.0
    assert settings.publish_timeout_seconds == 4.0 and settings.delay_tiers_seconds == (1, 5, 15, 60, 300, 900, 3600)
    assert settings.url.get_secret_value() == SECRET_URL


def test_the_url_is_a_secret_in_every_representation_and_every_error():
    settings = BrokerSettings.from_api_settings(api(broker_url=SecretStr(SECRET_URL), app_env="production"))
    for text in (repr(settings), str(settings), str(settings.summary()), settings.model_dump_json()):
        assert "Sup3rS3cretPW" not in text and "broker.internal" not in text
    with pytest.raises(ValueError) as raised:
        BrokerSettings(url=SecretStr("amqp://svc:Sup3rS3cretPW@broker.internal/"), app_env="production")
    assert "Sup3rS3cretPW" not in str(raised.value) and "broker.internal" not in str(raised.value)


def test_a_policy_violation_in_the_settings_is_a_value_error_naming_only_the_reason():
    with pytest.raises(ValueError) as raised:
        BrokerSettings.from_api_settings(api(broker_url=SecretStr("amqp://svc:pw@host/"), app_env="production"))
    assert "tls_is_required_outside_development" in str(raised.value) and "host" not in str(raised.value).replace("hosts", "")


def test_a_url_and_a_url_file_together_are_an_error(tmp_path):
    path = tmp_path / "url"
    path.write_text("amqp://h/")
    with pytest.raises(BrokerConfigurationError) as raised:
        BrokerSettings.from_api_settings(api(broker_url=SecretStr("amqp://h/"), broker_url_file=str(path)))
    assert raised.value.details["reason"] == "url_and_url_file_are_mutually_exclusive"


def test_the_url_can_come_from_a_mounted_file_and_is_read_only_when_asked(tmp_path):
    path = tmp_path / "url"
    path.write_text("  amqp://localhost/  \n")
    settings = BrokerSettings.from_api_settings(api(broker_url_file=str(path)))
    assert settings.url.get_secret_value() == "amqp://localhost/"
    assert BrokerSettings.from_api_settings(api(broker_url_file=str(tmp_path / "missing")), read_url_file=False) is None


@pytest.mark.parametrize("make, reason", [
    (lambda tmp: str(tmp), "url_file_is_not_a_regular_file"),
    (lambda tmp: _file(tmp, "x" * 9000), "url_file_is_too_large"),
    (lambda tmp: _file(tmp, "   \n"), "url_file_is_empty"),
    (lambda tmp: str(tmp / "missing"), "url_file_is_unreadable"),
    (lambda tmp: _file(tmp, b"\xff\xfe"), "url_file_is_unreadable"),
])
def test_a_bad_url_file_is_refused_without_naming_its_path_or_content(tmp_path, make, reason):
    path = make(tmp_path)
    with pytest.raises(BrokerConfigurationError) as raised:
        BrokerSettings.from_api_settings(api(broker_url_file=path))
    assert raised.value.details["reason"] == reason and str(tmp_path) not in repr(raised.value)


def _file(tmp, content):
    path = tmp / "f"
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    return str(path)


@pytest.mark.parametrize("values", [
    {"queue_prefix": "Bad Prefix"}, {"queue_prefix": ""}, {"queue_prefix": "1abc"}, {"queue_prefix": "a" * 49},
    {"max_in_flight": 0}, {"max_in_flight": 101}, {"connect_timeout_seconds": 0.5}, {"connect_timeout_seconds": float("inf")},
    {"publish_timeout_seconds": 61}, {"delivery_limit": 0},
    {"delay_tiers_seconds": ()}, {"delay_tiers_seconds": (5, 1)}, {"delay_tiers_seconds": (1, 1)}, {"delay_tiers_seconds": (0, 5)},
    {"delay_tiers_seconds": tuple(range(1, 14))}, {"delay_tiers_seconds": (1, 7 * 24 * 3600 + 1)},
])
def test_every_setting_is_bounded(values):
    with pytest.raises(ValueError):
        BrokerSettings(url=SecretStr("amqp://h/"), **values)


def test_extra_fields_are_refused_and_settings_are_immutable():
    with pytest.raises(ValueError):
        BrokerSettings(url=SecretStr("amqp://h/"), surprise=1)
    settings = BrokerSettings(url=SecretStr("amqp://h/"))
    with pytest.raises(ValueError):
        settings.max_in_flight = 5
