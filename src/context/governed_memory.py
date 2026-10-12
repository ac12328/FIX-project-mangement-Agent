"""Governed-memory context assembly shared by production surfaces."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from config import defaults
from src.context.memory_injection import MemoryInjectionAdapter, MemorySurface
from src.memory.retriever import MemoryRetrievalRequest


@dataclass(frozen=True)
class AuthorizationContext:
    operator_id: str | None
    project_id: str | None = None
    contact_id: str | None = None
    counterparty_id: str | None = None
    market: str | None = None
    organization_id: str | None = None


def authorize_project_operator(
    conn: sqlite3.Connection,
    project_id: str,
    operator_id: str | None,
    *,
    trusted_internal: bool = False,
) -> str | None:
    """Return the authorized operator without loading project business fields."""
    row = conn.execute(
        "SELECT operator_id FROM projects WHERE project_id=?", (project_id,),
    ).fetchone()
    owner_id = (row["operator_id"] or None) if row else None
    if trusted_internal and not operator_id:
        return owner_id
    if operator_id and owner_id and operator_id == owner_id:
        return operator_id
    return None


def surface_v2_enabled(surface: str) -> bool:
    return {
        "chat": defaults.MEMORY_RETRIEVAL_V2_CHAT,
        "web": defaults.MEMORY_RETRIEVAL_V2_WEB,
        "draft": defaults.MEMORY_RETRIEVAL_V2_DRAFT,
        "advisory": defaults.MEMORY_RETRIEVAL_V2_ADVISORY,
        "tool": defaults.MEMORY_RETRIEVAL_V2_TOOL,
    }[surface]


def legacy_authority_suppressed(surface: str) -> bool:
    """Compatibility helper; request-specific callers use compose_governed_memory."""
    return surface_v2_enabled(surface) and not defaults.MEMORY_RETRIEVER_SHADOW


def resolve_authorization_context(
    conn: sqlite3.Connection,
    *,
    operator_id: str | None,
    project_id: str | None = None,
    contact_id: str | None = None,
    counterparty_id: str | None = None,
    organization_id: str | None = None,
    trusted_project_operator_fallback: bool = False,
) -> AuthorizationContext:
    market = None
    if project_id:
        row = conn.execute(
            "SELECT operator_id,market FROM projects WHERE project_id=?", (project_id,),
        ).fetchone()
        if row:
            project_operator_id = row["operator_id"] or None
            if operator_id and project_operator_id and operator_id != project_operator_id:
                operator_id = None
            elif not operator_id and trusted_project_operator_fallback:
                operator_id = project_operator_id
            market = row["market"]
        party = conn.execute(
            """SELECT org_id,party_role FROM project_parties
               WHERE project_id=? ORDER BY is_primary DESC,id ASC""", (project_id,),
        ).fetchall()
        for item in party:
            if item["party_role"] == "counterparty" and not counterparty_id:
                counterparty_id = item["org_id"]
            if item["party_role"] in {"client", "counterparty"} and not organization_id:
                organization_id = item["org_id"]
    if contact_id and not organization_id:
        row = conn.execute(
            "SELECT organisation_id FROM contacts WHERE contact_id=?", (contact_id,),
        ).fetchone()
        organization_id = row[0] if row and row[0] else None
    return AuthorizationContext(
        operator_id=operator_id,
        project_id=project_id,
        contact_id=contact_id,
        counterparty_id=counterparty_id,
        market=market,
        organization_id=organization_id,
    )



def compatibility_memory_request(conn, *, operator_id, project_id=None, contact_id=None,
                                 surface="chat", task_type=None):
    """Bind legacy projection eligibility to the same trusted request scope."""
    auth = resolve_authorization_context(conn, operator_id=operator_id,
        project_id=project_id, contact_id=contact_id)
    return MemoryRetrievalRequest(operator_id=auth.operator_id, project_id=auth.project_id,
        contact_id=auth.contact_id, counterparty_id=auth.counterparty_id,
        organization_id=auth.organization_id, market=auth.market,
        surface=surface, task_type=task_type, at=datetime.now(timezone.utc))


def compose_governed_memory(
    conn: sqlite3.Connection,
    legacy_text: str,
    *,
    surface: str,
    query: str,
    profile: str,
    operator_id: str | None,
    project_id: str | None,
    contact_id: str | None = None,
    counterparty_id: str | None = None,
    organization_id: str | None = None,
    task_type: str | None = None,
    trusted_project_operator_fallback: bool = False,
):
    # Resolve authorization before any governed retrieval or cohort-specific use.
    auth = resolve_authorization_context(
        conn,
        operator_id=operator_id,
        project_id=project_id,
        contact_id=contact_id,
        counterparty_id=counterparty_id,
        organization_id=organization_id,
        trusted_project_operator_fallback=trusted_project_operator_fallback,
    )
    from src.learning.rollout import RolloutMode, resolve_effective_mode
    rollout = resolve_effective_mode(
        conn, surface=surface, operator_id=auth.operator_id, project_id=auth.project_id,
    )
    if rollout.mode is RolloutMode.LEGACY:
        from src.context.memory_injection import MemoryInjectionResult
        return MemoryInjectionResult(text=legacy_text, used_v2=False)
    request = MemoryRetrievalRequest(
        operator_id=auth.operator_id,
        project_id=auth.project_id,
        contact_id=auth.contact_id,
        counterparty_id=auth.counterparty_id,
        market=auth.market,
        organization_id=auth.organization_id,
        task_type=task_type,
        surface=surface,
        profile=profile,
        query=query,
        at=datetime.now(timezone.utc),
    )
    return MemoryInjectionAdapter().inject(
        legacy_text=legacy_text,
        request=request,
        surface=MemorySurface(surface),
        v2_enabled=rollout.mode is RolloutMode.V2,
        shadow_enabled=rollout.mode is RolloutMode.SHADOW,
        retrieval_disabled=defaults.MEMORY_RETRIEVAL_DISABLED,
        telemetry_conn=conn,
        cohort=rollout.cohort,
        policy_version=rollout.policy_version,
    )


def inject_governed_memory(conn: sqlite3.Connection, legacy_text: str, **kwargs) -> str:
    """Backward-compatible text-only facade."""
    return compose_governed_memory(conn, legacy_text, **kwargs).text
