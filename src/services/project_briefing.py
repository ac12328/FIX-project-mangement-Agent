"""Evidence-aware project briefing generation."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.email.body_cleaner import clean_email_body
from src.llm.client import llm_call
from src.models.enums import BallWith, MILESTONE_DISPLAY_NAMES
from src.repositories.project_repo import get_project
from src.services.project_briefing_evidence import (
    extract_pct_datetimes,
    extract_telnet_results,
    is_pct_confirmation,
    is_pct_proposal,
)
from src.wiki.domain_index import get_wiki_domain_index

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
_BRIEFING_CACHE_VERSION = 2
_REPLY_PREFIX = re.compile(r"^(?:(?:re|fw|fwd)\s*:\s*)+", re.I)
_PROJECT_TAG = re.compile(r"\[[A-Z]{1,8}\d{4,}\]", re.I)
_TAG1 = re.compile(r"\bTag1\s*=\s*([A-Za-z0-9_-]+)\b", re.I)
_STOCK = re.compile(
    r"\b(?:test\s+stock|stock\s+code|stock)\s*(?:code)?\s*(?:is|:|=)?\s*([0-9]{4,5})\b",
    re.I,
)
_MILESTONE_MENTION_RE = re.compile(r"\bM([1-8])(?:\b|[_-])", re.I)
_CANNED_EXIT_NEXT_RE = re.compile(
    r"^Obtain eligible evidence for M([1-8])-EXIT-\d+\.?$",
    re.I,
)
_GENERIC_NEXT_ACTION = (
    "Review the latest project evidence and confirm the next operational step."
)

@dataclass(frozen=True)
class _EmailEvidence:
    email_id: str
    occurred_at: str
    sender_email: str
    subject: str
    body: str
    direction: str
    thread_id: str | None
    thread_fp: str | None


def get_project_briefing(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    """Build a current project briefing from canonical state and recent email evidence."""
    project = get_project(conn, project_id)
    if project is None:
        return None

    emails = _load_latest_thread(conn, project_id, limit=8)
    fingerprint = _fingerprint(project, emails)
    if not force:
        with _CACHE_LOCK:
            cached = _CACHE.get(project_id)
            if cached and cached[0] == fingerprint:
                return dict(cached[1])

    deterministic = _build_deterministic_briefing(conn, project, emails)
    llm_payload = None
    if not deterministic.get("_strong_evidence"):
        llm_payload = _generate_llm_briefing(project, emails, deterministic)
    result = _merge_briefings(
        deterministic,
        llm_payload,
        canonical_milestone=project.current_milestone.value if project.current_milestone else None,
    )
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["evidence_cutoff_at"] = emails[-1].occurred_at if emails else None
    result["source_count"] = len(emails)

    with _CACHE_LOCK:
        _CACHE[project_id] = (fingerprint, dict(result))
    return result


def clear_project_briefing_cache() -> None:
    """Clear the process-local briefing cache (primarily for tests)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _load_latest_thread(
    conn: sqlite3.Connection, project_id: str, *, limit: int
) -> list[_EmailEvidence]:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(processed_emails)")}
    received_expr = "received_at" if "received_at" in columns else "NULL"
    rows = conn.execute(
        f"""SELECT email_id, sender_email, subject, body_snippet, direction,
                   thread_id, thread_fp,
                   COALESCE({received_expr}, processed_at) AS evidence_at
            FROM processed_emails
            WHERE project_id=?
            ORDER BY COALESCE({received_expr}, processed_at) DESC, email_id DESC
            LIMIT 40""",
        (project_id,),
    ).fetchall()
    if not rows:
        return []

    newest = dict(rows[0])
    thread_values = {
        value for value in (newest.get("thread_id"), newest.get("thread_fp")) if value
    }
    normalized_subject = _normalize_subject(newest.get("subject") or "")
    selected: list[_EmailEvidence] = []
    for index, raw in enumerate(rows):
        row = dict(raw)
        same_thread = bool(
            thread_values.intersection({row.get("thread_id"), row.get("thread_fp")})
        )
        same_subject = bool(
            normalized_subject
            and _normalize_subject(row.get("subject") or "") == normalized_subject
        )
        if index == 0 or same_thread or same_subject:
            body = clean_email_body(row.get("body_snippet"))
            if body:
                selected.append(
                    _EmailEvidence(
                        email_id=row["email_id"],
                        occurred_at=row.get("evidence_at") or "",
                        sender_email=row.get("sender_email") or "",
                        subject=row.get("subject") or "",
                        body=body,
                        direction=row.get("direction") or "inbound",
                        thread_id=row.get("thread_id"),
                        thread_fp=row.get("thread_fp"),
                    )
                )
        if len(selected) >= limit:
            break
    selected.reverse()
    return selected


def _normalize_subject(subject: str) -> str:
    value = _PROJECT_TAG.sub("", subject or "")
    value = _REPLY_PREFIX.sub("", value).lower()
    return " ".join(value.split())


def _fingerprint(project: Any, emails: list[_EmailEvidence]) -> str:
    payload = {
        "version": _BRIEFING_CACHE_VERSION,
        "project": {
            "status": project.project_status.value,
            "ball_with": project.ball_with.value,
            "milestone": project.current_milestone.value if project.current_milestone else None,
            "next_action": project.next_action,
            "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        },
        "emails": [email.__dict__ for email in emails],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _canonical_milestone_number(milestone: str | None) -> str | None:
    if not milestone:
        return None
    match = re.match(r"M([1-8])", str(milestone))
    return match.group(1) if match else None


def _mentioned_milestone_numbers(text: str) -> set[str]:
    return set(_MILESTONE_MENTION_RE.findall(text or ""))


def _text_contradicts_milestone(text: str, milestone: str | None) -> bool:
    canonical = _canonical_milestone_number(milestone)
    if not canonical:
        return False
    return bool(_mentioned_milestone_numbers(text) - {canonical})


def _usable_next_action(next_action: str | None, milestone: str | None) -> str | None:
    text = " ".join((next_action or "").split()).strip()
    if not text:
        return None
    match = _CANNED_EXIT_NEXT_RE.match(text)
    if not match:
        return text
    canonical = _canonical_milestone_number(milestone)
    if canonical and match.group(1) != canonical:
        return None
    return text


def _state_with_milestone(state: str, milestone_display: str | None) -> str:
    text = (state or "").strip()
    if not milestone_display:
        return text
    if text.lower().startswith("project is at"):
        return text
    if not text:
        return f"Project is at {milestone_display}."
    return f"Project is at {milestone_display}. {text}"


def _actor(conn: sqlite3.Connection, email: str, direction: str) -> tuple[str, str]:
    if direction == "outbound":
        return "Internal team", "internal"
    normalized = (email or "").lower().strip()
    row = conn.execute(
        """SELECT display_name FROM contacts
           WHERE lower(primary_email)=?
              OR lower(',' || replace(email_addresses, ' ', '') || ',') LIKE ?
           LIMIT 1""",
        (normalized, f"%,{normalized},%"),
    ).fetchone()
    name = row[0] if row and row[0] else ""
    if not name:
        local = normalized.split("@", 1)[0]
        name = " ".join(part.capitalize() for part in re.split(r"[._-]+", local) if part) or "Unknown sender"
    domain = normalized.split("@", 1)[1] if "@" in normalized else ""
    role = "internal" if get_wiki_domain_index().is_internal(domain) else "external"
    return name, role


def _build_deterministic_briefing(
    conn: sqlite3.Connection, project: Any, emails: list[_EmailEvidence]
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    pct_confirmations: list[tuple[str, str]] = []
    pct_proposals: list[tuple[str, list[str]]] = []
    stocks: list[tuple[str, str]] = []
    primary_telnet = False
    backup_telnet = False

    for email in emails:
        text = email.body
        lower = text.lower()
        details: list[str] = []
        event_type = "project_update"
        pct_options = extract_pct_datetimes(text, email.occurred_at)
        pct_at = pct_options[0] if pct_options else None
        is_confirmation = is_pct_confirmation(lower)
        is_proposal = is_pct_proposal(lower, pct_options)

        primary_passed, backup_passed, generic_passed = extract_telnet_results(text)
        if primary_passed or backup_passed or generic_passed:
            event_type = "connectivity_result"
            if primary_passed:
                primary_telnet = True
                details.append("Production primary telnet passed")
            if backup_passed:
                backup_telnet = True
                details.append("Production backup telnet passed")
            if not details:
                details.append("Production telnet passed")

        if pct_at and is_confirmation:
            event_type = "pct_confirmed"
            pct_confirmations.append((email.occurred_at, pct_at))
            details.append(f"PCT confirmed for {pct_at}")
        elif pct_options and is_proposal:
            event_type = "pct_proposed"
            pct_proposals.append((email.occurred_at, pct_options))
            details.append(f"PCT proposed for {' or '.join(pct_options)}")

        stock_match = _STOCK.search(text)
        if stock_match:
            stock = stock_match.group(1)
            stocks.append((email.occurred_at, stock))
            details.append(f"Test stock {stock} specified")

        tag_match = _TAG1.search(text)
        if tag_match:
            details.append(f"Test order Tag1={tag_match.group(1)} specified")

        if not details:
            details.append(_one_line(email.body))
        actor_name, actor_role = _actor(conn, email.sender_email, email.direction)
        events.append(
            {
                "source_ref": email.email_id,
                "occurred_at": email.occurred_at,
                "actor_name": actor_name,
                "actor_role": actor_role,
                "event_type": event_type,
                "summary": "; ".join(dict.fromkeys(details)),
            }
        )

    combined = "\n".join([project.next_action or "", *(email.body for email in emails)])
    tag_match = _TAG1.search(combined)
    tag1 = tag_match.group(1) if tag_match else None
    confirmed_pct = pct_confirmations[-1][1] if pct_confirmations else None
    proposed_pct = pct_proposals[-1][1] if pct_proposals else []
    stock = stocks[-1][1] if stocks else None
    milestone = project.current_milestone.value if project.current_milestone else None
    milestone_display = (
        MILESTONE_DISPLAY_NAMES.get(milestone, milestone) if milestone else None
    )
    stored_next = _usable_next_action(project.next_action, milestone)

    connectivity_state = None
    if primary_telnet and backup_telnet:
        connectivity_state = "Production primary and backup telnet connectivity passed"
    elif primary_telnet:
        connectivity_state = "Production primary telnet connectivity passed"
    elif backup_telnet:
        connectivity_state = "Production backup telnet connectivity passed"

    strong_evidence = bool(confirmed_pct or proposed_pct or connectivity_state or stock)
    if confirmed_pct:
        state_parts = [connectivity_state] if connectivity_state else []
        state_parts.append(f"PCT is confirmed for {confirmed_pct}")
        if stock:
            state_parts.append(f"test stock {stock} is specified")
        current_state = _state_with_milestone("; ".join(state_parts) + ".", milestone_display)

        execution = "including failover and a test order"
        if tag1:
            execution += f" with Tag1={tag1}"
        if stock:
            execution += f" using stock {stock}"
        next_action = f"Run PCT on {confirmed_pct}, {execution}, then record the outcome."
        ball_with = BallWith.INTERNAL.value
        blocker = "No active blocker"
    elif proposed_pct:
        state_parts = []
        if connectivity_state:
            state_parts.append(connectivity_state)
        state_parts.append(
            f"PCT was proposed for {' or '.join(proposed_pct)} and awaits internal confirmation"
        )
        if stock:
            state_parts.append(f"test stock {stock} is specified")
        current_state = _state_with_milestone("; ".join(state_parts) + ".", milestone_display)
        next_action = stored_next or (
            f"Confirm one of the proposed PCT slots ({' or '.join(proposed_pct)}) "
            "and record the agreed test plan."
        )
        ball_with = BallWith.INTERNAL.value
        blocker = _canonical_blocker(project)
    elif connectivity_state or stock:
        state_parts = [connectivity_state] if connectivity_state else []
        if stock:
            state_parts.append(f"Test stock {stock} is specified")
        current_state = _state_with_milestone("; ".join(state_parts) + ".", milestone_display)
        next_action = stored_next or _GENERIC_NEXT_ACTION
        ball_with = project.ball_with.value
        blocker = _canonical_blocker(project)
    else:
        if milestone_display:
            current_state = f"Project is at {milestone_display}."
        else:
            current_state = stored_next or "Project state is not yet recorded."
        next_action = stored_next or _GENERIC_NEXT_ACTION
        ball_with = project.ball_with.value
        blocker = _canonical_blocker(project)

    return {
        "current_state": current_state,
        "blocker": blocker,
        "recommended_action": next_action,
        "next_action": next_action,
        "has_blocker": blocker.lower() != "no active blocker",
        "ball_with": ball_with,
        "events": events,
        "_strong_evidence": strong_evidence,
    }


def _canonical_blocker(project: Any) -> str:
    if project.project_status.value == "Blocked":
        return project.next_action or "Project is marked blocked; review the latest evidence."
    return "No active blocker"


def _generate_llm_briefing(
    project: Any,
    emails: list[_EmailEvidence],
    deterministic: dict[str, Any],
) -> dict[str, Any] | None:
    evidence_lines = [
        f"- {email.occurred_at} | {email.email_id} | {email.sender_email} | {email.body}"
        for email in emails
    ]
    milestone = project.current_milestone.value if project.current_milestone else None
    milestone_display = (
        MILESTONE_DISPLAY_NAMES.get(milestone, milestone) if milestone else "not set"
    )
    stored_next = _usable_next_action(project.next_action, milestone)
    prompt = (
        "Summarize the effective current state of this FIX onboarding project. "
        "Later confirmations and results supersede earlier proposals. Do not treat a "
        "confirmed appointment as a blocker. The canonical milestone is authoritative; "
        "do not infer an earlier milestone from historical UAT or conformance emails. "
        "Return JSON only with keys current_state, "
        "blocker, recommended_action, next_action, has_blocker, and ball_with.\n\n"
        f"Project: {project.project_id}\n"
        f"Canonical status: {project.project_status.value}\n"
        f"Canonical milestone: {milestone or 'not set'} ({milestone_display})\n"
        f"Canonical ball_with: {project.ball_with.value}\n"
        f"Canonical next_action: {stored_next or 'None'}\n"
        f"Deterministic evidence interpretation: {json.dumps({k: v for k, v in deterministic.items() if k not in {'events', '_strong_evidence'}}, ensure_ascii=False)}\n"
        "Chronological email evidence:\n" + ("\n".join(evidence_lines) or "No email evidence")
    )
    try:
        raw = llm_call(
            "project_summary",
            [{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        parsed = _parse_json_object(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        logger.info("Project briefing LLM unavailable for %s", project.project_id, exc_info=True)
        return None


def _merge_briefings(
    deterministic: dict[str, Any],
    llm_payload: dict[str, Any] | None,
    *,
    canonical_milestone: str | None = None,
) -> dict[str, Any]:
    result = {key: value for key, value in deterministic.items() if not key.startswith("_")}
    if not llm_payload or deterministic.get("_strong_evidence"):
        return result
    for key in ("current_state", "blocker", "recommended_action", "next_action"):
        value = llm_payload.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if key in {"current_state", "recommended_action", "next_action"} and (
            _text_contradicts_milestone(text, canonical_milestone)
        ):
            continue
        result[key] = text
    ball_with = llm_payload.get("ball_with")
    valid_ball_values = {item.value for item in BallWith}
    if isinstance(ball_with, str) and ball_with.strip() in valid_ball_values:
        result["ball_with"] = ball_with.strip()
    if isinstance(llm_payload.get("has_blocker"), bool):
        result["has_blocker"] = llm_payload["has_blocker"]
    else:
        result["has_blocker"] = result["blocker"].lower() != "no active blocker"
    return result


def _parse_json_object(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return raw if isinstance(raw, dict) else None
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None

def _one_line(text: str, limit: int = 180) -> str:
    value = " ".join(text.split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."
