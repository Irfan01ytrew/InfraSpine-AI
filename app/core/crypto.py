"""Authenticated encryption for credentials stored at rest.

AES-256-GCM provides confidentiality and integrity: any modification of the
ciphertext, nonce or associated data makes decryption fail. The associated
data binds each ciphertext to its database row, so an encrypted key copied
into another row (or another provider) cannot be decrypted there.

The master key never touches the database; it is supplied via environment or
secret manager. `key_id` is stored with each ciphertext so the master key can
be rotated in the future without ambiguity.
"""

import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12


class DecryptionError(Exception):
    """Ciphertext could not be authenticated (tampered, wrong key or wrong row)."""


@dataclass(frozen=True)
class EncryptedValue:
    ciphertext: bytes
    nonce: bytes
    key_id: str


class CredentialCipher:
    def __init__(self, master_key: bytes, key_id: str) -> None:
        if len(master_key) != 32:
            raise ValueError("Master key must be 32 bytes for AES-256-GCM")
        self._aead = AESGCM(master_key)
        self._key_id = key_id

    def encrypt(self, plaintext: str, *, associated_data: bytes) -> EncryptedValue:
        nonce = os.urandom(NONCE_BYTES)  # 96-bit random nonce, never reused with this key in practice
        ciphertext = self._aead.encrypt(nonce, plaintext.encode("utf-8"), associated_data)
        return EncryptedValue(ciphertext=ciphertext, nonce=nonce, key_id=self._key_id)

    def decrypt(self, value: EncryptedValue, *, associated_data: bytes) -> str:
        if value.key_id != self._key_id:
            raise DecryptionError(f"Ciphertext was encrypted with unknown key id {value.key_id!r}")
        try:
            return self._aead.decrypt(value.nonce, value.ciphertext, associated_data).decode("utf-8")
        except InvalidTag:
            raise DecryptionError("Ciphertext failed authentication") from None


def credential_aad(credential_id: object, provider: str, origin: str | None = None) -> bytes:
    """Associated data that binds a provider credential ciphertext to its row.

    Custom providers also bind the normalized endpoint origin (scheme://host:port) the key was verified
    against, so the ciphertext cannot be decrypted for any other destination. Built-in providers have a
    fixed endpoint in code and keep the original (v1) binding.
    """
    if origin is None:
        return f"infraspine:provider-credential:v1:{credential_id}:{provider}".encode()
    return f"infraspine:provider-credential:v2:{credential_id}:{provider}:{origin}".encode()
