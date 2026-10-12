"""Read-only external search adapters for the AI Workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from config import defaults


@dataclass(frozen=True)
class SearchSource:
    source_id: str
    title: str
    url: str
    snippet: str = ""

    def to_part(self) -> dict[str, str]:
        return {
            "type": "source",
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
        }


class WebSearchProvider(Protocol):
    def search(self, query: str, *, limit: int = 5) -> tuple[str, list[SearchSource]]: ...


def web_search_capability() -> tuple[bool, str | None, str | None]:
    """Return provider availability without performing network I/O."""
    if not getattr(defaults, "AI_WORKSPACE_WEB_SEARCH_ENABLED", False):
        return False, "Web Search is disabled by configuration.", None
    provider = str(getattr(defaults, "AI_WORKSPACE_WEB_SEARCH_PROVIDER", "") or "").strip().lower()
    if provider not in {"litellm", "openai"}:
        return False, "No supported Web Search provider is configured.", provider or None
    return True, None, provider


class LiteLLMWebSearchProvider:
    """OpenAI Responses-compatible web search through the configured LiteLLM route."""

    def search(self, query: str, *, limit: int = 5) -> tuple[str, list[SearchSource]]:
        import litellm
        from src.llm.routing import get_model_for_task

        model_config = get_model_for_task("reasoning")
        kwargs: dict[str, Any] = {
            "model": model_config.primary,
            "input": query,
            "tools": [{"type": "web_search"}],
            "include": ["web_search_call.action.sources"],
            "max_output_tokens": 800,
        }
        if model_config.api_base:
            kwargs["api_base"] = model_config.api_base
        if model_config.api_key:
            kwargs["api_key"] = model_config.api_key
        response = litellm.responses(**kwargs)
        payload = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        text = str(payload.get("output_text") or "").strip()
        sources = _extract_sources(payload, limit=limit)
        return text, sources


def _extract_sources(payload: dict[str, Any], *, limit: int) -> list[SearchSource]:
    seen: set[str] = set()
    sources: list[SearchSource] = []

    def visit(value: Any) -> None:
        if len(sources) >= limit:
            return
        if isinstance(value, dict):
            url = value.get("url")
            if isinstance(url, str) and url.startswith(("https://", "http://")) and url not in seen:
                seen.add(url)
                sources.append(SearchSource(
                    source_id=f"web-{len(sources) + 1}",
                    title=str(value.get("title") or value.get("name") or url),
                    url=url,
                    snippet=str(value.get("snippet") or value.get("description") or ""),
                ))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return sources


def get_web_search_provider() -> WebSearchProvider:
    available, reason, _ = web_search_capability()
    if not available:
        raise RuntimeError(reason or "Web Search is unavailable")
    return LiteLLMWebSearchProvider()
