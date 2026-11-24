"""
In-memory fake KMS provider for development and testing (Team Member 3, Phase 4B).

SECURITY BOUNDARY:
- Strictly forbidden in production and staging environments (checked at instantiation).
- Simulates KMS envelope encryption, key versioning, rotation, and rewrapping.
- Provides test hooks to simulate key revocation, network outages, and access denial.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Optional, Set

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from doc_extractor.infrastructure.storage.kms.base import DataKey, KmsProvider
from doc_extractor.infrastructure.storage.kms.errors import (
    KmsAuthenticationError,
    KmsConfigurationError,
    KmsKeyNotFoundError,
    KmsUnavailableError,
)

HARDENED_ENVIRONMENTS = ("staging", "production")


class FakeKmsProvider(KmsProvider):
    """
    In-memory KMS test double.
    Uses AES-256-GCM to simulate KEK wrapping of DEKs across key versions.
    """

    def __init__(
        self,
        kek_id: str = "fake-kek-dev",
        key_version: str = "v1",
        app_env: str = "development",
    ) -> None:
        if app_env in HARDENED_ENVIRONMENTS:
            raise KmsConfigurationError("fake_kms_forbidden_outside_development", setting="app_env")

        self.kek_id = kek_id
        self.key_version = key_version
        self.app_env = app_env

        # Master key simulation: map (kek_id, version) -> 32-byte AESGCM instance
        self._master_keys: Dict[str, AESGCM] = {}
        self._revoked_versions: Set[str] = set()
        self._simulate_outage = False
        self._simulate_access_denied = False

        self._ensure_master_key(self.kek_id, self.key_version)

    def _key_identifier(self, kek_id: str, version: str) -> str:
        return f"{kek_id}#{version}"

    def _ensure_master_key(self, kek_id: str, version: str) -> AESGCM:
        kid = self._key_identifier(kek_id, version)
        if kid not in self._master_keys:
            raw_key = hashlib.sha256(f"fake-master-seed-{kid}".encode("utf-8")).digest()
            self._master_keys[kid] = AESGCM(raw_key)
        return self._master_keys[kid]

    def _check_simulation_gates(self, version: str) -> None:
        if self._simulate_outage:
            raise KmsUnavailableError("kms_endpoint_unreachable")
        if self._simulate_access_denied:
            raise KmsAuthenticationError("kms_access_denied")
        if version in self._revoked_versions:
            raise KmsKeyNotFoundError("kms_key_revoked_or_disabled")

    def generate_data_key(
        self,
        kek_id: Optional[str] = None,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> DataKey:
        effective_kek = kek_id or self.kek_id
        effective_ver = self.key_version
        self._check_simulation_gates(effective_ver)

        # Generate 256-bit plaintext DEK
        plaintext_dek = os.urandom(32)

        # Wrap with master key
        master_cipher = self._ensure_master_key(effective_kek, effective_ver)
        nonce = os.urandom(12)
        aad = json.dumps(encryption_context or {}, sort_keys=True).encode("utf-8")
        encrypted_blob = nonce + master_cipher.encrypt(nonce, plaintext_dek, aad)

        return DataKey(
            plaintext_key=plaintext_dek,
            encrypted_key=encrypted_blob,
            kek_id=effective_kek,
            key_version=effective_ver,
        )

    def decrypt_data_key(
        self,
        encrypted_key: bytes,
        kek_id: Optional[str] = None,
        key_version: Optional[str] = None,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> bytes:
        effective_kek = kek_id or self.kek_id
        effective_ver = key_version or self.key_version
        self._check_simulation_gates(effective_ver)

        kid = self._key_identifier(effective_kek, effective_ver)
        if kid not in self._master_keys:
            raise KmsKeyNotFoundError("kms_key_not_found")

        master_cipher = self._master_keys[kid]
        if len(encrypted_key) < 28:  # 12 bytes nonce + 16 bytes tag
            raise KmsKeyNotFoundError("encrypted_key_blob_invalid")

        nonce = encrypted_key[:12]
        ciphertext = encrypted_key[12:]
        aad = json.dumps(encryption_context or {}, sort_keys=True).encode("utf-8")
        try:
            return master_cipher.decrypt(nonce, ciphertext, aad)
        except Exception as exc:
            raise KmsAuthenticationError("kms_decryption_failed") from exc

    def rewrap_data_key(
        self,
        encrypted_key: bytes,
        source_kek_id: Optional[str] = None,
        target_kek_id: Optional[str] = None,
        source_version: Optional[str] = None,
        target_version: Optional[str] = None,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> DataKey:
        src_kek = source_kek_id or self.kek_id
        tgt_kek = target_kek_id or self.kek_id
        src_ver = source_version or self.key_version
        tgt_ver = target_version or self.key_version

        # Decrypt with source KEK
        plaintext = self.decrypt_data_key(encrypted_key, src_kek, src_ver, encryption_context)

        # Encrypt with target KEK
        self._check_simulation_gates(tgt_ver)
        master_cipher = self._ensure_master_key(tgt_kek, tgt_ver)
        nonce = os.urandom(12)
        aad = json.dumps(encryption_context or {}, sort_keys=True).encode("utf-8")
        new_encrypted_blob = nonce + master_cipher.encrypt(nonce, plaintext, aad)

        return DataKey(
            plaintext_key=b"",  # Re-wrapped key doesn't expose plaintext to caller
            encrypted_key=new_encrypted_blob,
            kek_id=tgt_kek,
            key_version=tgt_ver,
        )

    def get_current_key_id(self) -> str:
        return self.kek_id

    def get_current_key_version(self) -> str:
        return self.key_version

    def check_health(self) -> Dict[str, Any]:
        self._check_simulation_gates(self.key_version)
        return {
            "status": "ok",
            "provider": "fake_kms",
            "kek_id": self.kek_id,
            "version": self.key_version,
        }

    # Simulation control methods for tests
    def rotate_key(self, new_version: str, new_kek_id: Optional[str] = None) -> None:
        """Rotate current active key to a new version."""
        if new_kek_id:
            self.kek_id = new_kek_id
        self.key_version = new_version
        self._ensure_master_key(self.kek_id, self.key_version)

    def revoke_key(self, version: str) -> None:
        """Revoke a specific key version."""
        self._revoked_versions.add(version)

    def simulate_outage(self, active: bool = True) -> None:
        """Simulate KMS network failure or service downtime."""
        self._simulate_outage = active

    def simulate_access_denied(self, active: bool = True) -> None:
        """Simulate IAM access denial."""
        self._simulate_access_denied = active
