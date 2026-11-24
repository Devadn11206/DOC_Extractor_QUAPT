"""
MongoDB connection settings and the connection-string policy (Team Member 3, Phase 2A-1).

``MongoSettings`` is the validated, immutable configuration of ONE MongoDB deployment. It is built from the API settings
(``MongoSettings.from_api_settings``), never from the environment directly: the API reads its environment through one
allow-list (``api/config.py``), and this module adds no second configuration path.

SAFETY RULES
- The connection string is a secret. It is held as ``SecretStr``, is never logged and never appears in an error: errors
  name a fixed reason and the setting, nothing else. It may be supplied inline or through a file (a mounted secret), not both.
- The database name is explicit. There is no default, so nothing can silently write into a database named ``test``.
- Staging and production require: TLS (``mongodb+srv`` or ``tls=true``), authentication, a replica set (so transactions
  exist and a failover is survived), and refuse the options that weaken transport or topology (invalid certificates,
  invalid host names, ``directConnection``, a non-primary read preference). Development accepts a plain standalone server.
- Every timeout and pool size is bounded, so a missing server cannot hang a request or a start-up.

The policy checks the SHAPE of the string with the standard library only. It does not resolve DNS and does not import the
driver, so settings load and validate without the ``mongo`` extra installed.
"""

from __future__ import annotations

import os
import re
import stat
from typing import Any, Dict, List, NamedTuple, Optional
from urllib.parse import parse_qsl

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from doc_extractor.infrastructure.mongo.errors import PersistenceConfigurationError

ENVIRONMENTS = ("development", "staging", "production")
HARDENED_ENVIRONMENTS = ("staging", "production")

DEFAULT_APP_NAME = "doc-extractor-api"
DEFAULT_SERVER_SELECTION_TIMEOUT_MS = 5_000
DEFAULT_CONNECT_TIMEOUT_MS = 5_000
DEFAULT_SOCKET_TIMEOUT_MS = 30_000
DEFAULT_MAX_POOL_SIZE = 50
MAX_URI_FILE_BYTES = 8_192

_DATABASE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")
_REPLICA_SET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_APP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_URI = re.compile(
    r"(?P<scheme>mongodb(?:\+srv)?)://"
    r"(?:(?P<userinfo>[^@/?#]*)@)?"
    r"(?P<hosts>[^/?#]+)"
    r"(?:/(?P<path>[^?#]*))?"
    r"(?:\?(?P<query>[^#]*))?"
)
_INSECURE_TLS_OPTIONS = ("tlsinsecure", "tlsallowinvalidcertificates", "tlsallowinvalidhostnames")
_PASSWORDLESS_AUTH_MECHANISMS = frozenset({"mongodb-x509", "mongodb-aws"})


class UriParts(NamedTuple):
    scheme: str
    has_credentials: bool
    hosts: List[str]
    options: Dict[str, str]


def _problem(reason: str, setting: str = "mongo_uri") -> PersistenceConfigurationError:
    return PersistenceConfigurationError(reason, setting=setting)


def parse_uri_shape(uri: str) -> UriParts:
    """Split a connection string into scheme, credential presence, hosts and lower-cased options. Raises on a malformed string."""
    if not isinstance(uri, str) or not uri:
        raise _problem("uri_empty")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in uri):
        raise _problem("uri_contains_whitespace_or_control_characters")
    match = _URI.fullmatch(uri)
    if match is None:
        raise _problem("uri_malformed")
    hosts = [host for host in match.group("hosts").split(",")]
    if not hosts or any(not host for host in hosts):
        raise _problem("uri_has_an_empty_host")
    if match.group("scheme") == "mongodb+srv" and (len(hosts) != 1 or ":" in hosts[0]):
        raise _problem("uri_srv_requires_exactly_one_host_without_a_port")
    options = {key.lower(): value for key, value in parse_qsl(match.group("query") or "", keep_blank_values=True)}
    userinfo = match.group("userinfo")
    return UriParts(match.group("scheme"), bool(userinfo), hosts, options)


def _is_true(value: Optional[str]) -> bool:
    return (value or "").strip().lower() == "true"


def check_uri_policy(uri: str, app_env: str, replica_set: Optional[str] = None) -> UriParts:
    """Validate ``uri`` for ``app_env``. Returns its parsed parts. Raises ``PersistenceConfigurationError`` (reason only)."""
    if app_env not in ENVIRONMENTS:
        raise _problem("unknown_environment", "app_env")
    parts = parse_uri_shape(uri)
    options = parts.options
    in_uri_set = options.get("replicaset")
    if in_uri_set is not None and replica_set is not None and in_uri_set != replica_set:
        raise _problem("replica_set_differs_from_the_uri", "mongo_replica_set")
    if app_env in HARDENED_ENVIRONMENTS:
        tls_on = parts.scheme == "mongodb+srv" or _is_true(options.get("tls")) or _is_true(options.get("ssl"))
        if not tls_on or options.get("tls", "true").lower() == "false" or options.get("ssl", "true").lower() == "false":
            raise _problem("tls_is_required_outside_development")
        if any(_is_true(options.get(name)) for name in _INSECURE_TLS_OPTIONS):
            raise _problem("insecure_tls_options_are_refused_outside_development")
        if not parts.has_credentials and options.get("authmechanism", "").lower() not in _PASSWORDLESS_AUTH_MECHANISMS:
            raise _problem("authentication_is_required_outside_development")
        if _is_true(options.get("directconnection")):
            raise _problem("direct_connection_is_refused_outside_development")
        if options.get("readpreference", "primary").lower() != "primary":
            raise _problem("a_non_primary_read_preference_is_refused_outside_development")
    return parts


class MongoSettings(BaseModel):
    """Validated, immutable configuration of one MongoDB deployment. Holds a secret; never print it."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    uri: SecretStr
    database: str
    app_env: str = "development"
    replica_set: Optional[str] = None
    app_name: str = DEFAULT_APP_NAME
    server_selection_timeout_ms: int = Field(default=DEFAULT_SERVER_SELECTION_TIMEOUT_MS, ge=100, le=30_000)
    connect_timeout_ms: int = Field(default=DEFAULT_CONNECT_TIMEOUT_MS, ge=100, le=30_000)
    socket_timeout_ms: int = Field(default=DEFAULT_SOCKET_TIMEOUT_MS, ge=1_000, le=120_000)
    max_pool_size: int = Field(default=DEFAULT_MAX_POOL_SIZE, ge=1, le=500)
    min_pool_size: int = Field(default=0, ge=0, le=500)
    ensure_indexes_on_startup: bool = True

    @field_validator("database")
    @classmethod
    def _database_name(cls, value: str) -> str:
        if not _DATABASE_NAME.fullmatch(value or ""):
            raise ValueError("The MongoDB database name must be 1 to 63 letters, digits, underscores or hyphens.")
        return value

    @field_validator("replica_set")
    @classmethod
    def _replica_set_name(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _REPLICA_SET_NAME.fullmatch(value):
            raise ValueError("The replica set name is not valid.")
        return value

    @field_validator("app_name")
    @classmethod
    def _app_name(cls, value: str) -> str:
        if not _APP_NAME.fullmatch(value or ""):
            raise ValueError("The application name is not valid.")
        return value

    @model_validator(mode="after")
    def _policy(self) -> "MongoSettings":
        if self.min_pool_size > self.max_pool_size:
            raise ValueError("min_pool_size must not exceed max_pool_size.")
        try:
            check_uri_policy(self.uri.get_secret_value(), self.app_env, self.replica_set)
        except PersistenceConfigurationError as exc:
            # Re-raised as a validation error that names the fixed reason only (never the string).
            raise ValueError(f"invalid MongoDB connection string: {exc.details.get('reason')}") from None
        return self

    @property
    def require_replica_set(self) -> bool:
        """Staging and production always need a replica set; development needs one only if it was asked for."""
        if self.app_env in HARDENED_ENVIRONMENTS or self.replica_set is not None:
            return True
        return "replicaset" in parse_uri_shape(self.uri.get_secret_value()).options

    @property
    def effective_replica_set(self) -> Optional[str]:
        return self.replica_set or parse_uri_shape(self.uri.get_secret_value()).options.get("replicaset")

    def summary(self) -> Dict[str, Any]:
        """Non-secret description for logs: no host, no credential, no string."""
        parts = parse_uri_shape(self.uri.get_secret_value())
        return {
            "scheme": parts.scheme,
            "host_count": len(parts.hosts),
            "database": self.database,
            "replica_set": self.effective_replica_set,
            "require_replica_set": self.require_replica_set,
        }

    @classmethod
    def from_api_settings(cls, api_settings: Any, *, read_uri_file: bool = True) -> Optional["MongoSettings"]:
        """
        The MongoDB settings carried by the API settings, or ``None`` when MongoDB is not configured at all.
        A partial configuration (a database or replica set with no connection string, or a connection string with no
        database) is an error, never a silent default. ``read_uri_file=False`` skips the file read (used while the API
        settings themselves are being validated, which must do no I/O).
        """
        inline = getattr(api_settings, "mongo_uri", None)
        file_path = getattr(api_settings, "mongo_uri_file", None)
        database = getattr(api_settings, "mongo_database", None)
        replica_set = getattr(api_settings, "mongo_replica_set", None)
        if inline is not None and file_path is not None:
            raise _problem("uri_and_uri_file_are_mutually_exclusive")
        if inline is None and file_path is None:
            if database is not None or replica_set is not None:
                raise _problem("mongo_settings_without_a_connection_string", "mongo_uri")
            return None
        if database is None:
            raise _problem("database_is_required", "mongo_database")
        if inline is not None:
            uri = inline.get_secret_value() if isinstance(inline, SecretStr) else str(inline)
        elif read_uri_file:
            uri = _read_secret_file(file_path)
        else:
            return None
        return cls(
            uri=SecretStr(uri),
            database=database,
            app_env=getattr(api_settings, "app_env", "development"),
            replica_set=replica_set,
            server_selection_timeout_ms=getattr(api_settings, "mongo_server_selection_timeout_ms", DEFAULT_SERVER_SELECTION_TIMEOUT_MS),
            connect_timeout_ms=getattr(api_settings, "mongo_connect_timeout_ms", DEFAULT_CONNECT_TIMEOUT_MS),
            socket_timeout_ms=getattr(api_settings, "mongo_socket_timeout_ms", DEFAULT_SOCKET_TIMEOUT_MS),
            max_pool_size=getattr(api_settings, "mongo_max_pool_size", DEFAULT_MAX_POOL_SIZE),
            min_pool_size=getattr(api_settings, "mongo_min_pool_size", 0),
            ensure_indexes_on_startup=getattr(api_settings, "mongo_ensure_indexes_on_startup", True),
        )


def _read_secret_file(path: str) -> str:
    """Read the connection string from a mounted secret file (a regular file, small, UTF-8). Errors name no path or content."""
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            raise _problem("uri_file_is_not_a_regular_file", "mongo_uri_file")
        if info.st_size > MAX_URI_FILE_BYTES:
            raise _problem("uri_file_is_too_large", "mongo_uri_file")
        with open(path, "rb") as handle:
            text = handle.read(MAX_URI_FILE_BYTES + 1).decode("utf-8")
    except PersistenceConfigurationError:
        raise
    except (OSError, UnicodeDecodeError):
        raise _problem("uri_file_is_unreadable", "mongo_uri_file") from None
    cleaned = text.strip()
    if not cleaned:
        raise _problem("uri_file_is_empty", "mongo_uri_file")
    return cleaned
