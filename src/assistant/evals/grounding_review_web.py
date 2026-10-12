"""Local-only guided web UI for blinded grounding-repair evidence review."""

from __future__ import annotations

import html
import ipaddress
import secrets
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.assistant.evals.grounding_review import (
    ISSUE_CODES,
    RUBRIC_DIMENSIONS,
    GroundingReviewWorkflow,
    ReviewCaseView,
)

LOCAL_BIND_HOST = "127.0.0.1"

DIMENSION_GUIDANCE = {
    "evidence_backed_factual_correctness": (
        "Are the factual statements correct and supported by the displayed evidence?"
    ),
    "citation_correctness_support": (
        "Do citations identify the right supplied source and support the nearby claim?"
    ),
    "completeness": "Does the answer cover every important part of the request?",
    "scope_as_of_correctness": (
        "Does it use the correct project, population, boundary, and time cutoff?"
    ),
    "clarity_actionability": (
        "Is it easy to understand and clear about the next useful action?"
    ),
}

ISSUE_GUIDANCE = {
    "citation_id_error": "Citation ID is missing, malformed, unknown, or points to the wrong source.",
    "unsupported_claim": "A factual claim is not supported by the displayed evidence.",
    "scope_error": "The answer uses the wrong project, population, boundary, or question scope.",
    "as_of_error": "The answer uses the wrong date cutoff or treats stale/future data as current.",
    "critical_claim_error": "A materially important claim is wrong or unsafe.",
}


def validate_bind_host(host: str) -> str:
    if host != LOCAL_BIND_HOST:
        raise ValueError("grounding reviewer must bind only to 127.0.0.1")
    return host


def _is_loopback_client(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _authorized(value: str | None, expected: str) -> bool:
    return bool(value) and secrets.compare_digest(str(value), expected)


def _security_headers(response: Any) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
:root{{--bg:#eef3f8;--panel:#fff;--ink:#142033;--muted:#526176;--line:#d7e0ea;--accent:#2459d3;--accent-soft:#eaf0ff;--danger:#a61b1b;--warning:#fff8d8;--success:#e8f7ef}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{max-width:1380px;margin:auto;padding:24px}}h1,h2,h3{{line-height:1.25}}h1{{margin-bottom:8px}}h2{{margin-top:0}}.muted,small{{color:var(--muted)}}
.banner{{background:var(--warning);border:1px solid #e7ce72;padding:13px 16px;border-radius:10px}}.progress{{display:flex;justify-content:space-between;gap:12px;align-items:center;background:#142033;color:white;padding:12px 16px;border-radius:10px}}
.walkthrough{{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:16px 0}}.walkthrough div{{background:var(--accent-soft);border:1px solid #cbd8fa;padding:10px;border-radius:8px;text-align:center;font-weight:700}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px;margin:16px 0;box-shadow:0 2px 10px #1d2a4415}}.step{{color:var(--accent);font-size:.82rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}.answer{{border-top:5px solid var(--accent)}}.text-block{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f8fafc;border:1px solid var(--line);padding:14px;border-radius:8px;max-height:560px;overflow:auto}}
.evidence-list{{display:grid;gap:12px;margin-top:12px}}.evidence-card{{border:1px solid var(--line);border-radius:9px;padding:14px;background:#fbfcfe}}.evidence-index{{display:inline-block;background:var(--accent-soft);color:#173e9b;border-radius:999px;padding:2px 9px;font-size:.82rem;font-weight:800;margin-bottom:8px}}
.structured{{display:grid;gap:10px}}.field{{border-left:3px solid #d9e3f7;padding-left:10px}}.field-label{{font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:800}}.field-value{{margin-top:3px;overflow-wrap:anywhere}}.empty{{font-style:italic;color:var(--muted)}}
.guide{{background:var(--success);border:1px solid #b9dfca;border-radius:9px;padding:12px 15px}}.score-card{{border-top:5px solid #57677d}}.score-row{{display:grid;grid-template-columns:minmax(0,1fr) 110px;gap:14px;align-items:center;border-top:1px solid var(--line);padding:13px 0}}.score-row:first-of-type{{border-top:0}}.score-row label{{font-weight:750}}.score-row small{{display:block;font-weight:400;margin-top:2px}}
input[type=number]{{width:100%;padding:10px;border:1px solid #9ba9ba;border-radius:7px;font:inherit}}fieldset{{border:1px solid var(--line);border-radius:9px;margin-top:16px;padding:12px}}fieldset label{{display:block;margin:9px 0;font-weight:650}}fieldset small{{display:block;margin-left:24px}}
button{{background:var(--accent);color:white;border:0;border-radius:8px;padding:13px 20px;font-size:1rem;font-weight:800;cursor:pointer}}.submit-panel{{position:sticky;bottom:10px;border:2px solid #b8c8ee}}.error{{color:var(--danger)}}code{{overflow-wrap:anywhere}}details summary{{cursor:pointer;padding:4px 0}}
@media(max-width:900px){{.grid{{grid-template-columns:1fr}}.walkthrough{{grid-template-columns:1fr 1fr}}main{{padding:12px}}.submit-panel{{position:static}}}}@media(max-width:520px){{.walkthrough{{grid-template-columns:1fr}}.score-row{{grid-template-columns:1fr}}}}
</style></head><body><main>{body}</main></body></html>"""


def _humanize_key(value: Any) -> str:
    return str(value).replace("_", " ").replace("-", " ").strip().title()


def _render_structured(value: Any, *, depth: int = 0) -> str:
    if value is None:
        return '<span class="empty">Not supplied</span>'
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        if not value:
            return '<span class="empty">No fields supplied</span>'
        fields = []
        for key, item in value.items():
            fields.append(
                '<div class="field">'
                f'<div class="field-label">{html.escape(_humanize_key(key))}</div>'
                f'<div class="field-value">{_render_structured(item, depth=depth + 1)}</div>'
                "</div>"
            )
        return '<div class="structured">' + "".join(fields) + "</div>"
    if isinstance(value, (list, tuple)):
        if not value:
            return '<span class="empty">None supplied</span>'
        cards = []
        for index, item in enumerate(value, start=1):
            cards.append(
                '<article class="evidence-card">'
                f'<span class="evidence-index">Item {index}</span>'
                f'{_render_structured(item, depth=depth + 1)}'
                "</article>"
            )
        return '<div class="evidence-list">' + "".join(cards) + "</div>"
    rendered = html.escape(str(value))
    if isinstance(value, str) and ("\n" in value or len(value) > 180):
        return f'<div class="text-block">{rendered}</div>'
    return rendered


def _score_form(prefix: str, label: str) -> str:
    fields = []
    for dimension in RUBRIC_DIMENSIONS:
        guidance = DIMENSION_GUIDANCE[dimension.key]
        fields.append(
            '<div class="score-row">'
            f'<label for="{prefix}_{dimension.key}">{html.escape(dimension.label)}'
            f'<small>{html.escape(guidance)}</small></label>'
            f'<input required type="number" min="0" max="{dimension.weight:g}" step="0.5" '
            f'id="{prefix}_{dimension.key}" name="{prefix}__{dimension.key}" '
            f'placeholder="0–{dimension.weight:g}" aria-label="{html.escape(label)} {html.escape(dimension.label)}">'
            "</div>"
        )
    issues = "".join(
        '<label>'
        f'<input type="checkbox" name="{prefix}_issue" value="{code}"> '
        f'{html.escape(_humanize_key(code))}'
        f'<small>{html.escape(ISSUE_GUIDANCE[code])}</small>'
        "</label>"
        for code in ISSUE_CODES
    )
    return (
        f'<section class="panel score-card"><h3>{html.escape(label)}</h3>'
        '<p class="muted">Give full points only when this dimension has no meaningful problem. '
        'Use the issue checkboxes for factual/citation/scope problems, not writing style.</p>'
        + "".join(fields)
        + f'<fieldset><legend><strong>Material issue checklist</strong></legend>{issues}</fieldset></section>'
    )


def _review_page(case: ReviewCaseView, token: str) -> str:
    sources = _render_structured(case.sources)
    tool_results = _render_structured(case.tool_results)
    initial_errors = ", ".join(_humanize_key(item) for item in case.initial_errors) or "None"
    body = f"""
<h1>Grounding Repair Guided Review</h1>
<p class="muted">One blinded case at a time. Candidate/baseline identity stays hidden until submission.</p>
<p class="banner"><strong>Confidential local view.</strong> Do not copy, screenshot, paste, or persist the decrypted question, answers, sources, or tool results. Stop the reviewer before screen sharing.</p>
<div class="progress"><strong>Case {case.position} of {case.total}</strong><span>Completed {case.completed} · <code>{html.escape(case.case_id)}</code></span></div>
<div class="walkthrough"><div>1 · Read question</div><div>2 · Check evidence</div><div>3 · Compare answers</div><div>4 · Score both</div></div>
<section class="panel"><div class="step">Step 1 · What was asked?</div><h2>Original question</h2><div class="text-block">{html.escape(case.user_request or '[Unavailable]')}</div></section>
<section class="panel"><div class="step">Why this case needs review</div><h2>Draft before repair</h2><p class="muted">This is context only. Do not score this draft.</p><div class="text-block">{html.escape(case.prepared_answer)}</div><p><strong>Detected problem:</strong> {html.escape(initial_errors)}</p></section>
<section class="panel"><div class="step">Step 2 · What evidence is available?</div><h2>Evidence sources</h2><p class="guide"><strong>How to use this:</strong> check whether each factual statement in the answers is supported here and whether each citation points to the correct source.</p>{sources}</section>
<section class="panel"><details><summary><strong>Supporting tool results ({len(case.tool_results)})</strong></summary><p class="muted">Open this only when a claim depends on tool output.</p>{tool_results}</details></section>
<div class="step">Step 3 · Read both blinded answers</div>
<div class="grid">
<section class="panel answer"><h2>Answer A</h2><div class="text-block">{html.escape(case.answer_a)}</div></section>
<section class="panel answer"><h2>Answer B</h2><div class="text-block">{html.escape(case.answer_b)}</div></section>
</div>
<section class="panel"><div class="step">Step 4 · Score independently</div><h2>Five-question checklist</h2><ol><li>Are the facts correct and evidenced?</li><li>Are citations valid and supportive?</li><li>Is the request fully answered?</li><li>Is the project/date scope correct?</li><li>Is the answer clear and actionable?</li></ol><p class="guide"><strong>Pass rule:</strong> at least 80/100 and no issue code. The server calculates totals and safely reverses the hidden A/B mapping.</p></section>
<form method="post" action="/submit" autocomplete="off">
<input type="hidden" name="session_token" value="{html.escape(token, quote=True)}">
<input type="hidden" name="case_id" value="{html.escape(case.case_id, quote=True)}">
<div class="grid">{_score_form('a', 'Score Answer A')}{_score_form('b', 'Score Answer B')}</div>
<section class="panel submit-panel"><p><strong>Before submitting:</strong> score each answer on its own. A better answer can still have an issue code.</p><button type="submit">Save this case and show the next one</button></section>
</form>"""
    return _page("Grounding Repair Guided Review", body)


def _message_page(title: str, message: str, *, error: bool = False) -> str:
    css = "error" if error else ""
    return _page(
        title,
        f'<h1>{html.escape(title)}</h1><section class="panel"><p class="{css}">{html.escape(message)}</p></section>',
    )


def _parse_dimensions(form: dict[str, list[str]], prefix: str) -> dict[str, str]:
    return {
        dimension.key: (form.get(f"{prefix}__{dimension.key}") or [""])[0]
        for dimension in RUBRIC_DIMENSIONS
    }


def create_review_app(
    workflow: GroundingReviewWorkflow,
    *,
    session_token: str,
    allow_test_client: bool = False,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        client_host = request.client.host if request.client else None
        if not _is_loopback_client(client_host) and not (
            allow_test_client and client_host == "testclient"
        ):
            response = HTMLResponse(
                _message_page("Forbidden", "Local access only.", error=True),
                status_code=403,
            )
        else:
            response = await call_next(request)
        _security_headers(response)
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(token: str | None = None) -> HTMLResponse:
        if not _authorized(token, session_token):
            raise HTTPException(status_code=403, detail="Invalid reviewer session token")
        case = workflow.next_case()
        if case is None:
            status = workflow.status()
            return HTMLResponse(
                _message_page(
                    "Review complete",
                    f"All {status['total']} repair cases have durable review decisions. Seal the ledger next.",
                )
            )
        return HTMLResponse(_review_page(case, session_token))

    @app.post("/submit")
    async def submit(request: Request):
        raw = (await request.body()).decode("utf-8", errors="strict")
        form = parse_qs(raw, keep_blank_values=True)
        token = (form.get("session_token") or [None])[0]
        if not _authorized(token, session_token):
            raise HTTPException(status_code=403, detail="Invalid reviewer session token")
        try:
            workflow.submit(
                case_id=(form.get("case_id") or [""])[0],
                answer_a_dimensions=_parse_dimensions(form, "a"),
                answer_b_dimensions=_parse_dimensions(form, "b"),
                answer_a_issue_codes=form.get("a_issue", []),
                answer_b_issue_codes=form.get("b_issue", []),
            )
        except ValueError as exc:
            return HTMLResponse(
                _message_page("Review not saved", str(exc), error=True),
                status_code=422,
            )
        return RedirectResponse(url=f"/?token={session_token}", status_code=303)

    return app


__all__ = ["LOCAL_BIND_HOST", "create_review_app", "validate_bind_host"]
