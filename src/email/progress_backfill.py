"""
Legacy regex hints for project next_action and optional ball from email text.

General for all projects — used by inbound email analysis backfill and status
answers so operators can see possible conflicts. These hints never establish milestone
completion; the versioned contract and deterministic evaluator own that decision.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

# Lower rank = more advanced progress (prefer best across a thread, not first hit).
_PROGRESS_RULES: list[tuple[str, int, re.Pattern[str], str, str | None]] = [
    (
        "pct_complete_launch",
        0,
        re.compile(
            r"(?:"
            r"all\s+(?:the\s+)?(?:production\s+)?(?:connectivity\s+)?tests?\s+(?:are\s+)?passed"
            r"|(?:all\s+)?pct\s+(?:tests?\s+)?(?:passed|complete[d]?)"
            r"|pct\s+(?:is\s+)?complete[d]?"
            r"|所有測試已通過|測試已通過|全部測試.*通過"
            r")"
            r"[\s\S]{0,400}?"
            r"(?:"
            r"production\s+launch|go[\s-]?live|ready\s+for\s+production|生產上線"
            r"|準備生產|上線安排|launch\s+arrangement|prepare\s+for\s+production"
            r")|"
            r"(?:"
            r"production\s+launch|go[\s-]?live|ready\s+for\s+production|生產上線"
            r"|準備生產|上線安排|launch\s+arrangement|prepare\s+for\s+production"
            r")"
            r"[\s\S]{0,400}?"
            r"(?:"
            r"all\s+(?:the\s+)?(?:production\s+)?(?:connectivity\s+)?tests?\s+(?:are\s+)?passed"
            r"|(?:all\s+)?pct\s+(?:tests?\s+)?(?:passed|complete[d]?)"
            r"|所有測試已通過|測試已通過"
            r")",
            re.IGNORECASE,
        ),
        "Arrange production launch / go-live with client after PCT completion "
        "(primary+backup logon and test-order ER path done).",
        "Internal",
    ),
    (
        "pct_complete",
        1,
        re.compile(
            r"all\s+(?:the\s+)?(?:production\s+)?(?:connectivity\s+)?tests?\s+(?:are\s+)?passed"
            r"|(?:all\s+)?pct\s+(?:tests?\s+)?(?:passed|complete[d]?)"
            r"|pct\s+(?:steps?\s+)?1\s*[-–]\s*6\s+(?:complete|done|passed)"
            r"|所有測試已通過|測試已通過|pct.*已完成|pct.*完成"
            r"|tests?\s+have\s+(?:all\s+)?passed|全部.*測試.*通過",
            re.IGNORECASE,
        ),
        "Record PCT pass evidence and arrange production launch / go-live schedule "
        "with client and Demo Provisioning/Network as needed.",
        "Internal",
    ),
    (
        "pct_order_pending",
        2,
        re.compile(
            r"(?:pct|production connectivity).{0,80}?"
            r"(?:step\s*5|steps?\s*5\s*[-–]\s*6|test\s+order|NewOrderSingle|下單)"
            r"|(?:next|下一步).{0,40}?(?:test\s+order|下單|execution\s+report)"
            r"|submit(?:\s+a)?\s+test\s+order|發送.*測試訂單",
            re.IGNORECASE | re.DOTALL,
        ),
        "Complete remaining PCT steps: submit production test order and confirm "
        "ExecutionReport (cancel/reject as expected), then record results.",
        "Internal",
    ),
    (
        "pct_failover_pending",
        3,
        re.compile(
            r"(?:primary|主).{0,40}?(?:logon|連線).{0,40}?(?:success|成功)"
            r".{0,80}?(?:backup|secondary|備).{0,40}?(?:logon|session|連線|測試)"
            r"|failover\s+to\s+(?:backup|secondary)|回退.*主\s*IP|secondary\s+IP",
            re.IGNORECASE | re.DOTALL,
        ),
        "Continue PCT: complete backup/primary failover logon+heartbeat, then "
        "test order + ExecutionReport path.",
        "Internal",
    ),
    (
        "pct_scheduled_vpn_ready",
        4,
        re.compile(
            r"(?:vpn|VPN).{0,60}?(?:granted|ready|開通|已授予|已就緒)"
            r"|(?:pct|production connectivity).{0,80}?(?:scheduled|arranged|schedule|本週|6:30)"
            r"|(?:will\s+)?schedule(?:\s+the)?\s+(?:production connectivity|pct)"
            r"|準備生產連線測試|安排.*PCT|PCT.*安排",
            re.IGNORECASE | re.DOTALL,
        ),
        "Execute scheduled PCT on production IPs (primary/backup logon, failover, "
        "test order) and capture results.",
        "Shared",
    ),
]

# DB next_action looks like "still waiting for PCT" while email says done
_STALE_PCT_NEXT_RE = re.compile(
    r"\bpct\b|production connectivity|confirm\s+pct|arranged\s+pct|"
    r"pct\s+date|pct\s+info|生產連線|確認.*pct",
    re.IGNORECASE,
)


# Legacy retrieval wording only. The deterministic evaluator decides whether M7
# exit criteria permit an M8 review candidate. This module only suggests the
# operator-facing next action and ownership hint.
_WIKI_PCT_PLAYBOOK = (
    "playbook/fix-connectivity/production-connectivity-test-pct-7-step-procedure"
)

_GO_LIVE_NEXT_ACTION = (
    "PCT completion indicated by recent email; review evidence against the "
    "versioned milestone contract, then arrange production launch / go-live or "
    "ask the operator to close if no residual work remains. "
    f"See wiki:{_WIKI_PCT_PLAYBOOK}"
)


@dataclass
class ProgressInference:
    rule_id: str
    next_action: str
    ball_with: str | None = None


def infer_progress_from_text(corpus: str) -> ProgressInference | None:
    """Return best (most advanced) progress inference, or None."""
    text = (corpus or "").strip()
    if not text:
        return None
    best: ProgressInference | None = None
    best_rank = 999
    for rule_id, rank, pattern, next_action, ball in _PROGRESS_RULES:
        if rank >= best_rank:
            continue
        if pattern.search(text):
            na = (
                _GO_LIVE_NEXT_ACTION
                if rule_id in {"pct_complete", "pct_complete_launch"}
                else next_action
            )
            best = ProgressInference(rule_id, na, ball)
            best_rank = rank
    return best


def backfill_progress_into_proposed_fields(
    proposed_fields: dict[str, Any] | None,
    *,
    summary: str = "",
    suggested_action: str = "",
    body: str = "",
    current_next_action: str | None = None,
    current_ball: str | None = None,
    current_milestone: str | None = None,
    allow_milestone: bool = False,
) -> dict[str, Any]:
    """Fill non-authoritative next-action/ownership hints from email text.

    ``current_milestone`` and ``allow_milestone`` remain compatibility-only for
    older callers. Regex matches never add ``current_milestone``; only the
    deterministic project-state evaluator may produce that review candidate.
    """
    fields = dict(proposed_fields or {})
    corpus = " ".join(
        filter(None, [summary, suggested_action, (body or "")[:3000]])
    )
    inferred = infer_progress_from_text(corpus)
    if not inferred:
        return fields

    proposed_na = (fields.get("next_action") or "").strip()
    db_na = (current_next_action or "").strip()

    should_set_na = False
    if not proposed_na:
        should_set_na = True
    elif (
        inferred.rule_id in {"pct_complete", "pct_complete_launch"}
        and _STALE_PCT_NEXT_RE.search(proposed_na)
    ):
        should_set_na = True

    if should_set_na:
        fields["next_action"] = inferred.next_action

    # Ball: only fill if missing; email guards may still strip internal-sender ball
    if inferred.ball_with and not fields.get("ball_with"):
        if inferred.rule_id in {"pct_complete", "pct_complete_launch"}:
            fields["ball_with"] = inferred.ball_with

    return fields


def _milestone_before_pct(value: str | None) -> bool:
    if not value:
        return False
    try:
        from src.models.enums import Milestone

        return (
            Milestone(value).index
            < Milestone.M7_LOG_REVIEW_AND_PRODUCTION_SESSION_DELIVERY.index
        )
    except ValueError:
        return False


def load_email_progress_recommendation(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    email_limit: int = 12,
    current_next_action: str | None = None,
    current_milestone: str | None = None,
) -> dict[str, Any]:
    """Scan recent project emails for progress vs DB next_action conflict."""
    tag = f"%{project_id}%"
    rows = conn.execute(
        """
        SELECT subject, sender_email, processed_at, body_snippet, direction
        FROM processed_emails
        WHERE project_id = ?
           OR subject LIKE ?
           OR COALESCE(body_snippet, '') LIKE ?
        ORDER BY processed_at DESC
        LIMIT ?
        """,
        (project_id, tag, tag, email_limit),
    ).fetchall()

    # Score whole thread + each message; keep most advanced progress signal
    parts: list[str] = []
    source_at = None
    source_sender = None
    source_subject = None
    best: ProgressInference | None = None
    best_rank = 999
    rank_by_id = {rid: rank for rid, rank, *_ in _PROGRESS_RULES}

    for row in rows:
        r = dict(row)
        blob = f"{r.get('subject') or ''}\n{r.get('body_snippet') or ''}"
        parts.append(blob[:1500])
        inferred = infer_progress_from_text(blob)
        if inferred:
            rnk = rank_by_id.get(inferred.rule_id, 999)
            if rnk < best_rank:
                best = inferred
                best_rank = rnk
                source_at = (r.get("processed_at") or "")[:19]
                source_sender = r.get("sender_email")
                source_subject = r.get("subject")

    # Thread-level join (completion + launch may span multiple mails)
    joined = "\n".join(parts[:8])
    joined_inf = infer_progress_from_text(joined)
    if joined_inf:
        rnk = rank_by_id.get(joined_inf.rule_id, 999)
        if rnk < best_rank:
            best = joined_inf
            best_rank = rnk

    if best is None:
        return {
            "recommended_next_action": None,
            "rule_id": None,
            "conflict": False,
            "emails_scanned": len(rows),
        }

    if current_next_action is None or current_milestone is None:
        row = conn.execute(
            "SELECT next_action, current_milestone FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is not None:
            if current_next_action is None:
                current_next_action = row["next_action"]
            if current_milestone is None:
                current_milestone = row["current_milestone"]

    db_na = (current_next_action or "").strip()
    rec = best.next_action
    conflict = False
    pct_rule = best.rule_id.startswith("pct_")
    if db_na:
        # Conflict if DB still talks about PCT scheduling while email says complete
        if best.rule_id.startswith("pct_complete") and _STALE_PCT_NEXT_RE.search(db_na):
            conflict = True
        elif db_na.lower()[:80] != rec.lower()[:80] and best.rule_id.startswith(
            "pct_complete"
        ):
            conflict = True
        elif best.rule_id != "pct_scheduled_vpn_ready" and _STALE_PCT_NEXT_RE.search(
            db_na
        ):
            # mid-PCT rules also supersede "arrange PCT date" style
            if best.rule_id in {"pct_order_pending", "pct_failover_pending"}:
                conflict = True
        elif pct_rule and not _STALE_PCT_NEXT_RE.search(db_na):
            # Email is on the PCT path; recorded next_action is an earlier-stage leftover.
            conflict = True
    if pct_rule and _milestone_before_pct(current_milestone):
        conflict = True

    return {
        "recommended_next_action": rec,
        "recommended_ball_with": best.ball_with,
        "rule_id": best.rule_id,
        "conflict": conflict,
        "source_at": source_at,
        "source_sender": source_sender,
        "source_subject": source_subject,
        "emails_scanned": len(rows),
        "current_milestone": current_milestone,
    }


def format_progress_conflict_block(payload: dict[str, Any]) -> str | None:
    """Operator-facing English block for status tool output."""
    rec = payload.get("recommended_next_action")
    if not rec:
        return None
    lines = [
        "Email-grounded progress (prefer when DB next_action looks stale):",
        f"  - Recommended next_action: {rec}",
    ]
    if payload.get("recommended_ball_with"):
        lines.append(f"  - Suggested ball_with: {payload['recommended_ball_with']}")
    if payload.get("conflict"):
        rule_id = str(payload.get("rule_id") or "")
        if rule_id.startswith("pct_complete"):
            lines.append(
                "  - CONFLICT: DB next_action may be outdated relative to recent emails. "
                "Show BOTH: DB next_action (possibly stale) AND email-grounded recommendation. "
                "Do NOT offer to re-schedule or re-confirm work that email says is done."
            )
        else:
            recorded = payload.get("current_milestone") or "recorded milestone"
            lines.append(
                "  - CONFLICT: recorded project state lags later-stage email evidence "
                f"({recorded}). Show BOTH: DB milestone/next_action AND email-grounded "
                "progress. Do NOT keep earlier-stage busywork the email has already passed."
            )
    if payload.get("source_at") or payload.get("source_subject"):
        lines.append(
            f"  - Source: {(payload.get('source_at') or '')} "
            f"{(payload.get('source_sender') or '')} | "
            f"{(payload.get('source_subject') or '')[:80]}"
        )
    return "\n".join(lines)
