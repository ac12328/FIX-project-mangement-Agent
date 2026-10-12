"""Bind source roles to envelope addresses and operator-confirmed authority policy.

Authorization lookup only: this module does not authenticate SMTP/DKIM or infer
authority from display names, message bodies, model labels, or unapproved domains.
"""
from dataclasses import dataclass
from email.utils import getaddresses
import json
import re
from src.models.progress_assessment import EvidenceLevel

@dataclass(frozen=True)
class SourceAuthority:
    actor: str | None
    evidence_level: EvidenceLevel
    basis: str


# Operator-approved Demo Connectivity Acceptance Support identities. Domain matching is exact and
# intentionally does not include subdomains. Keep exceptions as full mailboxes.
CONNECTIVITY_ACCEPTANCE_SUPPORT_DOMAINS = frozenset({"acceptance.demo.invalid"})
CONNECTIVITY_ACCEPTANCE_SUPPORT_ADDRESSES = frozenset({"acceptance-special@exception.demo.invalid"})


def normalized_sender(value):
    if not isinstance(value,str) or "\n" in value or "\r" in value:
        return None
    addresses=getaddresses([value])
    if len(addresses)!=1:
        return None
    address=addresses[0][1].strip().lower()
    return address if address.count("@") == 1 and all(address.split("@")) else None


def resolve_source_authority(conn, *, project_id, sender_email):
    sender=normalized_sender(sender_email)
    unknown=SourceAuthority(sender,EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT,"unverified_sender")
    if sender is None:
        return unknown
    local_part, domain = sender.rsplit("@", 1)
    if local_part and domain in CONNECTIVITY_ACCEPTANCE_SUPPORT_DOMAINS:
        return SourceAuthority(
            "Demo Connectivity Acceptance Support",
            EvidenceLevel.L3_NAMED_AUTHORITY_WRITTEN,
            "operator_confirmed_support_domain",
        )
    if sender in CONNECTIVITY_ACCEPTANCE_SUPPORT_ADDRESSES:
        return SourceAuthority(
            "Demo Connectivity Acceptance Support",
            EvidenceLevel.L3_NAMED_AUTHORITY_WRITTEN,
            "operator_confirmed_support_address",
        )
    rows=conn.execute("SELECT c.*,o.org_type FROM contacts c LEFT JOIN organisations o ON c.organisation_id=o.org_id WHERE c.created_by='operator_confirmed'").fetchall()
    matches=[]
    support={"demo provisioning support":"Demo Provisioning Support","demo connectivity acceptance support":"Demo Connectivity Acceptance Support"}
    for row in rows:
        raw_aliases = row["email_addresses"] or ""
        try:
            aliases = json.loads(raw_aliases)
        except (ValueError, TypeError):
            # The existing contact repository also stores comma-separated mailboxes.
            aliases = [address for _, address in getaddresses([raw_aliases])] if isinstance(raw_aliases, str) and not raw_aliases.lstrip().startswith("[") else []
        if isinstance(aliases, str):
            aliases = [address for _, address in getaddresses([aliases])]
        if not isinstance(aliases, list):
            aliases = []
        emails={normalized_sender(value) for value in [row["primary_email"],*aliases]}
        if sender not in emails:
            continue
        role=(row["role"] or "").strip().casefold()
        if row["contact_type"]=="internal" or row["org_type"]=="internal":
            department = (row["department"] or "").strip().casefold()
            teams = {support[value] for value in (role, department) if value in support}
            if len(teams) > 1:
                return SourceAuthority(sender, EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT, "conflicting_contact_roles")
            matches.append(next(iter(teams), "Internal"))
            continue
        linked=conn.execute("SELECT 1 FROM contact_project_links WHERE project_id=? AND contact_id=?",(project_id,row["contact_id"])).fetchone()
        party=conn.execute("SELECT party_role FROM project_parties WHERE project_id=? AND org_id=?",(project_id,row["organisation_id"])).fetchall() if row["organisation_id"] else []
        roles={r[0] for r in party}
        if "vendor" in roles:matches.append("Vendor")
        elif "client" in roles or linked:matches.append("Client")
    roles=set(matches)
    if len(roles)>1:
        return SourceAuthority(sender,EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT,"conflicting_contact_roles")
    if roles:
        actor=next(iter(roles))
        level=EvidenceLevel.L3_NAMED_AUTHORITY_WRITTEN if actor in support.values() else EvidenceLevel.L4_TRUSTED_PARTY_WRITTEN
        return SourceAuthority(actor,level,"operator_confirmed_contact")
    project=conn.execute("SELECT client_email FROM projects WHERE project_id=?",(project_id,)).fetchone()
    if project and normalized_sender(project[0])==sender:
        return SourceAuthority("Client",EvidenceLevel.L4_TRUSTED_PARTY_WRITTEN,"project_client_address")
    return unknown


_PCT_SUBJECT = r"(?:PCT(?:\s+tests?)?|production\s+connectivity\s+test)"
_PCT_AFFIRMATION = re.compile(
    rf"\b{_PCT_SUBJECT}\b\s*(?:\(PCT\))?\s*(?:(?:is|was|has\s+been)\s+)?(?:successfully\s+)?(?:completed|complete|passed|done)\b|"
    rf"\b(?:completed|passed)\s+(?:the\s+)?{_PCT_SUBJECT}\b|"
    r"PCT\s*(?:\u5df2)?(?:\u5b8c\u6210|\u901a\u904e|\u901a\u8fc7)|(?:\u5b8c\u6210|\u901a\u904e|\u901a\u8fc7)\s*PCT",
    re.IGNORECASE,
)
_PCT_PREDICATES = frozenset({"provisioning_support_pct_completed", "connectivity_acceptance_support_pct_completed", "internal_pct_completed_with_outstanding_issue"})


def bound_pct_modality(predicate, citation, authored_body):
    """Reject clear plans/questions/relays masquerading as direct PCT signoff.

Conservative explicit-signoff gate, not a general natural-language verifier.
A contextual short reply requires separately verified thread evidence; it is not
promoted merely because the model labels it confirmed.
"""
    from src.models.progress_assessment import EvidenceModality
    if predicate not in _PCT_PREDICATES or citation.modality != EvidenceModality.CONFIRMED:
        return citation
    match = _PCT_AFFIRMATION.search(citation.quote)
    if not match:
        return citation.model_copy(update={"modality": EvidenceModality.UNKNOWN})
    locations = [m.start() for m in re.finditer(re.escape(citation.quote), authored_body)]
    if len(locations) != 1:
        return citation.model_copy(update={"modality": EvidenceModality.UNKNOWN})
    position = locations[0] + match.start()
    prefix = re.split(r"[.!?;\n\u3002\uff01\uff1f]|\bbut\b", authored_body[:position], flags=re.I)[-1]
    prefix = re.sub(r"\b(we|I)\s+would\s+like\s+to\s+confirm\b", r"\1 confirm", prefix, flags=re.I)
    if re.search(r"\b(?:if|unless|when|once|provided|whether)\b|\u5982\u679c|\u82e5|\u5047\u5982|\u662f\u5426", prefix, re.I):
        return citation.model_copy(update={"modality": EvidenceModality.CONDITIONAL})
    if re.search(r"\b(?:not|never|cannot|says|reports)\b|\u672a|\u4e0d|\u5ba2\u6236(?:\u8868\u793a|\u8aaa|\u78ba\u8a8d)|\u5ba2\u6237(?:\u8868\u793a|\u8bf4|\u786e\u8ba4)", prefix, re.I):
        return citation.model_copy(update={"modality": EvidenceModality.UNKNOWN})
    if re.search(r"\b(?:will|would|could|may|might|plan|plans|planned|planning|expect|expected|hope|intend|should)\b|\u8a08\u5283|\u8ba1\u5212|\u9810\u8a08|\u9884\u8ba1", prefix, re.I):
        return citation.model_copy(update={"modality": EvidenceModality.PLANNED})
    if re.search(r"\b(?:confirm|verify)\b|\u8acb\u78ba\u8a8d|\u8bf7\u786e\u8ba4|\u8acb\u554f|\u8bf7\u95ee", prefix, re.I):
        if not re.search(r"\b(?:we|I)\s+(?:(?:hereby|can|do)\s+)?confirm\b", prefix, re.I):
            return citation.model_copy(update={"modality": EvidenceModality.UNKNOWN})
    tail = authored_body[locations[0] + match.end():]
    if re.match(r"\s*[?\uff1f]|\s+(?:is\s+false|was\s+incorrect|is\s+not\s+confirmed)\b", tail, re.I):
        return citation.model_copy(update={"modality": EvidenceModality.UNKNOWN})
    if re.match(r"\s+(?:tomorrow|next\s+(?:week|month|year|Monday|Tuesday|Wednesday|Thursday|Friday))\b|\s*\u660e\u5929", tail, re.I):
        return citation.model_copy(update={"modality": EvidenceModality.PLANNED})
    return citation

