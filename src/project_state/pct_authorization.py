"""Scoped PCT write capability issued only from persisted, verified source data.

Role authorization is directory-based, not cryptographic mail authentication.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import re
from src.email.body_cleaner import clean_email_body
from src.email.source_chronology import find_newer_substantive_email
from src.email.source_authority import resolve_source_authority,bound_pct_modality,_PCT_AFFIRMATION
from src.models.progress_assessment import EvidenceCitation,EvidenceModality,EvidenceLevel

class PctCompletionError(ValueError):
    pass

@dataclass(frozen=True)
class PctSource:
    project_id: str
    source_ref: str
    sender: str
    authority: str
    body: str
    quote: str
    source_hash: str

_SCOPE=ContextVar("verified_pct_write",default=None)

# At M7, the testing context is already production connectivity testing. A named
# support authority may therefore close with a direct short-form confirmation
# such as "All tests are passed" without repeating "PCT" in the same sentence.
_M7_ALL_TESTS_AFFIRMATION = re.compile(
    r"\ball\s+(?:the\s+)?tests?\s+"
    r"(?:(?:are|were|have\s+been)\s+)?"
    r"(?:successfully\s+)?(?:passed|completed|complete|done)\b",
    re.IGNORECASE,
)


def verify_pct_source(conn,project_id,source_ref):
    row=conn.execute("SELECT project_id,sender_email,body_snippet FROM processed_emails WHERE email_id=?",(source_ref,)).fetchone()
    if not row or row["project_id"]!=project_id:raise PctCompletionError("PCT source must be stored under this project")
    newer=find_newer_substantive_email(conn,project_id=project_id,source_email_id=source_ref)
    if newer:raise PctCompletionError(f"PCT source was superseded by newer email {newer['email_id']}; reassess the latest thread")
    authority=resolve_source_authority(conn,project_id=project_id,sender_email=row["sender_email"])
    if authority.actor not in {"Demo Provisioning Support","Demo Connectivity Acceptance Support"} or authority.evidence_level!=EvidenceLevel.L3_NAMED_AUTHORITY_WRITTEN:
        raise PctCompletionError("named internal PCT authority required")
    body=clean_email_body(row["body_snippet"] or "")
    project = conn.execute(
        "SELECT current_milestone FROM projects WHERE project_id=?",
        (project_id,),
    ).fetchone()
    accepted=None
    for match in _PCT_AFFIRMATION.finditer(body):
        start=body.rfind("\n",0,match.start())+1
        end=body.find("\n",match.end())
        end=len(body) if end<0 else end
        quote=body[start:end].strip()
        if len(quote)>4000:quote=body[max(start,match.start()-300):min(end,match.end()+300)].strip()
        citation=EvidenceCitation(source_type="email",source_ref=source_ref,quote=quote,actor=authority.actor,
            evidence_level=authority.evidence_level,modality="confirmed")
        guarded=bound_pct_modality("provisioning_support_pct_completed",citation,body)
        if guarded.modality==EvidenceModality.CONFIRMED:accepted=quote;break
    if not accepted and project and project["current_milestone"] == "M7_LogReviewAndProductionSessionDelivery":
        match = _M7_ALL_TESTS_AFFIRMATION.search(body)
        if match:
            start = body.rfind("\n", 0, match.start()) + 1
            end = body.find("\n", match.end())
            end = len(body) if end < 0 else end
            accepted = body[start:end].strip()
    if not accepted:raise PctCompletionError("direct, unambiguous PCT completion confirmation required")
    digest=hashlib.sha256((row["sender_email"]+"\0"+body).encode()).hexdigest()
    return PctSource(project_id,source_ref,row["sender_email"],authority.actor,body,accepted,digest)

@contextmanager
def verified_pct_write_scope(conn,source):
    fresh=verify_pct_source(conn,source.project_id,source.source_ref)
    if fresh!=source:raise PctCompletionError("PCT source changed")
    token=_SCOPE.set((conn,source))
    try:yield
    finally:_SCOPE.reset(token)


def permits_pct_completion(conn,project_id,fields):
    scope=_SCOPE.get()
    if scope is None or scope[0] is not conn or scope[1].project_id!=project_id:return False
    if fields.get("current_milestone")!="M8_GoLive" or fields.get("project_status")!="Completed" or fields.get("next_action")!="":return False
    if set(fields)-{"current_milestone","project_status","next_action","ball_with"}:return False
    try:return verify_pct_source(conn,project_id,scope[1].source_ref)==scope[1]
    except PctCompletionError:return False
