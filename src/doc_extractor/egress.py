"""
Outbound network policy: default deny (PRD SEC-011, ARC-001, GR-013).

The deployment denies all outbound traffic by default. A destination is reachable only when it is on the allow-list:
an explicit ``host:port`` entry in ``DOC_EGRESS_ALLOWED_DESTINATIONS`` (comma-separated), or loopback, which never
leaves the host. Every outbound call in this package goes through ``EgressPolicy`` before it opens a connection:

- the vLLM / OpenAI-compatible extraction endpoint (``extractor``),
- the clamd malware-scanner TCP listener (``malware``).

Network-level enforcement (a deny-all egress rule in the container platform) is the deployment's job and is set up
alongside the profile (ARC-006). This module makes the application refuse the call itself, so a misconfigured
endpoint fails closed with a typed error instead of connecting.
"""

from __future__ import annotations

import os
from typing import Dict, FrozenSet, Iterable, Optional, Tuple
from urllib.parse import urlsplit

from doc_extractor.exceptions import SecurityCheckFailedError

ALLOWED_DESTINATIONS_ENV = "DOC_EGRESS_ALLOWED_DESTINATIONS"
LOOPBACK_HOSTS: FrozenSet[str] = frozenset({"localhost", "127.0.0.1", "::1"})
_DEFAULT_PORTS: Dict[str, int] = {"http": 80, "https": 443}


class EgressDeniedError(SecurityCheckFailedError):
    """An outbound connection was refused by the egress policy (SEC-011). Carries the purpose only, never the target."""

    def __init__(self, purpose: str) -> None:
        super().__init__(
            message="Outbound connection denied by the egress policy.",
            code="EGRESS_DENIED",
            details={"purpose": purpose},
        )


def _parse_entry(entry: str) -> Tuple[str, int]:
    host, sep, port_text = entry.strip().rpartition(":")
    if not sep or not host or not port_text.isdigit():
        raise ValueError("Egress allow-list entries must be host:port.")
    port = int(port_text)
    if not 0 < port < 65536:
        raise ValueError("Egress allow-list port is out of range.")
    return host.strip().lower(), port


class EgressPolicy:
    """Default-deny allow-list of ``(host, port)`` destinations."""

    def __init__(self, allowed_destinations: Iterable[str] = ()) -> None:
        self._allowed: FrozenSet[Tuple[str, int]] = frozenset(
            _parse_entry(entry) for entry in allowed_destinations if entry and entry.strip()
        )

    @classmethod
    def from_environment(cls, environ: Optional[Dict[str, str]] = None) -> "EgressPolicy":
        source = os.environ if environ is None else environ
        raw = source.get(ALLOWED_DESTINATIONS_ENV, "")
        return cls(part for part in raw.split(","))

    def permits_host_port(self, host: Optional[str], port: Optional[int]) -> bool:
        if not host or port is None:
            return False
        normalized = host.strip().lower()
        if normalized in LOOPBACK_HOSTS:
            return True
        return (normalized, int(port)) in self._allowed

    def check_host_port(self, host: Optional[str], port: Optional[int], *, purpose: str) -> None:
        if not self.permits_host_port(host, port):
            # SEC-021: every refused outbound connection is an alert. Only the purpose is recorded, never the target.
            from doc_extractor.alerts import get_default_monitor

            get_default_monitor().record_egress_attempt(purpose)
            raise EgressDeniedError(purpose=purpose)

    def check_url(self, url: str, *, purpose: str) -> None:
        parts = urlsplit(url)
        if parts.scheme.lower() not in _DEFAULT_PORTS:
            raise EgressDeniedError(purpose=purpose)
        try:
            port = parts.port or _DEFAULT_PORTS[parts.scheme.lower()]
        except ValueError as exc:
            raise EgressDeniedError(purpose=purpose) from exc
        self.check_host_port(parts.hostname, port, purpose=purpose)
