"""
Harness-level project scope policy (all projects, not case-specific).

Precedence for effective project_id:
  1. Explicit ID in the current user message (always wins)
  2. Explicit negative scope (not related to any current project)
  3. Portfolio / ad-hoc / standalone knowledge scope
  4. Sticky active project / conversation inference only when the message
     does not name another project or request standalone guidance

Also provides:
  - Scope system text for the LLM every turn/round
  - ContextVar so tool execution can fill missing project_id args when
    the operator named a project (prevents sticky-active wrong answers)
"""
from __future__ import annotations

import re
import sqlite3
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

# Tools that accept project_id and should inherit explicit message scope
# when the model omits it.
_PROJECT_SCOPED_TOOLS = frozenset({
    "get_project_status",
    "get_project_truth",
    "get_project_emails",
    "search_emails",
    "get_email_body",
    "load_project_context",
    "get_working_memory",
    "get_activity_log",
    "get_advisory",
    "manage_followup",
    "list_internal_actions",
    "create_internal_action",
    "complete_internal_action",
    "update_project",
    "update_project_batch",
    "confirm_update",
    "cancel_update",
    "add_note",
    "log_evidence",
    "project_reconcile",
    "draft_reply",
    "list_drafts",
    "get_draft",
    "edit_draft",
    "discard_draft",
    "list_contacts",
    "add_contact",
    "get_relationship_prefs",
})

_forced_project_id: ContextVar[str | None] = ContextVar(
    "forced_project_id", default=None,
)


_NO_CURRENT_PROJECT_PATTERNS = (
    re.compile(
        r"\b(?:not|isn['\u2019]?t|unrelated)\b.{0,48}"
        r"\b(?:current|existing|active)\b.{0,24}\bprojects?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:standalone|ad[ -]?hoc|general)\b.{0,40}\b(?:not|without)\b"
        r".{0,24}\bprojects?\b",
        re.IGNORECASE,
    ),
    re.compile(
        "(?:\u4e0d|\u4e26\u4e0d|\u4e0d\u662f|\u7121).{0,12}"
        "(?:\u76f8\u95dc|\u5c6c\u65bc|\u95dc\u806f).{0,16}"
        "(?:\u76ee\u524d|\u73fe\u6709|\u7576\u524d|\u9032\u884c\u4e2d).{0,8}"
        "\u5c08\u6848"
    ),
    re.compile(
        "(?:\u76ee\u524d|\u73fe\u6709|\u7576\u524d|\u9032\u884c\u4e2d).{0,8}"
        "\u5c08\u6848.{0,12}(?:\u7121\u95dc|\u4e0d\u76f8\u95dc|\u6c92\u6709\u95dc\u806f)"
    ),
)


@dataclass(frozen=True)
class ProjectScope:
    """Resolved scope for one agent turn."""
    effective_project_id: str | None
    explicit_from_message: bool
    source: str  # explicit | active | none | portfolio_block | project_scope_none | general_knowledge


def requests_no_current_project_scope(user_text: str) -> bool:
    """Return whether the operator explicitly rejects current-project scope."""
    text = str(user_text or "").strip()
    return bool(text and any(pattern.search(text) for pattern in _NO_CURRENT_PROJECT_PATTERNS))


def get_forced_project_id() -> str | None:
    return _forced_project_id.get()


def set_forced_project_id(project_id: str | None) -> Token:
    return _forced_project_id.set(project_id)


def reset_forced_project_id(token: Token) -> None:
    _forced_project_id.reset(token)


def resolve_project_scope(
    conn: sqlite3.Connection | None,
    user_text: str,
    chat_id: int | None = None,
    operator_id: str | None = None,
) -> ProjectScope:
    """Canonical harness project resolution for any operator turn."""
    from src.agent.intent import (
        detect_project_id,
        is_adhoc_email_request,
        is_portfolio_query,
    )
    from src.agent.project_resolve import resolve_project_from_db

    explicit: str | None = None
    source = "none"
    resolved = None
    if conn is not None:
        resolved = resolve_project_from_db(conn, user_text or "")
        if not resolved.ambiguous and resolved.project_id:
            explicit = resolved.project_id
            source = resolved.source or "explicit"
    if not explicit:
        pid = detect_project_id(user_text or "")
        if pid:
            explicit = pid
            source = "explicit"

    if explicit:
        return ProjectScope(explicit, True, source)
    if resolved is not None and resolved.source == "unresolved_explicit":
        return ProjectScope(None, True, "unresolved_explicit")

    # A negative scope instruction is deterministic and must run before sticky
    # active-project or conversation fallback. Explicit named project IDs still
    # win because they are resolved above.
    if requests_no_current_project_scope(user_text or ""):
        from src.agent.control_plane_metrics import control_plane_metrics

        control_plane_metrics.increment("negative_scope", outcome="cleared")
        return ProjectScope(None, False, "project_scope_none")

    # General/plural portfolio wording must not inherit a stale active project.
    # Explicit identifiers were resolved above, so an explicit project-status request
    # remains selected-project scope while "latest update of the projects" is
    # correctly portfolio-scoped.
    if is_portfolio_query(user_text or ""):
        return ProjectScope(None, False, "portfolio_block")

    # Ad-hoc compose (send/draft to an address, no project named) must NOT
    # inherit sticky active project; otherwise internal handoffs get bound
    # to the last case and draft_reply uses the wrong envelope.
    if is_adhoc_email_request(user_text or ""):
        return ProjectScope(None, False, "adhoc_compose")

    # Reuse the existing knowledge guard, but preserve project-relative questions.
    from src.agent.guards import is_general_knowledge_query

    project_relative = re.search(
        r"\b(?:this|that|current|our|the)\s+(?:project|client|case)\b"
        r"|\b(?:the|our|current)\s+(?:next\s+action|milestone|status|ball\s+with)\b"
        r"|(?:\u9019\u500b|\u9019\u9805|\u6b64|\u76ee\u524d|\u8a72)(?:\u5c08\u6848|\u9805\u76ee|\u5ba2\u6236)", user_text or "", re.IGNORECASE,
    )
    if not project_relative and is_general_knowledge_query(user_text):
        return ProjectScope(None, False, "general_knowledge")

    # Sticky / conversation only when message did not name a project
    active: str | None = None
    if chat_id is not None:
        from src.agent.active_project import resolve_active_project_id

        active = resolve_active_project_id(conn, chat_id, operator_id=operator_id)
        if active:
            return ProjectScope(active, False, "active")

    return ProjectScope(None, False, "none")


def project_scope_system_message(scope: ProjectScope, user_text: str) -> dict[str, str] | None:
    """System meta for the model — applies to every project equally."""
    if not scope.effective_project_id:
        if scope.source == "general_knowledge":
            return {"role": "system", "content": (
                '<project-scope force="false" mode="none">'
                "This is standalone general knowledge guidance. Do not inherit sticky or "
                "conversation-inferred project context, project values, or project writes. "
                "Use approved general knowledge sources.</project-scope>"
            )}
        if scope.source == "portfolio_block":
            return {
                "role": "system",
                "content": (
                    "<project-scope>\n"
                    "This is a portfolio / multi-project question. "
                    "Do NOT answer for a single sticky active project only. "
                    "Query Active + Blocked + Waiting as appropriate.\n"
                    "</project-scope>"
                ),
            }
        if scope.source == "adhoc_compose":
            return {
                "role": "system",
                "content": (
                    "<project-scope force=\"false\">\n"
                    "Operator requested an **ad hoc email** (standalone; not bound "
                    "to a project or support ticket). Do NOT use sticky/active "
                    "project context.\n"
                    "Use draft_reply with to_email=<recipient>, omit project_id, "
                    "context=<what to say / attach>. If To is missing, ask for it. "
                    "Operator confirms before send.\n"
                    "</project-scope>"
                ),
            }
        if scope.source == "unresolved_explicit":
            return {
                "role": "system",
                "content": (
                    "<project-scope force=\"false\">\n"
                    "The operator supplied a project-like ID, but it did not match "
                    "a known project and could not be safely corrected. Do NOT use "
                    "the sticky active project or wiki fallback. Ask the operator "
                    "to verify the project ID.\n"
                    "</project-scope>"
                ),
            }
        if scope.source == "project_scope_none":
            return {
                "role": "system",
                "content": (
                    "<project-scope force=\"false\" mode=\"none\">\n"
                    "The operator explicitly said this request is not related "
                    "to any current or existing project. Do NOT use sticky active "
                    "project context, conversation-inferred project context, or "
                    "project-specific values. Treat it as standalone guidance.\n"
                    "</project-scope>"
                ),
            }
        return None

    pid = scope.effective_project_id
    if scope.explicit_from_message:
        correction = ""
        if scope.source == "typo_corrected":
            correction = (
                f"The supplied project ID contained a likely one-character typo; "
                f"the unique DB match is **{pid}**. Briefly tell the operator you "
                f"interpreted their ID as {pid}.\n"
            )
        body = (
            f"<project-scope force=\"true\">\n"
            f"The operator named project **{pid}** in this message.\n"
            f"{correction}"
            f"RULES (harness, all tools):\n"
            f"1. Every project-scoped tool MUST use project_id={pid} unless the "
            f"operator clearly asks about a different project or the portfolio.\n"
            f"2. Do NOT answer using a different sticky/active project.\n"
            f"3. Status, email, draft, and wiki answers must be grounded in {pid}.\n"
            f"</project-scope>"
        )
    else:
        body = (
            f"<project-scope force=\"false\">\n"
            f"Working project context (sticky/inferred): **{pid}**.\n"
            f"Use it for deictic references (this project / the project). "
            f"If the operator names another project ID, switch immediately.\n"
            f"</project-scope>"
        )
    return {"role": "system", "content": body}


def inject_project_id_args(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    forced_project_id: str | None = None,
    override_explicit: bool = False,
) -> dict[str, Any]:
    """Apply harness-owned project scope to project-scoped tool arguments.

    Normal explicit scope fills only missing IDs. A uniquely typo-corrected scope
    may also replace the malformed ID copied from the operator message.
    """
    pid = forced_project_id if forced_project_id is not None else get_forced_project_id()
    if not pid or tool_name not in _PROJECT_SCOPED_TOOLS:
        return arguments
    current = arguments.get("project_id")
    if (
        override_explicit
        or current is None
        or (isinstance(current, str) and not current.strip())
    ):
        out = dict(arguments)
        out["project_id"] = pid
        return out
    return arguments
