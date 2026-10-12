"""Cohort loaders for the Email -> Project Truth A/B/R experiment."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from src.assistant.workspace_read_support import parse_as_of, project_row_as_of
from src.email.body_cleaner import clean_email_body
from src.email.project_truth.contracts import (
    EmailProjectTruthCase,
    EvidenceDocument,
    ProjectSnapshot,
)
from src.models.enums import BallWith, Milestone, ProjectStatus
from src.models.progress_assessment import EvidenceLevel, EvidenceSourceType


# The public edition contains no production Message-ID cohort.
PRODUCTION_EMAIL_IDS: tuple[str, ...] = ()

CONTROLLED_CASE_IDS: tuple[str, ...] = (
    "SYN-WP10-T1-CLEAR-POSITIVE-01",
    "SYN-WP10-T5-CLEAR-POSITIVE-02",
    "SYN-WP10-T2-INSUFFICIENT-02",
    "SYN-WP10-T7-INSUFFICIENT-03",
    "SYN-WP10-T1-CONTRADICTION-02",
    "SYN-WP10-T6-FUTURE-CONDITIONAL-02",
    "SYN-WP10-T7-IDENTITY-AMBIGUITY-01",
    "SYN-WP10-T7-POST-GO-LIVE-SUPPORT-02",
)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    return parse_as_of(value)


def _snapshot_from_row(row: dict[str, Any], *, as_of: datetime | None) -> ProjectSnapshot:
    return ProjectSnapshot(
        project_id=str(row["project_id"]),
        current_milestone=Milestone(row["current_milestone"]) if row.get("current_milestone") else None,
        project_status=ProjectStatus(row.get("project_status") or ProjectStatus.ACTIVE.value),
        ball_with=BallWith(row.get("ball_with") or BallWith.UNKNOWN.value),
        next_action=row.get("next_action"),
        client_name=row.get("client_name"),
        snapshot_as_of=as_of,
        reconstructed=bool(row.get("snapshot_reconstructed", False)),
    )


def load_production_case(conn: sqlite3.Connection, email_id: str) -> EmailProjectTruthCase:
    row = conn.execute(
        """SELECT email_id, project_id, subject, sender_email, received_at, processed_at,
                  body_snippet
           FROM processed_emails WHERE email_id=?""",
        (email_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"production email not found: {email_id}")
    email = dict(row)
    evidence_rows = conn.execute(
        """SELECT evidence_type, source_ref, content_text, evidence_at, created_at
           FROM evidence_store
           WHERE source_ref=? OR source_ref LIKE ?
           ORDER BY CASE WHEN source_ref=? THEN 0 ELSE 1 END, source_ref""",
        (email_id, f"attachment:{email_id}:%", email_id),
    ).fetchall()
    email_evidence = next(
        (dict(item) for item in evidence_rows if item["source_ref"] == email_id),
        None,
    )
    candidates = [
        value for value in (
            email_evidence.get("content_text") if email_evidence else None,
            email.get("body_snippet"),
        ) if value
    ]
    raw_body = max(candidates, key=len, default="")
    newest = clean_email_body(raw_body)
    received_at = _parse_datetime(email.get("received_at") or email.get("processed_at"))
    project = project_row_as_of(conn, email["project_id"], received_at or parse_as_of(None))
    if project is None:
        raise KeyError(f"project not found: {email['project_id']}")

    from src.email.source_authority import resolve_source_authority
    authority = resolve_source_authority(conn, project_id=email["project_id"], sender_email=email.get("sender_email"))
    sources = [EvidenceDocument(
        source_type=EvidenceSourceType.EMAIL,
        source_ref=email_id,
        text=newest,
        eligible_current_evidence=True,
        source_role="newest_authored_email",
        evidence_level=authority.evidence_level,
        actor=authority.actor,
        event_at=received_at,
    )]
    for item in evidence_rows:
        data = dict(item)
        if not str(data["source_ref"]).startswith(f"attachment:{email_id}:"):
            continue
        sources.append(EvidenceDocument(
            source_type=EvidenceSourceType.ATTACHMENT,
            source_ref=data["source_ref"],
            text=data.get("content_text") or "",
            eligible_current_evidence=True,
            source_role="attachment",
            evidence_level=EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT,
            actor=email.get("sender_email") or None,
            event_at=_parse_datetime(data.get("evidence_at") or data.get("created_at")),
        ))
    return EmailProjectTruthCase(
        case_id=(f"PROD-{PRODUCTION_EMAIL_IDS.index(email_id) + 1:02d}" if email_id in PRODUCTION_EMAIL_IDS else f"PROD-{str(email_id).encode('utf-8').hex()[:12]}"),
        source_ref=email_id,
        project_id=email["project_id"],
        subject=email.get("subject") or "",
        sender_email=email.get("sender_email") or "",
        received_at=received_at,
        newest_authored_body=newest,
        sources=sources,
        project_snapshot=_snapshot_from_row(project, as_of=received_at),
        synthetic=False,
        category="production",
    )


def _controlled_source_provenance(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build trusted source metadata from the controlled dataset contract."""
    provenance: dict[str, dict[str, Any]] = {}
    for fact in case.get("facts") or []:
        for citation in fact.get("citations") or []:
            source_ref = str(citation["source_ref"])
            current = {
                "evidence_level": EvidenceLevel(citation["evidence_level"]),
                "actor": citation.get("actor"),
            }
            previous = provenance.get(source_ref)
            if previous is not None and previous != current:
                raise ValueError(f"inconsistent controlled provenance for {source_ref}")
            provenance[source_ref] = current
    return provenance


def _controlled_email_sources(case: dict[str, Any]) -> list[EvidenceDocument]:
    facts = case.get("facts") or []
    email_refs: dict[str, list[str]] = {}
    attachment_refs: dict[str, list[str]] = {}
    for fact in facts:
        for citation in fact.get("citations") or []:
            target = attachment_refs if citation.get("source_type") == "attachment" else email_refs
            target.setdefault(citation["source_ref"], []).append(citation["quote"])

    provenance = _controlled_source_provenance(case)
    sources: list[EvidenceDocument] = []
    body = str(case.get("body") or "")
    body_ref = str(case.get("source_ref") or f"email:{case['case_id']}")
    refs = list(email_refs) or [body_ref]
    for ref in refs:
        metadata = provenance.get(ref) or {}
        sources.append(EvidenceDocument(
            source_type=EvidenceSourceType.EMAIL,
            source_ref=ref,
            text=body,
            eligible_current_evidence=not bool(case.get("quoted_history")),
            source_role="newest_authored_email",
            evidence_level=metadata.get(
                "evidence_level", EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT
            ),
            actor=metadata.get("actor"),
        ))
    attachment_text = str(case.get("attachment_text") or "")
    if attachment_text:
        refs = list(attachment_refs) or [f"attachment:{case['case_id']}:1"]
        for ref in refs:
            metadata = provenance.get(ref) or {}
            sources.append(EvidenceDocument(
                source_type=EvidenceSourceType.ATTACHMENT,
                source_ref=ref,
                text=attachment_text,
                eligible_current_evidence=not bool(case.get("forwarded_attachment")),
                source_role="attachment",
                evidence_level=metadata.get(
                    "evidence_level", EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT
                ),
                actor=metadata.get("actor"),
            ))
    return sources


def load_controlled_cases(dataset_path: Path) -> tuple[list[EmailProjectTruthCase], dict[str, dict[str, Any]]]:
    payload = json.loads(dataset_path.read_text(encoding="utf-8"))
    selected = {item["case_id"]: item for item in payload["cases"] if item["case_id"] in CONTROLLED_CASE_IDS}
    cases: list[EmailProjectTruthCase] = []
    oracle: dict[str, dict[str, Any]] = {}
    for case_id in CONTROLLED_CASE_IDS:
        item = selected[case_id]
        cases.append(EmailProjectTruthCase(
            case_id=case_id,
            source_ref=str(item.get("source_ref") or f"email:{case_id}"),
            project_id=item["project_id"],
            subject=f"Synthetic project-state case {case_id}",
            sender_email="synthetic.authority@example.invalid",
            newest_authored_body=str(item.get("body") or ""),
            sources=_controlled_email_sources(item),
            project_snapshot=ProjectSnapshot(
                project_id=item["project_id"],
                current_milestone=Milestone(item["current_milestone"]) if item.get("current_milestone") else None,
                project_status=ProjectStatus(item.get("current_status") or ProjectStatus.ACTIVE.value),
                ball_with=BallWith(item.get("current_ball_with") or BallWith.UNKNOWN.value),
                next_action=item.get("current_next_action"),
            ),
            project_identity_resolved_hint=bool(item.get("project_identity_resolved", True)),
            quoted_history=bool(item.get("quoted_history")),
            forwarded_attachment=bool(item.get("forwarded_attachment")),
            synthetic=True,
            category=str(item.get("category") or "controlled"),
        ))
        oracle[case_id] = dict(item["expected"])
    return cases, oracle


def load_experiment_cohort(
    conn: sqlite3.Connection,
    *,
    dataset_path: Path = Path("agent_eval/project_state_dataset.json"),
) -> tuple[list[EmailProjectTruthCase], dict[str, dict[str, Any]]]:
    production = [load_production_case(conn, email_id) for email_id in PRODUCTION_EMAIL_IDS]
    controlled, oracle = load_controlled_cases(dataset_path)
    return production + controlled, oracle


def cohort_manifest(cases: Iterable[EmailProjectTruthCase]) -> dict[str, Any]:
    selected = list(cases)
    return {
        "schema_version": 1,
        "case_count": len(selected),
        "production_case_ids": [case.case_id for case in selected if not case.synthetic],
        "controlled_case_ids": [case.case_id for case in selected if case.synthetic],
    }
