"""
Task-to-Model routing configuration.
Maps each task type to a specific LLM model + fallback chain.
"""

import copy
from dataclasses import dataclass, field


@dataclass
class ModelConfig:
    """Configuration for a single model assignment."""
    primary: str
    fallbacks: list[str] = field(default_factory=list)
    temperature: float = 0.2
    max_tokens: int | None = 2000
    api_base: str | None = None   # for local/custom endpoints
    api_key: str | None = None    # per-model key override
    max_retries: int = 2          # LiteLLM retry count for transient errors


# Model aliases — local LiteLLM proxy (also used as backup base defaults)
_LOCAL_API_BASE = "http://llm.demo.invalid/v1"
_LOCAL_API_KEY  = ""
_MODEL          = "openai/default-model"

# Backup provider defaults — local Gemma 4 via LiteLLM proxy (not cloud MiniMax).
# Overridable via llm_keys.backup_model / backup_api_base / backup_api_key.
_BACKUP_API_BASE = _LOCAL_API_BASE
_BACKUP_API_KEY  = _LOCAL_API_KEY
_BACKUP_MODEL    = "openai/default-model"


def _extract_model_name(model_ref: str) -> str:
    """Convert a provider/model reference into the raw model name."""
    return model_ref.rsplit("/", 1)[-1].split(":", 1)[0]

# Default task-to-model mapping — override via config/llm_routing.json
TASK_MODEL_MAP: dict[str, ModelConfig] = {
    # Fast tasks → local LLM
    "triage":        ModelConfig(primary=_MODEL, temperature=0.1, max_tokens=3000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "status":        ModelConfig(primary=_MODEL, temperature=0.2, max_tokens=2000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "follow_up":     ModelConfig(primary=_MODEL, temperature=0.35, max_tokens=500, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=0),
    "trace_diagnosis": ModelConfig(primary=_MODEL, temperature=0.1, max_tokens=2000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "analysis":      ModelConfig(primary=_MODEL, temperature=0.2, max_tokens=3000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "learning":      ModelConfig(primary=_MODEL, temperature=0.1, max_tokens=2500, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "morning_brief": ModelConfig(primary=_MODEL, temperature=0.3, max_tokens=2000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "operational":   ModelConfig(primary=_MODEL, temperature=0.3, max_tokens=2000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    # Quality tasks → local LLM
    "draft":         ModelConfig(primary=_MODEL, temperature=0.4, max_tokens=3000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "email_draft":   ModelConfig(primary=_MODEL, temperature=0.4, max_tokens=3000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "reasoning":     ModelConfig(primary=_MODEL, temperature=0.1, max_tokens=8192, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "lead_reasoning": ModelConfig(primary=_MODEL, temperature=0.1, max_tokens=8192, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "truth_write_reasoning": ModelConfig(primary=_MODEL, temperature=0.1, max_tokens=8192, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "flush":         ModelConfig(primary=_MODEL, temperature=0.2, max_tokens=2000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "image_description": ModelConfig(primary=_MODEL, temperature=0.2, max_tokens=1500, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "inbound_reply_analysis": ModelConfig(primary=_MODEL, temperature=0.2, max_tokens=3000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "feedback_rule_extraction": ModelConfig(primary=_MODEL, temperature=0.1, max_tokens=2500, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "knowledge_mining": ModelConfig(primary=_MODEL, temperature=0.2, max_tokens=3000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "insight_mining": ModelConfig(primary=_MODEL, temperature=0.2, max_tokens=3000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
    "wiki_curator":  ModelConfig(primary=_MODEL, temperature=0.3, max_tokens=8000, api_base=_LOCAL_API_BASE, api_key=_LOCAL_API_KEY, max_retries=1),
}


# Snapshot factory defaults so reset_routing() can restore them
_FACTORY_DEFAULTS: dict[str, ModelConfig] = copy.deepcopy(TASK_MODEL_MAP)


def reset_routing() -> None:
    """Restore all task routes to factory defaults (call before reconfiguring)."""
    for task, default in _FACTORY_DEFAULTS.items():
        cfg = TASK_MODEL_MAP[task]
        cfg.primary = default.primary
        cfg.fallbacks = list(default.fallbacks)
        cfg.api_base = default.api_base
        cfg.api_key = default.api_key


def get_model_for_task(task: str) -> ModelConfig:
    """Get the model config for a given task type or business TaskType value.

    Accepts LlmRoute keys (e.g. ``\"draft\"``), business TaskType values
    (e.g. ``\"reply_to_client\"`` → draft), or unknown strings (fall back
    to reasoning after resolve pass-through).
    """
    from src.models.tasks import resolve_llm_route

    key = resolve_llm_route(task)
    return TASK_MODEL_MAP.get(key, TASK_MODEL_MAP["reasoning"])


def configure_local_llm(api_base: str, api_key: str, model: str | None = None) -> None:
    """Route all task models through a local OpenAI-compatible endpoint."""
    for cfg in TASK_MODEL_MAP.values():
        selected_model = model or _extract_model_name(cfg.primary)
        cfg.primary = f"openai/{selected_model}"
        cfg.fallbacks = []
        cfg.api_base = api_base
        cfg.api_key = api_key


def configure_vertex_ai(model: str | None = None) -> None:
    """Route all task models through Vertex AI."""
    for cfg in TASK_MODEL_MAP.values():
        selected_model = model or _extract_model_name(cfg.primary)
        cfg.primary = f"vertex_ai/{selected_model}"
        cfg.fallbacks = []
        cfg.api_base = None
        cfg.api_key = None


# Provider → LiteLLM model prefix mapping
_PROVIDER_PREFIX: dict[str, str] = {
    "openrouter": "openrouter/",
    "openai": "",               # bare model name → LiteLLM auto-detects OpenAI
    "anthropic": "",            # claude-* auto-detected by LiteLLM
    "gemini": "gemini/",
    "vertex_ai": "vertex_ai/",
    "minimax": "openai/",       # OpenAI-compatible with custom api_base
    "ollama": "openai/",        # Local Ollama OpenAI-compatible /v1
    "custom": "openai/",        # OpenAI-compatible with custom api_base
}

# Provider → default api_base (None = use LiteLLM default)
_PROVIDER_API_BASE: dict[str, str | None] = {
    "minimax": "https://api.minimaxi.chat/v1",
    "ollama": "http://llm.demo.invalid/v1",
}


def configure_vision_routing(
    *,
    api_base: str,
    api_key: str,
    model: str,
) -> None:
    """Route image_description to a vision-capable model (text-only models reject images)."""
    cfg = TASK_MODEL_MAP.get("image_description")
    if not cfg:
        return
    selected = model.split("/", 1)[-1] if model.startswith("openai/") else model
    cfg.primary = f"openai/{selected}"
    cfg.api_base = api_base
    cfg.api_key = api_key


def configure_provider(
    provider: str,
    model: str,
    api_key: str | None = None,
    api_base: str | None = None,
) -> None:
    """Route all task models through the specified provider."""
    prefix = _PROVIDER_PREFIX.get(provider, "openai/")
    full_model = f"{prefix}{model}" if prefix else model
    base = api_base or _PROVIDER_API_BASE.get(provider)
    for cfg in TASK_MODEL_MAP.values():
        cfg.primary = full_model
        cfg.fallbacks = []
        cfg.api_base = base
        cfg.api_key = api_key


def load_routing_overrides(overrides: dict) -> None:
    """Apply overrides from config file to TASK_MODEL_MAP."""
    for task, cfg in overrides.items():
        if isinstance(cfg, dict):
            TASK_MODEL_MAP[task] = ModelConfig(
                primary=cfg.get("primary", _MODEL),
                fallbacks=cfg.get("fallbacks", []),
                temperature=cfg.get("temperature", 0.2),
                max_tokens=cfg.get("max_tokens", 2000),
                api_base=cfg.get("api_base"),
                api_key=cfg.get("api_key"),
                max_retries=cfg.get("max_retries", 2),
            )


def _as_openai_model(model: str) -> str:
    """Ensure LiteLLM OpenAI-compatible model id has openai/ prefix."""
    model = (model or "").strip()
    if not model:
        return _BACKUP_MODEL
    if model.startswith("openai/"):
        return model
    return f"openai/{model}"


def _resolve_backup_settings() -> tuple[str, str, str]:
    """Return (api_base, api_key, model) for Gemma through the local proxy alias.

    Resolution order for base/key:
      backup_api_* → local_llm_api_* → factory defaults
    Model: backup_model → ``default-model``
    """
    api_base = _BACKUP_API_BASE
    api_key = _BACKUP_API_KEY
    model = _BACKUP_MODEL
    try:
        from config.settings import CONFIG

        k = CONFIG.llm_keys
        api_base = (
            (getattr(k, "backup_api_base", None) or "").strip()
            or (k.local_llm_api_base or "").strip()
            or api_base
        )
        api_key = (
            (getattr(k, "backup_api_key", None) or "").strip()
            or (k.local_llm_api_key or "").strip()
            or api_key
            or "local"
        )
        configured = (getattr(k, "backup_model", None) or "").strip()
        if configured:
            model = _as_openai_model(configured)
    except Exception:
        pass
    return api_base, api_key, model


def get_backup_config() -> ModelConfig:
    """Return a ModelConfig for Gemma through the local backup proxy.

    Primary remains the active provider (e.g. Ollama ``glm-5.2:cloud``).
    Backup uses the local LiteLLM proxy with ``llm_keys.backup_model``
    (default ``default-model``).
    """
    api_base, api_key, model = _resolve_backup_settings()
    return ModelConfig(
        primary=model,
        temperature=0.2,
        max_tokens=4000,
        api_base=api_base,
        api_key=api_key,
        max_retries=1,
    )


def get_primary_endpoint() -> tuple[str, str, str]:
    """Return (api_base, api_key, model) for the live primary LLM route."""
    cfg = get_model_for_task("reasoning")
    return (cfg.api_base or _LOCAL_API_BASE, cfg.api_key or "", cfg.primary)


def get_backup_endpoint() -> tuple[str, str, str]:
    """Return (api_base, api_key, model) for the backup LLM."""
    return _resolve_backup_settings()
