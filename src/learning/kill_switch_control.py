"""Audited, idempotent emergency learning switch control."""
from __future__ import annotations

import json

from src.db.connection import scoped_write
from src.learning.governance_flags import operational_state, set_kill_switch


def set_switch(name: str, value: bool, *, actor_id: str, reason: str,
               idempotency_key: str) -> dict:
    marker = f"learning-kill-switch:{idempotency_key}"
    with scoped_write() as conn:
        prior = conn.execute("SELECT metadata_json FROM activity_log WHERE action_type='learning_kill_switch_changed' AND object_ref=?", (marker,)).fetchone()
        if prior:
            return json.loads(prior[0])["result"]
        set_kill_switch(conn, name, value, actor=actor_id, reason=reason, object_ref=marker)
        result = {"name": name, "effective": operational_state()[name]}
        conn.execute("UPDATE activity_log SET metadata_json=? WHERE action_type='learning_kill_switch_changed' AND object_ref=?",
                     (json.dumps({"reason": reason, "result": result}, sort_keys=True), marker))
        return result
