"""
AES-256-GCM envelope encryption and packaging (Team Member 3, Phase 4B).

Adheres to:
- PRD SEC-005: Authenticated AES-256-GCM encryption at rest.
- Cryptographic binding: Authenticates tenant identity and logical URI as Associated Authenticated Data (AAD)
  to prevent ciphertext substitution across tenants, documents, or artifact types.
- Key protection: Ephemeral Data Encryption Keys (DEKs) wrapped by KMS Key Encryption Keys (KEKs).
- Clean memory handling: Plaintext key material is wiped immediately after cipher operations.
"""

from __future__ import annotations

import base64
import json
import os
import struct
from typing import Any, Dict, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from doc_extractor.exceptions import CrossTenantStorageAccessError
from doc_extractor.infrastructure.storage.errors import ObjectStorageError
from doc_extractor.infrastructure.storage.kms.base import KmsProvider

ENVELOPE_MAGIC = b"DOCE"
ENVELOPE_VERSION = 1
ALGORITHM = "AES-256-GCM"
HEADER_PREFIX_FORMAT = ">4sHI"  # magic (4s), version (H, uint16), header_len (I, uint32)
HEADER_PREFIX_LEN = struct.calcsize(HEADER_PREFIX_FORMAT)  # 10 bytes


def compute_aad(tenant_id: str, logical_uri: str, algorithm: str = ALGORITHM) -> bytes:
    """
    Constructs deterministic Associated Authenticated Data (AAD).
    Cryptographically binds ciphertext to the exact tenant and logical object path.
    """
    return f"tenant:{tenant_id}|uri:{logical_uri}|alg:{algorithm}".encode("utf-8")


def is_envelope_encrypted(data: bytes) -> bool:
    """Checks whether binary data begins with the valid DOCE envelope magic."""
    return len(data) >= HEADER_PREFIX_LEN and data[:4] == ENVELOPE_MAGIC


def wipe_key_memory(key: bytearray | bytes) -> None:
    """Overwrites sensitive key material in memory if mutable."""
    if isinstance(key, bytearray):
        for i in range(len(key)):
            key[i] = 0


def encrypt_envelope(
    plaintext: bytes,
    tenant_id: str,
    logical_uri: str,
    kms_provider: KmsProvider,
    content_type: str = "application/octet-stream",
    kek_id: Optional[str] = None,
) -> bytes:
    """
    Encrypts plaintext with a fresh AES-256 DEK, wraps the DEK under the KMS KEK,
    and serializes the result into a binary envelope container.
    """
    enc_context = {"tenant_id": tenant_id, "uri": logical_uri}
    datakey = kms_provider.generate_data_key(kek_id=kek_id, encryption_context=enc_context)

    # Convert plaintext key to mutable bytearray for secure wiping
    raw_key = bytearray(datakey.plaintext_key)
    try:
        cipher = AESGCM(bytes(raw_key))
        nonce = os.urandom(12)
        aad = compute_aad(tenant_id, logical_uri, ALGORITHM)
        ciphertext_and_tag = cipher.encrypt(nonce, plaintext, aad)
    finally:
        wipe_key_memory(raw_key)

    header_dict = {
        "v": ENVELOPE_VERSION,
        "alg": ALGORITHM,
        "kek_id": datakey.kek_id,
        "key_ver": datakey.key_version,
        "edek": base64.b64encode(datakey.encrypted_key).decode("ascii"),
        "iv": base64.b64encode(nonce).decode("ascii"),
        "tenant_id": tenant_id,
        "uri": logical_uri,
        "mime": content_type,
    }
    header_bytes = json.dumps(header_dict, separators=(",", ":"), sort_keys=True).encode("utf-8")

    prefix = struct.pack(HEADER_PREFIX_FORMAT, ENVELOPE_MAGIC, ENVELOPE_VERSION, len(header_bytes))
    return prefix + header_bytes + ciphertext_and_tag


def decrypt_envelope(
    container: bytes,
    expected_tenant_id: str,
    expected_uri: str,
    kms_provider: KmsProvider,
) -> Tuple[bytes, Dict[str, Any]]:
    """
    Parses and verifies binary envelope, unwraps DEK via KMS, checks AAD tags,
    and returns decrypted plaintext and envelope metadata.
    """
    if len(container) < HEADER_PREFIX_LEN:
        raise ObjectStorageError("envelope_too_short")

    magic, version, header_len = struct.unpack(HEADER_PREFIX_FORMAT, container[:HEADER_PREFIX_LEN])
    if magic != ENVELOPE_MAGIC:
        raise ObjectStorageError("invalid_envelope_magic")
    if version != ENVELOPE_VERSION:
        raise ObjectStorageError(f"unsupported_envelope_version_{version}")

    header_end = HEADER_PREFIX_LEN + header_len
    if len(container) < header_end:
        raise ObjectStorageError("corrupt_envelope_header_truncated")

    try:
        header_dict = json.loads(container[HEADER_PREFIX_LEN:header_end].decode("utf-8"))
    except Exception as exc:
        raise ObjectStorageError("corrupt_envelope_header_json") from exc

    # Cross-tenant and URI substitution checks
    stored_tenant = header_dict.get("tenant_id")
    if stored_tenant != expected_tenant_id:
        raise CrossTenantStorageAccessError(
            details={
                "reason": "tenant_mismatch_in_ciphertext",
                "expected_tenant": expected_tenant_id,
            }
        )

    stored_uri = header_dict.get("uri")
    if stored_uri != expected_uri:
        raise ObjectStorageError(
            "object_uri_substitution_detected",
            details={"reason": "uri_mismatch_in_envelope"},
        )

    edek_bytes = base64.b64decode(header_dict["edek"])
    nonce = base64.b64decode(header_dict["iv"])
    ciphertext_and_tag = container[header_end:]

    enc_context = {"tenant_id": expected_tenant_id, "uri": expected_uri}
    plaintext_dek = kms_provider.decrypt_data_key(
        encrypted_key=edek_bytes,
        kek_id=header_dict.get("kek_id"),
        key_version=header_dict.get("key_ver"),
        encryption_context=enc_context,
    )

    raw_key = bytearray(plaintext_dek)
    try:
        cipher = AESGCM(bytes(raw_key))
        aad = compute_aad(expected_tenant_id, expected_uri, header_dict.get("alg", ALGORITHM))
        plaintext = cipher.decrypt(nonce, ciphertext_and_tag, aad)
        return plaintext, header_dict
    except InvalidTag as exc:
        raise ObjectStorageError(
            "ciphertext_authentication_failed",
            details={"reason": "aes_gcm_tag_mismatch_or_tampered"},
        ) from exc
    finally:
        wipe_key_memory(raw_key)


def rewrap_envelope(
    container: bytes,
    target_kek_id: str,
    kms_provider: KmsProvider,
    target_version: Optional[str] = None,
) -> bytes:
    """
    Re-encrypts the wrapped DEK under a new KEK or version WITHOUT decrypting the underlying data payload.
    Preserves existing ciphertext and authentication tags intact.
    """
    if len(container) < HEADER_PREFIX_LEN:
        raise ObjectStorageError("envelope_too_short")

    magic, version, header_len = struct.unpack(HEADER_PREFIX_FORMAT, container[:HEADER_PREFIX_LEN])
    if magic != ENVELOPE_MAGIC:
        raise ObjectStorageError("invalid_envelope_magic")

    header_end = HEADER_PREFIX_LEN + header_len
    header_dict = json.loads(container[HEADER_PREFIX_LEN:header_end].decode("utf-8"))

    edek_bytes = base64.b64decode(header_dict["edek"])
    enc_context = {"tenant_id": header_dict["tenant_id"], "uri": header_dict["uri"]}

    new_datakey = kms_provider.rewrap_data_key(
        encrypted_key=edek_bytes,
        source_kek_id=header_dict.get("kek_id"),
        target_kek_id=target_kek_id,
        source_version=header_dict.get("key_ver"),
        target_version=target_version,
        encryption_context=enc_context,
    )

    header_dict["kek_id"] = new_datakey.kek_id
    header_dict["key_ver"] = new_datakey.key_version
    header_dict["edek"] = base64.b64encode(new_datakey.encrypted_key).decode("ascii")

    new_header_bytes = json.dumps(header_dict, separators=(",", ":"), sort_keys=True).encode("utf-8")
    new_prefix = struct.pack(HEADER_PREFIX_FORMAT, ENVELOPE_MAGIC, ENVELOPE_VERSION, len(new_header_bytes))
    ciphertext_and_tag = container[header_end:]

    return new_prefix + new_header_bytes + ciphertext_and_tag
