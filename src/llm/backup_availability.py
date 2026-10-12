"""Runtime validation and quarantine for the optional backup LLM."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Literal
from urllib.request import Request, urlopen

from src.llm.routing import ModelConfig

logger = logging.getLogger(__name__)

BackupValidationStatus = Literal["available", "unavailable", "unknown"]


@dataclass(frozen=True)
class BackupValidationResult:
    status: BackupValidationStatus
    reason: str | None = None


_lock = Lock()
_unavailable_reasons: dict[str, str] = {}


def _config_text(value: object) -> str:
    """Return a normalized configured string without coercing mocks or objects."""
    return value.strip() if isinstance(value, str) else ""


def _signature(config: ModelConfig) -> str:
    return f"{_config_text(config.api_base).rstrip('/')}|{_config_text(config.primary)}"


def _model_aliases(model: str) -> set[str]:
    value = _config_text(model).casefold()
    aliases = {value}
    if value.startswith("openai/"):
        aliases.add(value.removeprefix("openai/"))
    return {alias for alias in aliases if alias}


def mark_backup_unavailable(config: ModelConfig, reason: str) -> None:
    """Quarantine one exact endpoint/model pair for the current process."""
    with _lock:
        _unavailable_reasons[_signature(config)] = reason


def clear_backup_unavailable(config: ModelConfig) -> None:
    with _lock:
        _unavailable_reasons.pop(_signature(config), None)


def get_backup_unavailable_reason(config: ModelConfig) -> str | None:
    with _lock:
        return _unavailable_reasons.get(_signature(config))


def mark_backup_unavailable_from_error(
    config: ModelConfig,
    error: Exception,
) -> bool:
    """Quarantine deterministic configuration failures, not transient outages."""
    message = str(error).casefold()
    invalid_model_markers = (
        "invalid model name",
        "model_not_found",
        "model not found",
        "unknown model",
        "does not exist or you do not have access",
    )
    if not any(marker in message for marker in invalid_model_markers):
        return False
    mark_backup_unavailable(config, "backup_model_unavailable")
    return True


def validate_configured_backup(
    config: ModelConfig,
    *,
    timeout_sec: float = 2.0,
    open_url: Callable = urlopen,
) -> BackupValidationResult:
    """Best-effort validation against an OpenAI-compatible ``/models`` endpoint.

    Endpoint outages are reported as ``unknown`` and never disable failover. A
    successful model listing that omits the configured model is deterministic,
    so that endpoint/model pair is quarantined until the process restarts or the
    configuration changes.
    """
    api_base = _config_text(config.api_base)
    model = _config_text(config.primary)
    if not api_base:
        # A genuinely absent endpoint is deterministic and can be skipped. An
        # unexpected runtime object (for example a partially mocked config)
        # makes validation inconclusive, but must never block startup.
        if config.api_base is None or isinstance(config.api_base, str):
            mark_backup_unavailable(config, "backup_endpoint_not_configured")
            return BackupValidationResult("unavailable", "backup_endpoint_not_configured")
        logger.warning(
            "Backup model validation inconclusive reason=invalid_endpoint_type"
        )
        return BackupValidationResult("unknown", "backup_configuration_unreadable")

    if not model:
        if config.primary is None or isinstance(config.primary, str):
            mark_backup_unavailable(config, "backup_model_not_configured")
            return BackupValidationResult("unavailable", "backup_model_not_configured")
        logger.warning(
            "Backup model validation inconclusive reason=invalid_model_type"
        )
        return BackupValidationResult("unknown", "backup_configuration_unreadable")

    try:
        headers = {}
        api_key = _config_text(config.api_key)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(f"{api_base.rstrip('/')}/models", headers=headers)
        with open_url(request, timeout=timeout_sec) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Backup models response must be a JSON object")

        available_aliases: set[str] = set()
        for item in payload.get("data", []):
            if isinstance(item, dict):
                available_aliases.update(_model_aliases(item.get("id")))
    except Exception as exc:
        logger.warning(
            "Backup model validation unavailable model=%s error_class=%s",
            model,
            type(exc).__name__,
        )
        return BackupValidationResult("unknown", "backup_models_endpoint_unavailable")

    if not (_model_aliases(model) & available_aliases):
        mark_backup_unavailable(config, "backup_model_unavailable")
        return BackupValidationResult("unavailable", "backup_model_unavailable")

    clear_backup_unavailable(config)
    return BackupValidationResult("available")


def reset_backup_availability() -> None:
    """Clear process-local quarantine state (tests only)."""
    with _lock:
        _unavailable_reasons.clear()
