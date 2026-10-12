"""Confidence lifecycle -- decay stale rules, promote proven rules to knowledge base."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_DECAY_THRESHOLD_DAYS = 90
_DECAY_AMOUNT = 0.05
_ARCHIVE_THRESHOLD = 0.3
_PROMOTION_MIN_USAGE = 10
_PROMOTION_MIN_CONFIDENCE = 0.85

# Canonical singular pattern dir (wiki reform) — never write to legacy patterns/
KB_DIR = Path(__file__).parent.parent.parent / "wiki" / "pattern"
_LEARNED_RULES_PAGE = "learned_rules.md"


def decay_stale_rules(conn: sqlite3.Connection) -> int:
    """Decay confidence of approved rules not used in 90+ days. Returns count."""
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "learned_rules")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_DECAY_THRESHOLD_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT rule_id, confidence FROM learned_rules
           WHERE status = 'approved' AND updated_at IS NOT NULL AND updated_at < ?""",
        (cutoff,),
    ).fetchall()

    count = 0
    for row in rows:
        rule_id = row[0] if not isinstance(row, dict) else row["rule_id"]
        old_confidence = row[1] if not isinstance(row, dict) else row["confidence"]
        try:
            new_confidence = round(old_confidence - _DECAY_AMOUNT, 4)
            if new_confidence < _ARCHIVE_THRESHOLD:
                conn.execute(
                    "UPDATE learned_rules SET confidence = ?, status = 'archived' WHERE rule_id = ?",
                    (new_confidence, rule_id),
                )
            else:
                conn.execute(
                    "UPDATE learned_rules SET confidence = ? WHERE rule_id = ?",
                    (new_confidence, rule_id),
                )
            from src.repositories.activity_log_repo import insert_activity
            status_str = "archived" if new_confidence < _ARCHIVE_THRESHOLD else "decayed"
            insert_activity(
                conn, actor="system", action_type="confidence_decay",
                summary=f"Rule {rule_id} {status_str}: {old_confidence} → {new_confidence}",
            )
            count += 1
        except Exception:
            logger.exception("Failed to decay rule %s", rule_id)
    return count


def promote_proven_rules(conn: sqlite3.Connection) -> int:
    """Promote high-usage, high-confidence rules to wiki/pattern/*.md pages.

    Global rules → ``pattern/learned_rules.md``.
    Market-scoped rules → ``pattern/learned_rules_{market}.md`` (snake_case).
    Never writes to legacy ``patterns/`` (plural).
    """
    from config import defaults
    from src.learning.governance_flags import wiki_publication_disabled
    from src.learning.legacy_retirement import require_legacy_mutation, written_to_kb_authoritative
    if (wiki_publication_disabled() or defaults.WIKI_PUBLICATION_V2
            or not written_to_kb_authoritative(conn)):
        return 0
    require_legacy_mutation(conn, "learned_rules")
    from src.wiki.schema import ensure_bold_summary, parse_page, render_page, WikiPage
    from src.wiki.index_sync import append_log, ensure_index_entry
    from src.wiki.indexer import invalidate_wiki_index
    from src.wiki.graph import invalidate_wiki_graph

    rows = conn.execute(
        """SELECT rule_id, rule, condition, applicable_to, confidence, usage_count
           FROM learned_rules
           WHERE status = 'approved'
             AND usage_count >= ?
             AND confidence >= ?
             AND (written_to_kb = 0 OR written_to_kb IS NULL)""",
        (_PROMOTION_MIN_USAGE, _PROMOTION_MIN_CONFIDENCE),
    ).fetchall()

    count = 0
    now = datetime.now(timezone.utc).isoformat()
    day = now[:10]
    wrote_any = False

    for row in rows:
        d = dict(row) if hasattr(row, "keys") else {
            "rule_id": row[0], "rule": row[1], "condition": row[2],
            "applicable_to": row[3], "confidence": row[4], "usage_count": row[5],
        }
        try:
            targets = json.loads(d.get("applicable_to") or '["*"]')
        except (json.JSONDecodeError, TypeError):
            targets = ["*"]

        if "*" in targets or not targets:
            kb_file = KB_DIR / _LEARNED_RULES_PAGE
            page_title = "Learned Rules (Global)"
        else:
            market = str(targets[0]).strip() or "Global"
            safe = re.sub(r"[^a-z0-9]+", "_", market.lower()).strip("_") or "global"
            kb_file = KB_DIR / f"learned_rules_{safe}.md"
            page_title = f"Learned Rules ({market})"

        try:
            KB_DIR.mkdir(parents=True, exist_ok=True)
            if not kb_file.exists():
                page = WikiPage(
                    title=page_title,
                    type="pattern",
                    tags=["learned-rules"],
                    sources=[f"rule:{d['rule_id']}"],
                    created=day,
                    updated=day,
                    confidence="high",
                    status="active",
                    body=ensure_bold_summary(
                        f"Proven learned rules promoted from the learning loop.\n\n"
                        f"## Learned Rules\n",
                        page_title,
                    ),
                )
                kb_file.write_text(render_page(page), encoding="utf-8")
            else:
                page = parse_page(kb_file.read_text(encoding="utf-8"))

            condition_part = f"IF {d['condition']} THEN " if d.get("condition") else ""
            entry = (
                f"\n- **[{d['rule_id']}]** {condition_part}{d['rule']} "
                f"(confidence: {d['confidence']}, used {d['usage_count']} times)"
            )
            body = page.body or ""
            if "## Learned Rules" not in body:
                body = body.rstrip() + "\n\n## Learned Rules\n"
            body = body.rstrip() + entry + "\n"
            page.body = body
            page.updated = day
            if f"rule:{d['rule_id']}" not in (page.sources or []):
                page.sources = list(page.sources or []) + [f"rule:{d['rule_id']}"]
            kb_file.write_text(render_page(page), encoding="utf-8")

            rel = f"pattern/{kb_file.name}"
            ensure_index_entry(
                KB_DIR.parent, rel.removesuffix(".md"), page.title or page_title,
                page_type="pattern",
            )
            append_log(
                KB_DIR.parent,
                f"promote rule {d['rule_id']} → {rel}",
                writer="confidence-lifecycle",
            )
            wrote_any = True

            entry_bytes = entry.encode("utf-8")
            ownership = conn.execute(
                "SELECT snapshot_json FROM memory_projection_ownership WHERE target='learned_rules' AND legacy_key=?",
                (d["rule_id"],),
            ).fetchone()
            snapshot = json.loads(ownership[0] or "{}") if ownership else {}
            snapshot.update({"kb_file": str(kb_file.name), "wiki_entry": entry + "\n",
                             "wiki_entry_hash": hashlib.sha256(entry_bytes + b"\n").hexdigest()})
            conn.execute(
                """UPDATE learned_rules
                   SET written_to_kb = 1, kb_file = ?, kb_written_at = ?
                   WHERE rule_id = ?""",
                (str(kb_file.name), now, d["rule_id"]),
            )
            conn.execute("UPDATE memory_projection_ownership SET snapshot_json=? WHERE target='learned_rules' AND legacy_key=?",
                         (json.dumps(snapshot, sort_keys=True), d["rule_id"]))
            from src.repositories.activity_log_repo import insert_activity
            insert_activity(
                conn, actor="system", action_type="rule_promoted_to_kb",
                summary=f"Rule {d['rule_id']} promoted to {kb_file.name}",
            )
            count += 1
        except Exception:
            logger.exception("Failed to promote rule %s", d["rule_id"])

    if wrote_any:
        try:
            invalidate_wiki_index()
            invalidate_wiki_graph()
        except Exception:
            pass
    return count


def auto_approve_pending_rules(conn: sqlite3.Connection) -> int:
    """Auto-approve high-confidence pending rules that are not duplicates of approved rules.

    Rules with confidence >= 0.85 are approved if no semantically similar
    approved rule exists (checked via SequenceMatcher against approved rules).
    Returns count of auto-approved rules.
    """
    from src.learning.governance_flags import auto_activation_disabled, global_memory_disabled
    if auto_activation_disabled() or global_memory_disabled():
        return 0
    from src.learning.legacy_retirement import require_legacy_mutation
    require_legacy_mutation(conn, "learned_rules")
    import difflib
    import re

    def _norm(text: str) -> str:
        text = (text or "").lower().strip()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"[^\w\s]", "", text)
        return text

    rows = conn.execute(
        """SELECT rule_id, rule, confidence FROM learned_rules
           WHERE status = 'pending_review' AND confidence >= 0.85""",
    ).fetchall()

    count = 0
    for row in rows:
        d = dict(row) if hasattr(row, "keys") else {"rule_id": row[0], "rule": row[1], "confidence": row[2]}

        # Check if a similar rule already exists among approved rules
        normalized = _norm(d["rule"])
        approved_rows = conn.execute(
            "SELECT rule_id, rule FROM learned_rules WHERE status = 'approved'",
        ).fetchall()
        is_duplicate = False
        for ar in approved_rows:
            ad = dict(ar) if hasattr(ar, "keys") else {"rule_id": ar[0], "rule": ar[1]}
            ratio = difflib.SequenceMatcher(None, normalized, _norm(ad["rule"])).ratio()
            if ratio >= 0.75:
                is_duplicate = True
                break
        if is_duplicate:
            continue

        # Auto-approve
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE learned_rules SET status = 'approved', updated_at = ? WHERE rule_id = ?",
            (now, d["rule_id"]),
        )
        try:
            from src.repositories.activity_log_repo import insert_activity
            insert_activity(
                conn, actor="system", action_type="rule_auto_approved",
                summary=f"Rule {d['rule_id']} auto-approved (confidence={d['confidence']})",
            )
        except Exception:
            pass
        count += 1

    if count:
        logger.info("Auto-approved %d pending rules", count)
    return count


_STATUS_TRUSTED_MIN_CONFIDENCE = 0.9
_STATUS_ACTIVE_MIN_CONFIDENCE = 0.7


def _confidence_to_status(confidence: float) -> str:
    if confidence >= _STATUS_TRUSTED_MIN_CONFIDENCE:
        return "trusted"
    if confidence >= _STATUS_ACTIVE_MIN_CONFIDENCE:
        return "active"
    return "draft"


# Paths that must never be auto-archived or confidence-demoted by lifecycle
_PROTECTED_STATUS_PREFIXES = (
    "markets/",
    "concept/hk_ipo_operator_reference",
    "concept/settlement/dtc-identifier-for-settlement-instructions",
)
_STRUCTURAL_NAMES = frozenset({
    "index.md", "log.md", "schema.md", "lint_report.md", "WIKI_GUIDE.md", "README.md",
})


def _is_status_protected(rel_posix: str) -> bool:
    if rel_posix in _STRUCTURAL_NAMES or "/" not in rel_posix:
        # root structural or bare files under wiki/
        name = rel_posix.rsplit("/", 1)[-1]
        return name in _STRUCTURAL_NAMES
    return any(rel_posix.startswith(p) or rel_posix == p.rstrip("/")
               for p in _PROTECTED_STATUS_PREFIXES)


def advance_wiki_page_statuses(wiki_dir: Path | None = None) -> int:
    """Leave wiki lifecycle status unchanged during the containment period.

    Model confidence is not an operator verification decision. WikiPage v2 will
    introduce an explicit verification state before automated lifecycle changes
    can safely resume.
    """
    return 0
