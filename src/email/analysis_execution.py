"""Stage inbound A-analysis follow-up intents until the email command commits."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator, Literal, Mapping, Any

from src.email.inbound_work import FollowupIntentReceipt


@dataclass(frozen=True)
class FollowupIntent:
    project_id: str
    email_id: str
    description: str
    due_date: str | None


@dataclass
class AnalysisWritePolicy:
    """Trusted, source-bound policy carried through the model and delegate loops."""

    project_id: str
    email_id: str
    followups: list[FollowupIntent] = field(default_factory=list)

    def authorize_tool(self, name: str, side_effect_type: str) -> None:
        if side_effect_type in {"none", "read"}:
            return
        if name not in {"manage_followup", "report_email_analysis"}:
            raise PermissionError(f"Tool '{name}' cannot write during inbound analysis")

    def authorize_arguments(self, name: str, args: Mapping[str, Any]) -> None:
        if name == "manage_followup":
            if args.get("project_id") != self.project_id or args.get("action") not in {"create", "list"}:
                raise PermissionError("Only source-project follow-up creation/listing is allowed during inbound analysis")
        elif name == "report_email_analysis":
            if args.get("project_id") != self.project_id or args.get("analysis_ref") != self.email_id:
                raise PermissionError("Inbound analysis report must match its trusted project and email")

    def stage_followup(self, *, project_id: str, description: str, due_date: str | None) -> None:
        if project_id != self.project_id or not description:
            raise PermissionError("Follow-up intent must belong to the inbound project")
        intent = FollowupIntent(project_id, self.email_id, description, due_date)
        if intent not in self.followups:
            self.followups.append(intent)


_active_policy: ContextVar[AnalysisWritePolicy | None] = ContextVar(
    "inbound_analysis_write_policy", default=None,
)


def active_analysis_policy() -> AnalysisWritePolicy | None:
    return _active_policy.get()


@contextmanager
def inbound_analysis_scope(project_id: str, email_id: str) -> Iterator[AnalysisWritePolicy]:
    if not project_id or not email_id:
        raise ValueError("Inbound analysis requires a project and source email")
    policy = AnalysisWritePolicy(project_id, email_id)
    token = _active_policy.set(policy)
    try:
        yield policy
    finally:
        _active_policy.reset(token)


def persist_followup_intents(
    conn: sqlite3.Connection, intents: tuple[FollowupIntent, ...],
    *, project_id: str, source_ref: str,
) -> tuple[FollowupIntentReceipt, ...]:
    """Apply source-bound, retry-safe intents inside the caller's A savepoint."""
    from src.portfolio.followup_tracker import create_followup

    outcomes = []
    for intent in intents:
        if (intent.project_id != project_id or intent.email_id != source_ref
                or not intent.description):
            raise ValueError("Follow-up intent does not match the application source and project")
        prior = conn.execute(
            "SELECT followup_id FROM internal_followups "
            "WHERE project_id=? AND linked_message_ref=? AND description=? AND "
            "COALESCE(due_date, '')=COALESCE(?, '') LIMIT 1",
            (intent.project_id, intent.email_id, intent.description, intent.due_date),
        ).fetchone()
        if prior is not None:
            followup_id = prior[0]
            status = "existing_in_application_transaction"
        else:
            followup_id = create_followup(
                conn, project_id=intent.project_id, description=intent.description,
                due_date=intent.due_date, linked_message_ref=intent.email_id,
                created_by="operator", commit=False,
            )
            status = "created_in_application_transaction"
        row = conn.execute(
            "SELECT project_id, linked_message_ref, description, due_date, status "
            "FROM internal_followups WHERE followup_id=?", (followup_id,),
        ).fetchone()
        if row is None or tuple(row[:4]) != (
            intent.project_id, intent.email_id, intent.description, intent.due_date,
        ) or row[4] not in {"open", "completed", "overdue", "cancelled"}:
            raise ValueError("Follow-up effect does not match its source-bound intent")
        outcomes.append(FollowupIntentReceipt(
            project_id=intent.project_id, source_ref=intent.email_id,
            description=intent.description, due_date=intent.due_date,
            followup_id=followup_id, status=status,
        ))
    return tuple(outcomes)


def unapplied_followup_intents(
    intents: tuple[FollowupIntent, ...], *, project_id: str, source_ref: str,
    reason: Literal["missing_stored_analysis", "application_incomplete"],
) -> tuple[FollowupIntentReceipt, ...]:
    """Account for staged intents that never passed the A application boundary."""
    outcomes = []
    for intent in intents:
        if (intent.project_id != project_id or intent.email_id != source_ref
                or not intent.description):
            raise ValueError("Follow-up intent does not match the application source and project")
        outcomes.append(FollowupIntentReceipt(
            project_id=intent.project_id, source_ref=intent.email_id,
            description=intent.description, due_date=intent.due_date,
            status="not_applied", reason=reason,
        ))
    return tuple(outcomes)
