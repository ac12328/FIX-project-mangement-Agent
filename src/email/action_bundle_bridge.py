"""Source-bound email action changes through the shared atomic action service.

No model-controlled flag grants operator authorization. Ambiguous semantics,
unknown targets and unanchored dates require review; no partial action writes.
"""
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import re
from src.email.body_cleaner import clean_email_body
from src.email.action_bundle_receipt import ActionBundleCommandReceipt, action_bundle_command_receipt
from src.email.source_chronology import find_newer_substantive_email
from src.email.source_authority import resolve_source_authority,normalized_sender
from src.models.followup_plan import ActionBundle
from src.models.progress_assessment import EvidenceLevel

_SCOPE=ContextVar("verified_email_action_bundle",default=None)

@dataclass(frozen=True)
class EmailActionOutcome:
    status: str
    reason: str = ""
    result: dict | None = None
    command: ActionBundleCommandReceipt | None = None


def permits_email_action_bundle(conn,project_id,bundle,actor_id):
    scope=_SCOPE.get()
    return scope is not None and scope[0] is conn and scope[1:]==(project_id,bundle.identity_json(),actor_id)


def _review(reason):
    return EmailActionOutcome("needs_review",reason)



def _content_terms(text):
    stop={"a","an","the","to","for","of","and","please","client","internal","external","task","we","i","will","must"}
    words=[]
    for word in re.findall(r"[a-z0-9]+",text.casefold()):
        if word in stop:continue
        if word=="sent":word="send"
        elif word.endswith("ed") and len(word)>4:word=word[:-2]
        elif word.endswith("s") and len(word)>3:word=word[:-1]
        words.append(word)
    return set(words)


def _description_supported(description,text,action_id):
    if action_id and action_id in text:return True
    if description.casefold().strip(" .") in text.casefold():return True
    terms=_content_terms(description)
    return bool(terms) and terms <= _content_terms(text)


def _validate_changes(case,changes,plan,body):
    existing={row["followup_id"]:row for row in plan["actions"]}
    new_ids={item.change.action_id for item in changes if item.change.operation=="create"}
    for item in changes:
        change=item.change
        if item.extraction_confidence < 0.95:return "low-confidence action extraction"
        if not item.citations:return "action citations missing"
        quotes=[]
        for citation in item.citations:
            if citation.source_type.value!="email" or citation.source_ref!=case.source_ref or citation.quote not in body:
                return "action citation is not bound to the current email"
            if citation.polarity.value!="positive":return "negative action evidence requires review"
            date_only_update = change.operation == "update" and (
                change.model_fields_set - {"action_id", "operation", "expected_revision"} == {"due_date"}
            )
            allowed={"confirmed","planned"} if change.operation=="create" or date_only_update else {"confirmed"}
            if citation.modality.value not in allowed:return "conditional or historical action evidence"
            quotes.append(citation.quote)
        text=" ".join(quotes)
        if change.owner_side is not None and re.search(r"\b(?:we|I)\s+(?:will|shall|commit)\b", text, re.I):
            primary = next((source for source in case.sources if source.source_ref == case.source_ref), None)
            expected_owner = "External" if primary and primary.actor in {"Client", "Vendor"} else "Internal"
            if change.owner_side != expected_owner:
                return "first-person commitment owner contradicts the bound sender"
        description = change.description if change.operation=="create" else existing.get(change.action_id,{}).get("description", "")
        if not description or not _description_supported(description,text,change.action_id if change.operation!="create" else None):
            return "citation does not identify the proposed task; preserve source wording or clarify the target"
        if change.operation=="update" and "description" in change.model_fields_set:
            if not change.description or not _description_supported(change.description,text,None):
                return "replacement description is not supported by the quote"
        if change.operation=="update" and "owner_side" in change.model_fields_set:
            if not re.search(r"\b(?:assign|reassign|responsible|take over)\b|\u8ca0\u8cac|\u8d1f\u8d23|\u8f49\u4ea4|\u8f6c\u4ea4",text,re.I):
                return "ownership reassignment is not explicit"
        if re.search(r"\b(?:might|maybe|possibly|hope|expect|tentative)\b|\u53ef\u80fd|\u9810\u8a08|\u9884\u8ba1",text,re.I):
            return "tentative action or date"
        if change.operation == "create":
            normalized = " ".join((change.description or "").casefold().split()).strip(" .")
            if any(" ".join(row["description"].casefold().split()).strip(" .") == normalized
                   and row["status"] in {"open", "overdue"} for row in existing.values()):
                return "matching open action already exists; identify an update instead of duplicating it"
            if re.search(r"\b(?:completed|finished|done|resolved)\b", text, re.I) and not re.search(r"\b(?:please|kindly|will|shall|commit|again|new)\b", text, re.I):
                return "completed work is not a new open action"
            if any(c.modality.value == "planned" for c in item.citations) and not re.search(r"\b(?:we|I|client|internal|Demo Provisioning|Demo Connectivity)\s+(?:will|shall|commit)\b", text, re.I):
                return "planned work lacks an explicit commitment"
        if change.operation in {"complete","cancel"}:
            if re.search(r"\b(?:will|would|could|if|unless|not|never|plan|planned)\b|\u5c1a\u672a|\u672a\u5b8c\u6210|[?\uff1f]",text,re.I):
                return "completion/cancellation is not confirmed"
            words=r"\b(?:completed|finished|done|fixed|resolved|passed)\b|\u5b8c\u6210|\u89e3\u6c7a|\u89e3\u51b3|\u901a\u904e|\u901a\u8fc7" if change.operation=="complete" else r"\b(?:cancel|cancelled|canceled|withdrawn)\b|\u53d6\u6d88"
            if not re.search(words,text,re.I):return "missing explicit action outcome"
            if change.operation=="cancel" and re.search(r"\border\b",text,re.I) and not re.search(r"\b(?:task|followup|follow-up)\b",text,re.I):
                return "FIX order cancellation is not task cancellation"
        if change.operation!="create":
            row=existing.get(change.action_id)
            if not row or row["status"] not in {"open","overdue"}:return "action is not in the captured open plan"
            if change.expected_revision!=row["action_revision"]:return "action revision was not copied from the captured plan"
        if "due_date" in change.model_fields_set:
            if change.due_date is None:return "deadline removal requires explicit operator review"
            if change.due_date.isoformat() not in text:
                from src.email.commitment_dates import source_relative_date
                if source_relative_date(text, case.authored_at) != change.due_date:
                    return "deadline lacks an explicit calendar date or an unambiguous authored-time/timezone anchor"
            if not re.search(r"\b(?:commit|committed|agreed|confirmed|we will|I will)\b|\u627f\u8afe|\u627f\u8bfa|\u78ba\u8a8d|\u786e\u8ba4", text, re.I):
                return "a requested or mentioned date is not a confirmed commitment"
        if "reminder_due_at" in change.model_fields_set:return "internal reminder scheduling is not an email commitment"
        if change.depends_on is not None:
            if any(key not in existing and key not in new_ids for key in change.depends_on):return "dependency outside captured plan"
            if change.depends_on and not re.search(r"\b(?:after|before|until|depends|first|once)\b|\u4e4b\u5f8c|\u5f8c|\u540e|\u5148",text,re.I):return "dependency not explicitly cited"
    return None


def apply_email_action_changes(conn,*,case,changes,sender_email):
    if not changes:return EmailActionOutcome("not_applicable")
    plan=case.project_snapshot.action_plan
    if not plan or case.project_snapshot.action_plan_truncated:return _review("a complete captured action plan is required")
    if plan.get("project_id")!=case.project_id:return _review("action plan project mismatch")
    if normalized_sender(sender_email)!=normalized_sender(case.sender_email):return _review("sender changed")
    source=conn.execute("SELECT project_id,sender_email,body_snippet,sent_at FROM processed_emails WHERE email_id=?",(case.source_ref,)).fetchone()
    if not source or source["project_id"]!=case.project_id or normalized_sender(source["sender_email"])!=normalized_sender(sender_email):
        return _review("stored source identity mismatch")
    newer=find_newer_substantive_email(conn,project_id=case.project_id,source_email_id=case.source_ref)
    if newer:return _review(f"source was superseded by newer email {newer['email_id']}; reassess the latest thread")
    from src.email.commitment_dates import authored_datetime
    stored_authored = authored_datetime(source["sent_at"])
    if case.authored_at != stored_authored:
        return _review("authored timestamp differs from the stored source")
    body=clean_email_body(source["body_snippet"] or "")
    if body!=case.newest_authored_body:return _review("stored source differs from evaluated email")
    authority=resolve_source_authority(conn,project_id=case.project_id,sender_email=sender_email)
    if authority.evidence_level==EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT:return _review("source authority is unverified")
    sources=[s for s in case.sources if s.source_ref==case.source_ref and s.source_type.value=="email"]
    if len(sources)!=1 or sources[0].actor!=authority.actor or sources[0].evidence_level!=authority.evidence_level:
        return _review("source authority changed after extraction")
    reason=_validate_changes(case,changes,plan,body)
    if reason:return _review(reason)
    from src.services.followup_plan import apply_action_bundle,ActionPlanError
    from src.repositories.evidence_repo import insert_evidence
    from datetime import datetime,timezone
    evidence_id="email-actions-"+hashlib.sha256((case.project_id+"\0"+case.source_ref).encode()).hexdigest()[:32]
    bundle=ActionBundle(operation_id=evidence_id,expected_plan_revision=plan["revision"],
        expected_project_version=plan["project_version"],reason="Source-bound email action changes",
        evidence_ref=evidence_id,actions=[item.change for item in changes])
    actor="email:"+normalized_sender(sender_email)
    started=not conn.in_transaction
    if started:conn.execute("BEGIN IMMEDIATE")
    savepoint="email_actions_"+hashlib.sha256(case.source_ref.encode()).hexdigest()[:16]
    conn.execute("SAVEPOINT "+savepoint)
    try:
        current_source=conn.execute("SELECT project_id,sender_email,body_snippet,sent_at FROM processed_emails WHERE email_id=?",(case.source_ref,)).fetchone()
        current_authority=resolve_source_authority(conn,project_id=case.project_id,sender_email=sender_email)
        newer=find_newer_substantive_email(conn,project_id=case.project_id,source_email_id=case.source_ref)
        if newer:raise ActionPlanError(f"source was superseded by newer email {newer['email_id']}; reassess the latest thread")
        if (not current_source or current_source["project_id"]!=case.project_id
            or normalized_sender(current_source["sender_email"])!=normalized_sender(sender_email)
            or clean_email_body(current_source["body_snippet"] or "")!=body
            or current_source["sent_at"]!=source["sent_at"]
            or current_authority!=authority):
            raise ActionPlanError("source or authority changed before write")
        prior=conn.execute("SELECT content_text,project_id FROM evidence_store WHERE evidence_id=?",(evidence_id,)).fetchone()
        if prior and (prior[0]!=body or prior[1]!=case.project_id):raise ActionPlanError("source evidence collision")
        if not prior:insert_evidence(conn,case.project_id,"email","Email action evidence",datetime.now(timezone.utc).isoformat(),
            content_text=body,source_ref=case.source_ref,evidence_id=evidence_id)
        token=_SCOPE.set((conn,case.project_id,bundle.identity_json(),actor))
        try:result=apply_action_bundle(conn,project_id=case.project_id,bundle=bundle,actor_id=actor)
        finally:_SCOPE.reset(token)
        command=action_bundle_command_receipt(conn,project_id=case.project_id,
            source_ref=case.source_ref,bundle=bundle,actor_id=actor,result=result)
        conn.execute("RELEASE SAVEPOINT "+savepoint)
        return EmailActionOutcome(result["outcome"],result=result,command=command)
    except ActionPlanError as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO SAVEPOINT "+savepoint);conn.execute("RELEASE SAVEPOINT "+savepoint)
        if started:conn.rollback()
        return _review(str(exc))
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO SAVEPOINT "+savepoint);conn.execute("RELEASE SAVEPOINT "+savepoint)
        if started:conn.rollback()
        raise
