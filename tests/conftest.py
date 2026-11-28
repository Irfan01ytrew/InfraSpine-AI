"""Test setup: a dedicated PostgreSQL test database, migrated with Alembic, and a fake
provider adapter so tests never make network calls.

The test database defaults to DATABASE_URL's database name + "_test", and can be
overridden with TEST_DATABASE_URL. Its name must end in "_test" and it must not
be the configured application database. Same-database tests require an explicit
uniquely named development schema.
"""

import asyncio
import base64
import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from app.db.testing import TEST_SCHEMA_RESET_SQL, validate_test_database_target
from sqlalchemy.engine import make_url


def _test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    from app.core.config import DatabaseSettings

    url = make_url(DatabaseSettings().database_url.get_secret_value())
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


def _configured_database_url():
    """Read the application target only for an equality check; never include it in errors or logs."""
    from app.core.config import DatabaseSettings

    try:
        return make_url(DatabaseSettings().database_url.get_secret_value())
    except Exception:
        # No usable DATABASE_URL: the module refuses to run below. DATABASE_URL is required even with an explicit
        # TEST_DATABASE_URL, so the tests can prove they never touch the configured application database.
        return None


TEST_DATABASE_URL = _test_database_url()
TEST_SCHEMA = os.environ.get("INFASPINE_TEST_SCHEMA")
configured_url = _configured_database_url()
if configured_url is None:
    raise RuntimeError("Set DATABASE_URL to the application database and TEST_DATABASE_URL to a separate *_test database.")
dotenv = {}
dotenv_path = Path(".env")
if dotenv_path.exists():
    for row in dotenv_path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = row.partition("=")
        if separator:
            dotenv[key.strip()] = value.strip().strip('"\'')
shared_development_schema_allowed = (
    TEST_SCHEMA is not None
    and re.fullmatch(r"infraspine_test_[0-9a-f]{12}", TEST_SCHEMA) is not None
    and os.environ.get("TEST_DATABASE_ALLOW_SHARED_SCHEMA") == "1"
    and (os.environ.get("APP_ENV") or dotenv.get("APP_ENV")) == "development"
)
validate_test_database_target(
    configured_url.render_as_string(hide_password=True),
    TEST_DATABASE_URL,
    test_schema=TEST_SCHEMA,
    shared_development_schema_allowed=shared_development_schema_allowed,
)

TEST_MASTER_KEY = base64.b64encode(b"k" * 32).decode()
RAG_TEST_DIR = Path(tempfile.mkdtemp(prefix="infraspine-rag-test-"))
os.environ.update(
    RAG_DATA_DIR=str(RAG_TEST_DIR),
    RAG_WORKER_ENABLED="false",  # tests drive the worker explicitly, step by step
    RAG_INGESTION_MODE="embedded",  # ...in the API process, which applies results to its index directly
    RAG_INDEX_SYNC_SECONDS="0",  # index sync is driven explicitly by the tests that need it
    RAG_PARSE_ISOLATION="false",  # parser child processes are exercised by test_ingestion_worker.py
    RAG_MIN_SIMILARITY="0.15",  # tuned for the fake bag-of-words embedder below
    RAG_RETRY_BASE_SECONDS="0",
    RAG_MAX_UPLOAD_MB="1",
    RAG_ADMIN_ONLY_UPLOADS="false",  # most tests exercise per-user permissions; test_rag covers the admin-only default
    RAG_TOP_K="3",
    DATABASE_URL=TEST_DATABASE_URL,
    APP_ENV="testing",
    MASTER_ENCRYPTION_KEY=TEST_MASTER_KEY,
    MASTER_ENCRYPTION_KEY_ID="v1",
    SESSION_COOKIE_SECURE="false",
    LOGIN_MAX_FAILED_ATTEMPTS="5",
    LOG_DIR="",  # tests log to stdout only
    RAG_OFFICE_CONVERTER="no-libreoffice-in-tests",  # a path that is not a file: no converter unless a test sets one
)

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.db.models import User  # noqa: E402
from app.db.session import test_engine_options  # noqa: E402
from app.main import app  # noqa: E402
from app.providers import registry  # noqa: E402
from app.rag import components as rag_components  # noqa: E402
from app.schemas.chat import ChatResponse  # noqa: E402
from app.services.llm import ClientParams, ProviderVerificationError  # noqa: E402

ADMIN_EMAIL = "admin@example.com"
USER_EMAIL = "user@example.com"
USER2_EMAIL = "user2@example.com"
PASSWORD = "correct-horse-battery-staple"
VALID_KEY_1 = 'sk' + '-test-valid-key-number-one-1111'
VALID_KEY_2 = 'sk' + '-test-valid-key-number-two-2222'
INVALID_KEY = 'sk' + '-test-invalid-key-000000000000'
CSRF = {"X-Requested-With": "test"}
_PASSWORD_HASH = hash_password(PASSWORD)  # Argon2 is deliberately slow; hash once per run


def run_db(fn):
    """Run `await fn(conn)` in a fresh engine/loop, independent of the app's loop."""

    async def _run():
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **test_engine_options())
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    cfg.attributes["configure_logger"] = False
    if TEST_SCHEMA:
        async def create_isolated_schema():
            engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(f'CREATE SCHEMA "{TEST_SCHEMA}"'))
            finally:
                await engine.dispose()

        asyncio.run(create_isolated_schema())
    else:
        async def clear_guarded_test_rows():
            engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool, **test_engine_options())
            try:
                async with engine.begin() as conn:
                    await conn.execute(text(TEST_SCHEMA_RESET_SQL))
            finally:
                await engine.dispose()

        # Guarded lifecycle migrations retain data in production. Clear only these dedicated *_test
        # targets before the fixture's deliberate schema reset so repeated local runs remain safe.
        asyncio.run(clear_guarded_test_rows())
        try:
            command.downgrade(cfg, "base")  # dedicated *_test databases only
        except Exception:
            pass
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean_tables(migrated_database):
    run_db(lambda conn: conn.execute(text(TEST_SCHEMA_RESET_SQL)))

    async def _seed(conn):
        for email, role in ((ADMIN_EMAIL, "admin"), (USER_EMAIL, "user"), (USER2_EMAIL, "user")):
            await conn.execute(
                User.__table__.insert().values(email=email, role=role, password_hash=_PASSWORD_HASH)
            )

    run_db(_seed)
    shutil.rmtree(RAG_TEST_DIR, ignore_errors=True)  # uploads + vector index start empty per test


class FakeProviderClient:
    """Stands in for the real network adapter. Class-level state is reset per test."""

    valid_keys: set[str] = set()
    revoked_keys: set[str] = set()
    available_models: set[str] = set()
    verify_delay: float = 0.0
    built: list["FakeProviderClient"] = []

    def __init__(self, params: ClientParams) -> None:
        self.params = params
        self.closed = False
        self.provider = "deepseek"
        FakeProviderClient.built.append(self)

    async def verify(self, timeout_seconds: float) -> None:
        if self.verify_delay:
            await asyncio.sleep(self.verify_delay)
        key = self.params.api_key
        if key not in self.valid_keys or key in self.revoked_keys:
            raise ProviderVerificationError("invalid_credentials")
        if self.params.model not in self.available_models:
            raise ProviderVerificationError("model_unavailable")

    calls: list[tuple[str | None, str]] = []  # (system_prompt, message) for every chat call
    rag_reply = None  # callable(message) -> reply text, used when a system prompt is given
    chat_error: Exception | None = None

    async def chat(self, message: str, *, system_prompt: str | None = None) -> ChatResponse:
        FakeProviderClient.calls.append((system_prompt, message))
        if self.chat_error is not None:
            raise self.chat_error
        if system_prompt is not None and self.rag_reply is not None:
            return ChatResponse(reply=FakeProviderClient.rag_reply(message), model=self.params.model, provider="deepseek")
        return ChatResponse(
            reply=f"key={self.params.api_key[-4:]} temp={self.params.settings['temperature']}",
            model=self.params.model,
            provider="deepseek",
        )

    # Description returned for an image; None = the model refuses images; an exception instance is raised.
    vision_reply: str | Exception | None = None
    image_calls: list[bytes] = []

    async def describe_image(self, image_png: bytes, instruction: str) -> ChatResponse:
        from app.services.llm import VisionUnsupportedError

        FakeProviderClient.image_calls.append(image_png)
        if FakeProviderClient.vision_reply is None:
            raise VisionUnsupportedError()
        if isinstance(FakeProviderClient.vision_reply, Exception):
            raise FakeProviderClient.vision_reply
        return ChatResponse(reply=FakeProviderClient.vision_reply, model=self.params.model, provider="deepseek")

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    FakeProviderClient.valid_keys = {VALID_KEY_1, VALID_KEY_2}
    FakeProviderClient.revoked_keys = set()
    FakeProviderClient.available_models = {"deepseek-flash", "deepseek-v4-pro"}
    FakeProviderClient.verify_delay = 0.0
    FakeProviderClient.built = []
    FakeProviderClient.calls = []
    FakeProviderClient.rag_reply = None
    FakeProviderClient.chat_error = None
    FakeProviderClient.vision_reply = None
    FakeProviderClient.image_calls = []
    monkeypatch.setitem(
        registry.PROVIDERS, "deepseek", replace(registry.DEEPSEEK, client_factory=FakeProviderClient)
    )
    return FakeProviderClient


@pytest.fixture
def client():
    with TestClient(app, headers=CSRF) as test_client:  # context manager runs the lifespan
        yield test_client


def login(client: TestClient, email: str = ADMIN_EMAIL, password: str = PASSWORD):
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


@pytest.fixture
def admin_client(client):
    login(client, ADMIN_EMAIL)
    return client


_STOPWORDS = {"the", "and", "for", "are", "with", "what", "which", "this", "that", "from", "how", "does", "our", "you"}


class FakeEmbedder:
    """Deterministic bag-of-words embedder: texts sharing words get similar vectors.
    Lets retrieval tests make exact assertions without downloading a model."""

    model_name = "test/fake-bow-embedder"
    dimension = 384
    fail = False

    @property
    def available_locally(self) -> bool:
        return True

    @property
    def is_loaded(self) -> bool:
        return True

    def health_check(self) -> None:
        if FakeEmbedder.fail:
            raise RuntimeError("embedding backend unavailable")

    def warmup(self) -> None:
        return None

    def embed(self, texts: list[str]) -> np.ndarray:
        if FakeEmbedder.fail:
            raise RuntimeError("embedding backend unavailable")
        out = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                if len(word) >= 3 and word not in _STOPWORDS:
                    out[row, int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dimension] += 1.0
            norm = np.linalg.norm(out[row])
            if norm:
                out[row] /= norm
        return out

    def embed_local(self, texts: list[str]) -> np.ndarray:
        return self.embed(texts)


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    FakeEmbedder.fail = False
    monkeypatch.setattr(rag_components, "build_embedder", lambda settings: FakeEmbedder())
    return FakeEmbedder
