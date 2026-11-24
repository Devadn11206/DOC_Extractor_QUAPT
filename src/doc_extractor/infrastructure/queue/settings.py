"""
Message broker settings and the connection-URL policy (Team Member 3, Phase 3).

``BrokerSettings`` is the validated, immutable configuration of ONE AMQP broker. It is built from the API settings
(``BrokerSettings.from_api_settings``), never from the environment directly (the API reads its environment through one allow-list).

SAFETY RULES (the same shape as ``infrastructure/mongo/settings.py``)
- The URL is a secret: held as ``SecretStr``, never logged, never in an error (errors name a fixed reason and the setting). It may be
  given inline or through a mounted file, not both.
- Staging and production require TLS (``amqps://``) and credentials, and refuse the options that weaken TLS verification.
  Development accepts a plain ``amqp://`` URL.
- Every timeout and the number of unacknowledged messages are bounded.

The policy checks the SHAPE of the URL with the standard library; it does not resolve DNS and does not import the AMQP client.
"""

from __future__ import annotations

import os
import re
import stat
from typing import Any, Dict, NamedTuple, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from doc_extractor.infrastructure.queue.errors import BrokerConfigurationError

ENVIRONMENTS = ("development", "staging", "production")
HARDENED_ENVIRONMENTS = ("staging", "production")
MAX_URL_FILE_BYTES = 8_192
DEFAULT_QUEUE_PREFIX = "doc_extractor"
DEFAULT_DELAY_TIERS_SECONDS: Tuple[int, ...] = (1, 5, 15, 60, 300, 900, 3600)

_PREFIX = re.compile(r"[a-z][a-z0-9_-]{0,47}")
_INSECURE_TLS_OPTIONS = ("tlsinsecure", "tlsallowinvalidcertificates", "tlsallowinvalidhostnames", "no_verify_ssl")


class UrlParts(NamedTuple):
    scheme: str
    has_credentials: bool
    host: str
    options: Dict[str, str]


def _problem(reason: str, setting: str = "broker_url") -> BrokerConfigurationError:
    return BrokerConfigurationError(reason, setting=setting)


def parse_url_shape(url: str) -> UrlParts:
    """Scheme, credential presence, host and lower-cased options of an AMQP URL. Raises on a malformed one (reason only)."""
    if not isinstance(url, str) or not url:
        raise _problem("url_empty")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise _problem("url_contains_whitespace_or_control_characters")
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port                                                       # noqa: B018 - validates the port
    except ValueError:
        raise _problem("url_malformed") from None
    if parts.scheme not in ("amqp", "amqps"):
        raise _problem("url_scheme_must_be_amqp_or_amqps")
    if not host:
        raise _problem("url_has_no_host")
    options = {key.lower(): value for key, value in parse_qsl(parts.query, keep_blank_values=True)}
    return UrlParts(parts.scheme, bool(parts.username), host, options)


def check_url_policy(url: str, app_env: str) -> UrlParts:
    """Validate ``url`` for ``app_env``. Returns its parts. Raises ``BrokerConfigurationError`` (reason only)."""
    if app_env not in ENVIRONMENTS:
        raise _problem("unknown_environment", "app_env")
    parts = parse_url_shape(url)
    if app_env in HARDENED_ENVIRONMENTS:
        if parts.scheme != "amqps":
            raise _problem("tls_is_required_outside_development")
        weakened = any(parts.options.get(name, "").lower() in ("true", "1") for name in _INSECURE_TLS_OPTIONS)
        if weakened or parts.options.get("verify", "").lower() in ("verify_none", "none"):
            raise _problem("insecure_tls_options_are_refused_outside_development")
        if not parts.has_credentials:
            raise _problem("authentication_is_required_outside_development")
    return parts


class BrokerSettings(BaseModel):
    """Validated, immutable configuration of one broker. Holds a secret; never print it."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    url: SecretStr
    app_env: str = "development"
    queue_prefix: str = DEFAULT_QUEUE_PREFIX
    max_in_flight: int = Field(default=10, ge=1, le=100, description="Unacknowledged messages one consumer may hold.")
    connect_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0, allow_inf_nan=False)
    publish_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0, allow_inf_nan=False)
    delay_tiers_seconds: Tuple[int, ...] = DEFAULT_DELAY_TIERS_SECONDS
    delivery_limit: int = Field(default=10, ge=1, le=1000, description="Broker-level redelivery bound (a safety net behind the ledger's attempt bound).")

    @field_validator("queue_prefix")
    @classmethod
    def _queue_prefix(cls, value: str) -> str:
        if not _PREFIX.fullmatch(value or ""):
            raise ValueError("The queue prefix must be 1 to 48 lowercase letters, digits, underscores or hyphens, starting with a letter.")
        return value

    @field_validator("delay_tiers_seconds")
    @classmethod
    def _tiers(cls, value: Tuple[int, ...]) -> Tuple[int, ...]:
        if not value or len(value) > 12 or list(value) != sorted(set(value)) or value[0] < 1 or value[-1] > 7 * 24 * 3600:
            raise ValueError("The delay tiers must be 1 to 12 strictly increasing whole seconds, at most 7 days.")
        return value

    @model_validator(mode="after")
    def _policy(self) -> "BrokerSettings":
        try:
            check_url_policy(self.url.get_secret_value(), self.app_env)
        except BrokerConfigurationError as exc:
            raise ValueError(f"invalid broker URL: {exc.details.get('reason')}") from None
        return self

    def summary(self) -> Dict[str, Any]:
        """Non-secret description for logs: no host, no credential, no URL."""
        parts = parse_url_shape(self.url.get_secret_value())
        return {"scheme": parts.scheme, "queue_prefix": self.queue_prefix, "max_in_flight": self.max_in_flight}

    @classmethod
    def from_api_settings(cls, api_settings: Any, *, read_url_file: bool = True) -> Optional["BrokerSettings"]:
        """
        The broker settings carried by the API settings, or ``None`` when no broker is configured. A URL and a URL file together are an
        error, never a silent choice. ``read_url_file=False`` skips the file read (used while the API settings themselves are validated).
        """
        inline = getattr(api_settings, "broker_url", None)
        file_path = getattr(api_settings, "broker_url_file", None)
        if inline is not None and file_path is not None:
            raise _problem("url_and_url_file_are_mutually_exclusive")
        if inline is None and file_path is None:
            return None
        if inline is not None:
            url = inline.get_secret_value() if isinstance(inline, SecretStr) else str(inline)
        elif read_url_file:
            url = _read_secret_file(file_path)
        else:
            return None
        return cls(
            url=SecretStr(url),
            app_env=getattr(api_settings, "app_env", "development"),
            queue_prefix=getattr(api_settings, "broker_queue_prefix", DEFAULT_QUEUE_PREFIX),
            max_in_flight=getattr(api_settings, "broker_max_in_flight", 10),
            connect_timeout_seconds=getattr(api_settings, "broker_connect_timeout_seconds", 10.0),
            publish_timeout_seconds=getattr(api_settings, "broker_publish_timeout_seconds", 10.0),
        )


def _read_secret_file(path: str) -> str:
    """Read the URL from a mounted secret file (a regular file, small, UTF-8). Errors name no path or content."""
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            raise _problem("url_file_is_not_a_regular_file", "broker_url_file")
        if info.st_size > MAX_URL_FILE_BYTES:
            raise _problem("url_file_is_too_large", "broker_url_file")
        with open(path, "rb") as handle:
            text = handle.read(MAX_URL_FILE_BYTES + 1).decode("utf-8")
    except BrokerConfigurationError:
        raise
    except (OSError, UnicodeDecodeError):
        raise _problem("url_file_is_unreadable", "broker_url_file") from None
    cleaned = text.strip()
    if not cleaned:
        raise _problem("url_file_is_empty", "broker_url_file")
    return cleaned
