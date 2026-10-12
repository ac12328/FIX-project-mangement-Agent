"""Create deterministic publication plans from approved canonical facts only."""
from __future__ import annotations

import hashlib
import json

from src.db.connection import scoped_read, scoped_write
from src.repositories.wiki_publication_repo import get_publication_for_fact, insert_plan, publication_id_for
from src.wiki.publication.models import PublicationPlan, PublicationRecord
from src.wiki.publication.validator import validate_path, validate_untrusted_fields

_APPROVALS = {"approve", "edit_and_approve", "narrow_scope", "supersede"}
_SCOPES = {"global", "project", "market", "counterparty"}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _scope(row, value: dict) -> tuple[str, str, list[str], list[str], list[str]]:
    scope = str(value.get("scope") or row["scope_type"])
    if scope not in _SCOPES:
        raise ValueError("unsupported Wiki publication scope")
    scope_id = str(value.get("scope_id") or row["scope_id"] or "").strip()
    project_ids = list(value.get("project_ids") or ([scope_id] if scope == "project" and scope_id else []))
    markets = list(value.get("markets") or ([scope_id] if scope == "market" and scope_id else []))
    counterparties = list(value.get("counterparties") or ([scope_id] if scope == "counterparty" and scope_id else []))
    visibility = str(value.get("visibility") or "internal_only")
    if scope == "project":
        visibility = "restricted"
    if scope != "global" and not (project_ids or markets or counterparties):
        raise ValueError(f"{scope} scope requires an identifier")
    return scope, visibility, project_ids, markets, counterparties


def _snapshot(fact_id: str) -> PublicationPlan:
    with scoped_read() as conn:
        row = conn.execute("""SELECT f.*,d.decision_type,d.actor_id,d.validation_id AS decision_validation_id,
          d.evaluation_id AS decision_evaluation_id,v.passed,v.requires_evaluation
          FROM memory_facts f JOIN memory_decisions d ON d.decision_id=f.decision_id
          JOIN candidate_validation_runs v ON v.validation_id=f.validation_id WHERE f.fact_id=?""", (fact_id,)).fetchone()
        state = conn.execute("""SELECT state FROM memory_fact_state_events WHERE fact_id=?
          ORDER BY effective_at DESC,created_at DESC,event_seq DESC LIMIT 1""", (fact_id,)).fetchone()
    if not row or row["target_type"] != "wiki_publication" or not state or state[0] != "active":
        raise ValueError("publication requires an active approved wiki-publication fact")
    if row["decision_type"] not in _APPROVALS or not row["passed"]:
        raise ValueError("publication requires an approved validated wiki-publication fact")
    if row["validation_id"] != row["decision_validation_id"]:
        raise ValueError("publication validation decision binding mismatch")
    if row["requires_evaluation"] and (not row["evaluation_id"] or row["evaluation_id"] != row["decision_evaluation_id"]):
        raise ValueError("publication evaluation decision binding mismatch")
    value = json.loads(row["value_json"])
    path = validate_path(str(value.get("path") or ""))
    title, body = str(value.get("title") or "").strip(), str(value.get("body") or "").strip()
    operation = str(value.get("operation") or "")
    if operation not in {"upsert", "revoke"} or not title or not body:
        raise ValueError("invalid or no-op Wiki publication operation")
    validate_untrusted_fields(path=path, title=title, body=body)
    evidence_refs = json.loads(row["evidence_refs_json"] or "[]")
    watermark = str(value.get("source_watermark") or (evidence_refs[0] if evidence_refs else ""))
    if not watermark:
        raise ValueError("publication source acknowledgement is missing")
    scope, visibility, project_ids, markets, counterparties = _scope(row, value)
    content_hash = _hash(body)
    pub_id = publication_id_for(fact_id, int(row["fact_version"]))
    ownership = {"publication_id": pub_id, "fact_id": fact_id, "fact_version": row["fact_version"],
                 "decision_id": row["decision_id"], "canonical_path": path, "owned_content_hash": content_hash}
    return PublicationPlan(publication_id=pub_id, fact_id=fact_id, fact_version=row["fact_version"],
        decision_id=row["decision_id"], operation=operation, canonical_path=path, title=title,
        page_type=str(value.get("page_type") or path.split("/", 1)[0]), body=body,
        scope=scope, visibility=visibility, project_ids=project_ids, markets=markets,
        counterparties=counterparties, source_watermark=watermark, evidence_refs=evidence_refs,
        planned_content_hash=content_hash, ownership=ownership, proposed_at=str(row["valid_from"]))


def propose_publication(fact_id: str) -> PublicationRecord:
    plan = _snapshot(fact_id)
    with scoped_write() as conn:
        insert_plan(conn, plan=plan)
        record = get_publication_for_fact(conn, fact_id)
    assert record is not None
    return record


def load_plan(fact_id: str) -> PublicationPlan:
    plan = _snapshot(fact_id)
    with scoped_read() as conn:
        row = conn.execute("SELECT * FROM wiki_publications WHERE fact_id=?", (fact_id,)).fetchone()
    if not row:
        raise KeyError(fact_id)
    persisted = json.loads(row["proposal_payload_json"])
    canonical = json.loads(plan.model_dump_json())
    if persisted != canonical:
        raise ValueError("publication record does not match approved fact snapshot")
    return plan
