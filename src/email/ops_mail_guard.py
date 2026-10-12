"""
Detect operational emails that must NOT drive FIX onboarding project SoR.

Shared vendor contacts serve many clients. Contact
membership alone is not enough to attach ops mail to an onboarding project.

See: wiki/pattern/email_vendor_multiplex_mislink.md
"""
from __future__ import annotations

import re

# Strong operational / non-onboarding subject signals
_OPS_SUBJECT_RE = re.compile(
    r"("
    r"stock\s*code\s*conversion|"
    r"ticker\s*conversion|"
    r"stock\s*list|"
    r"\bwithdrawal\b|"
    r"securities\s*lending|\bsbl\b|"
    r"margin\s*call|"
    r"corporate\s*action|"
    r"dividend|"
    r"allotment|"
    r"account\s*opening\b|"
    r"authorized\s*(dealer|person|trader)|"
    r"\bEDDA\b|"
    r"fund\s*remittance|"
    r"\bIPO\b.*order\s*form"
    r")",
    re.IGNORECASE,
)

# FIX onboarding context — ops guard is waived when clearly onboarding
_FIX_ONBOARDING_RE = re.compile(
    r"("
    r"\bFIX\b|"
    r"\bUAT\b|"
    r"conformance|"
    r"compid|sendercomp|targetcomp|"
    r"session\s*(info|param|setup)|"
    r"\blogon\b|"
    r"grey\s*market.*(fix|session|connectivity)|"
    r"provisioning\s*hub|"
    r"\bPCT\b|"
    r"production\s*connectivity|"
    r"onboarding"
    r")",
    re.IGNORECASE,
)

_PROJECT_TAG_RE = re.compile(r"\[([A-Za-z]{1,4}\d{4})\]")


def has_project_subject_tag(subject: str | None, project_id: str | None = None) -> bool:
    if not subject:
        return False
    tags = [t.upper() for t in _PROJECT_TAG_RE.findall(subject)]
    if not tags:
        return False
    if project_id is None:
        return True
    return project_id.upper() in tags


def has_fix_onboarding_context(subject: str | None, body: str | None = None) -> bool:
    text = f"{subject or ''}\n{body or ''}"
    return bool(_FIX_ONBOARDING_RE.search(text))


def is_ops_only_email(subject: str | None, body: str | None = None) -> bool:
    """True when mail looks like ops/BAU and not FIX onboarding evidence."""
    subj = subject or ""
    if not _OPS_SUBJECT_RE.search(subj):
        # body-only weak ops without subject signal — do not hard-block
        return False
    if has_fix_onboarding_context(subject, body):
        return False
    return True


def should_block_reopen_completed_without_tag(
    subject: str | None,
    *,
    project_id: str | None,
    current_status: str | None,
) -> bool:
    """Block reopening Completed/Archived via untagged client/vendor mail.

    Completed onboarding must not be reopened from unrelated, untagged mail.
    """
    if not project_id or not current_status:
        return False
    if current_status not in {"Completed", "Archived", "Cancelled"}:
        return False
    return not has_project_subject_tag(subject, project_id)


def should_block_project_truth_from_email(
    subject: str | None,
    *,
    project_id: str | None = None,
    body: str | None = None,
) -> bool:
    """
    Block using this email as project_update / SoR evidence.

    Allowed exceptions:
    - Subject has matching [PROJECT_ID] tag AND FIX onboarding context, or
    - Explicit FIX onboarding context without pure ops subject (handled above)
    """
    if not is_ops_only_email(subject, body):
        return False
    # Ops subject with no project tag → block
    if not has_project_subject_tag(subject, project_id):
        return True
    # Tag present but still pure ops (an operational stock list) → still block SoR
    return True
