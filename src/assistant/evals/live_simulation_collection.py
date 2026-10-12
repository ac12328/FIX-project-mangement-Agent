"""Controlled multi-turn Workspace simulation evidence collection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from config.settings import DB_PATH
from src.assistant.evals.artifacts import read_model, stamp_content_hash, verify_content_hash
from src.assistant.evals.grounding_repair import (
    GroundingRepairSelectionItem,
    GroundingRepairSelectionManifest,
)
from src.assistant.evals.simulation_models import (
    COLLECTION_CLASS,
    ControlledLiveSimulationQuestionSet,
    SimulationConversation,
)
from src.auth.repository import bootstrap_primary_admin
from src.auth.sessions import create_session, revoke_session
from src.db.connection import scoped_write
from src.observability.payload_crypto import filter_secrets

_SESSION_COOKIE = "agentfixb_session"
_CSRF_COOKIE = "agentfixb_csrf"
_CSRF_HEADER = "x-csrf-token"
_SAFE_SHADOW_FIELDS = (
    "policy_version",
    "decision",
    "reason",
    "baseline_repair_attempted",
    "would_skip_repair",
    "initial_error_codes",
    "candidate_error_codes",
    "shadow_compute_ms",
    "candidate_ready_ms",
    "baseline_ready_ms",
    "projected_completion_savings_ms",
    "served_answer_source",
    "shadow_additional_llm_calls",
    "shadow_synchronous_db_writes",
    "user_visible_behavior_changed",
    "business_persistence_changed",
    "prepared_answer_hash",
    "candidate_answer_hash",
    "baseline_answer_hash",
)


@dataclass(frozen=True)
class SessionMaterial:
    session_id: str
    user_id: str
    token: str
    csrf: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _answer_hash(value: Any) -> str:
    encoded = json.dumps(
        filter_secrets(value), ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _create_session() -> SessionMaterial:
    with scoped_write() as conn:
        user = bootstrap_primary_admin(conn)
        material = create_session(
            conn,
            user_id=str(user["user_id"]),
            auth_method="local",
            ip_address="127.0.0.1",
            user_agent="agentfixbpro-controlled-live-simulation/1",
        )
    return SessionMaterial(
        session_id=material.session_id,
        user_id=str(user["user_id"]),
        token=material.session_token,
        csrf=material.csrf_token,
    )


def _http_session(material: SessionMaterial) -> tuple[requests.Session, dict[str, str]]:
    session = requests.Session()
    session.cookies.set(_SESSION_COOKIE, material.token)
    session.cookies.set(_CSRF_COOKIE, material.csrf)
    return session, {
        _CSRF_HEADER: material.csrf,
        "User-Agent": "agentfixbpro-controlled-live-simulation/1",
    }


def _revoke_session(
    material: SessionMaterial,
    session: requests.Session,
    headers: dict[str, str],
    base_url: str,
) -> bool:
    try:
        response = session.post(base_url + "/api/auth/logout", headers=headers, timeout=(10, 30))
        if response.status_code == 200:
            return True
    except requests.RequestException:
        pass
    try:
        with scoped_write() as conn:
            return revoke_session(
                conn,
                material.session_id,
                actor_user_id=material.user_id,
                reason="controlled_live_simulation_cleanup",
            )
    except Exception:
        return False


def _iter_sse(response: requests.Response):
    event_name = "message"
    data_lines: list[str] = []
    for raw in response.iter_lines(chunk_size=1, decode_unicode=True):
        line = raw or ""
        if line == "":
            if data_lines:
                raw_payload = "\n".join(data_lines)
                try:
                    payload: Any = json.loads(raw_payload)
                except json.JSONDecodeError:
                    payload = raw_payload
                yield event_name, payload
            event_name = "message"
            data_lines = []
        elif line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())


def _shadow_row(database: Path, turn_id: str) -> dict[str, Any] | None:
    with _connect_readonly(database) as conn:
        row = conn.execute(
            """SELECT event_id, run_id, trace_id, turn_id, status, duration_ms,
                      created_at, metadata_json, payload_ciphertext, payload_nonce,
                      payload_key_version, payload_hash
               FROM trace_events
               WHERE event_type='grounding.repair.shadow' AND turn_id=?
               ORDER BY created_at DESC, sequence DESC LIMIT 1""",
            (turn_id,),
        ).fetchone()
    if row is None:
        return None
    metadata = json.loads(str(row["metadata_json"] or "{}"))
    safe = {field: metadata.get(field) for field in _SAFE_SHADOW_FIELDS}
    safe.update({
        "event_id": str(row["event_id"]),
        "run_id": str(row["run_id"]),
        "trace_id": str(row["trace_id"] or "") or None,
        "turn_id": str(row["turn_id"] or "") or None,
        "event_status": str(row["status"] or ""),
        "event_duration_ms": float(row["duration_ms"] or 0.0),
        "created_at": str(row["created_at"]),
        "encrypted_complete": all(
            row[field]
            for field in ("payload_ciphertext", "payload_nonce", "payload_key_version", "payload_hash")
        ),
    })
    return safe


def _wait_for_shadow(database: Path, turn_id: str, timeout_seconds: float) -> dict[str, Any] | None:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        shadow = _shadow_row(database, turn_id)
        if shadow is not None:
            return shadow
        time.sleep(0.25)
    return None


def _archive_thread(
    session: requests.Session,
    headers: dict[str, str],
    base_url: str,
    thread_id: str,
) -> bool:
    response = session.patch(
        base_url + f"/api/assistant/threads/{thread_id}",
        headers=headers,
        json={"archived": True},
        timeout=(10, 30),
    )
    return response.status_code == 200 and bool(response.json().get("archived_at"))


def _run_turn(
    *,
    conversation: SimulationConversation,
    question_turn: Any,
    attempt_id: str,
    thread_id: str,
    database: Path,
    base_url: str,
    session: requests.Session,
    headers: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    event_counts: Counter[str] = Counter()
    response_parts: list[str] = []
    response_chars = 0
    live_turn_id: str | None = None
    http_status: int | None = None
    error_type: str | None = None
    error_hash: str | None = None
    shadow: dict[str, Any] | None = None
    try:
        with session.post(
            base_url + f"/api/assistant/threads/{thread_id}/turns",
            headers={**headers, "Accept": "text/event-stream"},
            json={
                "client_turn_id": f"controlled-simulation-{uuid.uuid4().hex}",
                "message": question_turn.question,
                "intent": {"type": "aggregate_query"},
            },
            stream=True,
            timeout=(30, timeout_seconds),
        ) as response:
            http_status = response.status_code
            response.raise_for_status()
            for event, payload in _iter_sse(response):
                event_counts[event] += 1
                if isinstance(payload, dict) and payload.get("turn_id"):
                    live_turn_id = str(payload["turn_id"])
                if event == "text.delta" and isinstance(payload, dict):
                    delta = str(payload.get("delta") or "")
                    response_chars += len(delta)
                    response_parts.append(delta)
                elif event == "turn.failed":
                    error_type = "turn_failed"
                    value = str(payload.get("error") or "") if isinstance(payload, dict) else str(payload)
                    error_hash = _sha256_text(value)
        if live_turn_id:
            shadow = _wait_for_shadow(database, live_turn_id, min(60.0, timeout_seconds))
    except Exception as exc:
        error_type = type(exc).__name__
        error_hash = _sha256_text(str(exc))
    response_digest = _answer_hash("".join(response_parts)) if response_chars else None
    invariant_checks = {
        "exactly_one_turn_completed": event_counts.get("turn.completed") == 1,
        "encrypted_shadow_payload_complete": bool(shadow and shadow.get("encrypted_complete")),
        "legacy_answer_hash_matches": bool(
            shadow and response_digest and shadow.get("baseline_answer_hash") == response_digest
        ),
        "served_answer_source_is_legacy": bool(
            shadow and shadow.get("served_answer_source") == "legacy_baseline"
        ),
        "user_visible_behavior_unchanged": bool(
            shadow and shadow.get("user_visible_behavior_changed") is False
        ),
        "business_persistence_unchanged": bool(
            shadow and shadow.get("business_persistence_changed") is False
        ),
    }
    status = "completed" if shadow and all(invariant_checks.values()) else "failed"
    if status == "failed" and error_type is None:
        error_type = "evidence_invariant_mismatch"
    return {
        "case_id": question_turn.turn_id,
        "conversation_id": conversation.conversation_id,
        "question_turn_id": question_turn.turn_id,
        "question_turn_index": question_turn.turn_index,
        "question_hash": question_turn.question_hash,
        "request_hash": _sha256_text(question_turn.question),
        "attempt_id": attempt_id,
        "thread_id": thread_id,
        "turn_id": live_turn_id,
        "status": status,
        "http_status": http_status,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "event_counts": dict(sorted(event_counts.items())),
        "response_chars": response_chars,
        "response_hash": "sha256:" + response_digest if response_digest else None,
        "invariant_checks": invariant_checks,
        "response_plaintext_recorded_in_artifact": False,
        "request_plaintext_recorded_in_artifact": False,
        "error_type": error_type,
        "error_hash": error_hash,
        "shadow": shadow,
    }


def _run_attempt(
    conversation: SimulationConversation,
    *,
    attempt_number: int,
    parent_attempt_id: str | None,
    database: Path,
    base_url: str,
    session: requests.Session,
    headers: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    attempt_id = f"attempt-{uuid.uuid4().hex}"
    thread_id: str | None = None
    archived = False
    turns: list[dict[str, Any]] = []
    error_type: str | None = None
    error_hash: str | None = None
    try:
        response = session.post(
            base_url + "/api/assistant/threads",
            headers=headers,
            json={
                "title": f"[controlled-simulation] {conversation.conversation_id}",
                "scope_mode": conversation.turns[0].scope_mode,
                "project_ids": list(conversation.turns[0].project_ids),
                "web_enabled": False,
            },
            timeout=(10, 60),
        )
        response.raise_for_status()
        thread_id = str(response.json()["thread_id"])
        for question_turn in conversation.turns:
            result = _run_turn(
                conversation=conversation,
                question_turn=question_turn,
                attempt_id=attempt_id,
                thread_id=thread_id,
                database=database,
                base_url=base_url,
                session=session,
                headers=headers,
                timeout_seconds=timeout_seconds,
            )
            turns.append(result)
            if result["status"] != "completed":
                break
    except Exception as exc:
        error_type = type(exc).__name__
        error_hash = _sha256_text(str(exc))
    finally:
        if thread_id:
            try:
                archived = _archive_thread(session, headers, base_url, thread_id)
            except Exception as exc:
                error_type = error_type or type(exc).__name__
                error_hash = error_hash or _sha256_text(str(exc))
    for turn in turns:
        turn["thread_archived"] = archived
    all_turns_completed = (
        len(turns) == len(conversation.turns)
        and all(turn["status"] == "completed" for turn in turns)
    )
    status = "completed" if all_turns_completed and archived else "failed"
    if status == "failed" and error_type is None:
        error_type = "conversation_invariant_mismatch"
    return {
        "conversation_id": conversation.conversation_id,
        "attempt_id": attempt_id,
        "parent_attempt_id": parent_attempt_id,
        "attempt_number": attempt_number,
        "thread_id": thread_id,
        "thread_archived": archived,
        "status": status,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "expected_turn_count": len(conversation.turns),
        "completed_turn_count": sum(turn["status"] == "completed" for turn in turns),
        "error_type": error_type,
        "error_hash": error_hash,
        "turns": turns,
    }


def _run_conversation(
    conversation: SimulationConversation,
    *,
    max_attempts: int,
    database: Path,
    base_url: str,
    session: requests.Session,
    headers: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    parent_attempt_id: str | None = None
    for attempt_number in range(1, max_attempts + 1):
        attempt = _run_attempt(
            conversation,
            attempt_number=attempt_number,
            parent_attempt_id=parent_attempt_id,
            database=database,
            base_url=base_url,
            session=session,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
        attempts.append(attempt)
        if attempt["status"] == "completed":
            break
        parent_attempt_id = str(attempt["attempt_id"])
    successful = next((item for item in reversed(attempts) if item["status"] == "completed"), None)
    return {
        "conversation_id": conversation.conversation_id,
        "status": "completed" if successful else "failed",
        "selected_attempt_id": successful["attempt_id"] if successful else None,
        "attempts": attempts,
        "selected_turns": successful["turns"] if successful else [],
    }


def _run_worker(
    conversations: list[SimulationConversation],
    *,
    max_attempts: int,
    database: Path,
    base_url: str,
    timeout_seconds: float,
) -> tuple[list[dict[str, Any]], bool]:
    material = _create_session()
    session, headers = _http_session(material)
    results: list[dict[str, Any]] = []
    try:
        for conversation in conversations:
            results.append(_run_conversation(
                conversation,
                max_attempts=max_attempts,
                database=database,
                base_url=base_url,
                session=session,
                headers=headers,
                timeout_seconds=timeout_seconds,
            ))
    finally:
        revoked = _revoke_session(material, session, headers, base_url)
        session.close()
    return results, {
        "session_id": material.session_id,
        "revoked": revoked,
    }


def select_phase_conversations(
    question_set: ControlledLiveSimulationQuestionSet,
    phase: str,
) -> list[SimulationConversation]:
    if phase == "full":
        return list(question_set.conversations)
    if phase != "canary":
        raise ValueError("phase must be canary or full")
    single = next((item for item in question_set.conversations if len(item.turns) == 1), None)
    multi = next((item for item in question_set.conversations if len(item.turns) == 3), None)
    if single is None or multi is None:
        raise ValueError("canary requires one single-turn and one three-turn conversation")
    return [single, multi]


def _preflight(base_url: str) -> dict[str, Any]:
    response = requests.get(base_url + "/api/health", timeout=(10, 30))
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not data.get("db_ok"):
        raise RuntimeError("live API database health is not ready for controlled simulation")
    return {
        "status": str(data.get("status") or "unknown"),
        "db_ok": bool(data.get("db_ok")),
        "passive_llm_ok": bool(data.get("llm_ok")),
        "schema_version": data.get("schema_version"),
    }


def _assert_plaintext_free(artifact: dict[str, Any], questions: Iterable[str]) -> str:
    rendered = json.dumps(artifact, ensure_ascii=False, indent=2, default=str)
    for question in questions:
        if question and question in rendered:
            raise RuntimeError("question plaintext leaked into simulation evidence artifact")
    return rendered


def collect_simulation(args: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    database = args.db.resolve()
    if database != Path(DB_PATH).resolve():
        raise RuntimeError("live collector database must match the configured production database")
    if not database.is_file():
        raise FileNotFoundError(database)
    question_set = read_model(args.question_set.resolve(), ControlledLiveSimulationQuestionSet)
    if not verify_content_hash(question_set):
        raise ValueError("simulation question set content_hash is missing or invalid")
    selected = select_phase_conversations(question_set, args.phase)
    preflight = _preflight(args.base_url)
    started_at = _utcnow()
    groups = [selected[index::args.concurrency] for index in range(args.concurrency)]
    groups = [group for group in groups if group]
    conversations: list[dict[str, Any]] = []
    session_audits: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(groups), thread_name_prefix="controlled-simulation") as pool:
        futures = [
            pool.submit(
                _run_worker,
                group,
                max_attempts=args.max_conversation_attempts,
                database=database,
                base_url=args.base_url,
                timeout_seconds=args.timeout_seconds,
            )
            for group in groups
        ]
        for future in as_completed(futures):
            worker_results, session_audit = future.result()
            conversations.extend(worker_results)
            if isinstance(session_audit, dict):
                session_audits.append({
                    "session_id": str(session_audit.get("session_id") or ""),
                    "revoked": bool(session_audit.get("revoked")),
                })
            else:
                # Backward-compatible test seam for older injected workers.
                session_audits.append({"session_id": "", "revoked": bool(session_audit)})
    order = {item.conversation_id: index for index, item in enumerate(selected)}
    conversations.sort(key=lambda item: order[str(item["conversation_id"])])
    cases = [turn for item in conversations for turn in item["selected_turns"]]
    completed_at = _utcnow()
    all_attempts = [attempt for item in conversations for attempt in item["attempts"]]
    artifact = {
        "schema_version": 3,
        "generated_at": completed_at.isoformat(),
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "collection_class": COLLECTION_CLASS,
        "evidence_origin": "controlled",
        "batch_id": args.batch_id,
        "phase": args.phase,
        "question_set_id": question_set.question_set_id,
        "question_set_content_hash": question_set.content_hash,
        "preflight_health": preflight,
        "concurrency": len(groups),
        "max_conversation_attempts": args.max_conversation_attempts,
        "conversation_count": len(selected),
        "query_count": sum(len(item.turns) for item in selected),
        "completed_count": len(cases),
        "failed_count": sum(item["status"] != "completed" for item in conversations),
        "shadow_event_count": sum(bool(item.get("shadow")) for item in cases),
        "encrypted_shadow_event_count": sum(
            bool(item.get("shadow", {}).get("encrypted_complete")) for item in cases
        ),
        "observed_repair_case_count": sum(
            bool(item.get("shadow", {}).get("baseline_repair_attempted")) for item in cases
        ),
        "observed_control_case_count": sum(
            not bool(item.get("shadow", {}).get("baseline_repair_attempted")) for item in cases
        ),
        "would_skip_repair_count": sum(
            bool(item.get("shadow", {}).get("would_skip_repair")) for item in cases
        ),
        "threads_created_count": len(all_attempts),
        "threads_archived_count": sum(bool(item.get("thread_archived")) for item in all_attempts),
        "temporary_auth_sessions_created": len(groups),
        "temporary_auth_sessions_revoked": sum(
            bool(item.get("revoked")) for item in session_audits
        ),
        "temporary_auth_sessions": session_audits,
        "request_plaintext_recorded_in_artifact": False,
        "response_plaintext_recorded_in_artifact": False,
        "conversations": conversations,
        "cases": cases,
    }
    selection_model = stamp_content_hash(GroundingRepairSelectionManifest(
        generated_at=completed_at,
        collection_class=COLLECTION_CLASS,
        evidence_origin="controlled",
        batch_id=args.batch_id,
        phase=args.phase,
        question_set_id=question_set.question_set_id,
        question_set_content_hash=question_set.content_hash,
        expected_turn_count=sum(len(item.turns) for item in selected),
        selected_turn_count=len(cases),
        complete=len(cases) == sum(len(item.turns) for item in selected),
        selected=[
            GroundingRepairSelectionItem(
                case_id=item["case_id"],
                conversation_id=item["conversation_id"],
                question_turn_id=item["question_turn_id"],
                question_turn_index=item["question_turn_index"],
                question_hash=item["question_hash"],
                request_hash=item["request_hash"],
                attempt_id=item["attempt_id"],
                thread_id=item["thread_id"],
                turn_id=item["turn_id"],
                run_id=item["shadow"]["run_id"],
                trace_id=item["shadow"].get("trace_id"),
                event_id=item["shadow"]["event_id"],
            )
            for item in cases
        ],
    ))
    selection = selection_model.model_dump(mode="json")
    questions = [turn.question for item in selected for turn in item.turns]
    _assert_plaintext_free(artifact, questions)
    _assert_plaintext_free(selection, questions)
    return artifact, selection
