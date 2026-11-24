"""
Checkpoint signing and independent verification interfaces and implementations (Team Member 3, Phase 4C).

Adheres to:
- PRD SEC-008: Independently verifiable audit checkpoints with asymmetric keys.
- Private Key Isolation: Private keys are held in KMS or HSM; private keys are never persisted or logged.
- Independent Verification: Anyone with public key material can verify checkpoints completely offline without KMS.
"""

from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from typing import Callable, Dict, Optional, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from doc_extractor.infrastructure.audit.errors import (
    CheckpointConfigurationError,
    CheckpointSignatureInvalidError,
    CheckpointSigningError,
    CheckpointVerificationError,
)
from doc_extractor.infrastructure.audit.models import AuditCheckpoint

HARDENED_ENVIRONMENTS = ("staging", "production")


class AuditCheckpointSigner(ABC):
    """Abstract interface for signing audit checkpoints using an asymmetric key managed by KMS/HSM."""

    @abstractmethod
    def sign(self, checkpoint: AuditCheckpoint) -> AuditCheckpoint:
        """
        Signs the checkpoint's canonical bytes using an asymmetric private key.
        Returns a new AuditCheckpoint instance with the signature populated.
        """
        pass

    @abstractmethod
    def get_public_key(self, key_id: Optional[str] = None, key_version: Optional[str] = None) -> bytes:
        """
        Retrieves SubjectPublicKeyInfo PEM-encoded public key bytes for independent verification.
        """
        pass

    @abstractmethod
    def get_key_id(self) -> str:
        """Returns the current signing key identifier (ARN, alias, or key name)."""
        pass

    @abstractmethod
    def get_key_version(self) -> str:
        """Returns the current active key version tag."""
        pass


class AuditCheckpointVerifier(ABC):
    """Abstract interface for verifying signed audit checkpoints."""

    @abstractmethod
    def verify(self, checkpoint: AuditCheckpoint, public_key_pem: Optional[bytes] = None) -> bool:
        """
        Verifies the cryptographic signature of the checkpoint.
        Returns True if signature is valid; False or raises CheckpointSignatureInvalidError otherwise.
        """
        pass


class IndependentCheckpointVerifier(AuditCheckpointVerifier):
    """
    Offline, independent checkpoint verifier.
    Verifies signatures using public keys without requiring access to the private signing key or KMS.
    """

    def __init__(
        self,
        public_key_resolver: Optional[Callable[[str, str], Optional[bytes]]] = None,
    ) -> None:
        """
        :param public_key_resolver: Optional callable (key_id, key_version) -> PEM bytes.
        """
        self._resolver = public_key_resolver
        self._key_cache: Dict[str, Any] = {}

    def register_public_key(self, key_id: str, key_version: str, pem_bytes: bytes) -> None:
        """Cache a public key for a specific key_id and version."""
        kid = f"{key_id}#{key_version}"
        parsed_key = serialization.load_pem_public_key(pem_bytes)
        self._key_cache[kid] = parsed_key

    def verify(self, checkpoint: AuditCheckpoint, public_key_pem: Optional[bytes] = None) -> bool:
        """
        Verifies the signature on ``checkpoint.canonical_bytes()``.
        Returns True if valid; False if signature is invalid or key mismatch.
        """
        if not checkpoint.signature:
            raise CheckpointVerificationError("checkpoint_missing_signature")

        try:
            sig_bytes = checkpoint.raw_signature_bytes()
        except CheckpointVerificationError:
            return False

        # Resolve public key
        public_key = None
        if public_key_pem is not None:
            try:
                public_key = serialization.load_pem_public_key(public_key_pem)
            except Exception as exc:
                raise CheckpointVerificationError("invalid_public_key_pem") from exc
        else:
            kid = f"{checkpoint.key_id}#{checkpoint.key_version}"
            if kid in self._key_cache:
                public_key = self._key_cache[kid]
            elif self._resolver is not None:
                resolved_pem = self._resolver(checkpoint.key_id, checkpoint.key_version)
                if resolved_pem:
                    public_key = serialization.load_pem_public_key(resolved_pem)
                    self._key_cache[kid] = public_key

        if public_key is None:
            raise CheckpointVerificationError(
                f"public_key_unavailable_for_{checkpoint.key_id}#{checkpoint.key_version}"
            )

        data = checkpoint.canonical_bytes()

        if checkpoint.algorithm == "RSASSA_PSS_SHA_256":
            if not isinstance(public_key, rsa.RSAPublicKey):
                raise CheckpointVerificationError("algorithm_key_type_mismatch")
            try:
                public_key.verify(
                    sig_bytes,
                    data,
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                    hashes.SHA256(),
                )
                return True
            except InvalidSignature:
                return False
        elif checkpoint.algorithm == "ECDSA_SHA_256":
            if not isinstance(public_key, ec.EllipticCurvePublicKey):
                raise CheckpointVerificationError("algorithm_key_type_mismatch")
            try:
                public_key.verify(
                    sig_bytes,
                    data,
                    ec.ECDSA(hashes.SHA256()),
                )
                return True
            except InvalidSignature:
                return False
        else:
            raise CheckpointVerificationError(f"unsupported_algorithm_{checkpoint.algorithm}")


class FakeAuditSigner(AuditCheckpointSigner):
    """
    In-memory development test double for asymmetric checkpoint signing.
    Strictly forbidden in staging and production environments.
    """

    def __init__(
        self,
        key_id: str = "fake-audit-signer-dev",
        key_version: str = "v1",
        algorithm: str = "RSASSA_PSS_SHA_256",
        app_env: str = "development",
    ) -> None:
        if app_env in HARDENED_ENVIRONMENTS:
            raise CheckpointConfigurationError("fake_audit_signer_forbidden_outside_development", setting="app_env")

        self.key_id = key_id
        self.key_version = key_version
        self.algorithm = algorithm
        self.app_env = app_env

        # Key storage: map key_version -> (private_key, public_key_pem)
        self._keys: Dict[str, Tuple[rsa.RSAPrivateKey, bytes]] = {}
        self._simulate_outage = False
        self._simulate_corrupt_signature = False

        self._ensure_key_pair(self.key_version)

    def _ensure_key_pair(self, version: str) -> Tuple[rsa.RSAPrivateKey, bytes]:
        if version not in self._keys:
            priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            pub_pem = priv.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            self._keys[version] = (priv, pub_pem)
        return self._keys[version]

    def rotate_key(self, new_version: str, new_key_id: Optional[str] = None) -> None:
        """Rotate the active signing key to a new version."""
        if new_key_id:
            self.key_id = new_key_id
        self.key_version = new_version
        self._ensure_key_pair(self.key_version)

    def simulate_outage(self, active: bool = True) -> None:
        """Simulate KMS service unavailability."""
        self._simulate_outage = active

    def simulate_corrupt_signature(self, active: bool = True) -> None:
        """Simulate generating invalid signatures for test tamper detection."""
        self._simulate_corrupt_signature = active

    def sign(self, checkpoint: AuditCheckpoint) -> AuditCheckpoint:
        if self._simulate_outage:
            raise CheckpointSigningError("kms_service_unavailable")

        priv, _ = self._ensure_key_pair(self.key_version)
        data = checkpoint.canonical_bytes()

        sig = priv.sign(
            data,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

        if self._simulate_corrupt_signature:
            corrupted = bytearray(sig)
            corrupted[-1] ^= 0xFF
            sig = bytes(corrupted)

        return checkpoint.with_signature(sig)

    def get_public_key(self, key_id: Optional[str] = None, key_version: Optional[str] = None) -> bytes:
        if self._simulate_outage:
            raise CheckpointSigningError("kms_service_unavailable")
        ver = key_version or self.key_version
        if ver not in self._keys:
            raise CheckpointSigningError(f"key_version_not_found_{ver}")
        _, pub_pem = self._keys[ver]
        return pub_pem

    def get_key_id(self) -> str:
        return self.key_id

    def get_key_version(self) -> str:
        return self.key_version
