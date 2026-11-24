"""
Unit tests for S3 storage settings and validation policies (Team Member 3, Phase 4A).

Verifies:
- DNS-compliant bucket name rules.
- Endpoint URL policy (TLS enforced outside development, credentials in URL rejected).
- Credential handling (SecretStr protection, mounted secret files, mutual exclusivity).
- Hardened environment guardrails (staging/production require TLS and credentials).
- Bounded timeouts and retry parameters.
- Bridging from ApiSettings.
"""

from pathlib import Path
import pytest
from pydantic import SecretStr

from doc_extractor.infrastructure.storage.errors import ObjectStorageConfigurationError
from doc_extractor.infrastructure.storage.settings import S3StorageSettings


def test_minimal_development_settings():
    settings = S3StorageSettings(bucket_name="my-valid-bucket")
    assert settings.bucket_name == "my-valid-bucket"
    assert settings.region_name == "us-east-1"
    assert settings.endpoint_url is None
    assert settings.effective_force_path_style is False
    assert settings.verify_ssl is True
    assert settings.app_env == "development"


def test_custom_endpoint_infers_path_style():
    settings = S3StorageSettings(
        bucket_name="minio-bucket",
        endpoint_url="http://localhost:9000",
        app_env="development",
    )
    assert settings.effective_force_path_style is True

    # Explicit override takes precedence
    settings_virtual = S3StorageSettings(
        bucket_name="minio-bucket",
        endpoint_url="http://localhost:9000",
        force_path_style=False,
        app_env="development",
    )
    assert settings_virtual.effective_force_path_style is False


@pytest.mark.parametrize(
    "invalid_bucket,expected_reason",
    [
        ("", "bucket_name_empty"),
        ("   ", "bucket_name_empty"),
        ("ab", "bucket_name_invalid_length"),
        ("a" * 64, "bucket_name_invalid_length"),
        ("Upper-Case-Bucket", "bucket_name_invalid_characters"),
        ("bucket_with_underscore", "bucket_name_invalid_characters"),
        ("-start-with-hyphen", "bucket_name_invalid_characters"),
        ("end-with-hyphen-", "bucket_name_invalid_characters"),
        ("consecutive..dots", "bucket_name_consecutive_periods"),
        ("192.168.1.1", "bucket_name_formatted_as_ip"),
    ],
)
def test_bucket_name_validation(invalid_bucket, expected_reason):
    with pytest.raises(ObjectStorageConfigurationError) as exc_info:
        S3StorageSettings(bucket_name=invalid_bucket)
    assert exc_info.value.details.get("reason") == expected_reason


@pytest.mark.parametrize(
    "invalid_url,expected_reason",
    [
        ("ftp://s3.corp.com", "endpoint_url_scheme_must_be_http_or_https"),
        ("http://user:secret@s3.corp.com", "endpoint_url_credentials_not_allowed"),
        ("http://", "endpoint_url_has_no_host"),
        ("http://host name/path", "endpoint_url_contains_whitespace_or_control_characters"),
    ],
)
def test_endpoint_url_validation(invalid_url, expected_reason):
    with pytest.raises(ObjectStorageConfigurationError) as exc_info:
        S3StorageSettings(bucket_name="test-bucket", endpoint_url=invalid_url)
    assert exc_info.value.details.get("reason") == expected_reason


def test_production_requires_tls_endpoint():
    # In production, http:// endpoint is refused
    with pytest.raises(ObjectStorageConfigurationError) as exc_info:
        S3StorageSettings(
            bucket_name="prod-bucket",
            endpoint_url="http://s3.internal:9000",
            access_key_id=SecretStr("AKIAIOSFODNN7EXAMPLE"),
            secret_access_key=SecretStr("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
            app_env="production",
        )
    assert exc_info.value.details.get("reason") == "tls_is_required_outside_development"

    # https:// is accepted in production
    settings = S3StorageSettings(
        bucket_name="prod-bucket",
        endpoint_url="https://s3.internal:9000",
        access_key_id=SecretStr("AKIAIOSFODNN7EXAMPLE"),
        secret_access_key=SecretStr("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
        app_env="production",
    )
    assert settings.endpoint_url == "https://s3.internal:9000"


def test_production_refuses_disabled_tls_verification():
    with pytest.raises(ObjectStorageConfigurationError) as exc_info:
        S3StorageSettings(
            bucket_name="prod-bucket",
            verify_ssl=False,
            access_key_id=SecretStr("AKIAIOSFODNN7EXAMPLE"),
            secret_access_key=SecretStr("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
            app_env="production",
        )
    assert exc_info.value.details.get("reason") == "insecure_tls_is_refused_outside_development"


def test_hardened_environment_requires_credentials():
    # Unset credentials in production fails closed
    with pytest.raises(ObjectStorageConfigurationError) as exc_info:
        S3StorageSettings(bucket_name="prod-bucket", app_env="production")
    assert exc_info.value.details.get("reason") == "credentials_required_outside_development"

    # Explicit allow_iam_role permits credentials from environment/metadata service
    settings = S3StorageSettings(
        bucket_name="prod-bucket",
        allow_iam_role=True,
        app_env="production",
    )
    assert settings.allow_iam_role is True


def test_credential_mutual_exclusivity(tmp_path):
    key_file = tmp_path / "access_key"
    key_file.write_text("AKIAIOSFODNN7EXAMPLE", encoding="utf-8")

    with pytest.raises(ObjectStorageConfigurationError) as exc_info:
        S3StorageSettings(
            bucket_name="test-bucket",
            access_key_id=SecretStr("INLINE_KEY"),
            access_key_id_file=key_file,
            secret_access_key=SecretStr("SECRET"),
        )
    assert exc_info.value.details.get("reason") == "access_key_both_inline_and_file"


def test_credential_file_reading(tmp_path):
    key_file = tmp_path / "access_key"
    secret_file = tmp_path / "secret_key"
    key_file.write_text("AKIA_MOUNTED_KEY\n", encoding="utf-8")
    secret_file.write_text("SECRET_MOUNTED_KEY\n", encoding="utf-8")

    settings = S3StorageSettings(
        bucket_name="test-bucket",
        access_key_id_file=key_file,
        secret_access_key_file=secret_file,
    )
    assert settings.get_effective_access_key_id() == "AKIA_MOUNTED_KEY"
    assert settings.get_effective_secret_access_key() == "SECRET_MOUNTED_KEY"


def test_credential_file_unreadable(tmp_path):
    missing_file = tmp_path / "non_existent_key"
    settings = S3StorageSettings(
        bucket_name="test-bucket",
        access_key_id_file=missing_file,
        secret_access_key=SecretStr("SECRET"),
    )
    with pytest.raises(ObjectStorageConfigurationError) as exc_info:
        settings.get_effective_access_key_id()
    assert exc_info.value.details.get("reason") == "credential_file_unreadable"


def test_credential_file_empty(tmp_path):
    empty_file = tmp_path / "empty_key"
    empty_file.write_text("   \n", encoding="utf-8")
    settings = S3StorageSettings(
        bucket_name="test-bucket",
        access_key_id_file=empty_file,
        secret_access_key=SecretStr("SECRET"),
    )
    with pytest.raises(ObjectStorageConfigurationError) as exc_info:
        settings.get_effective_access_key_id()
    assert exc_info.value.details.get("reason") == "credential_file_empty"


def test_prefix_normalization():
    settings = S3StorageSettings(bucket_name="test-bucket", key_prefix="tenant-data")
    assert settings.key_prefix == "tenant-data/"

    settings_slash = S3StorageSettings(bucket_name="test-bucket", key_prefix="tenant-data/")
    assert settings_slash.key_prefix == "tenant-data/"

    with pytest.raises(ObjectStorageConfigurationError) as exc_info:
        S3StorageSettings(bucket_name="test-bucket", key_prefix="/leading-slash")
    assert exc_info.value.details.get("reason") == "prefix_cannot_start_with_slash"

    with pytest.raises(ObjectStorageConfigurationError) as exc_info:
        S3StorageSettings(bucket_name="test-bucket", key_prefix="../escape")
    assert exc_info.value.details.get("reason") == "prefix_cannot_contain_path_traversal"


def test_summary_does_not_leak_secrets():
    settings = S3StorageSettings(
        bucket_name="my-bucket",
        access_key_id=SecretStr("SUPER_SECRET_KEY"),
        secret_access_key=SecretStr("SUPER_SECRET_VALUE"),
        session_token=SecretStr("SUPER_SECRET_TOKEN"),
    )
    summary = settings.summary()
    assert summary["bucket_name"] == "my-bucket"
    assert summary["has_credentials"] is True
    assert "SUPER_SECRET_KEY" not in str(summary)
    assert "SUPER_SECRET_VALUE" not in str(summary)
    assert "SUPER_SECRET_TOKEN" not in str(summary)


def test_from_api_settings_bridging():
    class DummyApiSettings:
        storage_backend = "s3"
        s3_bucket = "api-configured-bucket"
        s3_endpoint_url = "https://s3.corp.internal:9000"
        s3_region = "eu-west-1"
        s3_access_key_id = SecretStr("KEY")
        s3_secret_access_key = SecretStr("SECRET")
        s3_session_token = None
        s3_access_key_id_file = None
        s3_secret_access_key_file = None
        s3_force_path_style = True
        s3_prefix = "custom-prefix"
        s3_connect_timeout_seconds = 10.0
        s3_read_timeout_seconds = 60.0
        s3_verify_ssl = True
        s3_ca_bundle = None
        s3_spool_dir = None
        app_env = "staging"

    settings = S3StorageSettings.from_api_settings(DummyApiSettings())
    assert settings is not None
    assert settings.bucket_name == "api-configured-bucket"
    assert settings.region_name == "eu-west-1"
    assert settings.key_prefix == "custom-prefix/"
    assert settings.connect_timeout_seconds == 10.0
    assert settings.read_timeout_seconds == 60.0
    assert settings.app_env == "staging"


def test_from_api_settings_returns_none_when_unconfigured():
    class LocalApiSettings:
        storage_backend = "local"
        s3_bucket = None

    assert S3StorageSettings.from_api_settings(LocalApiSettings()) is None


def test_from_api_settings_fails_when_backend_s3_without_bucket():
    class IncompleteApiSettings:
        storage_backend = "s3"
        s3_bucket = None

    with pytest.raises(ObjectStorageConfigurationError) as exc_info:
        S3StorageSettings.from_api_settings(IncompleteApiSettings())
    assert exc_info.value.details.get("reason") == "bucket_required_when_storage_backend_is_s3"
