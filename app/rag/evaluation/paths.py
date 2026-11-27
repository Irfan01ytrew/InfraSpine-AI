"""Evaluation files stay inside this deployment's data directory.

Customer datasets and results are not written anywhere else, and a path that escapes
`data/evaluation` is rejected.
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import Settings
from app.rag.evaluation.dataset import EvaluationError


def evaluation_root(settings: Settings) -> Path:
    root = (settings.rag_data_dir / "evaluation").resolve()
    for name in ("datasets", "personas", "reports"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def resolve_inside(root: Path, candidate: Path) -> Path:
    """Resolve `candidate` and require it to remain inside `root`."""
    path = candidate if candidate.is_absolute() else root / candidate
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise EvaluationError("Evaluation files must stay inside this deployment's data/evaluation directory.")
    return resolved
