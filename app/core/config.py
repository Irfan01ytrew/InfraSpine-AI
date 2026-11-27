"""Application settings loaded from environment variables and `.env`.

Infrastructure settings and secrets that must live *outside* the database are
configured here, including the application database URL, master encryption key,
and optional read-only external analysis source. AI provider credentials and
model choices live in PostgreSQL and are managed at runtime through the admin API.
"""

import base64
import binascii
import math
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_CONFIG = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


class DatabaseSettings(BaseSettings):
    """The subset needed by migrations, so Alembic never needs the master key."""

    model_config = _ENV_CONFIG

    database_url: SecretStr


class Settings(DatabaseSettings):
    # Application
    app_name: str = "InfraSpine AI"
    app_env: Literal["development", "testing", "staging", "production"] = "development"
    # Public HTTPS origin used when showing the Meta webhook callback URL. Empty uses the request host.
    whatsapp_webhook_base_url: str = Field(default="", max_length=1000)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_pool_max_overflow: int = Field(default=10, ge=0, le=50)
    database_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    database_pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)

    # Credential encryption (AES-256-GCM). Base64 of exactly 32 random bytes.
    master_encryption_key: SecretStr
    master_encryption_key_id: str = Field(default="v1", min_length=1, max_length=32)

    # LLM runtime
    # When enabled, content-bearing requests may use only explicitly trusted private custom endpoints.
    # Built-in cloud providers (including Gemini) remain configured but are not selectable for content.
    llm_local_only: bool = False
    llm_timeout_seconds: float = Field(default=120.0, gt=0, le=900)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    # Provider id of a configured provider (with an active key) that answers when the active provider fails
    # with a balance, rate-limit, timeout or availability error, e.g. a local Ollama endpoint. Empty = no fallback.
    llm_fallback_provider: str = Field(default="", max_length=64)
    provider_verify_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    # Custom providers may only use public HTTPS endpoints, unless their exact origin is listed here
    # (comma-separated scheme://host[:port], e.g. an on-premises model server). Metadata/link-local
    # addresses stay blocked even for listed origins.
    provider_trusted_private_origins: str = ""
    # Webhook destinations follow the same rule: public HTTPS only, unless the exact origin is listed here.
    webhook_trusted_private_origins: str = ""

    # Authentication
    session_ttl_minutes: int = Field(default=480, ge=5, le=10080)
    session_cookie_secure: bool = True
    login_max_failed_attempts: int = Field(default=5, ge=1, le=100)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1440)
    # Operator-owned policy; app administrators cannot disable organization MFA.
    org_mfa_required: bool = False

    # Enterprise SSO (OIDC / SAML 2.0)
    sso_enabled: bool = False
    sso_provider_type: Literal["oidc", "saml"] = "oidc"
    sso_issuer_url: str = ""
    sso_client_id: str = ""
    sso_client_secret: SecretStr = SecretStr("")
    sso_authorization_endpoint: str = ""
    sso_token_endpoint: str = ""
    sso_userinfo_endpoint: str = ""
    sso_default_role: Literal["employee", "manager", "admin"] = "employee"
    sso_auto_provision: bool = True

    # Enterprise SCIM 2.0 Directory Sync
    scim_enabled: bool = False
    scim_bearer_token: SecretStr = SecretStr("")

    # Communication platforms (Slack & Microsoft Teams Bot Integrations)
    slack_enabled: bool = False
    slack_signing_secret: SecretStr = SecretStr("")
    slack_bot_token: SecretStr = SecretStr("")
    teams_enabled: bool = False
    teams_app_id: str = ""
    teams_app_password: SecretStr = SecretStr("")

    # Offline commercial licensing. The public key is base64-encoded raw Ed25519 bytes; the
    # corresponding signing key stays with the licensor and is never deployed to a customer.
    license_public_key: str = ""
    license_enforcement_enabled: bool = False


    # RAG (documents + retrieval)
    rag_data_dir: Path = Path("data")  # uploads/, vector_index/ and models/; never served over HTTP
    # Optional local text-to-image model. When configured, it is used as a private fallback if a cloud
    # image provider has no quota. Keep large model weights outside the source tree.
    rag_local_image_model_path: Path | None = None
    rag_local_image_cpu_threads: int = Field(default=6, ge=1, le=32)
    rag_local_image_steps: int = Field(default=4, ge=2, le=12)
    rag_local_image_size: int = Field(default=512, ge=256, le=512)
    rag_max_upload_mb: int = Field(default=20, ge=1, le=200)
    # Resumable local-folder imports bypass the browser upload cap, but stay bounded per source file.
    rag_import_max_file_mb: int = Field(default=4096, ge=1, le=65_536)
    # Scheduled folder synchronization can only read beneath this operator-managed root.
    rag_import_root: Path = Path("data/import")
    # Audio/video transcription is opt-in and offline. The model path must already contain a local CTranslate2 model.
    rag_media_enabled: bool = False
    rag_media_model_path: str = ""
    rag_media_device: Literal["cpu", "cuda"] = "cpu"
    rag_media_cpu_threads: int = Field(default=4, ge=1, le=64)
    rag_media_max_file_mb: int = Field(default=8192, ge=1, le=65_536)
    rag_media_max_duration_seconds: int = Field(default=14_400, ge=1, le=86_400)
    rag_media_segment_seconds: int = Field(default=300, ge=30, le=1800)
    # Video frames are sampled at bounded intervals and OCRed locally; no frames leave the deployment.
    rag_media_video_frame_ocr_enabled: bool = True
    # Short clips often show a sequence of meaningful states; sample frames every two seconds,
    # with the existing max-frame and document-unit budgets bounding longer videos.
    rag_media_video_frame_interval_seconds: int = Field(default=2, ge=1, le=3600)
    rag_media_video_max_frames: int = Field(default=300, ge=1, le=5000)
    # Live competitor/market research is off by default because queries are sent to Gemini's public search service.
    # Explicit live-web requests in chat may send the current question to the configured Gemini Search tool.
    # Kept opt-in for deployments; user intent is recorded in the audit log before the external request.
    rag_external_research_enabled: bool = False
    # Google Search grounding is preferred when the active Gemini credential/model supports it; otherwise
    # choose this SSRF-protected public HTML/RSS search provider first, then fall back to the others.
    rag_public_search_provider: Literal["auto", "duckduckgo", "bing_rss", "yahoo"] = "auto"
    # Legacy-browser capture is disabled unless an operator explicitly allowlists exact origins and a browser binary.
    rag_legacy_browser_enabled: bool = False
    rag_legacy_browser_allowed_origins: str = ""
    rag_legacy_browser_executable_path: Path | None = None
    rag_legacy_browser_timeout_seconds: int = Field(default=25, ge=5, le=60)
    rag_legacy_browser_max_requests: int = Field(default=40, ge=5, le=80)
    rag_legacy_browser_max_response_bytes: int = Field(default=8 * 1024 * 1024, ge=65_536, le=16 * 1024 * 1024)
    rag_legacy_browser_max_result_chars: int = Field(default=12_000, ge=1000, le=24_000)
    # Internal legacy sites require a separate operator opt-in. Loopback remains available for local QA only.
    rag_legacy_browser_allow_private_networks: bool = False
    # The document library is managed by administrators: only they add documents, and every user can ask about
    # the shared ones in any chat. False also lets regular users upload private documents for themselves.
    rag_admin_only_uploads: bool = True
    rag_max_pages: int = Field(default=500, ge=1, le=5000)  # pages, slides or sheets per file
    rag_max_extracted_chars: int = Field(default=2_000_000, ge=10_000)
    # Office files are ZIP packages: refuse ones that expand beyond this (ZIP bombs).
    rag_max_unpacked_mb: int = Field(default=256, ge=1, le=4096)
    rag_chunk_size: int = Field(default=800, ge=200, le=4000)  # characters
    rag_chunk_overlap: int = Field(default=150, ge=0, le=1000)  # characters
    rag_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    rag_embedding_dimension: int = Field(default=384, ge=8, le=4096)
    # Keep online model downloads a deliberate choice in customer deployments; local development keeps the
    # historical first-use download behavior unless the operator disables it.
    rag_embedding_allow_download: bool = True
    # Load the embedding model in the background when the API starts. Importing torch and the model takes
    # tens of seconds on a cold machine; without this the first question after a restart pays that cost.
    rag_embedding_warmup: bool = True
    rag_top_k: int = Field(default=5, ge=1, le=20)
    # A noise floor, not the relevance judge: the LLM decides sufficiency and must say INSUFFICIENT_EVIDENCE.
    rag_min_similarity: float = Field(default=0.2, ge=-1, le=1)
    # Hybrid retrieval (docs/hybrid-retrieval.md): PostgreSQL lexical matches fused with the semantic ones, and
    # at most `rag_max_chunks_per_document` chunks of one document while other documents have eligible ones.
    rag_hybrid_retrieval: bool = True
    rag_max_chunks_per_document: int = Field(default=3, ge=1, le=20)
    rag_lexical_candidates: int = Field(default=50, ge=5, le=500)
    # Query analysis: typo tolerance from the corpus vocabulary, and a follow-up takes the previous question as
    # context only when it does not name a topic of its own. No LLM call either way.
    rag_query_analysis: bool = True
    # Document discovery and hierarchical retrieval (docs/document-discovery.md): documents named by their filename,
    # title, headings or dates are found from per-document descriptors; documents are ranked before chunks; a found
    # document of up to `rag_small_document_chars` arrives whole, a larger one with the chunks around its hits.
    rag_hierarchical_retrieval: bool = True
    rag_discovery_max_documents: int = Field(default=3, ge=1, le=10)
    rag_small_document_chars: int = Field(default=4000, ge=0, le=50_000)
    # How numeric dates in filenames and document headers are read: "dmy" (06.07.2026 = 6 July) or "mdy".
    rag_date_order: Literal["dmy", "mdy"] = "dmy"

    # Finance / invoice flow (step E). Payments go through a payment connector with read-back verification.
    # "mock" is the in-process test bank (references start with MOCK-); it is refused in production unless
    # finance_allow_mock_in_production is set deliberately (demos). "http" is the generic bank/ERP REST adapter.
    finance_payment_connector: Literal["mock", "http"] = "mock"
    finance_payment_fallback: Literal["none", "mock", "http"] = "none"
    finance_bank_api_url: str = Field(default="", max_length=1000)
    finance_bank_api_token: SecretStr | None = None
    finance_allow_mock_in_production: bool = False
    finance_verify_slice: int = Field(default=25, ge=1, le=500)  # invoices per checkpointed slice
    finance_approval_ttl_hours: int = Field(default=24, ge=1, le=168)
    finance_max_invoice_age_days: int = Field(default=365, ge=1, le=3650)
    finance_max_batch_invoices: int = Field(default=1000, ge=1, le=10000)
    rag_max_context_chars: int = Field(default=12_000, ge=1000, le=200_000)
    # Optional operator-configured read-only business database analysis. Credentials remain in the
    # deployment environment; the API accepts structured operations over this explicit catalog only.
    rag_database_analysis_url: SecretStr | None = None
    # Optional file-backed SQLite reporting source. Its resolved path must stay under this
    # operator-managed root; the connector opens it with SQLite URI mode=ro.
    rag_database_analysis_sqlite_path: Path | None = None
    rag_database_analysis_sqlite_root: Path = Path("data/business")
    rag_database_analysis_catalog: str = Field(default="", max_length=32_768)
    # Maps internal department group UUIDs to operator-owned reporting row filters. Each value is
    # an array of {table, column, value} objects; the API validates these against the catalog.
    rag_database_analysis_department_grants: str = Field(default="", max_length=65_536)
    rag_database_analysis_source_label: str = Field(default="Business database", min_length=1, max_length=80)
    rag_database_analysis_statement_timeout_ms: int = Field(default=5000, ge=100, le=30_000)
    rag_database_analysis_max_rows: int = Field(default=100, ge=1, le=100)
    # True: when the documents don't cover a question, the model answers from general knowledge, labelled as such.
    # False: strict mode, answers only from the user's documents (otherwise "couldn't answer").
    rag_general_answers: bool = True
    # Budget for whole-document questions ("summarize this report"); larger documents are sampled evenly.
    rag_overview_max_chars: int = Field(default=40_000, ge=2000, le=400_000)
    # FAISS remains the default rollback provider. Qdrant is an explicit server-backed option with cold/on-disk
    # vectors and HNSW; changing providers requires a rebuild from PostgreSQL's stored embeddings.
    rag_vector_store: Literal["faiss", "qdrant"] = "faiss"
    rag_qdrant_url: str = "http://127.0.0.1:6333"
    rag_qdrant_api_key: SecretStr | None = None
    # Which API process writes this data directory's vector index files. "auto" (default) lets the first process own
    # process holds them. "auto" lets several API processes share one installation (e.g. uvicorn --workers N):
    # the first takes ownership, the others serve searches from an in-memory replica kept in step with
    # PostgreSQL and run no background workers. "replica" never takes ownership. Replicas require FAISS.
    rag_index_role: Literal["owner", "replica", "auto"] = "auto"
    # Image understanding: pictures in PDFs (charts, diagrams, screenshots) and uploaded PNG/JPEG images are
    # described by the active LLM provider when its model accepts images and an admin has authorized sending
    # document images to that provider (stored per provider); the descriptions are indexed like text.
    rag_image_max_per_document: int = Field(default=20, ge=1, le=200)
    rag_image_min_pixels: int = Field(default=96, ge=16, le=2000)  # smaller icons and logos are skipped
    # Uploaded images larger than this are refused before they are decoded (decompression bombs).
    rag_image_max_megapixels: int = Field(default=40, ge=1, le=200)
    # Document processing (parse, chunk, embed). "worker": a separate process started with `python -m app.worker`
    # does it and the API never parses files. "embedded": the API process runs the worker loop itself (single
    # process; simplest for small installations and development).
    rag_ingestion_mode: Literal["worker", "embedded"] = "worker"
    rag_worker_enabled: bool = True  # embedded mode only: run the worker loop inside the API process
    rag_worker_concurrency: int = Field(default=1, ge=1, le=32)  # documents processed in parallel per worker
    rag_worker_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    rag_worker_heartbeat_seconds: float = Field(default=10.0, ge=1, le=300)
    rag_worker_shutdown_grace_seconds: float = Field(default=30.0, ge=0, le=3600)
    # Durable read-only agent-run queue. Off by default so API-only deployments can run the separate CLI worker.
    agent_queue_worker_enabled: bool = False
    agent_queue_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    # Agent Studio runs are persisted before execution. Enable this only on the designated worker owner.
    agent_studio_worker_enabled: bool = False
    agent_studio_worker_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    # Transactional outbox dispatcher (retry, backoff, dead-letter). On by default for the index owner;
    # tests and replicas leave it off so they do not drain events as a side effect of starting the app.
    platform_outbox_worker_enabled: bool = True
    platform_outbox_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    agent_studio_run_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    # Optional exact provider/model rates, keyed as "provider:model". No rate means cost is unavailable.
    llm_model_pricing_json: dict[str, dict[str, float]] = Field(default_factory=dict)

    @field_validator("llm_model_pricing_json")
    @classmethod
    def validate_llm_model_pricing(cls, value: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
        for key, rates in value.items():
            if not isinstance(key, str) or ":" not in key or not key.split(":", 1)[1].strip():
                raise ValueError("LLM model pricing keys must use the form 'provider:model'.")
            for rate_name in ("input_per_1m_tokens_usd", "output_per_1m_tokens_usd"):
                rate = rates.get(rate_name)
                if rate is None or not math.isfinite(rate) or rate < 0:
                    raise ValueError(f"LLM model pricing requires a finite nonnegative {rate_name}.")
        return value
    # Optional SMTP transport for assignment deadline reminders. SMTP secrets stay outside PostgreSQL;
    # delivery is disabled by default and requires an authenticated TLS channel when enabled.
    personal_task_email_enabled: bool = False
    personal_task_smtp_host: str = ""
    personal_task_smtp_port: int = Field(default=587, ge=1, le=65535)
    personal_task_smtp_security: Literal["starttls", "ssl"] = "starttls"
    personal_task_smtp_username: str = ""
    personal_task_smtp_password: SecretStr | None = None
    personal_task_email_from: str = ""
    personal_task_smtp_timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    # Email approvers (pending, reminder, escalation) over the same SMTP transport. Off by default.
    approval_email_enabled: bool = False
    # Parse each file in a separate, killable process with these limits (recommended; required for the limits).
    rag_parse_isolation: bool = True
    rag_processing_timeout_seconds: int = Field(default=600, ge=5, le=86_400)
    # The cap applies to committed memory, which runs well above resident memory for Docling. A 100-page PDF
    # with 200 tables committed 3.6 GB (2.7 GB resident) and a scanned PDF 3.3 GB; at a 3072 MB cap they
    # crashed the parser (a failed native allocation, 0xC0000005) instead of failing cleanly. 0 = no limit.
    rag_parser_memory_mb: int = Field(default=4096, ge=0, le=65_536)
    # PDF, DOCX and PPTX try Docling first. Other formats, and a Docling failure, use the built-in parsers.
    rag_use_docling: bool = True
    # When enabled, locally cached RapidOCR OCRs PDF pages that have no extractable text. Model loading is offline.
    rag_ocr_enabled: bool = True
    # Cap OpenCV/ONNX thread creation per isolated parser process; tune with worker concurrency on CPU-limited hosts.
    rag_ocr_cpu_threads: int = Field(default=1, ge=1, le=32)
    # Also read Urdu/Arabic-script scans with RapidOCR's Arabic-script recognizer (arabic_PP-OCRv5_rec_mobile.onnx
    # must be in RapidOCR's models folder, plus the python-bidi package). Each page is read by both recognizers.
    rag_ocr_urdu: bool = False
    # Page images of Word, PowerPoint and Excel files are rendered through LibreOffice (converted to PDF once and
    # cached). Path to `soffice`; empty = look on PATH and in the standard install folders. Without it, only PDF
    # pages can be shown as images.
    rag_office_converter: str = ""
    rag_preview_timeout_seconds: int = Field(default=120, ge=10, le=1800)
    # The API process applies new chunks written by workers to its vector index this often (0 = off).
    rag_index_sync_seconds: float = Field(default=2.0, ge=0, le=3600)
    rag_max_attempts: int = Field(default=3, ge=1, le=10)
    rag_retry_base_seconds: float = Field(default=30.0, ge=0, le=3600)
    # Conversations: how many recent turns are sent to the LLM as context, and a size cap per conversation.
    chat_context_turns: int = Field(default=3, ge=0, le=10)
    chat_max_messages_per_conversation: int = Field(default=400, ge=10, le=10_000)
    # A claimed job's lease. Workers renew it while processing, so an abandoned job (crashed or killed worker)
    # is picked up again by another worker shortly after the lease runs out.
    rag_processing_lease_seconds: int = Field(default=60, ge=10, le=86400)
    # Personal memory: per user, never shared, never cited. Bounded storage and prompt budget.
    memory_max_items: int = Field(default=100, ge=1, le=1000)  # active items per user
    memory_context_chars: int = Field(default=2000, ge=200, le=20_000)
    memory_context_items: int = Field(default=20, ge=1, le=100)
    # Observability. One metadata-only trace per knowledge-chat question (no question or document text), kept
    # this many days. Logs also go to a rotating file in LOG_DIR (empty = stdout only), so a failed request's
    # traceback survives the terminal that started the server.
    rag_trace_enabled: bool = True
    rag_trace_retention_days: int = Field(default=30, ge=1, le=3650)
    log_dir: str = "logs"
    # Where the embedding model is cached. Empty = RAG_DATA_DIR/models. The evaluation gate points a throwaway
    # data directory at the shared cache instead of downloading the model again.
    rag_models_dir: str = ""

    @field_validator("master_encryption_key")
    @classmethod
    def _validate_master_key(cls, value: SecretStr) -> SecretStr:
        try:
            raw = base64.b64decode(value.get_secret_value(), validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("MASTER_ENCRYPTION_KEY must be valid base64") from None
        if len(raw) != 32:
            raise ValueError("MASTER_ENCRYPTION_KEY must decode to exactly 32 bytes")
        return value

    @field_validator("rag_database_analysis_url", mode="before")
    @classmethod
    def _empty_database_analysis_url_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("rag_database_analysis_sqlite_path", mode="before")
    @classmethod
    def _empty_database_analysis_sqlite_path_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("rag_database_analysis_source_label")
    @classmethod
    def _safe_database_analysis_source_label(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("RAG_DATABASE_ANALYSIS_SOURCE_LABEL must be a printable label")
        return value

    @field_validator("license_public_key")
    @classmethod
    def _validate_license_public_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("LICENSE_PUBLIC_KEY must be base64-encoded Ed25519 public key bytes") from None
        if len(raw) != 32:
            raise ValueError("LICENSE_PUBLIC_KEY must decode to exactly 32 bytes")
        return value

    @model_validator(mode="after")
    def _validate_environment_safety(self) -> "Settings":
        if "rag_index_role" not in self.model_fields_set and self.rag_vector_store != "faiss":
            # The "auto" default only applies to FAISS (in-memory replicas). Choosing Qdrant without an explicit
            # RAG_INDEX_ROLE means a single index-owning process, so Qdrant stays a plain opt-in alternative.
            self.rag_index_role = "owner"
        if self.rag_index_role != "owner" and self.rag_vector_store != "faiss":
            raise ValueError("RAG_INDEX_ROLE=replica/auto currently requires RAG_VECTOR_STORE=faiss")
        if self.app_env == "production" and not self.session_cookie_secure:
            raise ValueError("SESSION_COOKIE_SECURE must be true in production")
        if self.license_enforcement_enabled and not self.license_public_key:
            raise ValueError("LICENSE_PUBLIC_KEY is required when LICENSE_ENFORCEMENT_ENABLED is true")
        if bool(self.personal_task_smtp_username) != bool(self.personal_task_smtp_password):
            raise ValueError("Configure both PERSONAL_TASK_SMTP_USERNAME and PERSONAL_TASK_SMTP_PASSWORD, or neither")
        if self.personal_task_email_enabled or self.approval_email_enabled:
            if not self.personal_task_smtp_host.strip() or not self.personal_task_email_from.strip():
                raise ValueError("PERSONAL_TASK_SMTP_HOST and PERSONAL_TASK_EMAIL_FROM are required when email is enabled")
            if any(ord(character) < 32 for character in self.personal_task_smtp_host + self.personal_task_email_from):
                raise ValueError("SMTP host and sender address must not contain control characters")
        if self.rag_chunk_overlap >= self.rag_chunk_size // 2:
            raise ValueError("RAG_CHUNK_OVERLAP must be less than half of RAG_CHUNK_SIZE")
        from app.providers.endpoint_security import parse_trusted_origins  # deferred: keeps settings import light

        parse_trusted_origins(self.provider_trusted_private_origins)  # fail at startup, not on first use
        parse_trusted_origins(self.webhook_trusted_private_origins)
        return self

    @property
    def master_key_bytes(self) -> bytes:
        return base64.b64decode(self.master_encryption_key.get_secret_value())

    @property
    def rag_max_upload_bytes(self) -> int:
        return self.rag_max_upload_mb * 1024 * 1024

    @property
    def rag_import_max_file_bytes(self) -> int:
        return self.rag_import_max_file_mb * 1024 * 1024

    @property
    def rag_media_max_file_bytes(self) -> int:
        return self.rag_media_max_file_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
