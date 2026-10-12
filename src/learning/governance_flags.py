"""Runtime controls for the incremental Learning Governance v2 rollout."""
from __future__ import annotations

import json
import sqlite3
from typing import Final

from config import defaults
from src.repositories.activity_log_repo import insert_activity

_KILL_SWITCHES: Final[dict[str, str]] = {
    "learning_writes_disabled": "LEARNING_WRITES_DISABLED",
    "worker_disabled": "LEARNING_WORKER_DISABLED",
    "retrieval_disabled": "MEMORY_RETRIEVAL_DISABLED",
    "global_memory_disabled": "GLOBAL_MEMORY_DISABLED",
    "auto_activation_disabled": "AUTO_ACTIVATION_DISABLED",
    "wiki_publication_disabled": "WIKI_PUBLICATION_DISABLED",
    "feedback_reinforcement_disabled": "FEEDBACK_REINFORCEMENT_DISABLED",
}


def _name(name: str) -> str:
    try:
        return _KILL_SWITCHES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown learning kill switch: {name}") from exc


def enabled() -> bool:
    return bool(defaults.LEARNING_V2_ENABLED)


def capture_enabled() -> bool:
    return bool(defaults.LEARNING_CAPTURE_ENABLED)


def outbox_v2_enabled() -> bool:
    return bool(defaults.LEARNING_OUTBOX_V2_ENABLED)


def legacy_direct_enabled() -> bool:
    return bool(defaults.LEARNING_LEGACY_DIRECT_ENABLED)


def candidate_only() -> bool:
    return bool(defaults.LEARNING_CANDIDATE_ONLY)


def writes_disabled() -> bool:
    return bool(defaults.LEARNING_WRITES_DISABLED)


def worker_disabled() -> bool:
    return bool(defaults.LEARNING_WORKER_DISABLED)


def retrieval_disabled() -> bool:
    return bool(defaults.MEMORY_RETRIEVAL_DISABLED)


def global_memory_disabled() -> bool:
    return bool(defaults.GLOBAL_MEMORY_DISABLED)


def auto_activation_disabled() -> bool:
    return bool(defaults.AUTO_ACTIVATION_DISABLED)


def wiki_publication_disabled() -> bool:
    return bool(defaults.WIKI_PUBLICATION_DISABLED)


def operational_state() -> dict[str, bool]:
    """Return the complete visible state for operations and future API exposure."""
    return {
        "learning_v2_enabled": enabled(),
        "capture_enabled": bool(defaults.LEARNING_CAPTURE_ENABLED),
        "outbox_v2_enabled": bool(defaults.LEARNING_OUTBOX_V2_ENABLED),
        "candidate_only": candidate_only(),
        "governance_enforced": bool(defaults.LEARNING_GOVERNANCE_ENFORCED),
        "learning_writes_disabled": writes_disabled(),
        "worker_disabled": worker_disabled(),
        "retrieval_disabled": bool(defaults.MEMORY_RETRIEVAL_DISABLED),
        "global_memory_disabled": global_memory_disabled(),
        "auto_activation_disabled": auto_activation_disabled(),
        "wiki_publication_disabled": wiki_publication_disabled(),
        "feedback_reinforcement_disabled": bool(defaults.FEEDBACK_REINFORCEMENT_DISABLED),
    }


def set_kill_switch(conn: sqlite3.Connection, name: str, value: bool, *, actor: str,
                    reason: str = "operator emergency control", object_ref: str | None = None) -> None:
    """Set one emergency switch and append its mandatory reason-bearing audit event."""
    attribute = _name(name)
    old_value = bool(getattr(defaults, attribute))
    new_value = bool(value)
    setattr(defaults, attribute, new_value)
    insert_activity(
        conn,
        actor=actor,
        surface="learning_governance",
        action_type="learning_kill_switch_changed",
        object_ref=object_ref or name,
        summary=f"Learning kill switch {name} set to {new_value}: {reason}",
        metadata_json=json.dumps({"flag": name, "old_value": old_value, "new_value": new_value,
                                  "reason": reason}, sort_keys=True),
    )
