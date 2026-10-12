"""
Host-turn delegate policy (Phase C harness).

Enforces one-plan / controlled multi-stage:
  - Prefer one skill per operator intent.
  - At most one *fresh write* skill per host turn.
  - After research/status, a write/draft stage must use resume_from.
  - Cap total fresh delegates; resume_from continues do not count as fresh.

Specialist loops never call delegate (tool excluded); ledger is host-only.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from src.agent.skills.registry import get_skill

# Skills that produce evidence the next stage should resume from
_EVIDENCE_SKILLS = frozenset({"research", "status", "email_to_truth"})
# Skills that consume evidence / mutate state — need resume after evidence skill
_FOLLOWON_SKILLS = frozenset({
    "draft",
    "project_write",
    "email_to_truth",
    "new_project",
    "followup",
    "contact",
    "support",
})

MAX_FRESH_DELEGATES = 2
MAX_FRESH_WRITES = 1
MAX_RESUMES = 2


@dataclass
class DelegateTurnLedger:
    """Per host-loop bookkeeping for delegate calls."""

    fresh_skills: list[str] = field(default_factory=list)
    resume_count: int = 0
    last_session_id: str | None = None
    last_skill_id: str | None = None
    last_was_evidence: bool = False
    blocked: list[str] = field(default_factory=list)


_ledger_var: ContextVar[DelegateTurnLedger | None] = ContextVar(
    "host_delegate_ledger", default=None
)


def reset_host_delegate_ledger() -> DelegateTurnLedger:
    """Start of a host (non-specialist) agent loop."""
    ledger = DelegateTurnLedger()
    _ledger_var.set(ledger)
    return ledger


def get_host_delegate_ledger() -> DelegateTurnLedger | None:
    return _ledger_var.get()


def clear_host_delegate_ledger() -> None:
    _ledger_var.set(None)


def _is_write_skill(skill_id: str | None) -> bool:
    skill = get_skill(skill_id or "")
    if skill is not None:
        return bool(skill.is_write)
    # Legacy specialist_type path
    return (skill_id or "").lower() in {
        "write", "draft", "project_write", "email_to_truth", "new_project",
        "followup", "contact", "support", "memory",
    }


def check_delegate_allowed(
    *,
    skill_id: str | None,
    resume_from: str | None,
    specialist_type: str | None = None,
) -> tuple[bool, str | None]:
    """
    Return (allowed, error_message).

    When ledger is None (no host turn / tests), allow all.
    """
    ledger = _ledger_var.get()
    if ledger is None:
        return True, None

    resolved = (skill_id or specialist_type or "research").strip().lower()
    is_resume = bool(resume_from and str(resume_from).strip())

    if is_resume:
        if ledger.resume_count >= MAX_RESUMES:
            msg = (
                f"Resume limit ({MAX_RESUMES}) reached this turn. "
                "Summarise for the operator or wait for the next message."
            )
            ledger.blocked.append(msg)
            return False, msg
        return True, None

    # ── Fresh spawn ────────────────────────────────────────────────
    if len(ledger.fresh_skills) >= MAX_FRESH_DELEGATES:
        hint = ""
        if ledger.last_session_id:
            hint = f" Prefer resume_from={ledger.last_session_id}."
        msg = (
            f"Fresh delegate limit ({MAX_FRESH_DELEGATES}) reached this turn."
            f"{hint} Use resume_from for a second stage, or finish with one skill."
        )
        ledger.blocked.append(msg)
        return False, msg

    write_fresh = sum(1 for s in ledger.fresh_skills if _is_write_skill(s))
    if _is_write_skill(resolved) and write_fresh >= MAX_FRESH_WRITES:
        hint = ""
        if ledger.last_session_id:
            hint = f" Use resume_from={ledger.last_session_id} if continuing prior work."
        msg = (
            "Only one fresh write skill per turn "
            f"(already used: {', '.join(ledger.fresh_skills)})."
            f"{hint} Prefer a composite skill (e.g. email_to_truth) in one delegate."
        )
        ledger.blocked.append(msg)
        return False, msg

    # Evidence skill then follow-on without resume → block
    if ledger.last_was_evidence and resolved in _FOLLOWON_SKILLS:
        sid = ledger.last_session_id or "<session_id from prior delegate>"
        msg = (
            f"Prior delegate was evidence skill '{ledger.last_skill_id}'. "
            f"Continue with resume_from={sid} instead of a fresh "
            f"delegate(skill_id={resolved}). "
            "This preserves tool evidence (emails/wiki)."
        )
        ledger.blocked.append(msg)
        return False, msg

    return True, None


def record_delegate_result(
    *,
    skill_id: str | None,
    resume_from: str | None,
    session_id: str | None,
    ok: bool,
    specialist_type: str | None = None,
) -> None:
    """Update ledger after a delegate attempt (success or policy-allowed run)."""
    ledger = _ledger_var.get()
    if ledger is None or not ok:
        return

    resolved = (skill_id or specialist_type or "research").strip().lower()
    is_resume = bool(resume_from and str(resume_from).strip())

    if is_resume:
        ledger.resume_count += 1
    else:
        ledger.fresh_skills.append(resolved)

    if session_id:
        ledger.last_session_id = session_id
    ledger.last_skill_id = resolved
    ledger.last_was_evidence = resolved in _EVIDENCE_SKILLS


def policy_error_payload(message: str, *, skill_id: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "delegate_policy",
        "summary": f"Delegate blocked by host policy: {message}",
        "findings": [],
        "skill_id": skill_id,
        "session_id": None,
    }
