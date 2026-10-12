"""Atomic verified M7 -> M8/Completed with lossless residual-work handoff.

Caller owns commit. This is the sole issuer of the scoped closing capability in
normal serving; no raw model flag grants closing authorization.
"""
from datetime import datetime,timezone
import json
import re
from uuid import uuid4
from src.project_state.pct_authorization import verify_pct_source,verified_pct_write_scope,PctCompletionError
from src.repositories.evidence_repo import insert_evidence
from src.repositories.ticket_repo import create_ticket,get_ticket
from src.repositories.ticket_evidence_repo import insert_ticket_evidence
from src.write_layer.write_helpers import safe_write_project


def complete_from_pct(conn,*,project_id,source_ref,expected_project_version):
    started=not conn.in_transaction
    if started:conn.execute("BEGIN IMMEDIATE")
    name="pct_"+uuid4().hex;conn.execute("SAVEPOINT "+name)
    try:
        project=conn.execute("SELECT * FROM projects WHERE project_id=?",(project_id,)).fetchone()
        if not project:raise PctCompletionError("project not found")
        prior=conn.execute("SELECT * FROM pct_completion_receipts WHERE project_id=?",(project_id,)).fetchone()
        if prior:
            if project["current_milestone"]!="M8_GoLive" or project["project_status"]!="Completed":raise PctCompletionError("completion receipt conflicts with project state")
            if prior["source_ref"] != source_ref:
                raise PctCompletionError("new post-completion source requires separate support review")
            source = verify_pct_source(conn, project_id, source_ref)
            if source.source_hash != prior["source_hash"] or source.authority != prior["authority"]:
                raise PctCompletionError("completion source changed; reconciliation required")
            result={"outcome":"duplicate","project_id":project_id,"ticket_ids":json.loads(prior["ticket_ids_json"]),
                "source_ref":source_ref,"source_hash":prior["source_hash"],
                "evidence_id":None,"update_id":None}
            conn.execute("RELEASE SAVEPOINT "+name)
            return result
        if project["updated_at"]!=expected_project_version:raise PctCompletionError("stale project version")
        if project["current_milestone"]!="M7_LogReviewAndProductionSessionDelivery" or project["project_status"] not in {"Active","Blocked","Waiting"}:
            raise PctCompletionError("verified completion requires an active M7 onboarding project")
        source=verify_pct_source(conn,project_id,source_ref)
        now=datetime.now(timezone.utc).isoformat();tickets=[]
        remaining=conn.execute("SELECT * FROM internal_followups WHERE project_id=? AND status IN ('open','overdue') ORDER BY followup_id",(project_id,)).fetchall()
        for row in remaining:
            fup=dict(row)
            existing=conn.execute("SELECT project_id,ticket_id FROM pct_completion_handoffs WHERE followup_id=?",(fup["followup_id"],)).fetchone()
            if existing:
                ticket=get_ticket(conn,existing["ticket_id"])
                if existing["project_id"]!=project_id or ticket is None or ticket.related_project_id!=project_id:
                    raise PctCompletionError("existing handoff scope mismatch")
            else:
                ticket=create_ticket(conn,"Post-onboarding follow-up: "+fup["description"][:120],"other",related_project_id=project_id,
                    source_type="email",reporter_email=source.sender,thread_anchor=source_ref,
                    description="Remaining work transferred after internal PCT confirmation.\n"+json.dumps(fup,ensure_ascii=False))
                conn.execute("INSERT INTO pct_completion_handoffs VALUES (?,?,?)",(fup["followup_id"],project_id,ticket.ticket_id))
            if ticket.ticket_id not in tickets:
                tickets.append(ticket.ticket_id)
            if not conn.execute("SELECT 1 FROM ticket_evidence WHERE ticket_id=? AND source_ref=? AND content_text=?",(ticket.ticket_id,source_ref,source.body)).fetchone():
                insert_ticket_evidence(conn,ticket.ticket_id,"email","Internal PCT completion and followup handoff",content_text=source.body,source_ref=source_ref)
            conn.execute("UPDATE internal_followups SET status='cancelled',resolution_note=?,action_revision=action_revision+1 WHERE followup_id=?",
                ("Transferred to "+ticket.ticket_id+"; not marked technically resolved",fup["followup_id"]))
            after=dict(conn.execute("SELECT * FROM internal_followups WHERE followup_id=?",(fup["followup_id"],)).fetchone())
            conn.execute("INSERT INTO followup_action_events VALUES (?,?,?,?,?,?,?,?,?,?,?)",(uuid4().hex,project_id,"pct:"+source_ref,fup["followup_id"],source.authority,"handoff",json.dumps(fup),json.dumps(after),"Onboarding completed; remaining work continues in linked support",source_ref,now))
        # Keep an explicitly mentioned residual issue even if no task captured it.
        issue_pattern = re.compile(
            r"\b(?:outstanding|unresolved|residual|remaining)\s+(?:[\w-]+\s+){0,3}(?:issue|defect)s?\b|\bwith\s+(?:an?\s+)?issue\b|"
            r"(?:\u4ecd\u6709|\u5c1a\u6709).{0,24}(?:\u554f\u984c|\u95ee\u9898)|"
            r"(?:\u554f\u984c|\u95ee\u9898).{0,24}(?:\u672a|\u5f85)(?:\u89e3\u6c7a|\u89e3\u51b3|\u8655\u7406|\u5904\u7406)", re.I)
        issues = [match for match in issue_pattern.finditer(source.body)
            if not re.search(r"\b(?:no|without)(?:\s+any)?\s*$|(?:\u6c92\u6709|\u6ca1\u6709|\u7121|\u65e0)\s*$",
                source.body[max(0,match.start()-12):match.start()],re.I)]
        if issues:
            # Reuse only the exact source-level residual record, never a fuzzy
            # description match that might hide a different unresolved issue.
            existing=conn.execute("SELECT ticket_id FROM support_tickets WHERE related_project_id=? AND thread_anchor=? AND title=? AND description=? ORDER BY ticket_id",
                (project_id,source_ref,"Residual issue reported at PCT completion",source.body)).fetchall()
            if len(existing)>1:
                raise PctCompletionError("ambiguous duplicate residual records require reconciliation")
            ticket=get_ticket(conn,existing[0][0]) if existing else create_ticket(conn,"Residual issue reported at PCT completion","other",related_project_id=project_id,
                source_type="email",reporter_email=source.sender,description=source.body,thread_anchor=source_ref)
            if ticket.ticket_id not in tickets:
                tickets.append(ticket.ticket_id)
            if not conn.execute("SELECT 1 FROM ticket_evidence WHERE ticket_id=? AND source_ref=? AND content_text=?",(ticket.ticket_id,source_ref,source.body)).fetchone():
                insert_ticket_evidence(conn,ticket.ticket_id,"email","Residual issue from PCT confirmation",content_text=source.body,source_ref=source_ref)
        evidence=insert_evidence(conn,project_id,"email","Verified internal PCT completion",now,content_text=source.body,source_ref=source_ref)
        fields={"current_milestone":"M8_GoLive","project_status":"Completed","next_action":"","ball_with":"Unknown"}
        with verified_pct_write_scope(conn,source):
            result=safe_write_project(conn,project_id,fields,actor="verified_pct_service",evidence_ref=evidence,
                reason="PCT completed confirmed by "+source.authority,surface="email",commit=False,publish_event=False,
                schedule_notifications=False,expected_updated_at=expected_project_version,rollback_on_error=False)
        if result.outcome!="accepted":raise PctCompletionError(result.rejection_reason or result.needs_review_reason or "completion not applied")
        conn.execute("UPDATE followup_plan_heads SET revision=revision+1 WHERE project_id=?",(project_id,))
        conn.execute("INSERT INTO pct_completion_receipts VALUES (?,?,?,?,?,?)",(project_id,source_ref,source.source_hash,source.authority,json.dumps(tickets),now))
        from src.models.proposed_update import ProposedUpdate,ProposedChange,WriteDecision
        from src.models.enums import TaskType,RiskLevel,WriteDecisionOutcome
        from src.repositories.proposed_update_repo import store_proposed_update
        from src.email.update_notification_outbox import enqueue_update_notification
        from src.email.pct_completion_routing import completion_summary
        import hashlib
        update_id="pct_"+hashlib.sha256((project_id+"\0"+source_ref).encode()).hexdigest()[:32]
        accepted=ProposedUpdate(update_id=update_id,project_id=project_id,
            proposed_changes=[ProposedChange(field=field,old_value=project[field],new_value=value,
                reason="Source-verified PCT completion",evidence_ref=evidence) for field,value in fields.items()],
            task_type=TaskType.TRUTH_WRITE_REASONING,confidence=1.0,risk_level=RiskLevel.HIGH,
            policy_version="2",source_type="email",source_ref=source_ref)
        store_proposed_update(conn,accepted,WriteDecision(update_id=update_id,project_id=project_id,outcome=WriteDecisionOutcome.ACCEPTED))
        enqueue_update_notification(conn,update_id=update_id,project_id=project_id,payload={
            "_notification_kind":"pct_completed", "project_id":project_id,"update_id":update_id,
            "summary":completion_summary(conn,project_id),
        })
        conn.execute("RELEASE SAVEPOINT "+name)
        return {"outcome":"applied","project_id":project_id,"ticket_ids":tickets,"authority":source.authority,
            "source_ref":source_ref,"source_hash":source.source_hash,
            "evidence_id":evidence,"update_id":update_id}
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO SAVEPOINT "+name);conn.execute("RELEASE SAVEPOINT "+name)
        if started:conn.rollback()
        raise
