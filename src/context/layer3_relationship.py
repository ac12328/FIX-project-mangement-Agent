"""
Context Layer 3: Relationship/Preference loader.
Loads response pattern memory for a contact+project pair.
Merges operator preferences (tone, language) as fallback.
"""

import sqlite3

from src.models.context_layers import RelationshipContext


def load_relationship(
    conn: sqlite3.Connection,
    contact_id: str | None,
    project_id: str | None,
    *, memory_request=None,
) -> tuple[RelationshipContext, list[str]]:
    """
    Load relationship preferences for a contact+project pair.
    Returns (RelationshipContext, missing_flags).
    """
    missing: list[str] = []

    if not contact_id:
        missing.append("no_contact_id")
        # Still try to load operator preferences for project
        rel = _merge_operator_preferences(conn, RelationshipContext(), project_id)
        return rel, missing

    # Try project-specific pattern first, then contact-level. In retired mode,
    # canonical governed preferences own injection; compatibility API reads remain available.
    from src.learning.legacy_retirement import legacy_injection_allowed
    if not legacy_injection_allowed(conn, "response_pattern_memory"):
        missing.append("legacy_response_pattern_retired")
        return _merge_operator_preferences(conn, RelationshipContext(), project_id), missing
    from src.repositories.response_pattern_repo import get_response_pattern
    row = get_response_pattern(conn, contact_id, project_id, memory_request=memory_request, for_context=True)

    if row is None:
        missing.append("no_response_pattern")
        rel = _merge_operator_preferences(conn, RelationshipContext(), project_id)
        return rel, missing

    d = dict(row)
    avoid = _parse_list(d.get("avoid_phrases"))
    positive = _parse_list(d.get("positive_signals"))

    rel = RelationshipContext(
        preferred_greeting=d.get("preferred_greeting"),
        tone=d.get("tone"),
        response_length_preference=d.get("response_length_preference"),
        preferred_language=d.get("preferred_language"),
        avoid_phrases=avoid,
        positive_signals=positive,
    )

    # Merge operator preferences (fill gaps only)
    rel = _merge_operator_preferences(conn, rel, project_id)

    return rel, missing


def _merge_operator_preferences(
    conn: sqlite3.Connection,
    rel: RelationshipContext,
    project_id: str | None,
) -> RelationshipContext:
    """Merge operator preferences into relationship context (fill gaps only)."""
    if not project_id:
        return rel
    try:
        from src.repositories.preference_repo import get_effective_preference
        pref = get_effective_preference(conn, project_id)
        updates = {}
        if not rel.tone and pref.tone:
            updates["tone"] = pref.tone
        if not rel.preferred_language and pref.language:
            updates["preferred_language"] = pref.language
        if updates:
            rel = rel.model_copy(update=updates)
    except Exception:
        pass
    return rel


def _parse_list(raw: str | None) -> list[str]:
    """Parse a comma-separated or JSON list string."""
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        import json
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return [s.strip() for s in raw.split(",") if s.strip()]
