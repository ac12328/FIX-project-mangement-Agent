"""Durable email action reviews; operator decisions reuse the atomic plan service."""
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4
from src.email.body_cleaner import clean_email_body
from src.email.source_chronology import find_newer_substantive_email
from src.email.source_authority import normalized_sender, resolve_source_authority
from src.models.followup_plan import ActionBundle
from src.models.progress_assessment import EvidenceLevel
from src.services.followup_plan import apply_action_bundle, plan_snapshot


class ActionReviewError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _time(value):
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _source(conn, project_id, source_ref):
    row = conn.execute("SELECT project_id,sender_email,body_snippet,sent_at FROM processed_emails WHERE email_id=?", (source_ref,)).fetchone()
    if not row or row["project_id"] != project_id:
        raise ActionReviewError("email source is not associated with this project")
    authority = resolve_source_authority(conn, project_id=project_id, sender_email=row["sender_email"])
    return {"project_id": project_id, "sender": normalized_sender(row["sender_email"]),
        "body": clean_email_body(row["body_snippet"] or ""), "authored_at": _time(row["sent_at"]),
        "actor": authority.actor, "evidence_level": authority.evidence_level.value}


def queue_email_action_review(conn, *, project_id, source_ref, case, changes, reason):
    current = _source(conn, project_id, source_ref)
    captured = dict(current)
    verified = case is not None and case.project_id == project_id and case.source_ref == source_ref
    if verified:
        sources = [s for s in case.sources if s.source_ref == source_ref and s.source_type.value == "email"]
        verified = len(sources) == 1
        captured.update(sender=normalized_sender(case.sender_email), body=case.newest_authored_body,
                        authored_at=_time(case.authored_at))
        if sources:
            captured.update(actor=sources[0].actor, evidence_level=sources[0].evidence_level.value)
    digest = _hash(captured) if verified else None
    proposed = [change.model_dump(mode="json", exclude_unset=True) for change in changes]
    if not proposed or len(proposed) > 50:
        raise ActionReviewError("one to fifty proposed action changes required")
    review_id = "far_" + _hash([project_id, source_ref, digest, proposed])[:32]
    snapshot = case.project_snapshot.model_dump(mode="json") if verified else {}
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""INSERT OR IGNORE INTO followup_plan_reviews
        (review_id,project_id,source_ref,source_hash,source_json,snapshot_json,changes_json,review_reason,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", (review_id, project_id, source_ref, digest, _json(captured), _json(snapshot), _json(proposed), reason, now, now))
    row = conn.execute("SELECT review_id,status,revision FROM followup_plan_reviews WHERE review_id=?", (review_id,)).fetchone()
    return dict(row)


def list_action_reviews(conn, project_id, *, status="pending", limit=50):
    if status not in {"pending", "approved", "rejected"}:
        raise ActionReviewError("invalid review status")
    rows = conn.execute("SELECT * FROM followup_plan_reviews WHERE project_id=? AND status=? ORDER BY created_at,review_id LIMIT ?",
                        (project_id, status, min(max(limit, 1), 100))).fetchall()
    result = []
    for row in rows:
        item = _review_dict(row)
        try:
            current = _source(conn, project_id, row["source_ref"])
            newer = find_newer_substantive_email(conn, project_id=project_id, source_email_id=row["source_ref"])
            item["source_superseded_by"] = newer["email_id"] if newer else None
            item["source_current"] = bool(row["source_hash"] and _hash(current) == row["source_hash"] and not newer)
            item["source_authorized"] = current["evidence_level"] != EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT.value
        except ActionReviewError:
            item["source_current"] = item["source_authorized"] = False
            item["source_superseded_by"] = None
        result.append(item)
    return result


def _review_dict(row):
    data = dict(row)
    for field in ("source_json", "snapshot_json", "changes_json"):
        data[field.removesuffix("_json")] = json.loads(data.pop(field))
    return data


def decide_action_review(conn, *, project_id, review_id, actor_id, expected_revision,
                         decision_id, decision, reason, bundle: ActionBundle | None = None, trusted_operator=False):
    """Caller owns commit. Failed decisions roll back both action and review writes."""
    if not trusted_operator:
        raise ActionReviewError("explicit operator review authorization required")
    if not actor_id.strip() or not decision_id.strip() or not reason.strip() or decision not in {"approved", "rejected"}:
        raise ActionReviewError("explicit decision identity, actor and reason required")
    if (decision == "approved") != (bundle is not None):
        raise ActionReviewError("approval requires an operator-reviewed bundle; rejection must not contain one")
    request_hash = _hash([project_id, review_id, actor_id, expected_revision, decision, reason,
                          bundle.identity_json() if bundle else None])
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    savepoint = "action_review_" + uuid4().hex
    conn.execute("SAVEPOINT " + savepoint)
    try:
        row = conn.execute("SELECT * FROM followup_plan_reviews WHERE review_id=? AND project_id=?", (review_id, project_id)).fetchone()
        if not row:
            raise KeyError(review_id)
        prior = conn.execute("SELECT request_hash,result_json FROM followup_plan_review_decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise ActionReviewError("review decision identity collision")
            result = {**json.loads(prior[1]), "outcome": "duplicate"}
            if row["status"] != result["status"] or row["revision"] != result["revision"]:
                raise ActionReviewError("review decision receipt conflicts with persisted review state")
            if result["action_result"]:
                result["action_result"] = {**result["action_result"], "outcome": "duplicate",
                                           "current_plan": plan_snapshot(conn, project_id)}
        else:
            if row["status"] != "pending" or row["revision"] != expected_revision:
                raise ActionReviewError("stale or resolved action review")
            applied = None
            if bundle:
                current = _source(conn, project_id, row["source_ref"])
                if not row["source_hash"] or _hash(current) != row["source_hash"]:
                    raise ActionReviewError("source changed or unavailable; reassess before approving")
                newer = find_newer_substantive_email(conn, project_id=project_id, source_email_id=row["source_ref"])
                if newer:
                    raise ActionReviewError(f"source was superseded by newer email {newer['email_id']}; reassess before approving")
                if current["evidence_level"] == EvidenceLevel.L5_UNVERIFIED_OR_INDIRECT.value:
                    raise ActionReviewError("confirm source directory authority before approving")
                applied = apply_action_bundle(conn, project_id=project_id, bundle=bundle, actor_id=actor_id, trusted_operator=True)
            now = datetime.now(timezone.utc).isoformat()
            changed = conn.execute("UPDATE followup_plan_reviews SET status=?,revision=revision+1,updated_at=? WHERE review_id=? AND revision=? AND status='pending'",
                (decision, now, review_id, expected_revision))
            if changed.rowcount != 1:
                raise ActionReviewError("concurrent review decision")
            result = {"outcome": "applied", "review_id": review_id, "status": decision, "revision": expected_revision + 1,
                      "decision_id": decision_id, "action_result": applied}
            conn.execute("INSERT INTO followup_plan_review_decisions VALUES (?,?,?,?,?,?,?,?,?)",
                (decision_id, review_id, actor_id, decision, request_hash, reason, bundle.operation_id if bundle else None, _json(result), now))
        conn.execute("RELEASE SAVEPOINT " + savepoint)
        return result
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
        conn.execute("RELEASE SAVEPOINT " + savepoint)
        if started:
            conn.rollback()
        raise
