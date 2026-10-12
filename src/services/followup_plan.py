"""Atomic, operator-authorized mutations of the existing followup action plan.

Caller owns commit and post-commit event publication. No model output is treated
as operator authorization, and this service never changes milestones/status.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
from typing import Annotated, Literal
import hashlib
from contextvars import ContextVar
import json
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.portfolio.followup_tracker import create_followup,complete_followup,cancel_followup
from src.repositories.evidence_repo import insert_evidence
from src.write_layer.write_helpers import safe_write_project

_IN_BUNDLE = ContextVar("followup_bundle_active", default=False)

from src.models.followup_plan import ActionMutation, ActionBundle

class ActionPlanError(ValueError):
    pass


def _row(conn,fid,pid):
    row=conn.execute("SELECT * FROM internal_followups WHERE followup_id=? AND project_id=?",(fid,pid)).fetchone()
    return dict(row) if row else None



def _event_snapshot(conn,fid,pid):
    result=_row(conn,fid,pid)
    if result is not None:
        result["depends_on"]=[r[0] for r in conn.execute(
            "SELECT prerequisite_id FROM followup_dependencies WHERE followup_id=? ORDER BY prerequisite_id",(fid,))]
    return result


def plan_snapshot(conn,project_id):
    project=conn.execute("SELECT updated_at,ball_with,next_action,project_status FROM projects WHERE project_id=?",(project_id,)).fetchone()
    if not project: raise KeyError(project_id)
    head=conn.execute("SELECT revision FROM followup_plan_heads WHERE project_id=?",(project_id,)).fetchone()
    rows=[dict(r) for r in conn.execute("SELECT * FROM internal_followups WHERE project_id=? ORDER BY followup_id",(project_id,))]
    for row in rows:
        row["depends_on"]=[r[0] for r in conn.execute("SELECT prerequisite_id FROM followup_dependencies WHERE followup_id=? ORDER BY prerequisite_id",(row["followup_id"],))]
    return {"project_id":project_id,"project_status":project["project_status"],"revision":head[0] if head else 0,"project_version":project[0],"ball_with":project[1],"next_action":project[2],"actions":rows}


def _validate_graph(conn,pid):
    rows={r[0] for r in conn.execute("SELECT followup_id FROM internal_followups WHERE project_id=?",(pid,))}
    edges={key:[] for key in rows}
    for key in rows:
        for row in conn.execute("SELECT prerequisite_id FROM followup_dependencies WHERE followup_id=?",(key,)):
            if row[0] not in rows: raise ActionPlanError("unknown or cross-project dependency")
            edges[key].append(row[0])
    visiting=set();done=set()
    def visit(key):
        if key in visiting: raise ActionPlanError("dependency cycle")
        if key in done:return
        visiting.add(key)
        for dep in edges[key]:visit(dep)
        visiting.remove(key);done.add(key)
    for key in rows:visit(key)


def _projection(conn,pid):
    rows={r["followup_id"]:dict(r) for r in conn.execute("SELECT * FROM internal_followups WHERE project_id=?",(pid,))}
    ready=[];waiting=[]
    for key,row in sorted(rows.items()):
        if row["status"] not in {"open","overdue"}:continue
        deps=[r[0] for r in conn.execute("SELECT prerequisite_id FROM followup_dependencies WHERE followup_id=?",(key,))]
        if all(rows.get(d,{}).get("status")=="completed" for d in deps):ready.append(row)
        else:waiting.append(row)
    owners={r["owner_side"] for r in ready}
    ball="Unknown" if not owners or None in owners else "Shared" if len(owners)>1 or "Shared" in owners else next(iter(owners))
    lines=[f'{r["owner_side"] or "Unassigned"}: {r["description"][:160]}' for r in ready]
    lines += [f'Review/wait for prerequisite: {r["description"][:160]}' for r in waiting]
    summary="; ".join(lines[:10])
    if len(lines)>10:summary+=f"; {len(lines)-10} additional pending actions."
    if not summary:
        summary="Review the action plan: all recorded tasks are closed; identify the next required FIX onboarding action."
    return {"ball_with":ball,"next_action":summary}


def apply_action_bundle(conn,*,project_id,bundle:ActionBundle,actor_id:str,trusted_operator:bool=False):
    from src.email.action_bundle_bridge import permits_email_action_bundle
    if not actor_id.strip() or (not trusted_operator and not permits_email_action_bundle(conn,project_id,bundle,actor_id)):
        raise ActionPlanError("explicit operator authorization or verified email bundle required")
    if not bundle.reason.strip():raise ActionPlanError("reason required")
    ids=[a.action_id for a in bundle.actions]
    if len(ids)!=len(set(ids)):raise ActionPlanError("duplicate action reference")
    digest="fields-v2:"+hashlib.sha256(bundle.identity_json().encode()).hexdigest()
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    try:
        prior=conn.execute("SELECT actor_id,content_hash,result_json FROM followup_bundle_receipts WHERE project_id=? AND operation_id=?",(project_id,bundle.operation_id)).fetchone()
        if prior:
            if prior[0] != actor_id:
                raise ActionPlanError("operation identity collision")
            matches = prior[1] == digest
            if not str(prior[1]).startswith("fields-v2:"):
                # Legacy hashes lost explicit-null field presence. Do not guess
                # the meaning of an old update receipt during recovery.
                ambiguous = any(action.operation == "update" and any(getattr(action, field) is None
                    for field in ("description", "owner_side", "due_date", "reminder_due_at", "depends_on")) for action in bundle.actions)
                if ambiguous:
                    raise ActionPlanError("legacy update receipt lacks field presence; reconciliation required")
                matches = prior[1] == hashlib.sha256(bundle.model_dump_json().encode()).hexdigest()
            if prior[0]!=actor_id or not matches:
                raise ActionPlanError("operation identity collision")
            result={**json.loads(prior[2]),"outcome":"duplicate","current_plan":plan_snapshot(conn,project_id)}
            if started:
                conn.rollback()  # Read-only duplicate: do not retain an idle write lock.
            return result
    except Exception:
        if started:
            conn.rollback()
        raise
    savepoint="followup_bundle_"+uuid4().hex
    conn.execute("SAVEPOINT "+savepoint)
    token = _IN_BUNDLE.set(True)
    try:
        project=conn.execute("SELECT * FROM projects WHERE project_id=?",(project_id,)).fetchone()
        if not project:raise KeyError(project_id)
        if project["project_status"] in {"Completed","Cancelled","Archived"}:raise ActionPlanError("terminal project: use linked support tickets")
        if project["updated_at"]!=bundle.expected_project_version:raise ActionPlanError("stale project version")
        head=conn.execute("SELECT revision FROM followup_plan_heads WHERE project_id=?",(project_id,)).fetchone()
        if (head[0] if head else 0)!=bundle.expected_plan_revision:raise ActionPlanError("stale plan version")
        if not head and project["next_action"] and not bundle.replace_legacy_summary:raise ActionPlanError("explicit legacy-summary replacement acknowledgement required")
        if bundle.evidence_ref and not conn.execute("SELECT 1 FROM evidence_store WHERE evidence_id=? AND project_id=?",(bundle.evidence_ref,project_id)).fetchone():raise ActionPlanError("evidence outside project")
        mapping={};before={}
        for action in bundle.actions:
            if action.operation=="create":
                if _row(conn,action.action_id,project_id):raise ActionPlanError("create reference already exists")
                mapping[action.action_id]=create_followup(conn,project_id,action.description.strip(),action.due_date.isoformat() if action.due_date else None,
                    owner_side=action.owner_side,reminder_due_at=action.reminder_due_at.isoformat() if action.reminder_due_at else None,
                    created_by="operator_confirmed" if trusted_operator else "auto_resolved",linked_message_ref=bundle.evidence_ref,commit=False)
                before[action.action_id]=None
            else:
                row=_row(conn,action.action_id,project_id)
                if not row or row["status"] not in {"open","overdue"}:raise ActionPlanError("action unavailable in this project")
                if row["action_revision"]!=action.expected_revision:raise ActionPlanError("stale action revision")
                mapping[action.action_id]=action.action_id;before[action.action_id]=_event_snapshot(conn,action.action_id,project_id)
        for action in bundle.actions:
            fid=mapping[action.action_id]
            if action.operation=="complete":
                if not complete_followup(conn,fid,expected_revision=action.expected_revision,commit=False):raise ActionPlanError("completion conflict")
            elif action.operation=="cancel":
                if not cancel_followup(conn,fid,expected_revision=action.expected_revision,commit=False):raise ActionPlanError("cancellation conflict")
            elif action.operation=="update":
                fields={k:getattr(action,k) for k in ("description","owner_side","due_date","reminder_due_at") if k in action.model_fields_set}
                for key in ("due_date","reminder_due_at"):
                    if fields.get(key) is not None:fields[key]=fields[key].isoformat()
                if "description" in fields and (not fields["description"] or not fields["description"].strip()):raise ActionPlanError("description required")
                if "due_date" in fields:fields["due_date_basis"]=("operator_entered" if trusted_operator else "reported_commitment") if fields["due_date"] else "none"
                assignments=[f"{key}=?" for key in fields]+["action_revision=action_revision+1"]
                conn.execute("UPDATE internal_followups SET "+",".join(assignments)+" WHERE followup_id=? AND project_id=?",(*fields.values(),fid,project_id))
            if action.depends_on is not None:
                conn.execute("DELETE FROM followup_dependencies WHERE followup_id=?",(fid,))
                for dep in dict.fromkeys(action.depends_on):
                    target=mapping.get(dep,dep)
                    if target==fid or not _row(conn,target,project_id):raise ActionPlanError("invalid dependency")
                    conn.execute("INSERT INTO followup_dependencies VALUES (?,?)",(fid,target))
        _validate_graph(conn,project_id)
        fields=_projection(conn,project_id)
        now=datetime.now(timezone.utc).isoformat()
        evidence=insert_evidence(conn,project_id,"manual_note" if trusted_operator else "fact",bundle.reason,now,content_text=bundle.identity_json(),source_ref="followup-bundle:"+bundle.operation_id)
        # The bundle savepoint owns rollback; preserve any borrowed caller transaction.
        written=safe_write_project(conn,project_id,fields,actor=actor_id,evidence_ref=evidence,reason=bundle.reason,surface="dashboard",
            commit=False,rollback_on_error=False,publish_event=False,schedule_notifications=False,expected_updated_at=bundle.expected_project_version)
        if written.outcome not in {"accepted","no_change"}:raise ActionPlanError(written.rejection_reason or written.needs_review_reason or written.outcome)
        for action in bundle.actions:
            fid=mapping[action.action_id]
            conn.execute("INSERT INTO followup_action_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",(uuid4().hex,project_id,bundle.operation_id,fid,actor_id,action.operation,
                json.dumps(before[action.action_id]),json.dumps(_event_snapshot(conn,fid,project_id)),bundle.reason,evidence,now))
        revision=bundle.expected_plan_revision+1
        conn.execute("INSERT INTO followup_plan_heads VALUES (?,?) ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision",(project_id,revision))
        result={"outcome":"applied","operation_id":bundle.operation_id,"action_ids":mapping,"plan_revision":revision,"applied_fields":fields}
        conn.execute("INSERT INTO followup_bundle_receipts VALUES (?,?,?,?,?,?)",(project_id,bundle.operation_id,actor_id,digest,json.dumps(result),now))
        conn.execute("RELEASE SAVEPOINT "+savepoint)
        return {**result,"current_plan":plan_snapshot(conn,project_id)}
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO SAVEPOINT "+savepoint);conn.execute("RELEASE SAVEPOINT "+savepoint)
        if started:
            conn.rollback()
        raise
    finally:
        _IN_BUNDLE.reset(token)



def refresh_enrolled_followup_plan(conn, project_id, followup_id, operation, before=None):
    """Keep legacy single-action callers consistent once a project adopts a plan.

No commit or network effects. The enclosing lifecycle operation owns the write.
"""
    if _IN_BUNDLE.get():
        return
    if not conn.execute("SELECT 1 FROM followup_plan_heads WHERE project_id=?",(project_id,)).fetchone():
        return
    project=conn.execute("SELECT project_status,updated_at FROM projects WHERE project_id=?",(project_id,)).fetchone()
    if not project:
        return
    savepoint="enrolled_followup_"+uuid4().hex
    conn.execute("SAVEPOINT " + savepoint)
    try:
        operation_id="legacy-followup:"+uuid4().hex
        now=datetime.now(timezone.utc).isoformat()
        after=_event_snapshot(conn,followup_id,project_id)
        if before is not None:
            before={**before,"depends_on":after["depends_on"] if after else []}
        conn.execute("INSERT INTO followup_action_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",(uuid4().hex,project_id,operation_id,followup_id,
            "legacy_followup",operation,json.dumps(before),json.dumps(after),"Legacy followup lifecycle operation",None,now))
        if project[0] not in {"Completed","Cancelled","Archived"}:
            fields=_projection(conn,project_id)
            evidence=insert_evidence(conn,project_id,"fact","Followup plan projection refreshed",now,
                content_text=json.dumps(after),source_ref=operation_id)
            result=safe_write_project(conn,project_id,fields,actor="followup_service",evidence_ref=evidence,
                reason="Project action plan changed",surface="internal",commit=False,publish_event=False,schedule_notifications=False,
                expected_updated_at=project[1],rollback_on_error=False)
            if result.outcome not in {"accepted","no_change"}:
                raise ActionPlanError(result.rejection_reason or result.needs_review_reason or result.outcome)
        conn.execute("UPDATE followup_plan_heads SET revision=revision+1 WHERE project_id=?",(project_id,))
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
        raise
    finally:
        conn.execute("RELEASE SAVEPOINT " + savepoint)
