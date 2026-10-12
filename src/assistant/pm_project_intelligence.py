"""Project work, evidence audit, and milestone readiness tools for Workspace."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Any
from urllib.parse import quote

from src.agent.progress_evaluation import (
    citation_refs,
    evaluate_progress_detail,
    priority_criterion,
)
from src.assistant.workspace_read_support import (
    add_source,
    ensure_project_in_scope,
    normalize_timestamp,
    paginate_records,
    parse_as_of,
    project_row_as_of,
)
from src.milestone.contracts import (
    get_milestone_contract,
    get_next_milestone_contract,
    load_milestone_contract_policy,
)

_WORK_KINDS = frozenset({"followup", "internal_action", "personal_work", "commitment"})
_WORK_STATUSES = frozenset({"all", "open", "completed", "cancelled"})


def _project_row(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM projects WHERE project_id=? AND project_id != '_system'",
        (project_id,),
    ).fetchone()
    return dict(row) if row else None


def _safe_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return fallback


def _normalized_work_status(value: Any) -> str:
    status = str(value or "").casefold()
    if status in {"open", "overdue"}:
        return "open"
    if status in {"done", "completed", "resolved"}:
        return "completed"
    if status in {"cancelled", "rejected"}:
        return "cancelled"
    return "unknown"


def _provenance_quality(
    *,
    source_ref: Any,
    created_by: Any = None,
    source_type: Any = None,
    title: Any = None,
) -> str:
    if str(source_ref or "").strip():
        return "linked"
    if (
        str(created_by or "").casefold().startswith("auto")
        or str(source_type or "").casefold() in {"email", "meeting", "agent"}
        or str(title or "").lstrip().casefold().startswith("[auto]")
    ):
        return "inferred"
    return "missing"


def _source_for_reference(
    conn: sqlite3.Connection,
    args: dict[str, Any],
    *,
    project_id: str,
    source_ref: str | None,
) -> str | None:
    """Resolve only real persisted email/evidence references into source parts."""
    raw = str(source_ref or "").strip()
    if not raw:
        return None
    candidates = [raw]
    for prefix in ("email:", "processed_email:", "evidence:"):
        if raw.casefold().startswith(prefix):
            candidates.append(raw[len(prefix):])
    candidates = list(dict.fromkeys(candidates))
    placeholders = ",".join("?" for _ in candidates)
    cutoff = parse_as_of(args.get("_assistant_as_of")).isoformat()
    email = conn.execute(
        f"""SELECT email_id, subject, body_snippet, received_at, processed_at,
                   sender_email, direction
            FROM processed_emails
            WHERE project_id=? AND email_id IN ({placeholders})
              AND datetime(COALESCE(received_at, processed_at)) <= datetime(?)
            ORDER BY COALESCE(received_at, processed_at) DESC LIMIT 1""",
        [project_id, *candidates, cutoff],
    ).fetchone()
    if email:
        occurred_at = normalize_timestamp(email["received_at"] or email["processed_at"])
        return add_source(
            args,
            kind="email",
            title=str(email["subject"] or "Email evidence"),
            locator=f"processed_email:{email['email_id']}",
            excerpt=str(email["body_snippet"] or "")[:500],
            project_id=project_id,
            occurred_at=occurred_at,
            url=f"/projects/{quote(project_id)}",
            metadata={
                "sender_email": email["sender_email"],
                "direction": email["direction"],
            },
        )
    evidence = conn.execute(
        f"""SELECT evidence_id, evidence_type, content_summary, content_text,
                   source_ref, evidence_at
            FROM evidence_store
            WHERE project_id=? AND (
                evidence_id IN ({placeholders}) OR source_ref IN ({placeholders})
            ) AND datetime(evidence_at) <= datetime(?)
            ORDER BY evidence_at DESC LIMIT 1""",
        [project_id, *candidates, *candidates, cutoff],
    ).fetchone()
    if evidence:
        return add_source(
            args,
            kind="evidence",
            title=f"{evidence['evidence_type']} evidence",
            locator=f"evidence:{evidence['evidence_id']}",
            excerpt=str(evidence["content_summary"] or evidence["content_text"] or "")[:500],
            project_id=project_id,
            occurred_at=normalize_timestamp(evidence["evidence_at"]),
            url=f"/projects/{quote(project_id)}",
            metadata={"source_ref": evidence["source_ref"]},
        )
    return None


def list_project_work(
    conn: sqlite3.Connection,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Normalize project follow-ups, actions, commitments, and operator work."""
    project_id = str(args.get("project_id") or "").strip()
    if not project_id:
        return {"error": "project_id is required"}
    ensure_project_in_scope(args, project_id)
    if _project_row(conn, project_id) is None:
        return {"error": f"Project {project_id} not found"}

    raw_kinds = args.get("kinds") or sorted(_WORK_KINDS)
    kinds = {str(item).strip().lower() for item in raw_kinds}
    invalid_kinds = sorted(kinds - _WORK_KINDS)
    if invalid_kinds:
        return {"error": f"Unsupported work kinds: {', '.join(invalid_kinds)}"}
    status_filter = str(args.get("status") or "all").strip().lower()
    if status_filter not in _WORK_STATUSES:
        return {"error": f"Unsupported work status: {status_filter}"}

    records: list[dict[str, Any]] = []
    if "followup" in kinds:
        for raw in conn.execute(
            "SELECT * FROM internal_followups WHERE project_id=?",
            (project_id,),
        ).fetchall():
            row = dict(raw)
            records.append({
                "work_type": "followup",
                "work_id": row["followup_id"],
                "project_id": project_id,
                "title": row["description"],
                "status": _normalized_work_status(row["status"]),
                "source_status": row["status"],
                "owner": row["owner_contact_id"],
                "due_at": normalize_timestamp(row["due_date"]),
                "created_at": normalize_timestamp(row["created_at"]),
                "completed_at": normalize_timestamp(row["resolved_at"]),
                "source_ref": row["linked_message_ref"],
                "provenance_quality": _provenance_quality(
                    source_ref=row["linked_message_ref"],
                    created_by=row["created_by"],
                    title=row["description"],
                ),
                "metadata": {
                    "created_by": row["created_by"],
                    "escalation_level": row["escalation_level"],
                    "resolution_note": row["resolution_note"],
                },
            })

    if "internal_action" in kinds:
        for raw in conn.execute(
            "SELECT * FROM internal_actions WHERE project_id=?",
            (project_id,),
        ).fetchall():
            row = dict(raw)
            records.append({
                "work_type": "internal_action",
                "work_id": row["action_id"],
                "project_id": project_id,
                "title": row["title"],
                "status": _normalized_work_status(row["status"]),
                "source_status": row["status"],
                "owner": row["owner_team"],
                "due_at": normalize_timestamp(row["deadline"]),
                "created_at": normalize_timestamp(row["created_at"]),
                "completed_at": normalize_timestamp(row["completed_at"]),
                "source_ref": None,
                "provenance_quality": "missing",
                "metadata": {},
            })

    operator_id = str(args.get("_operator_id") or "").strip()
    if "personal_work" in kinds and operator_id:
        rows = conn.execute(
            """SELECT w.title, w.status, w.priority, p.*
               FROM work_items w
               JOIN personal_work_items p ON p.work_item_id=w.work_item_id
               WHERE p.operator_id=? AND p.related_entity_type='project'
                 AND p.related_entity_id=?""",
            (operator_id, project_id),
        ).fetchall()
        for raw in rows:
            row = dict(raw)
            records.append({
                "work_type": "personal_work",
                "work_id": row["work_item_id"],
                "project_id": project_id,
                "title": row["title"],
                "description": row["description"],
                "status": _normalized_work_status(row["status"]),
                "source_status": row["status"],
                "owner": row["owner_ref"] or row["owner_type"],
                "due_at": normalize_timestamp(row["due_at"]),
                "created_at": normalize_timestamp(row["created_at"]),
                "completed_at": normalize_timestamp(row["completed_at"]),
                "source_ref": row["source_ref"],
                "provenance_quality": _provenance_quality(
                    source_ref=row["source_ref"],
                    source_type=row["source_type"],
                    title=row["title"],
                ),
                "metadata": {
                    "kind": row["kind"],
                    "priority": row["priority"],
                    "review_status": row["review_status"],
                    "confidence": row["confidence"],
                },
            })

    if "commitment" in kinds:
        for raw in conn.execute(
            """SELECT evidence_id, content_summary, content_text, source_ref,
                      evidence_at, created_at
               FROM evidence_store
               WHERE project_id=? AND evidence_type='commitment'""",
            (project_id,),
        ).fetchall():
            row = dict(raw)
            records.append({
                "work_type": "commitment",
                "work_id": row["evidence_id"],
                "project_id": project_id,
                "title": row["content_summary"],
                "description": row["content_text"],
                "status": "unknown",
                "source_status": "evidence_only",
                "owner": None,
                "due_at": "",
                "created_at": normalize_timestamp(row["evidence_at"] or row["created_at"]),
                "completed_at": "",
                "source_ref": row["source_ref"],
                "provenance_quality": _provenance_quality(source_ref=row["source_ref"]),
                "metadata": {"warning": "Evidence does not prove the commitment is still open."},
            })

    if status_filter != "all":
        records = [item for item in records if item["status"] == status_filter]

    as_of = parse_as_of(args.get("_assistant_as_of"))
    for item in records:
        due_at = str(item.get("due_at") or "")
        item["is_overdue"] = bool(
            item["status"] == "open" and due_at and due_at < as_of.isoformat()
        )

    total = len(records)
    limit = max(1, min(int(args.get("limit") or 50), 100))
    try:
        page, next_cursor = paginate_records(
            records,
            key=lambda item: (
                str(item.get("created_at") or ""),
                f"{item.get('work_type')}:{item.get('work_id')}",
            ),
            cursor=str(args.get("cursor") or "") or None,
            limit=limit,
            descending=True,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    for item in page:
        item["source_id"] = add_source(
            args,
            kind="project_work",
            title=str(item.get("title") or item["work_type"]),
            locator=f"{item['work_type']}:{item['work_id']}",
            excerpt=str(item.get("description") or item.get("title") or "")[:500],
            project_id=project_id,
            occurred_at=str(item.get("created_at") or ""),
            url=f"/projects/{quote(project_id)}",
            metadata={
                "status": item["source_status"],
                "due_at": item.get("due_at"),
                "provenance_quality": item["provenance_quality"],
                "source_ref": item.get("source_ref"),
            },
        )
        linked_source_id = _source_for_reference(
            conn,
            args,
            project_id=project_id,
            source_ref=item.get("source_ref"),
        )
        if linked_source_id:
            item["linked_source_id"] = linked_source_id

    summary_source_id = add_source(
        args,
        kind="project_work_query",
        title=f"{project_id} project work query",
        locator=(
            f"project-work-query:{project_id}:{','.join(sorted(kinds))}:"
            f"{status_filter}:{as_of.isoformat()}"
        ),
        excerpt=(
            f"Found {total} matching project work records and returned {len(page)} "
            f"as of {as_of.isoformat()}."
        ),
        project_id=project_id,
        occurred_at=as_of.isoformat(),
        url=f"/projects/{quote(project_id)}",
        metadata={
            "kinds": sorted(kinds),
            "status": status_filter,
            "total": total,
            "result_count": len(page),
        },
    )
    return {
        "project_id": project_id,
        "as_of": as_of.isoformat(),
        "summary_source_id": summary_source_id,
        "filters": {"kinds": sorted(kinds), "status": status_filter},
        "total": total,
        "result_count": len(page),
        "next_cursor": next_cursor,
        "results": page,
        "provenance_guidance": (
            "linked means a persisted source_ref resolved to evidence; inferred means an "
            "automated record lacks a direct link; missing means no source lineage was stored."
        ),
        "citation_instruction": "Cite each work record with its source_id.",
    }


def get_project_evidence_audit(
    conn: sqlite3.Connection,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Return current truth beside historical evidence and update decisions."""
    project_id = str(args.get("project_id") or "").strip()
    if not project_id:
        return {"error": "project_id is required"}
    ensure_project_in_scope(args, project_id)
    as_of = parse_as_of(args.get("_assistant_as_of"))
    cutoff = as_of.isoformat()
    project = project_row_as_of(conn, project_id, as_of)
    if project is None:
        return {"error": f"Project {project_id} not found"}
    limit = max(1, min(int(args.get("limit") or 50), 100))

    project_source_id = add_source(
        args,
        kind="project",
        title=f"{project_id} project truth as of {as_of.isoformat()}",
        locator=(
            f"project:{project_id}:{project.get('updated_at') or ''}:"
            f"as-of:{as_of.isoformat()}"
        ),
        excerpt=(
            f"Status {project.get('project_status')}; milestone "
            f"{project.get('current_milestone') or 'not set'}; ball with {project.get('ball_with')}."
        ),
        project_id=project_id,
        occurred_at=normalize_timestamp(project.get("updated_at")),
        url=f"/projects/{quote(project_id)}",
    )

    proposals: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    conflicts: list[dict[str, Any]] = []
    rows = conn.execute(
        """SELECT * FROM proposed_updates WHERE project_id=?
             AND datetime(COALESCE(decided_at, created_at)) <= datetime(?)
           ORDER BY datetime(COALESCE(decided_at, created_at)) DESC,
                    created_at DESC, update_id DESC LIMIT ?""",
        (project_id, cutoff, limit),
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        changes = _safe_json(row.get("proposed_changes"), [])
        rejection = _safe_json(row.get("rejection_reasons"), row.get("rejection_reasons"))
        assessment = _safe_json(row.get("assessment_json"), None)
        outcome = str(row.get("decision_outcome") or "unknown")
        decision_counts[outcome] += 1
        tags = rejection.get("tags", []) if isinstance(rejection, dict) else []
        source_id = add_source(
            args,
            kind="proposed_update",
            title=f"{project_id} update {row['update_id']} ({outcome})",
            locator=f"proposed_update:{row['update_id']}",
            excerpt=json.dumps(changes, ensure_ascii=False)[:500],
            project_id=project_id,
            occurred_at=normalize_timestamp(row.get("decided_at") or row.get("created_at")),
            url=f"/projects/{quote(project_id)}",
            metadata={"decision_outcome": outcome, "tags": tags},
        )
        linked_source_id = _source_for_reference(
            conn, args, project_id=project_id, source_ref=row.get("source_ref")
        )
        item = {
            "update_id": row["update_id"],
            "decision_outcome": outcome,
            "proposed_changes": changes,
            "confidence": row.get("confidence"),
            "risk_level": row.get("risk_level"),
            "requires_operator_review": bool(row.get("requires_operator_review")),
            "rejection": rejection,
            "is_superseded": "superseded_evidence" in tags,
            "source_type": row.get("source_type"),
            "source_ref": row.get("source_ref"),
            "policy_version": row.get("policy_version"),
            "assessment": assessment,
            "created_at": normalize_timestamp(row.get("created_at")),
            "decided_at": normalize_timestamp(row.get("decided_at")),
            "source_id": source_id,
            "linked_source_id": linked_source_id,
        }
        proposals.append(item)
        if isinstance(changes, list):
            for change in changes:
                if not isinstance(change, dict):
                    continue
                field = str(change.get("field") or "")
                if field not in project:
                    continue
                new_value = change.get("new_value")
                if new_value == project.get(field):
                    continue
                if outcome in {"needs_review", "deferred"}:
                    conflicts.append({
                        "type": "pending_change_differs_from_current_truth",
                        "field": field,
                        "current_value": project.get(field),
                        "proposed_value": new_value,
                        "update_id": row["update_id"],
                        "source_ids": [project_source_id, source_id],
                    })

    milestones: list[dict[str, Any]] = []
    for raw in conn.execute(
        """SELECT * FROM milestone_history WHERE project_id=?
             AND datetime(changed_at) <= datetime(?)
           ORDER BY changed_at DESC, id DESC LIMIT ?""",
        (project_id, cutoff, limit),
    ).fetchall():
        row = dict(raw)
        row["source_id"] = add_source(
            args,
            kind="milestone",
            title=f"Milestone transition to {row['to_milestone']}",
            locator=f"milestone_history:{row['id']}",
            excerpt=str(row.get("reason") or "")[:500],
            project_id=project_id,
            occurred_at=normalize_timestamp(row.get("changed_at")),
            url=f"/projects/{quote(project_id)}",
            metadata={
                "from_milestone": row.get("from_milestone"),
                "to_milestone": row.get("to_milestone"),
                "evidence_ref": row.get("evidence_ref"),
            },
        )
        milestones.append(row)
    if milestones and milestones[0].get("to_milestone") != project.get("current_milestone"):
        conflicts.append({
            "type": "latest_milestone_history_differs_from_current_truth",
            "current_value": project.get("current_milestone"),
            "history_value": milestones[0].get("to_milestone"),
            "source_ids": [project_source_id, milestones[0]["source_id"]],
        })

    evidence: list[dict[str, Any]] = []
    for raw in conn.execute(
        """SELECT * FROM evidence_store WHERE project_id=?
             AND datetime(evidence_at) <= datetime(?)
           ORDER BY evidence_at DESC, evidence_id DESC LIMIT ?""",
        (project_id, cutoff, limit),
    ).fetchall():
        row = dict(raw)
        row["source_id"] = add_source(
            args,
            kind="evidence",
            title=f"{row['evidence_type']} evidence",
            locator=f"evidence:{row['evidence_id']}",
            excerpt=str(row.get("content_summary") or row.get("content_text") or "")[:500],
            project_id=project_id,
            occurred_at=normalize_timestamp(row.get("evidence_at")),
            url=f"/projects/{quote(project_id)}",
            metadata={"source_ref": row.get("source_ref")},
        )
        evidence.append(row)

    return {
        "project_id": project_id,
        "as_of": as_of.isoformat(),
        "current_truth": {**project, "source_id": project_source_id},
        "decision_counts": dict(decision_counts),
        "proposed_updates": proposals,
        "milestone_history": milestones,
        "evidence": evidence,
        "conflicts": conflicts,
        "superseded_count": sum(1 for item in proposals if item["is_superseded"]),
        "guidance": (
            "current_truth is authoritative. Proposed updates, evidence, and milestone history "
            "are audit records and must not silently override the current project row."
        ),
        "citation_instruction": "Cite audit claims with the returned source_ids.",
    }


def _latest_assessment_proposal(
    conn: sqlite3.Connection,
    project_id: str,
    assessment,
    *,
    as_of: datetime,
) -> dict[str, Any] | None:
    policy_version = load_milestone_contract_policy().policy_version
    rows = conn.execute(
        """SELECT * FROM proposed_updates
           WHERE project_id=? AND assessment_json IS NOT NULL
             AND datetime(COALESCE(decided_at, created_at)) <= datetime(?)
           ORDER BY datetime(COALESCE(decided_at, created_at)) DESC,
                    created_at DESC, update_id DESC LIMIT 20""",
        (project_id, as_of.isoformat()),
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        detail = _safe_json(row.get("assessment_json"), None)
        if not isinstance(detail, dict):
            continue
        if (
            row.get("source_ref") == assessment.source_ref
            and row.get("source_type") == assessment.source_type.value
            and row.get("policy_version") == policy_version
            and detail.get("project_id") == project_id
            and detail.get("source_ref") == assessment.source_ref
        ):
            return row
    return None


def assess_milestone_readiness(
    conn: sqlite3.Connection,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Re-evaluate the latest valid source-bound assessment against current truth."""
    project_id = str(args.get("project_id") or "").strip()
    if not project_id:
        return {"error": "project_id is required"}
    ensure_project_in_scope(args, project_id)
    as_of = parse_as_of(args.get("_assistant_as_of"))
    project = project_row_as_of(conn, project_id, as_of)
    if project is None:
        return {"error": f"Project {project_id} not found"}

    project_source_id = add_source(
        args,
        kind="project",
        title=f"{project_id} project truth as of {as_of.isoformat()}",
        locator=(
            f"project:{project_id}:{project.get('updated_at') or ''}:"
            f"as-of:{as_of.isoformat()}"
        ),
        excerpt=(
            f"Current milestone {project.get('current_milestone') or 'not set'}; "
            f"status {project.get('project_status')}."
        ),
        project_id=project_id,
        occurred_at=normalize_timestamp(project.get("updated_at")),
        url=f"/projects/{quote(project_id)}",
    )
    current_milestone = project.get("current_milestone")
    if not current_milestone:
        return {
            "project_id": project_id,
            "as_of": as_of.isoformat(),
            "assessment_status": "not_available",
            "reason": "The project has no current milestone to assess.",
            "current_milestone": None,
            "source_ids": [project_source_id],
        }
    try:
        current_contract = get_milestone_contract(current_milestone)
        next_contract = get_next_milestone_contract(current_milestone)
    except ValueError:
        return {
            "project_id": project_id,
            "as_of": as_of.isoformat(),
            "assessment_status": "not_available",
            "reason": "The current milestone is not recognized by the active contract policy.",
            "current_milestone": current_milestone,
            "source_ids": [project_source_id],
        }

    policy = load_milestone_contract_policy()
    policy_source_id = add_source(
        args,
        kind="milestone_policy",
        title=f"{current_milestone} milestone contract",
        locator=f"milestone-policy:{policy.policy_version}:{current_milestone}",
        excerpt="; ".join(
            criterion.description for criterion in current_contract.required_exit_criteria
        )[:500],
        project_id=project_id,
        metadata={"policy_version": policy.policy_version},
    )
    requirements = [
        criterion.model_dump(mode="json")
        for criterion in current_contract.required_exit_criteria
    ]
    assessment = evaluate_progress_detail(
        conn,
        project_id,
        as_of=as_of,
        project_snapshot=project,
    )
    if assessment is None:
        return {
            "project_id": project_id,
            "as_of": as_of.isoformat(),
            "assessment_status": "not_available",
            "reason": (
                "No valid persisted, source-bound structured assessment is available. "
                "Readiness was not inferred from arbitrary email text."
            ),
            "current_milestone": current_milestone,
            "target_milestone": next_contract.id.value if next_contract else None,
            "policy_version": policy.policy_version,
            "required_exit_criteria": requirements,
            "source_ids": [project_source_id, policy_source_id],
        }

    proposal = _latest_assessment_proposal(
        conn,
        project_id,
        assessment,
        as_of=as_of,
    )
    proposal_source_id = None
    if proposal:
        proposal_source_id = add_source(
            args,
            kind="proposed_update",
            title=f"{project_id} persisted milestone assessment",
            locator=f"proposed_update:{proposal['update_id']}",
            excerpt=str(proposal.get("assessment_json") or "")[:500],
            project_id=project_id,
            occurred_at=normalize_timestamp(proposal.get("created_at")),
            url=f"/projects/{quote(project_id)}",
            metadata={
                "decision_outcome": proposal.get("decision_outcome"),
                "source_ref": proposal.get("source_ref"),
                "policy_version": proposal.get("policy_version"),
            },
        )

    resolved_source_ids = [project_source_id, policy_source_id]
    if proposal_source_id:
        resolved_source_ids.append(proposal_source_id)
    unresolved_refs: list[str] = []
    for ref in citation_refs(assessment):
        resolved = _source_for_reference(
            conn, args, project_id=project_id, source_ref=ref
        )
        if resolved:
            resolved_source_ids.append(resolved)
        else:
            unresolved_refs.append(ref)
    resolved_source_ids = list(dict.fromkeys(resolved_source_ids))

    field_assessment = assessment.milestone_assessment
    decision = field_assessment.decision.value if field_assessment else None
    if decision == "accept_candidate":
        assessment_status = "ready"
    elif decision == "needs_review":
        assessment_status = "needs_review"
    else:
        assessment_status = "not_ready"
    priority = priority_criterion(assessment)
    criteria = [item.model_dump(mode="json") for item in assessment.milestone_criteria]
    counts = Counter(item.result.value for item in assessment.milestone_criteria)

    return {
        "project_id": project_id,
        "as_of": as_of.isoformat(),
        "assessment_status": assessment_status,
        "current_milestone": current_milestone,
        "target_milestone": (
            field_assessment.candidate_value
            if field_assessment and field_assessment.candidate_value
            else (next_contract.id.value if next_contract else None)
        ),
        "decision": decision,
        "decision_reasons": list(field_assessment.reasons) if field_assessment else [],
        "policy_version": assessment.policy_version,
        "criteria_counts": dict(counts),
        "criteria": criteria,
        "priority_gap": priority.model_dump(mode="json") if priority else None,
        "assessment_codes": [code.value for code in assessment.assessment_codes],
        "uncertainties": list(assessment.uncertainties),
        "source_ids": resolved_source_ids,
        "unresolved_source_refs": unresolved_refs,
        "required_exit_criteria": requirements,
        "guidance": (
            "This is a deterministic re-evaluation of persisted structured facts. "
            "It does not approve or apply a milestone change."
        ),
    }
