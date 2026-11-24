"""
Webhook security, destination validation, and SSRF prevention (Team Member 3, Phase 5C).

Adheres to:
- PRD §10.1 SEC-011 & ARC-001 (Default-deny outbound egress policy).
- Anti-SSRF (Server-Side Request Forgery) protection:
  - Enforces HTTPS scheme (rejects insecure HTTP outside explicitly allowed testing loopback).
  - Rejects private IPv4 ranges (RFC 1918: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16).
  - Rejects link-local and cloud metadata addresses (169.254.0.0/16, including AWS/GCP 169.254.169.254).
  - Rejects loopback (127.0.0.0/8, ::1) unless explicitly permitted for test/development.
  - Rejects IPv6 unique local (fc00::/7) and link-local (fe80::/10) addresses.
  - Resolves domain names via DNS and validates EVERY returned IP against forbidden ranges
    to prevent DNS rebinding and TOCTOU attacks.
  - Integrates with EgressPolicy (SEC-011).
- Open Decision M5-D09 (Webhook secret storage):
  - Defines WebhookSecretResolver protocol for resolving `secret_ref` without exposing plaintext secrets in logs or DB.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from typing import Any, Dict, Optional, Protocol, Sequence, Tuple
from urllib.parse import urlsplit

from doc_extractor.egress import EgressPolicy, LOOPBACK_HOSTS
from doc_extractor.exceptions import SecurityCheckFailedError

logger = logging.getLogger("doc_extractor.webhook.security")

# Cloud metadata services and forbidden hostnames
METADATA_HOSTNAMES = frozenset({
    "metadata.google.internal",
    "metadata",
    "instance-data",
})

FORBIDDEN_INTERNAL_SUFFIXES = (
    ".local",
    ".internal",
    ".localhost",
    ".corp",
    ".lan",
)

# Cloud instance metadata IP: 169.254.169.254
METADATA_IPV4 = ipaddress.IPv4Address("169.254.169.254")


class WebhookDestinationError(SecurityCheckFailedError):
    """Raised when a webhook destination URL violates SSRF or egress policies."""

    def __init__(self, message: str, code: str = "SSRF_DESTINATION_FORBIDDEN", details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(
            message=message,
            code=code,
            details=details or {},
        )


def _check_ip_safety(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_loopback: bool = False) -> None:
    """Validate that an IP address is not internal, private, loopback, link-local, or reserved."""
    # Handle IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if ip == METADATA_IPV4:
        raise WebhookDestinationError("Outbound request to cloud instance metadata service is forbidden.")

    if ip.is_loopback:
        if allow_loopback:
            return
        raise WebhookDestinationError("Outbound request to loopback address is forbidden.")

    if ip.is_private:
        raise WebhookDestinationError("Outbound request to private network address is forbidden.")

    if ip.is_link_local:
        raise WebhookDestinationError("Outbound request to link-local address is forbidden.")

    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise WebhookDestinationError("Outbound request to special-use or reserved IP address is forbidden.")


def validate_webhook_destination(
    url: str,
    *,
    egress_policy: Optional[EgressPolicy] = None,
    allow_loopback: bool = False,
    resolve_dns: bool = True,
) -> Tuple[str, int]:
    """
    Validate an outbound webhook destination URL against SSRF and egress policies.
    Returns (hostname, port) if valid; raises WebhookDestinationError if denied.
    """
    if not url or not isinstance(url, str):
        raise WebhookDestinationError("Webhook destination URL must be a non-empty string.")

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()

    # Protocol validation
    if scheme != "https":
        if scheme == "http" and allow_loopback:
            pass  # Permitted for local development/testing only
        else:
            raise WebhookDestinationError(f"Webhook URL scheme must be HTTPS (received '{scheme}').")

    # Credential validation (embedded user/password forbidden)
    if parts.username or parts.password:
        raise WebhookDestinationError("Credentials in webhook URLs are forbidden.")

    # Host validation
    hostname = parts.hostname
    if not hostname:
        raise WebhookDestinationError("Webhook URL missing valid hostname.")

    hostname_clean = hostname.lower().strip()

    # Port extraction
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise WebhookDestinationError("Invalid port in webhook destination URL.") from exc

    # Check forbidden metadata hostnames
    if hostname_clean in METADATA_HOSTNAMES or hostname_clean.endswith(".internal"):
        raise WebhookDestinationError("Outbound request to cloud metadata host is forbidden.")

    if not allow_loopback:
        if hostname_clean in LOOPBACK_HOSTS or any(hostname_clean.endswith(sfx) for sfx in FORBIDDEN_INTERNAL_SUFFIXES):
            raise WebhookDestinationError(f"Outbound request to internal hostname '{hostname_clean}' is forbidden.")

    # Check if hostname is directly an IP literal
    try:
        ip = ipaddress.ip_address(hostname_clean)
        _check_ip_safety(ip, allow_loopback=allow_loopback)
    except ValueError:
        # Hostname is a domain name: perform DNS resolution to prevent DNS rebinding
        if resolve_dns:
            try:
                addr_info = socket.getaddrinfo(hostname_clean, port, proto=socket.IPPROTO_TCP)
                if not addr_info:
                    raise WebhookDestinationError(f"Could not resolve webhook host '{hostname_clean}'.")
                for entry in addr_info:
                    sockaddr = entry[4]
                    ip_str = sockaddr[0]
                    resolved_ip = ipaddress.ip_address(ip_str)
                    _check_ip_safety(resolved_ip, allow_loopback=allow_loopback)
            except socket.gaierror as exc:
                raise WebhookDestinationError(f"DNS resolution failed for webhook host '{hostname_clean}'.") from exc

    # EgressPolicy enforcement (SEC-011)
    if egress_policy is not None:
        if not egress_policy.permits_host_port(hostname_clean, port):
            raise WebhookDestinationError(
                f"Destination '{hostname_clean}:{port}' is not on the outbound egress allow-list.",
                code="EGRESS_DENIED",
            )

    return hostname_clean, port


class WebhookSecretResolver(Protocol):
    """Port for resolving webhook signing secrets from secure secret references (M5-D09)."""

    def resolve_secret(self, secret_ref: str, tenant_id: str) -> bytes:
        """Resolve raw secret bytes given a secret reference; never logs or leaks secret."""
        ...


class EphemeralSecretResolver:
    """
    In-memory secret resolver for testing and development environments.
    Maps secret_ref -> secret bytes without external secret manager dependencies.
    """

    def __init__(self, secrets_map: Optional[Dict[str, bytes]] = None) -> None:
        self._secrets: Dict[Tuple[str, str], bytes] = {}
        if secrets_map:
            for ref, sec in secrets_map.items():
                self._secrets[("*", ref)] = sec

    def register_secret(self, secret_ref: str, secret: bytes | str, tenant_id: str = "*") -> None:
        raw = secret.encode("utf-8") if isinstance(secret, str) else secret
        self._secrets[(tenant_id, secret_ref)] = raw

    def resolve_secret(self, secret_ref: str, tenant_id: str) -> bytes:
        # Look up tenant-specific key first, then global
        key = (tenant_id, secret_ref)
        if key in self._secrets:
            return self._secrets[key]
        global_key = ("*", secret_ref)
        if global_key in self._secrets:
            return self._secrets[global_key]
        raise WebhookDestinationError(
            f"Could not resolve secret for secret reference '{secret_ref}'.",
            code="WEBHOOK_SECRET_NOT_FOUND",
        )
