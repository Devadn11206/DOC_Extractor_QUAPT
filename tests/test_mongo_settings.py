"""
MongoDB configuration tests (Team Member 3, Phase 2A-1). Pure unit tests: no server, no driver.

Verified: the connection-string policy per environment, the database and replica-set names, every bound, the secret never
appearing in any representation or error, the mounted-secret file path, and the integration with the API settings and the
``DOC_API_*`` environment allow-list.
"""

import pytest
from pydantic import ValidationError

from doc_extractor.api.config import ENV_VARIABLES, ApiSettings, settings_from_environment
from doc_extractor.infrastructure.mongo.errors import PersistenceConfigurationError
from doc_extractor.infrastructure.mongo.settings import (
    MongoSettings,
    check_uri_policy,
    parse_uri_shape,
)

PASSWORD = "S3cr3t-Pa55word-Marker"
DEV_URI = f"mongodb://app:{PASSWORD}@localhost:27017/"
PROD_URI = f"mongodb://app:{PASSWORD}@mongo-0:27017,mongo-1:27017,mongo-2:27017/?replicaSet=rs0&tls=true"
SRV_URI = f"mongodb+srv://app:{PASSWORD}@cluster.example.net/?retryWrites=true"


def make(uri=DEV_URI, **overrides):
    values = {"uri": uri, "database": "doc_extractor", "app_env": "development"}
    values.update(overrides)
    return MongoSettings(**values)


# ---- connection-string policy -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("uri", [DEV_URI, "mongodb://localhost", "mongodb://localhost:27017/?directConnection=true",
                                 "mongodb://h1,h2/?replicaSet=rs0", "mongodb://localhost/?tlsAllowInvalidCertificates=true"])
def test_development_accepts_a_plain_or_loosely_configured_server(uri):
    assert check_uri_policy(uri, "development").hosts


@pytest.mark.parametrize("uri", [PROD_URI, SRV_URI, f"mongodb://u:{PASSWORD}@h1,h2/?tls=true&replicaSet=rs0",
                                 f"mongodb://u:{PASSWORD}@h/?ssl=true", "mongodb://h/?tls=true&authMechanism=MONGODB-X509",
                                 "mongodb://h/?tls=true&authMechanism=MONGODB-AWS"])
@pytest.mark.parametrize("env", ["staging", "production"])
def test_hardened_environments_accept_tls_with_authentication(uri, env):
    check_uri_policy(uri, env)


@pytest.mark.parametrize("uri, reason", [
    (f"mongodb://u:{PASSWORD}@h1,h2/?replicaSet=rs0", "tls_is_required_outside_development"),
    (f"mongodb://u:{PASSWORD}@h/?tls=false", "tls_is_required_outside_development"),
    (f"mongodb+srv://u:{PASSWORD}@c.example.net/?tls=false", "tls_is_required_outside_development"),
    (f"mongodb://u:{PASSWORD}@h/?tls=true&tlsInsecure=true", "insecure_tls_options_are_refused_outside_development"),
    (f"mongodb://u:{PASSWORD}@h/?tls=true&tlsAllowInvalidCertificates=true", "insecure_tls_options_are_refused_outside_development"),
    (f"mongodb://u:{PASSWORD}@h/?tls=true&tlsAllowInvalidHostnames=TRUE", "insecure_tls_options_are_refused_outside_development"),
    ("mongodb://h1,h2/?tls=true&replicaSet=rs0", "authentication_is_required_outside_development"),
    (f"mongodb://u:{PASSWORD}@h/?tls=true&directConnection=true", "direct_connection_is_refused_outside_development"),
    (f"mongodb://u:{PASSWORD}@h/?tls=true&readPreference=secondary", "a_non_primary_read_preference_is_refused_outside_development"),
])
@pytest.mark.parametrize("env", ["staging", "production"])
def test_hardened_environments_refuse_weak_transport_topology_and_anonymous_access(uri, reason, env):
    with pytest.raises(PersistenceConfigurationError) as caught:
        check_uri_policy(uri, env)
    assert caught.value.details["reason"] == reason
    assert PASSWORD not in str(caught.value) and PASSWORD not in repr(caught.value.details)


@pytest.mark.parametrize("uri, reason", [
    ("", "uri_empty"),
    ("mongodb://", "uri_malformed"),
    ("http://localhost", "uri_malformed"),
    ("localhost:27017", "uri_malformed"),
    ("mongodb://localhost\n", "uri_contains_whitespace_or_control_characters"),
    ("mongodb://local host", "uri_contains_whitespace_or_control_characters"),
    ("mongodb://u:p@,h2/", "uri_has_an_empty_host"),
    ("mongodb+srv://h1.example.net,h2.example.net/", "uri_srv_requires_exactly_one_host_without_a_port"),
    ("mongodb+srv://h1.example.net:27017/", "uri_srv_requires_exactly_one_host_without_a_port"),
])
def test_a_malformed_connection_string_is_refused_by_shape(uri, reason):
    with pytest.raises(PersistenceConfigurationError) as caught:
        parse_uri_shape(uri)
    assert caught.value.details["reason"] == reason


def test_a_non_string_uri_is_refused():
    with pytest.raises(PersistenceConfigurationError):
        parse_uri_shape(None)  # type: ignore[arg-type]


def test_the_replica_set_in_the_uri_must_match_the_configured_one():
    with pytest.raises(PersistenceConfigurationError) as caught:
        check_uri_policy("mongodb://h/?replicaSet=rs0", "development", "rs1")
    assert caught.value.details["reason"] == "replica_set_differs_from_the_uri"
    check_uri_policy("mongodb://h/?replicaSet=rs0", "development", "rs0")


def test_an_unknown_environment_is_refused():
    with pytest.raises(PersistenceConfigurationError):
        check_uri_policy(DEV_URI, "qa")


def test_options_are_parsed_case_insensitively():
    parts = parse_uri_shape("mongodb://u:p@h/?ReplicaSet=rs0&TLS=true")
    assert parts.options == {"replicaset": "rs0", "tls": "true"} and parts.has_credentials


# ---- the settings model ------------------------------------------------------------------------------------------------

def test_defaults_are_bounded_and_documented():
    settings = make()
    assert (settings.server_selection_timeout_ms, settings.connect_timeout_ms, settings.socket_timeout_ms) == (5000, 5000, 30000)
    assert (settings.max_pool_size, settings.min_pool_size, settings.ensure_indexes_on_startup) == (50, 0, True)
    assert settings.app_name == "doc-extractor-api" and settings.replica_set is None


@pytest.mark.parametrize("name", ["", " ", "a b", "a.b", "a/b", "../x", "$x", "x" * 64, "_leading", "-leading", "naïve", "a\nb"])
def test_a_database_name_must_be_plain(name):
    with pytest.raises(ValidationError):
        make(database=name)


@pytest.mark.parametrize("name", ["doc_extractor", "d", "Doc-Extractor_01", "x" * 63])
def test_a_plain_database_name_is_accepted(name):
    assert make(database=name).database == name


@pytest.mark.parametrize("name", ["", "rs 0", "rs/0", "-rs", "x" * 65, "rs\n"])
def test_a_replica_set_name_must_be_plain(name):
    with pytest.raises(ValidationError):
        make(replica_set=name)


@pytest.mark.parametrize("field, bad", [
    ("server_selection_timeout_ms", 99), ("server_selection_timeout_ms", 30_001), ("connect_timeout_ms", 0), ("connect_timeout_ms", 30_001),
    ("socket_timeout_ms", 999), ("socket_timeout_ms", 120_001), ("max_pool_size", 0), ("max_pool_size", 501), ("min_pool_size", -1),
    ("min_pool_size", 501),
])
def test_every_timeout_and_pool_size_is_bounded(field, bad):
    with pytest.raises(ValidationError):
        make(**{field: bad})


@pytest.mark.parametrize("field, good", [("server_selection_timeout_ms", 100), ("server_selection_timeout_ms", 30_000),
                                         ("socket_timeout_ms", 1_000), ("socket_timeout_ms", 120_000), ("max_pool_size", 500)])
def test_the_bounds_are_inclusive(field, good):
    assert getattr(make(**{field: good}), field) == good


def test_the_minimum_pool_cannot_exceed_the_maximum():
    with pytest.raises(ValidationError):
        make(max_pool_size=5, min_pool_size=6)
    assert make(max_pool_size=5, min_pool_size=5).min_pool_size == 5


def test_extra_fields_are_refused_and_the_model_is_frozen():
    with pytest.raises(ValidationError):
        make(unknown_option=1)
    with pytest.raises(ValidationError):
        make().database = "other"  # type: ignore[misc]


@pytest.mark.parametrize("env", ["staging", "production"])
def test_a_hardened_environment_cannot_be_built_from_a_weak_string(env):
    with pytest.raises(ValidationError):
        make(DEV_URI, app_env=env)
    assert make(PROD_URI, app_env=env).require_replica_set is True


def test_a_replica_set_is_required_outside_development_and_when_asked_for_in_development():
    assert make(DEV_URI).require_replica_set is False
    assert make(DEV_URI, replica_set="rs0").require_replica_set is True
    assert make("mongodb://h/?replicaSet=rs0").require_replica_set is True
    assert make(PROD_URI, app_env="production").effective_replica_set == "rs0"
    assert make(DEV_URI, replica_set="rs9").effective_replica_set == "rs9"


@pytest.mark.parametrize("render", [repr, str, lambda s: str(s.model_dump()), lambda s: s.model_dump_json(), lambda s: str(s.summary()),
                                    lambda s: repr(s.model_copy())])
def test_the_connection_string_never_appears_in_any_representation(render):
    text = render(make(PROD_URI, app_env="production", replica_set="rs0"))
    assert PASSWORD not in text and "mongo-0" not in text and "app:" not in text


def test_the_validation_error_for_a_bad_string_names_a_reason_and_hides_the_input():
    with pytest.raises(ValidationError) as caught:
        make(f"mongodb://u:{PASSWORD}@h/", app_env="production")
    text = str(caught.value)
    assert "tls_is_required_outside_development" in text and PASSWORD not in text and "input_value" not in text


def test_the_summary_has_no_host_or_credential():
    summary = make(PROD_URI, app_env="production").summary()
    assert summary == {"scheme": "mongodb", "host_count": 3, "database": "doc_extractor", "replica_set": "rs0",
                       "require_replica_set": True}


# ---- from the API settings ---------------------------------------------------------------------------------------------

class _Api:
    def __init__(self, **values):
        self.app_env = "development"
        self.__dict__.update(values)


def test_nothing_configured_means_no_mongodb():
    assert MongoSettings.from_api_settings(_Api()) is None
    assert MongoSettings.from_api_settings(ApiSettings(app_env="development")) is None


@pytest.mark.parametrize("values, reason", [
    ({"mongo_database": "d"}, "mongo_settings_without_a_connection_string"),
    ({"mongo_replica_set": "rs0"}, "mongo_settings_without_a_connection_string"),
    ({"mongo_uri": "mongodb://h"}, "database_is_required"),
    ({"mongo_uri_file": "/x"}, "database_is_required"),
])
def test_a_partial_configuration_is_an_error_never_a_default(values, reason):
    with pytest.raises(PersistenceConfigurationError) as caught:
        MongoSettings.from_api_settings(_Api(**values), read_uri_file=False)
    assert caught.value.details["reason"] == reason


def test_the_uri_and_the_uri_file_are_mutually_exclusive():
    with pytest.raises(PersistenceConfigurationError) as caught:
        MongoSettings.from_api_settings(_Api(mongo_uri="mongodb://h", mongo_uri_file="/x", mongo_database="d"))
    assert caught.value.details["reason"] == "uri_and_uri_file_are_mutually_exclusive"


def test_settings_are_built_from_an_inline_uri_with_every_option():
    api = ApiSettings(app_env="development", mongo_uri=DEV_URI, mongo_database="d", mongo_replica_set="rs0",
                      mongo_server_selection_timeout_ms=2000, mongo_connect_timeout_ms=3000, mongo_socket_timeout_ms=4000,
                      mongo_max_pool_size=10, mongo_min_pool_size=2, mongo_ensure_indexes_on_startup=False)
    built = MongoSettings.from_api_settings(api)
    assert built.database == "d" and built.replica_set == "rs0" and built.app_env == "development"
    assert (built.server_selection_timeout_ms, built.connect_timeout_ms, built.socket_timeout_ms) == (2000, 3000, 4000)
    assert (built.max_pool_size, built.min_pool_size, built.ensure_indexes_on_startup) == (10, 2, False)
    assert built.uri.get_secret_value() == DEV_URI


def test_the_uri_is_read_from_a_mounted_secret_file_and_stripped(tmp_path):
    secret = tmp_path / "mongo_uri"
    secret.write_text(DEV_URI + "\n", encoding="utf-8")
    api = ApiSettings(app_env="development", mongo_uri_file=str(secret), mongo_database="d")
    assert MongoSettings.from_api_settings(api).uri.get_secret_value() == DEV_URI
    assert MongoSettings.from_api_settings(api, read_uri_file=False) is None       # settings validation does no I/O


@pytest.mark.parametrize("content, reason", [
    (b"", "uri_file_is_empty"), (b"  \n\t ", "uri_file_is_empty"), (b"\xff\xfe\x00bad", "uri_file_is_unreadable"),
    (b"x" * 9000, "uri_file_is_too_large"),
])
def test_a_bad_secret_file_is_refused_without_echoing_it(tmp_path, content, reason):
    secret = tmp_path / "uri"
    secret.write_bytes(content)
    with pytest.raises(PersistenceConfigurationError) as caught:
        MongoSettings.from_api_settings(_Api(mongo_uri_file=str(secret), mongo_database="d"))
    assert caught.value.details["reason"] == reason and str(secret) not in str(caught.value)


def test_a_missing_or_non_regular_secret_file_is_refused(tmp_path):
    for path, reason in ((tmp_path / "absent", "uri_file_is_unreadable"), (tmp_path, "uri_file_is_not_a_regular_file")):
        with pytest.raises(PersistenceConfigurationError) as caught:
            MongoSettings.from_api_settings(_Api(mongo_uri_file=str(path), mongo_database="d"))
        assert caught.value.details["reason"] == reason


def test_a_hardened_environment_checks_the_uri_read_from_a_file(tmp_path):
    secret = tmp_path / "uri"
    secret.write_text(f"mongodb://u:{PASSWORD}@h/", encoding="utf-8")
    api = ApiSettings(app_env="production", mongo_uri_file=str(secret), mongo_database="d")     # no I/O at validation time
    with pytest.raises(ValidationError) as caught:
        MongoSettings.from_api_settings(api)
    assert PASSWORD not in str(caught.value)


# ---- the API settings model and the environment -------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, fragment", [
    ({"mongo_database": "d"}, "mongo_settings_without_a_connection_string"),
    ({"mongo_uri": "mongodb://h"}, "database_is_required"),
    ({"mongo_uri": "mongodb://h", "mongo_uri_file": "/x", "mongo_database": "d"}, "mutually_exclusive"),
    ({"mongo_uri": f"mongodb://u:{PASSWORD}@h/", "mongo_database": "d", "app_env": "production"}, "tls_is_required_outside_development"),
    ({"mongo_uri": "mongodb://h", "mongo_database": "bad name"}, "database name"),
    ({"mongo_uri": "mongodb://h", "mongo_database": "d", "mongo_max_pool_size": 0}, "mongo_max_pool_size"),
])
def test_the_api_settings_refuse_an_invalid_mongodb_configuration_without_leaking_it(kwargs, fragment):
    values = {"app_env": "development"}
    values.update(kwargs)
    with pytest.raises(ValidationError) as caught:
        ApiSettings(**values)
    text = str(caught.value)
    assert fragment in text and PASSWORD not in text


def test_any_other_settings_error_does_not_echo_the_connection_string():
    with pytest.raises(ValidationError) as caught:
        ApiSettings(app_env="nonsense", mongo_uri=f"mongodb://u:{PASSWORD}@h/", mongo_database="d")
    assert PASSWORD not in str(caught.value) and "input_value" not in str(caught.value)


def test_the_connection_string_is_a_secret_in_the_api_settings():
    api = ApiSettings(app_env="development", mongo_uri=DEV_URI, mongo_database="d")
    assert PASSWORD not in repr(api) and PASSWORD not in str(api.model_dump()) and PASSWORD not in api.model_dump_json()


def test_every_mongodb_variable_maps_to_an_api_setting_and_is_read_from_the_environment():
    environment = {
        "DOC_API_MONGO_URI": DEV_URI, "DOC_API_MONGO_DATABASE": "d", "DOC_API_MONGO_REPLICA_SET": "rs0",
        "DOC_API_MONGO_SERVER_SELECTION_TIMEOUT_MS": "2500", "DOC_API_MONGO_CONNECT_TIMEOUT_MS": "2600",
        "DOC_API_MONGO_SOCKET_TIMEOUT_MS": "2700", "DOC_API_MONGO_MAX_POOL_SIZE": "12", "DOC_API_MONGO_MIN_POOL_SIZE": "3",
        "DOC_API_MONGO_ENSURE_INDEXES": "false", "DOC_API_ENV": "development",
    }
    api = ApiSettings(**settings_from_environment(environment))
    assert api.mongo_database == "d" and api.mongo_replica_set == "rs0" and api.mongo_ensure_indexes_on_startup is False
    assert (api.mongo_server_selection_timeout_ms, api.mongo_max_pool_size, api.mongo_min_pool_size) == (2500, 12, 3)
    assert api.mongo_uri.get_secret_value() == DEV_URI
    assert {name for name in ENV_VARIABLES if name.startswith("DOC_API_MONGO_")} == {
        name for name in environment if name.startswith("DOC_API_MONGO_")} | {"DOC_API_MONGO_URI_FILE"}


def test_the_uri_file_variable_is_read_from_the_environment():
    api = ApiSettings(**settings_from_environment({"DOC_API_MONGO_URI_FILE": "/run/secrets/m", "DOC_API_MONGO_DATABASE": "d",
                                                    "DOC_API_ENV": "development"}))
    assert api.mongo_uri_file == "/run/secrets/m" and api.mongo_uri is None


def test_a_mistyped_mongodb_variable_is_refused_by_the_allow_list():
    from doc_extractor.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError):
        settings_from_environment({"DOC_API_MONGO_URL": DEV_URI})
    with pytest.raises(ConfigurationError):
        settings_from_environment({"DOC_API_MONGO_URI": "  "})
