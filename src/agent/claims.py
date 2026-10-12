"""Final-reply write claim extraction and verification."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from src.agent.tool_receipts import ToolExecutionReceipt

from src.agent.anchors import extract_anchors

_WRITE_TOOLS = frozenset({
    "update_project", "update_project_batch", "confirm_update",
    "create_project", "confirm_create_project", "cancel_create_project",
    "add_note", "draft_reply", "manage_followup", "add_contact",
    "log_evidence",
})
_PROJECT_INTAKE_TOOLS = frozenset({
    "create_project", "confirm_create_project", "cancel_create_project",
})

_MILESTONE_CLAIM_RE = re.compile(
    r"(?:advanced|moved|updated|推進|已推|已更新|已推到|推進到).{0,40}?"
    r"(M[1-8][_\w]*)",
    re.IGNORECASE,
)
_REJECTION_NEAR_MILESTONE_RE = re.compile(
    r"(?:唔可以|不能|不可|無法|请勿|不要|拒絕|拒绝|without\s+evidence|cannot|can't|do\s+not|"
    r"not\s+allowed|需要\s*evidence|requires?\s+evidence"
    r"|仲未|尚未|重未|未推進|未更新|未推到)",
    re.IGNORECASE,
)
_RECOMMENDATION_FRAME_RE = re.compile(
    r"(?:recommend(?:ed|ation)?|suggest(?:ed|ion)?|propos(?:e|ed|al)|"
    r"should|could|consider|next\s+action|next\s+step|plan(?:ned)?\s+to|"
    r"\u5efa\u8b70|\u5efa\u8bae|\u4e0b\u4e00\u6b65|"
    r"\u53ef\u8003\u616e|\u53ef\u8003\u8651|"
    r"\u61c9\u8a72|\u5e94\u8be5|\u8a08\u5283|\u8ba1\u5212)",
    re.IGNORECASE,
)
_SUCCESS_RE = re.compile(
    r"(?:successfully\s+updated|update\s+(?:successful|completed|applied|confirmed)|"
    r"has\s+been\s+updated|已更新|成功更新|成功推進)",
    re.IGNORECASE,
)
# Email-draft claims only — bare 「草稿」is used for project-intake proposals too.
_DRAFT_CLAIM_RE = re.compile(
    r"(?:"
    r"email\s+draft\s+(?:saved|created|ready|is\s+ready)|"
    r"draft_reply|"
    r"(?<![A-Za-z_])draft\s+(?:saved|created)\b|"
    r"郵件草稿|"
    r"草稿已(?:建立|準備|儲存|存檔|生成|就緒)|"
    r"已建立(?:郵件)?草稿|"
    r"已準備.{0,20}郵件草稿|"
    r"準備好了.{0,40}郵件草稿"
    r")",
    re.IGNORECASE,
)
_DRAFT_OFFER_FRAME_RE = re.compile(
    r"(?:"
    r"would\s+you\s+like(?:\s+me)?(?:\s+to)?|"
    r"do\s+you\s+want(?:\s+me)?(?:\s+to)?|"
    r"(?:shall|should|can|could|may)\s+i|"
    r"(?:want|need)\s+me\s+to|"
    r"if\s+you(?:'d|\s+would)?\s+like.{0,60}\bi\s+(?:can|could|will)|"
    r"if\s+you\s+(?:want|need).{0,60}\bi\s+(?:can|could|will)|"
    r"是否需要我|需(?:要|唔需要)我|要不要我|要唔要我|"
    r"(?:如(?:果)?(?:你)?需要|如有需要).{0,40}(?:我)?(?:可以|可|能夠|能)|"
    r"可以幫你|可幫你"
    r")",
    re.IGNORECASE,
)
# Project intake staging language — not an email draft claim.
_PROJECT_INTAKE_FRAME_RE = re.compile(
    r"(?:create_project|confirm_create_project|new\s+project|project\s+intake|"
    r"Proposed\s+NEW\s+project|not\s+created\s+yet|"
    r"新專案|新項目|建立專案|建立項目|專案接收|項目接收|"
    r"project_id\s*[:：])",
    re.IGNORECASE,
)


def _has_pending_draft(conn: sqlite3.Connection, project_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM drafts WHERE project_id = ? "
        "AND status IN ('created', 'in_review', 'approved') LIMIT 1",
        (project_id,),
    ).fetchone()
    return row is not None

# State-description frames. When success/milestone wording sits inside one of
# these (目前 / 處於 / 進度 / 上次 / currently / last update / status: ...) it
# describes existing project state, not an agent write — read-only status
# replies must not trip the fabrication guard.
_STATE_FRAME_RE = re.compile(
    r"(?:目前|現在|現處|現況|處於|位於|狀態|進度|"
    r"上次|最近|已於|"
    r"current(?:ly)?|last\s+update(?:d)?|recently|"
    r"status\s*[:：]|progress\s*[:：]|as\s+of)",
    re.IGNORECASE,
)
# Perfective "已X" verbs (already confirmed / updated / advanced) appear in both
# genuine claims and state reports. They are state-prone: "已確認到 M5" reports
# state, "已更新" right after a real write is a claim. Disambiguated by the
# positional check below; agentive verbs (成功推進 / successfully updated) skip it.
_PERFECTIVE_VERB_RE = re.compile(r"已確認|已更新|已推(?:到)?")
# A milestone target or date right after a perfective verb — "已確認到 M5",
# "已更新於 6/20" — is the shape of a state report.
_POSITIONAL_STATE_RE = re.compile(r"(?:到\s*M[1-8]|於\s*\d|at\s+M[1-8])", re.IGNORECASE)


@dataclass(frozen=True)
class WriteClaim:
    kind: str
    detail: str


@dataclass(frozen=True)
class ClaimViolation:
    code: str
    message: str


def _flatten_tool_history(tool_history: list[tuple[str, ...]]) -> list[str]:
    return [name for rnd in tool_history for name in rnd]


def _is_rejection_context(text: str, match_start: int) -> bool:
    """True when milestone wording appears in a refusal, not a completion claim."""
    window_start = max(0, match_start - 80)
    window = text[window_start:match_start + 20]
    return bool(_REJECTION_NEAR_MILESTONE_RE.search(window))


def _is_recommendation_context(text: str, match_start: int) -> bool:
    """Return True when a write-shaped phrase is explicitly framed as a proposal."""
    sentence_start = max(
        text.rfind("\n", 0, match_start),
        text.rfind(".", 0, match_start),
        text.rfind("!", 0, match_start),
        text.rfind("?", 0, match_start),
    )
    window = text[max(sentence_start + 1, match_start - 120):match_start + 40]
    return bool(_RECOMMENDATION_FRAME_RE.search(window))


def _is_state_description(text: str, match_start: int) -> bool:
    """True when wording sits in a read-only state-report frame, not a write claim.

    Status replies can use perfective language for an already confirmed state,
    "上次已更新於 6/20", "進度：成功推進到 M6" — to describe existing project
    state. Without this guard the verifier flags them as writes the agent never
    performed (false "no write tools were called" / unconfirmed milestone).

    A match is suppressed when a state frame precedes it, OR a perfective verb
    is immediately followed by a milestone target / date. Agentive claims
    ("Successfully updated the project.") have no
    preceding frame and a non-perfective verb, so they still flag.
    """
    pre = text[max(0, match_start - 80):match_start + 20]
    if _STATE_FRAME_RE.search(pre):
        return True
    if _PERFECTIVE_VERB_RE.match(text, match_start):
        post = text[match_start:match_start + 30]
        if _POSITIONAL_STATE_RE.search(post):
            return True
    return False


def _is_draft_offer_context(text: str, match_start: int) -> bool:
    """Return True when draft wording is an offer/question, not a write claim."""
    sentence_start = max(
        text.rfind("\n", 0, match_start),
        text.rfind(".", 0, match_start),
        text.rfind("!", 0, match_start),
        text.rfind("?", 0, match_start),
        text.rfind("。", 0, match_start),
        text.rfind("！", 0, match_start),
        text.rfind("？", 0, match_start),
    )
    prefix = text[max(sentence_start + 1, match_start - 160):match_start]
    return bool(_DRAFT_OFFER_FRAME_RE.search(prefix))


def extract_write_claims(text: str) -> list[WriteClaim]:
    if not text:
        return []
    claims: list[WriteClaim] = []

    success_hit = False
    for m in _SUCCESS_RE.finditer(text):
        if _is_recommendation_context(text, m.start()):
            continue
        if _is_rejection_context(text, m.start()):
            continue
        if _is_state_description(text, m.start()):
            continue
        success_hit = True
        break
    if success_hit:
        claims.append(WriteClaim(kind="write_success", detail="success language"))

    for m in _MILESTONE_CLAIM_RE.finditer(text):
        if _is_recommendation_context(text, m.start()):
            continue
        if _is_rejection_context(text, m.start()):
            continue
        if _is_state_description(text, m.start()):
            continue
        claims.append(WriteClaim(kind="milestone", detail=m.group(1)))
    if not _PROJECT_INTAKE_FRAME_RE.search(text):
        for match in _DRAFT_CLAIM_RE.finditer(text):
            if _is_draft_offer_context(text, match.start()):
                continue
            claims.append(WriteClaim(kind="draft", detail="draft created"))
            break
    return claims


def verify_claims(
    text: str,
    *,
    tool_history: list[tuple[str, ...]],
    conn: sqlite3.Connection | None,
    effective_project_id: str | None,
    execution_receipts: list[ToolExecutionReceipt] | None = None,
) -> list[ClaimViolation]:
    claims = extract_write_claims(text)
    if not claims:
        return []

    tools = set(_flatten_tool_history(tool_history))
    violations: list[ClaimViolation] = []

    anchors = extract_anchors(conn, effective_project_id=effective_project_id)

    receipts = execution_receipts or []
    committed_write = any(
        receipt.status == "succeeded" and receipt.committed
        for receipt in receipts
    )
    staged_only = any(
        receipt.status == "succeeded" and receipt.outcome == "staged"
        for receipt in receipts
    )
    delegated = "delegate" in tools

    if any(c.kind == "write_success" for c in claims):
        if committed_write:
            pass
        elif delegated:
            violations.append(ClaimViolation(
                code="delegate_write_unverified",
                message="Reply claims success but the delegated run has no committed write receipt.",
            ))
        elif staged_only:
            violations.append(ClaimViolation(
                code="write_only_staged",
                message="Reply claims success but the change is only staged for confirmation.",
            ))
        elif not tools.intersection(_WRITE_TOOLS):
            violations.append(ClaimViolation(
                code="no_write_tools",
                message="Reply claims success but no write tools were called.",
            ))
        elif (
            "update_project" in tools or "update_project_batch" in tools
        ) and "confirm_update" not in tools:
            violations.append(ClaimViolation(
                code="missing_confirm",
                message="Reply claims update success but confirm_update was not called.",
            ))
        elif (
            "create_project" in tools
            and "confirm_create_project" not in tools
            and not tools.intersection({"update_project", "update_project_batch", "confirm_update"})
        ):
            # Staging create_project is not full creation — only flag agentive "created" claims.
            if re.search(
                r"(?:created\s+successfully|project\s+created|已成功建立|已建立專案|已建立項目)",
                text or "",
                re.IGNORECASE,
            ):
                violations.append(ClaimViolation(
                    code="missing_create_confirm",
                    message=(
                        "Reply claims project created but confirm_create_project was not called."
                    ),
                ))

    if any(c.kind == "milestone" for c in claims):
        if delegated and not committed_write:
            violations.append(ClaimViolation(
                code="delegate_milestone_unverified",
                message="Milestone claim is not supported by a committed delegated write receipt.",
            ))
        elif "confirm_update" not in tools and not committed_write and anchors.current_milestone:
            for c in claims:
                if c.kind == "milestone" and c.detail not in (anchors.current_milestone or ""):
                    if "confirm_update" not in tools:
                        violations.append(ClaimViolation(
                            code="milestone_unconfirmed",
                            message=f"Milestone claim {c.detail} not confirmed in tool history.",
                        ))
                        break

    if any(c.kind == "draft" for c in claims) and "draft_reply" not in tools:
        # Project intake often says 「草稿」for staged proposals — not email drafts.
        if (
            delegated
            or tools.intersection(_PROJECT_INTAKE_TOOLS)
            or _PROJECT_INTAKE_FRAME_RE.search(text or "")
        ):
            pass
        else:
            draft_verified = (
                conn is not None
                and effective_project_id
                and _has_pending_draft(conn, effective_project_id)
            )
            if not draft_verified:
                violations.append(ClaimViolation(
                    code="draft_unverified",
                    message="Reply claims draft creation but draft_reply was not called.",
                ))

    return violations


def format_claim_violation_reply(violations: list[ClaimViolation]) -> str:
    lines = [
        "⚠️ 回覆聲稱已完成操作，但工具紀錄未能確認：",
        *[f"- {v.message}" for v in violations],
        "請重新發送請求，或確認是否需要 confirm_update。",
    ]
    return "\n".join(lines)
