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
import base64
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

    @property
    def key_id(self) -> str:
        """Id of the key this cipher encrypts with (stored on each ciphertext)."""
        return self._key_id

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

    def encrypt_sealed_json(self, plaintext: str, *, associated_data: bytes) -> dict:
        """Seal a JSON string as the {"ct", "iv", "kid"} dict used by the
        integration / workflow / agent-run stores (same shape as
        app/integrations/security.py: seal)."""
        encrypted = self.encrypt(plaintext, associated_data=associated_data)
        return {
            "ct": base64.b64encode(encrypted.ciphertext).decode(),
            "iv": base64.b64encode(encrypted.nonce).decode(),
            "kid": encrypted.key_id,
        }

    def decrypt_sealed_json(self, value: dict, *, associated_data: bytes) -> str:
        """Unseal a {"ct", "iv", "kid"} dict. Raises DecryptionError when the
        payload is malformed, sealed under an unknown key, or tampered with."""
        try:
            encrypted = EncryptedValue(
                ciphertext=base64.b64decode(value["ct"], validate=True),
                nonce=base64.b64decode(value["iv"], validate=True),
                key_id=value["kid"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DecryptionError("Sealed JSON payload is malformed") from exc
        return self.decrypt(encrypted, associated_data=associated_data)


class RotatingCredentialCipher(CredentialCipher):
    """A cipher that decrypts with the current key or any retired key.

    This is what makes master-key rotation safe: deploy the new key as
    current with the old key retired, re-encrypt stored values at leisure
    (see app/security/key_rotation.py), then drop the retired key. At no
    point is a stored value undecryptable. New encryptions always use the
    current key, so the retired key naturally drains away.
    """

    def __init__(self, master_key: bytes, key_id: str, retired: dict[str, bytes] | None = None) -> None:
        super().__init__(master_key, key_id)
        self._retired: dict[str, AESGCM] = {}
        for retired_id, retired_key in (retired or {}).items():
            if retired_id == key_id:
                raise ValueError("A retired key id must differ from the current key id")
            if len(retired_key) != 32:
                raise ValueError("A retired master key must be 32 bytes for AES-256-GCM")
            self._retired[retired_id] = AESGCM(retired_key)

    @property
    def retired_key_ids(self) -> tuple[str, ...]:
        """Key ids that can still decrypt (old values), in insertion order."""
        return tuple(self._retired)

    def decrypt(self, value: EncryptedValue, *, associated_data: bytes) -> str:
        if value.key_id == self._key_id:
            return super().decrypt(value, associated_data=associated_data)
        aead = self._retired.get(value.key_id)
        if aead is None:
            raise DecryptionError(f"Ciphertext was encrypted with unknown key id {value.key_id!r}")
        try:
            return aead.decrypt(value.nonce, value.ciphertext, associated_data).decode("utf-8")
        except InvalidTag:
            raise DecryptionError("Ciphertext failed authentication") from None

    def reencrypt(self, value: EncryptedValue, *, associated_data: bytes) -> EncryptedValue:
        """Decrypt with whichever key sealed the value, then seal under the current key.

        A value already under the current key is returned unchanged (same
        object), so a rotation sweep never rewrites — or re-audits — rows
        that are already current.
        """
        if value.key_id == self._key_id:
            return value
        return self.encrypt(self.decrypt(value, associated_data=associated_data), associated_data=associated_data)


def rotating_cipher_from_settings(settings) -> RotatingCredentialCipher:
    """Build the app cipher from Settings, honouring the retired key pair.

    ``settings`` is duck-typed (master_key_bytes, master_encryption_key_id,
    master_previous_key_bytes, master_encryption_key_previous_id) so this
    stays importable without pulling in the settings module.
    """
    retired = {}
    previous = settings.master_previous_key_bytes
    if previous is not None:
        retired[settings.master_encryption_key_previous_id] = previous
    return RotatingCredentialCipher(
        settings.master_key_bytes, settings.master_encryption_key_id, retired
    )


def credential_aad(credential_id: object, provider: str, origin: str | None = None) -> bytes:
    """Associated data that binds a provider credential ciphertext to its row.

    Custom providers also bind the normalized endpoint origin (scheme://host:port) the key was verified
    against, so the ciphertext cannot be decrypted for any other destination. Built-in providers have a
    fixed endpoint in code and keep the original (v1) binding.
    """
    if origin is None:
        return f"infraspine:provider-credential:v1:{credential_id}:{provider}".encode()
    return f"infraspine:provider-credential:v2:{credential_id}:{provider}:{origin}".encode()
