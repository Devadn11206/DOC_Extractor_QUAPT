"""
KMS and HSM key provider abstractions (Team Member 3, Phase 4B).

Adheres to:
- PRD SEC-005: Tenant documents encrypted at rest with AES-256 using keys held in KMS or HSM.
- Envelope encryption: Plaintext Data Encryption Keys (DEKs) protected by KMS-managed Key Encryption Keys (KEKs).
- Sanitized operations: Raw key material is never persisted or logged; errors contain fixed reasons only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class DataKey:
    """
    Data Encryption Key (DEK) bundle.
    ``plaintext_key`` is used for immediate in-memory cipher operations and must be wiped after use.
    ``encrypted_key`` is stored in the object envelope.
    """
    plaintext_key: bytes
    encrypted_key: bytes
    kek_id: str
    key_version: str


class KmsProvider(ABC):
    """
    Abstract interface for Key Management Service (KMS) or Hardware Security Module (HSM) providers.
    Supports AWS KMS, customer-managed KMS/HSMs, and local fake implementations for testing.
    """

    @abstractmethod
    def generate_data_key(
        self,
        kek_id: Optional[str] = None,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> DataKey:
        """
        Generate a cryptographically secure 256-bit DEK, returning both plaintext and KEK-encrypted representations.
        """
        pass

    @abstractmethod
    def decrypt_data_key(
        self,
        encrypted_key: bytes,
        kek_id: Optional[str] = None,
        key_version: Optional[str] = None,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> bytes:
        """
        Decrypt an encrypted DEK using the KMS-managed KEK.
        """
        pass

    @abstractmethod
    def rewrap_data_key(
        self,
        encrypted_key: bytes,
        source_kek_id: Optional[str] = None,
        target_kek_id: Optional[str] = None,
        source_version: Optional[str] = None,
        target_version: Optional[str] = None,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> DataKey:
        """
        Re-encrypt an existing encrypted DEK under a new KEK or key version (KMS-side rewrapping without plaintext exposure).
        """
        pass

    @abstractmethod
    def get_current_key_id(self) -> str:
        """Return the current default KEK identifier."""
        pass

    @abstractmethod
    def get_current_key_version(self) -> str:
        """Return the current active key version string."""
        pass

    @abstractmethod
    def check_health(self) -> Dict[str, Any]:
        """Verify connectivity and key availability."""
        pass
