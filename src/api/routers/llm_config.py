"""LLM configuration endpoints — atomic config.json updates."""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(tags=["llm-config"])

if TYPE_CHECKING:
    from config.settings import AppConfig

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "config.json"
_CONFIG_LOCK = threading.RLock()

_MASKED_FIELDS = {
    "local_llm_api_key",
    "ollama_api_key",
    "openrouter_api_key",
    "openai_api_key",
    "anthropic_api_key",
    "gemini_api_key",
    "minimax_api_key",
    "backup_api_key",
}


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


def _load_raw_unlocked() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"Could not read config.json: {exc}") from exc
    if not isinstance(raw, dict):
        raise HTTPException(500, "config.json root must be an object")
    return raw


def _load_raw() -> dict:
    with _CONFIG_LOCK:
        return _load_raw_unlocked()


def _write_raw_unlocked(raw: dict) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=_CONFIG_PATH.parent,
        prefix=f".{_CONFIG_PATH.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(raw, handle, indent=4, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, _CONFIG_PATH)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _validate_runtime_config(raw: dict) -> tuple[AppConfig, dict[str, dict]]:
    from config.settings import AppConfig

    config = AppConfig(**raw)
    stored = raw.get("llm_routing")
    if stored is None:
        stored = {}
    if not isinstance(stored, dict) or any(
        not isinstance(task, str) or not isinstance(override, dict)
        for task, override in stored.items()
    ):
        raise ValueError("llm_routing must map task names to override objects")
    return config, stored


def _apply_runtime_config(config: AppConfig, stored: dict[str, dict]) -> None:
    from config import settings
    from src.llm.routing import load_routing_overrides, reset_routing

    reset_routing()
    settings.inject_llm_keys(config)
    if stored:
        load_routing_overrides(stored)
    # Health and backup routing read this singleton, not TASK_MODEL_MAP.
    settings.CONFIG.llm_keys = config.llm_keys


def _persist_validated_config(raw: dict) -> None:
    """Validate before writing; compensate if runtime activation fails.

    This is not crash-atomic across the file and in-process readers.
    """
    from config import settings
    from src.llm import routing

    try:
        config, stored = _validate_runtime_config(raw)
    except Exception as exc:
        raise HTTPException(422, "Invalid LLM configuration") from exc

    previous_raw = _load_raw_unlocked()
    file_existed = _CONFIG_PATH.exists()
    previous_routes = copy.deepcopy(routing.TASK_MODEL_MAP)
    previous_keys = settings.CONFIG.llm_keys
    env_names = (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
        "OPENROUTER_API_KEY", "MINIMAX_API_KEY", "VERTEXAI_PROJECT",
        "VERTEXAI_LOCATION", "VERTEXAI_MODEL", "VERTEXAI_CREDENTIALS",
        "GOOGLE_APPLICATION_CREDENTIALS",
    )
    previous_env = {name: os.environ.get(name) for name in env_names}
    try:
        _write_raw_unlocked(raw)
    except OSError as exc:
        raise HTTPException(500, "Could not save LLM configuration") from exc

    try:
        _apply_runtime_config(config, stored)
    except Exception as exc:
        # Keep a failed activation from advertising a new disk configuration.
        # A second disk failure is reported loudly rather than claiming rollback.
        routing.TASK_MODEL_MAP.clear()
        routing.TASK_MODEL_MAP.update(previous_routes)
        settings.CONFIG.llm_keys = previous_keys
        for name, previous in previous_env.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        try:
            if file_existed:
                _write_raw_unlocked(previous_raw)
            else:
                _CONFIG_PATH.unlink(missing_ok=True)
        except OSError as rollback_exc:
            logger.critical("LLM config activation and disk rollback both failed", exc_info=True)
            raise HTTPException(500, "LLM configuration rollback failed; restart required") from rollback_exc
        raise HTTPException(500, "Could not activate LLM configuration") from exc


@router.get("/llm-config")
def get_llm_config():
    """Return current LLM configuration with masked API keys."""
    raw = _load_raw()
    keys = raw.get("llm_keys", {})
    return {
        "active_provider": keys.get("active_provider", ""),
        "active_model": keys.get("active_model", ""),
        "backup_model": keys.get("backup_model", "default-model"),
        "backup_api_base": keys.get("backup_api_base", ""),
        "backup_api_key": _mask(keys.get("backup_api_key", "")),
        "openrouter_api_key": _mask(keys.get("openrouter_api_key", "")),
        "openai_api_key": _mask(keys.get("openai_api_key", "")),
        "anthropic_api_key": _mask(keys.get("anthropic_api_key", "")),
        "gemini_api_key": _mask(keys.get("gemini_api_key", "")),
        "minimax_api_key": _mask(keys.get("minimax_api_key", "")),
        "ollama_api_base": keys.get("ollama_api_base", "http://llm.demo.invalid/v1"),
        "ollama_api_key": _mask(keys.get("ollama_api_key", "")),
        "local_llm_api_base": keys.get("local_llm_api_base", ""),
        "local_llm_api_key": _mask(keys.get("local_llm_api_key", "")),
        "local_llm_model": keys.get("local_llm_model", ""),
        "vertex_ai_project": keys.get("vertex_ai_project", ""),
        "vertex_ai_location": keys.get("vertex_ai_location", "us-central1"),
        "vertex_ai_model": keys.get("vertex_ai_model", ""),
        "vertex_ai_credentials_path": keys.get("vertex_ai_credentials_path", ""),
    }


class LLMConfigUpdate(BaseModel):
    active_provider: str | None = None
    active_model: str | None = None
    backup_model: str | None = None
    backup_api_base: str | None = None
    backup_api_key: str | None = None
    openrouter_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    minimax_api_key: str | None = None
    ollama_api_base: str | None = None
    ollama_api_key: str | None = None
    local_llm_api_base: str | None = None
    local_llm_api_key: str | None = None
    local_llm_model: str | None = None
    vertex_ai_project: str | None = None
    vertex_ai_location: str | None = None
    vertex_ai_model: str | None = None
    vertex_ai_credentials_path: str | None = None


@router.patch("/llm-config")
def update_llm_config(body: LLMConfigUpdate):
    """Atomically update llm_keys and apply the validated runtime config."""
    with _CONFIG_LOCK:
        raw = _load_raw_unlocked()
        keys = dict(raw.get("llm_keys") or {})
        for field, value in body.model_dump(exclude_none=True).items():
            if field in _MASKED_FIELDS and "****" in str(value):
                continue
            keys[field] = value
        raw["llm_keys"] = keys
        _persist_validated_config(raw)
    return {
        "success": True,
        "provider": keys.get("active_provider", ""),
        "model": keys.get("active_model", ""),
    }


_TASK_LABELS: dict[str, str] = {
    "triage": "Email Triage & Classification",
    "status": "Project Status Assessment",
    "analysis": "Deep Project Analysis",
    "learning": "Operator Style Learning",
    "morning_brief": "Morning Brief Generation",
    "operational": "Operational Tasks",
    "draft": "Email Draft Writing",
    "email_draft": "Email Draft (Specialized)",
    "reasoning": "General Reasoning",
    "lead_reasoning": "Lead Agent Reasoning",
    "truth_write_reasoning": "Write Layer Reasoning",
}


@router.get("/llm-config/routing")
def get_routing_table():
    """Return the current task-to-model routing table."""
    from src.llm.routing import TASK_MODEL_MAP

    raw = _load_raw()
    overrides = raw.get("llm_routing", {})
    return [
        {
            "task": task,
            "label": _TASK_LABELS.get(task, task),
            "model": cfg.primary,
            "api_base": cfg.api_base or "",
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "has_override": task in overrides,
        }
        for task, cfg in TASK_MODEL_MAP.items()
    ]


class TaskOverride(BaseModel):
    task: str
    model: str | None = None
    temperature: float | None = None


@router.patch("/llm-config/routing")
def update_routing_table(overrides: list[TaskOverride]):
    """Atomically save validated task-model overrides and apply them."""
    from src.llm.routing import TASK_MODEL_MAP

    with _CONFIG_LOCK:
        raw = _load_raw_unlocked()
        stored = dict(raw.get("llm_routing") or {})
        for override in overrides:
            if override.task not in TASK_MODEL_MAP:
                raise HTTPException(400, f"Unknown task: {override.task}")
            if override.model:
                entry = dict(stored.get(override.task) or {})
                entry["primary"] = override.model
                if override.temperature is not None:
                    entry["temperature"] = override.temperature
                stored[override.task] = entry
            else:
                stored.pop(override.task, None)
        raw["llm_routing"] = stored
        _persist_validated_config(raw)
    return {"success": True, "overrides_count": len(stored)}
