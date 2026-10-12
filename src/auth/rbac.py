"""Local Dashboard RBAC policy and API permission resolution."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from fastapi import HTTPException

ROLE_VIEWER = "Viewer"
ROLE_EDITOR = "Editor"
ROLE_ADMIN = "Admin"
VALID_ROLES = frozenset({ROLE_VIEWER, ROLE_EDITOR, ROLE_ADMIN})
VALID_STATUSES = frozenset({"active", "disabled"})

P_SHARED_READ = "shared.read"
P_PERSONAL_WRITE = "personal.write"
P_ASSISTANT_READ = "assistant.read"
P_BUSINESS_WRITE = "business.write"
P_PROJECT_CLOSE = "project.close"
P_TRACE_SANITIZED = "traces.sanitized"
P_TRACE_DIAGNOSE = "traces.diagnose"
P_TRACE_REVEAL = "traces.reveal"
P_ADMIN_USERS = "admin.users"
P_ADMIN_SESSIONS = "admin.sessions"
P_ADMIN_SECURITY = "admin.security"
P_ADMIN_LLM = "admin.llm_config"
P_ADMIN_AGENT_CONTEXT = "admin.agent_context"
P_ADMIN_LEARNING = "admin.learning_governance"

_VIEWER = frozenset({P_SHARED_READ, P_PERSONAL_WRITE, P_ASSISTANT_READ})
_EDITOR = _VIEWER | frozenset({P_BUSINESS_WRITE, P_PROJECT_CLOSE, P_TRACE_SANITIZED, P_TRACE_DIAGNOSE})
_ADMIN = _EDITOR | frozenset({
    P_TRACE_REVEAL,
    P_ADMIN_USERS,
    P_ADMIN_SESSIONS,
    P_ADMIN_SECURITY,
    P_ADMIN_LLM,
    P_ADMIN_AGENT_CONTEXT,
    P_ADMIN_LEARNING,
})

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_VIEWER: _VIEWER,
    ROLE_EDITOR: _EDITOR,
    ROLE_ADMIN: _ADMIN,
}


class ActorLike(Protocol):
    role: str
    status: str
    permissions: Iterable[str]


def permissions_for_role(role: str) -> frozenset[str]:
    """Return the immutable permission set for a Dashboard role."""
    return ROLE_PERMISSIONS.get(role, frozenset())


def require_permission(actor: ActorLike, permission: str) -> None:
    """Raise HTTP 403 unless *actor* is active and owns *permission*."""
    if getattr(actor, "status", None) != "active":
        raise HTTPException(status_code=403, detail="User account is disabled")
    if permission not in frozenset(getattr(actor, "permissions", ())):
        raise HTTPException(status_code=403, detail=f"Permission required: {permission}")


def _is_personal_mutation(path: str) -> bool:
    prefixes = (
        "/api/preferences",
        "/api/notifications",
        "/api/work-items",
        "/api/conversation-context",
        "/api/assistant/brief/feedback",
    )
    return path.startswith(prefixes)


def _is_assistant_request(path: str, method: str) -> bool:
    if path.startswith("/api/assistant"):
        return method in {"GET", "HEAD", "OPTIONS"} or path.endswith("/turns")
    if method != "POST":
        return False
    return (
        path.startswith("/api/projects/") and path.endswith("/chat")
    ) or (
        path.startswith("/api/support/") and path.endswith("/chat")
    )


def permission_for_request(method: str, path: str) -> str | None:
    """Resolve the backend permission required for an authenticated API request.

    Returning ``None`` means authentication alone is sufficient. Public-path
    decisions remain in the application middleware.
    """
    method = method.upper()
    if path.startswith("/api/auth/"):
        return None
    if path.startswith("/api/go-live-deliveries/"):
        return P_ADMIN_SECURITY
    if path.startswith("/api/draft-card-deliveries/"):
        return P_ADMIN_SECURITY
    if path.startswith("/api/inbound-presentations/"):
        return P_ADMIN_SECURITY
    if path.startswith("/api/project-advisories/"):
        return P_ADMIN_SECURITY
    if path.startswith("/api/contact-identity-cards/"):
        return P_ADMIN_SECURITY
    if path.startswith("/api/support-deliveries/"):
        return P_ADMIN_SECURITY
    if path.startswith("/api/draft-outbox/"):
        return P_ADMIN_SECURITY
    if path.startswith("/api/inbound-email-work/"):
        return P_ADMIN_SECURITY
    if path.startswith("/api/admin/security-events"):
        return P_ADMIN_SECURITY
    if path.startswith("/api/admin/users"):
        return P_ADMIN_USERS
    if path.startswith("/api/admin/sessions"):
        return P_ADMIN_SESSIONS
    if path.startswith("/api/llm-config"):
        return P_ADMIN_LLM
    if (
        path.startswith("/api/traces/events/")
        or path.startswith("/api/traces/runs/")
    ) and path.endswith("/reveal"):
        return P_TRACE_REVEAL
    if path.startswith("/api/traces/") and path.endswith("/diagnose"):
        return P_TRACE_DIAGNOSE
    if path == "/api/traces" or path.startswith("/api/traces/"):
        return P_TRACE_SANITIZED
    if method not in {"GET", "HEAD", "OPTIONS"} and (
        path.startswith("/api/learning")
        or path.startswith("/api/learned-rules")
        or path.startswith("/api/wiki/pages/approve")
    ):
        return P_ADMIN_LEARNING
    if _is_assistant_request(path, method):
        return P_ASSISTANT_READ
    if method not in {"GET", "HEAD", "OPTIONS"} and path.startswith("/api/projects/"):
        if (
            "/closing" in path
            or path.endswith("/close")
            or path.endswith("/completion-approval")
        ):
            return P_PROJECT_CLOSE
    if path.startswith("/api/assistant"):
        return P_PERSONAL_WRITE
    if path.startswith("/api/operator-updates") or "/operator-updates" in path:
        return P_BUSINESS_WRITE
    if method in {"GET", "HEAD", "OPTIONS"}:
        return P_SHARED_READ
    if _is_personal_mutation(path):
        return P_PERSONAL_WRITE
    return P_BUSINESS_WRITE


def is_read_only_actor(actor: ActorLike) -> bool:
    """Return whether agent/tool execution must be forced read-only."""
    return P_BUSINESS_WRITE not in frozenset(getattr(actor, "permissions", ()))
