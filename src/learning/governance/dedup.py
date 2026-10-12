"""Conservative deterministic equivalence for governed memory candidates."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_VALUE_ALIASES = {"brief": "concise", "short": "concise", "permitted": "allow", "allowed": "allow", "prohibited": "deny", "forbidden": "deny", "blocked": "deny"}
_SET_PREDICATES = {"notification_channels"}
_PREDICATE_ALIASES = {"external sharing": "external_sharing", "external-sharing": "external_sharing", "External-Sharing": "external_sharing"}
_TYPED_OPPOSITES = {"external_sharing": ({"allow", "deny"}, {"enabled", "disabled"}), "auto_send": ({"allow", "deny"}, {"enabled", "disabled"})}


def canonical_predicate(predicate: str) -> str:
    return _PREDICATE_ALIASES.get(predicate, predicate)


def identity_payload(*, operator_id: str, scope_type: str, scope_id: str | None, subject_type: str, subject_id: str | None, predicate: str) -> dict[str, str | None]:
    """Return lossless identity fields; never normalize identifiers."""
    return {"operator_id": operator_id, "scope_type": scope_type, "scope_id": scope_id, "subject_type": subject_type, "subject_id": subject_id, "predicate": predicate}


def canonical_key(**identity: str | None) -> str:
    return _hash(identity_payload(**identity))


def normalize_value(value: Any, *, predicate: str | None = None) -> Any:
    if isinstance(value, str):
        tokens = re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE)
        return "_".join(_VALUE_ALIASES.get(token, token) for token in tokens)
    if isinstance(value, bool):
        return value
    if isinstance(value, list):
        items = [normalize_value(item, predicate=predicate) for item in value]
        return sorted(items, key=canonical_json) if predicate in _SET_PREDICATES else items
    if isinstance(value, dict):
        return {str(key): normalize_value(item, predicate=predicate) for key, item in sorted(value.items())}
    return value


def semantic_fingerprint(value: Any, *, predicate: str | None = None) -> str:
    return _hash(normalize_value(value, predicate=predicate))


def claims_equivalent(predicate: str, left: Any, right: Any) -> bool:
    return normalize_value(left, predicate=predicate) == normalize_value(right, predicate=predicate)


def claims_opposed(predicate: str, left: Any, right: Any) -> bool:
    predicate = canonical_predicate(predicate)
    if predicate not in _TYPED_OPPOSITES:
        return False
    left_condition, left_value = _condition_value(left)
    right_condition, right_value = _condition_value(right)
    if left_condition != right_condition:
        return False
    if isinstance(left_value, bool) and isinstance(right_value, bool):
        return left_value is not right_value
    normalized = {normalize_value(left_value), normalize_value(right_value)}
    if any(normalized == pair for pair in _TYPED_OPPOSITES[predicate]):
        return True
    left_norm, right_norm = normalize_value(left_value), normalize_value(right_value)
    return isinstance(left_norm, str) and isinstance(right_norm, str) and (left_norm == f"not_{right_norm}" or right_norm == f"not_{left_norm}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _condition_value(value: Any) -> tuple[Any, Any]:
    if isinstance(value, dict):
        polar = [field for field in ("value", "action") if field in value]
        if len(polar) != 1:
            return {"ambiguous": normalize_value(value)}, None
        field = polar[0]
        applicability = {key: item for key, item in value.items() if key != field}
        return normalize_value(applicability), value[field]
    return None, value


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
