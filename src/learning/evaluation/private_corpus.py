"""Validate and load a private evaluation corpus outside the Git repository."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from config.settings import BASE_DIR
from src.learning.evaluation.fixtures import EvaluationCase, load_private_cases


class PrivateCorpusConfig(BaseModel):
    """Typed opt-in location for private evaluation fixtures."""

    model_config = ConfigDict(frozen=True)
    enabled: bool = False
    path: Path | None = None


def validate_private_corpus(config: PrivateCorpusConfig, repo_root: Path = BASE_DIR) -> Path | None:
    """Return a valid external directory, or None when disabled."""
    if not config.enabled:
        return None
    if config.path is None or not config.path.is_absolute():
        raise ValueError("private corpus path must be absolute")
    path = config.path.resolve()
    root = repo_root.resolve()
    if not path.exists() or not path.is_dir():
        raise ValueError("private corpus path must exist")
    try:
        path.relative_to(root)
    except ValueError:
        return path
    raise ValueError("private corpus path must be outside Git repository")


def load_configured_cases(config: PrivateCorpusConfig, repo_root: Path = BASE_DIR) -> list[EvaluationCase]:
    """Load a non-empty validated corpus whenever private evaluation is enabled."""
    path = validate_private_corpus(config, repo_root)
    if path is None:
        return []
    cases = load_private_cases(path)
    if not cases:
        raise ValueError("enabled private corpus must contain at least one valid case")
    return cases


def configured_private_corpus() -> PrivateCorpusConfig:
    """Build the typed corpus configuration from application settings."""
    from config.settings import CONFIG

    governance = CONFIG.learning_governance
    path = Path(governance.private_evaluation_corpus) if governance.private_evaluation_corpus else None
    return PrivateCorpusConfig(enabled=governance.private_evaluation_enabled, path=path)
