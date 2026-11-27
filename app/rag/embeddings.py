"""Local CPU embeddings. Documents are embedded on this machine; no text leaves it during ingestion."""

import logging
import threading
from pathlib import Path
from typing import Protocol

import numpy as np

logger = logging.getLogger(__name__)


class EmbeddingModelUnavailableError(RuntimeError):
    """A safe, typed failure used when the configured local embedding model is absent or unusable."""


class Embedder(Protocol):
    model_name: str
    dimension: int

    @property
    def available_locally(self) -> bool: ...

    @property
    def is_loaded(self) -> bool: ...

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an (n, dimension) float32 array of L2-normalised vectors. Blocking."""
        ...

    def embed_local(self, texts: list[str]) -> np.ndarray:
        """Return local embeddings without permitting model downloads. Blocking."""
        ...

    def warmup(self) -> None: ...

    def health_check(self) -> None: ...


class SentenceTransformerEmbedder:
    """Lazily loads a sentence-transformers model on CPU, optionally downloading it into `cache_dir`."""

    def __init__(
        self,
        model_name: str,
        dimension: int,
        cache_dir: Path,
        batch_size: int = 32,
        allow_download: bool = True,
    ) -> None:
        self.model_name = model_name
        self.dimension = dimension
        self._cache_dir = cache_dir
        self._batch_size = batch_size
        self._allow_download = allow_download
        self._model = None
        self._lock = threading.Lock()

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._model is not None

    @property
    def available_locally(self) -> bool:
        return self._local_model_path() is not None

    def _local_model_path(self) -> Path | None:
        """Find a complete local model snapshot without importing or loading the model."""
        required = ("modules.json", "config.json")

        def is_model_dir(path: Path) -> bool:
            return (
                path.is_dir()
                and all((path / name).is_file() for name in required)
                and any((path / name).is_file() for name in ("model.safetensors", "pytorch_model.bin"))
                and any((path / name).is_file() for name in ("tokenizer.json", "vocab.txt", "tokenizer_config.json"))
            )

        configured_path = Path(self.model_name)
        if is_model_dir(configured_path):
            return configured_path

        cache_name = "models--" + self.model_name.replace("/", "--")
        model_cache = self._cache_dir / cache_name
        ref = model_cache / "refs" / "main"
        revisions: list[Path] = []
        try:
            revision = ref.read_text(encoding="utf-8").strip()
            if revision:
                revisions.append(model_cache / "snapshots" / revision)
        except OSError:
            pass
        snapshots = model_cache / "snapshots"
        try:
            revisions.extend(path for path in snapshots.iterdir() if path.is_dir() and path not in revisions)
        except OSError:
            pass
        return next((path for path in revisions if is_model_dir(path)), None)

    def _load(self, *, local_files_only: bool | None = None):
        if local_files_only is None:
            local_files_only = not self._allow_download
        with self._lock:
            if self._model is None:
                if local_files_only and not self.available_locally:
                    raise EmbeddingModelUnavailableError(
                        "Embedding model files are not available locally. Pre-stage the configured model or "
                        "explicitly enable RAG_EMBEDDING_ALLOW_DOWNLOAD in a connected, trusted deployment."
                    )
                from sentence_transformers import SentenceTransformer  # heavy import, deferred

                try:  # use the local copy without contacting the model hub
                    model = SentenceTransformer(
                        self.model_name,
                        device="cpu",
                        cache_folder=str(self._cache_dir),
                        local_files_only=True,
                    )
                except Exception as exc:  # first run: download once into cache_dir
                    if local_files_only:
                        raise EmbeddingModelUnavailableError("Embedding model could not be loaded from local files") from exc
                    logger.info("Downloading embedding model %s (one-time)", self.model_name)
                    model = SentenceTransformer(self.model_name, device="cpu", cache_folder=str(self._cache_dir))
                actual = model.get_embedding_dimension()
                if actual != self.dimension:
                    raise RuntimeError(
                        f"Embedding model dimension is {actual}, but RAG_EMBEDDING_DIMENSION is {self.dimension}"
                    )
                self._model = model
                logger.info("Embedding model loaded: %s (dim=%s, device=cpu)", self.model_name, actual)
        return self._model

    def warmup(self) -> None:
        self._load()

    def health_check(self) -> None:
        """Run one fixed embedding without allowing a model download."""
        model = self._load(local_files_only=True)
        vectors = model.encode(
            ["InfraSpine embedding health check"],
            batch_size=1,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        result = np.asarray(vectors, dtype=np.float32)
        if result.shape != (1, self.dimension) or not np.isfinite(result).all():
            raise RuntimeError("Embedding health check returned invalid vectors")
        if not np.isclose(float(np.linalg.norm(result[0])), 1.0, atol=0.05):
            raise RuntimeError("Embedding health check returned an unnormalized vector")

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        vectors = self._load().encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.ascontiguousarray(vectors, dtype=np.float32)

    def embed_local(self, texts: list[str]) -> np.ndarray:
        """Embed without permitting model downloads, for optional request-path analysis."""
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)
        vectors = self._load(local_files_only=True).encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.ascontiguousarray(vectors, dtype=np.float32)


def to_bytes(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype="<f4").tobytes()


def from_bytes(data: bytes, dimension: int) -> np.ndarray:
    vector = np.frombuffer(data, dtype="<f4")
    if vector.shape != (dimension,):
        raise ValueError("Stored embedding has the wrong dimension")
    return vector
