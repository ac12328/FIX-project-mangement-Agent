"""Public API for the Email -> Project Truth research pipeline."""

from src.email.project_truth.baseline import run_baseline
from src.email.project_truth.citations import validate_extraction_citations
from src.email.project_truth.contracts import *  # noqa: F403
from src.email.project_truth.evaluator import evaluate_validated_evidence
from src.email.project_truth.extraction import run_fact_extraction
from src.email.project_truth.serve import (
    EmailProjectTruthServeApplicationResult,
    EmailProjectTruthServeResult,
    apply_email_project_truth_serve_result,
    run_email_project_truth_serve,
)

__all__ = [
    "EmailProjectTruthServeApplicationResult",
    "EmailProjectTruthServeResult",
    "apply_email_project_truth_serve_result",
    "run_email_project_truth_serve",
    "evaluate_validated_evidence",
    "run_baseline",
    "run_fact_extraction",
    "validate_extraction_citations",
]
