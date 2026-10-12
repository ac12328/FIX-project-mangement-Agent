"""Source-event coordinator for recoverable v2 and legacy learning ingress."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from config import defaults

T = TypeVar("T")
_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class IngressReceipt(Generic[T]):
    event_id: str
    status: str
    v2_receipt: T | None = None
    legacy_receipt: object | None = None
    v2_error: str | None = None
    legacy_error: str | None = None


Callback = Callable[[], object | None]


def coordinate_ingress(
    conn: sqlite3.Connection, *, source_type: str, source_id: str,
    operator_id: str, project_id: str | None, v2_callback: Callback,
    legacy_callback: Callback | None, candidate_only: bool = False,
    dry_run: bool = False, _after_side: Callable[[str], None] | None = None,
) -> IngressReceipt:
    """Claim an event before effects and retry only incomplete sides.

    Each callback must use ``event_id``/source-derived idempotency internally.  The
    coordinator commits the claim before callbacks and checkpoints each successful
    side with its side effect, so a crash before final status recomputation remains
    recoverable without repeating the completed side.
    """
    event_id = "lie_" + hashlib.sha256(f"{source_type}\0{source_id}".encode()).hexdigest()[:24]
    if dry_run:
        return IngressReceipt(event_id=event_id, status="dry_run")
    run_legacy = bool(defaults.LEARNING_DUAL_WRITE and legacy_callback and not candidate_only)
    row = _claim(conn, event_id, source_type, source_id, operator_id, project_id, run_legacy)

    for side, callback in (("v2", v2_callback), ("legacy", legacy_callback)):
        if side == "legacy" and not run_legacy:
            continue
        if callback is None or not _retryable(row, side):
            continue
        try:
            receipt = callback()
        except Exception as exc:
            _checkpoint(conn, event_id, side, "failed", None, _safe_error(exc))
        else:
            _checkpoint(conn, event_id, side, "succeeded", receipt, None)
        row = conn.execute(
            "SELECT * FROM learning_ingress_receipts WHERE event_id=?", (event_id,),
        ).fetchone()
        if _after_side is not None:
            _after_side(side)

    _finalize(conn, event_id)
    return _from_row(conn.execute(
        "SELECT * FROM learning_ingress_receipts WHERE event_id=?", (event_id,),
    ).fetchone())


def _claim(conn, event_id, source_type, source_id, operator_id, project_id, run_legacy):
    conn.execute(
        """INSERT OR IGNORE INTO learning_ingress_receipts
           (event_id,source_type,source_id,operator_id,project_id,dual_write_requested,
            v2_status,legacy_status,status,v2_attempts,legacy_attempts,max_attempts,
            updated_at,created_at)
           VALUES (?,?,?,?,?,?,? ,?,?,0,0,?,datetime('now'),datetime('now'))""",
        (event_id, source_type, source_id, operator_id, project_id, int(run_legacy),
         "pending", "pending" if run_legacy else "not_requested", "pending", _MAX_ATTEMPTS),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM learning_ingress_receipts WHERE event_id=?", (event_id,),
    ).fetchone()


def _retryable(row, side):
    status = row[f"{side}_status"]
    return status != "succeeded" and status != "not_requested" and int(row[f"{side}_attempts"]) < int(row["max_attempts"])


def _checkpoint(conn, event_id, side, status, receipt, error):
    conn.execute(
        f"""UPDATE learning_ingress_receipts
            SET {side}_status=?,{side}_receipt_json=?,{side}_error=?,
                {side}_attempts={side}_attempts+1,updated_at=datetime('now')
            WHERE event_id=?""",
        (status, _json(receipt), error, event_id),
    )
    conn.commit()


def _finalize(conn, event_id):
    row = conn.execute(
        "SELECT * FROM learning_ingress_receipts WHERE event_id=?", (event_id,),
    ).fetchone()
    statuses = [row["v2_status"]]
    if row["legacy_status"] != "not_requested":
        statuses.append(row["legacy_status"])
    exhausted = any(
        row[f"{side}_status"] == "failed"
        and int(row[f"{side}_attempts"]) >= int(row["max_attempts"])
        for side in ("v2", "legacy") if row[f"{side}_status"] != "not_requested"
    )
    if all(value == "succeeded" for value in statuses):
        status = "succeeded"
    elif exhausted:
        status = "terminal_failure" if all(value == "failed" for value in statuses) else "partial_terminal_failure"
    elif "succeeded" in statuses:
        status = "partial_failure"
    elif "failed" in statuses:
        status = "failed"
    else:
        status = "pending"
    conn.execute(
        "UPDATE learning_ingress_receipts SET status=?,updated_at=datetime('now') WHERE event_id=?",
        (status, event_id),
    )
    conn.commit()


def _json(value):
    return json.dumps(value, default=str, sort_keys=True) if value is not None else None


def _safe_error(exc: Exception) -> str:
    return type(exc).__name__[:120]


def _from_row(row) -> IngressReceipt:
    return IngressReceipt(
        event_id=row["event_id"], status=row["status"],
        v2_receipt=json.loads(row["v2_receipt_json"]) if row["v2_receipt_json"] else None,
        legacy_receipt=json.loads(row["legacy_receipt_json"]) if row["legacy_receipt_json"] else None,
        v2_error=row["v2_error"], legacy_error=row["legacy_error"],
    )
