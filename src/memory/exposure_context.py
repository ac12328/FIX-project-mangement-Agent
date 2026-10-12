"""Request-local composable memory exposure scopes."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator

from src.memory.exposure_repo import ExposureCandidate


@dataclass(frozen=True)
class ExposureScope:
    operator_id: str
    surface: str
    output_revision_id: str | None
    candidates: tuple[ExposureCandidate, ...]
    task_type: str | None = None
    project_id: str | None = None


_scopes: ContextVar[tuple[ExposureScope, ...]] = ContextVar(
    "memory_exposure_scopes", default=(),
)
_primary_scope: ContextVar[ExposureScope | None] = ContextVar(
    "memory_inference_primary_scope", default=None,
)


@contextmanager
def inference_scope(*, operator_id: str, surface: str,
                    output_revision_id: str | None = None,
                    task_type: str | None = None,
                    project_id: str | None = None,
                    candidate_scopes: tuple[ExposureScope, ...] = ()) -> Iterator[ExposureScope]:
    """Bind primary provenance and candidates for exactly one actual LLM call.

    Candidate scopes are request data until this boundary. Nested inference calls
    inherit the active scopes, append their own candidates, and restore both
    ContextVars with tokens even when the model call raises.
    """
    value = ExposureScope(
        operator_id, surface, output_revision_id, (), task_type, project_id,
    )
    primary_token = _primary_scope.set(value)
    scopes_token: Token = _scopes.set((*_scopes.get(), *candidate_scopes))
    try:
        yield value
    finally:
        _scopes.reset(scopes_token)
        _primary_scope.reset(primary_token)


def get_inference_scope() -> ExposureScope | None:
    return _primary_scope.get()


@contextmanager
def exposure_scope(*, operator_id: str, surface: str,
                   output_revision_id: str | None = None,
                   candidates: list[ExposureCandidate], task_type: str | None = None,
                   project_id: str | None = None) -> Iterator[ExposureScope]:
    value = ExposureScope(
        operator_id, surface, output_revision_id, tuple(candidates), task_type, project_id,
    )
    token: Token = _scopes.set((*_scopes.get(), value))
    try:
        yield value
    finally:
        _scopes.reset(token)


def make_exposure_scope(*, operator_id: str, surface: str,
                        candidates: list[ExposureCandidate] | tuple[ExposureCandidate, ...],
                        output_revision_id: str | None = None,
                        task_type: str | None = None,
                        project_id: str | None = None) -> ExposureScope:
    """Create immutable request-local scope data without mutating ContextVars."""
    return ExposureScope(
        operator_id, surface, output_revision_id, tuple(candidates), task_type, project_id,
    )


def clear_exposure_scope() -> None:
    _scopes.set(())
    _primary_scope.set(None)


def get_exposure_scopes() -> tuple[ExposureScope, ...]:
    return _scopes.get()


def get_exposure_scope() -> ExposureScope | None:
    scopes = _scopes.get()
    return scopes[-1] if scopes else None


def get_pending_exposures() -> list[ExposureCandidate]:
    return [candidate for scope in _scopes.get() for candidate in scope.candidates]
