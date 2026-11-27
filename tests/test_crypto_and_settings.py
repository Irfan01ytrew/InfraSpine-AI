import base64
import uuid

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.crypto import CredentialCipher, DecryptionError, EncryptedValue, credential_aad

KEY = b"k" * 32
SECRET = 'sk' + '-super-secret-value-123456'


def _aad() -> bytes:
    return credential_aad(uuid.UUID(int=1), "deepseek")


def test_round_trip_and_ciphertext_hides_plaintext():
    cipher = CredentialCipher(KEY, "v1")
    encrypted = cipher.encrypt(SECRET, associated_data=_aad())
    assert SECRET.encode() not in encrypted.ciphertext
    assert cipher.decrypt(encrypted, associated_data=_aad()) == SECRET


def test_nonce_is_unique_per_encryption():
    cipher = CredentialCipher(KEY, "v1")
    assert cipher.encrypt(SECRET, associated_data=_aad()).nonce != cipher.encrypt(SECRET, associated_data=_aad()).nonce


def test_tampered_ciphertext_is_rejected():
    cipher = CredentialCipher(KEY, "v1")
    enc = cipher.encrypt(SECRET, associated_data=_aad())
    tampered = EncryptedValue(bytes([enc.ciphertext[0] ^ 1]) + enc.ciphertext[1:], enc.nonce, enc.key_id)
    with pytest.raises(DecryptionError):
        cipher.decrypt(tampered, associated_data=_aad())


def test_ciphertext_copied_to_another_row_is_rejected():
    cipher = CredentialCipher(KEY, "v1")
    enc = cipher.encrypt(SECRET, associated_data=_aad())
    with pytest.raises(DecryptionError):
        cipher.decrypt(enc, associated_data=credential_aad(uuid.UUID(int=2), "deepseek"))


def test_wrong_master_key_or_key_id_is_rejected():
    enc = CredentialCipher(KEY, "v1").encrypt(SECRET, associated_data=_aad())
    with pytest.raises(DecryptionError):
        CredentialCipher(b"x" * 32, "v1").decrypt(enc, associated_data=_aad())
    with pytest.raises(DecryptionError):
        CredentialCipher(KEY, "v2").decrypt(enc, associated_data=_aad())


def _settings(**overrides) -> Settings:
    values = {
        "database_url": "postgresql+asyncpg://u@localhost/db",
        "master_encryption_key": base64.b64encode(KEY).decode(),
        **overrides,
    }
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize("bad_key", ["not-base64!!", base64.b64encode(b"short").decode()])
def test_settings_reject_invalid_master_key(bad_key):
    with pytest.raises(ValidationError):
        _settings(master_encryption_key=bad_key)


def test_master_key_is_masked_in_settings_repr():
    settings = _settings()
    assert base64.b64encode(KEY).decode() not in repr(settings)


def test_production_requires_secure_cookies():
    with pytest.raises(ValidationError, match="SESSION_COOKIE_SECURE"):
        _settings(app_env="production", session_cookie_secure=False)


def test_faiss_is_default_and_qdrant_is_an_explicit_alternative():
    assert _settings().rag_vector_store == "faiss"
    assert _settings(rag_vector_store="faiss").rag_vector_store == "faiss"
    assert _settings(rag_vector_store="qdrant").rag_vector_store == "qdrant"
    for unsupported in ("pgvector", "milvus"):
        with pytest.raises(ValidationError):
            _settings(rag_vector_store=unsupported)


@pytest.mark.parametrize("origins", ["http://localhost:11434/v1", "ftp://llm.corp", "https://user@llm.corp"])
def test_invalid_trusted_provider_origins_fail_at_startup(origins):
    with pytest.raises(ValidationError, match="trusted provider origin|without a path"):
        _settings(provider_trusted_private_origins=origins)


def test_custom_provider_credentials_bind_the_endpoint_origin():
    cipher = CredentialCipher(KEY, "v1")
    enc = cipher.encrypt(SECRET, associated_data=credential_aad(uuid.UUID(int=1), "custom-x", "https://a.example:443"))
    assert cipher.decrypt(enc, associated_data=credential_aad(uuid.UUID(int=1), "custom-x", "https://a.example:443")) == SECRET
    for other in (None, "https://b.example:443", "https://a.example:8443"):
        with pytest.raises(DecryptionError):
            cipher.decrypt(enc, associated_data=credential_aad(uuid.UUID(int=1), "custom-x", other))


def test_qdrant_without_an_explicit_index_role_runs_as_the_single_owner():
    assert _settings().rag_index_role == "auto"
    assert _settings(rag_vector_store="qdrant").rag_index_role == "owner"
    with pytest.raises(ValidationError, match="RAG_INDEX_ROLE"):
        _settings(rag_vector_store="qdrant", rag_index_role="auto")
