"""Research-only model normalization for the Email -> Project Truth experiment."""

from __future__ import annotations


def normalize_research_model(model: str | None) -> str | None:
    """Normalize bare model ids for the configured OpenAI-compatible proxy.

    LiteLLM can misclassify some bare ids (notably xAI-like names) even when a
    custom ``api_base`` is configured. Explicit provider ids are preserved.
    """
    if model is None:
        return None
    value = model.strip()
    if not value or "/" in value:
        return value or None

    from config.settings import CONFIG

    keys = CONFIG.llm_keys
    uses_openai_compatible_proxy = (
        keys.active_provider in {"custom", "ollama"}
        or bool((keys.local_llm_api_base or "").strip())
    )
    if uses_openai_compatible_proxy:
        return f"openai/{value}"
    return value
