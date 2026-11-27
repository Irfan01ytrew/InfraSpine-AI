"""Retrieval and grounded-answer evaluation for this deployment.

Datasets and reports stay under this deployment's data directory. Retrieval evaluation
calls `retrieve`. Answer evaluation calls `run_chat`, the production question path.
"""

from app.rag.evaluation.answers import evaluate_answers
from app.rag.evaluation.dataset import EvaluationError, load_dataset, load_personas
from app.rag.evaluation.runner import evaluate

__all__ = ["EvaluationError", "evaluate", "evaluate_answers", "load_dataset", "load_personas"]
