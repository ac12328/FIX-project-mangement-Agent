"""Clone-safe live holdout collector for Policy-v4 research screening."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

from src.assistant.evals.artifacts import read_model, write_model
from src.assistant.evals.grounding_repair_v4_protocol import (
    EncryptedBlob,
    EncryptedQuestionSet,
    HoldoutCase,
    HoldoutCollection,
    Preregistration,
    QuestionManifest,
    SelectionManifest,
    normalized_request_hash,
    sha256_text,
    stamp_v4,
    verify_v4,
)
from src.assistant.evals.grounding_repair_v4_questions import database_fingerprint
from src.assistant.evals.grounding_repair_v4_trace_lifecycle import trace_lifecycle
from src.assistant.evals.live_simulation_collection import _archive_thread, _http_session, _iter_sse, _preflight
from src.auth.repository import bootstrap_primary_admin
from src.auth.sessions import create_session, revoke_session
from src.observability.payload_crypto import decrypt_payload, encrypt_payload, filter_secrets

_NON_BUSINESS_TABLES = frozenset({
    "schema_version", "dashboard_users", "external_identities", "local_auth_credentials",
    "local_recovery_codes", "llm_traces", "trace_runs", "trace_events",
    "trace_diagnostics", "assistant_threads", "assistant_thread_projects",
    "assistant_turns", "assistant_messages",
})


def _is_business_table(name: str, sql: str) -> bool:
    lowered = name.casefold()
    return not (
        name in _NON_BUSINESS_TABLES
        or lowered.startswith(("sqlite_", "auth_"))
        or "virtual table" in str(sql or "").casefold()
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if readonly:
        conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(resolved, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _safe_hash(value: Any) -> str:
    encoded = json.dumps(
        filter_secrets(value), ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _answer_digest(value: str) -> str:
    encoded = json.dumps(filter_secrets(value), ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def business_fingerprint(database: Path) -> str:
    """Hash every non-runtime SQLite table so unexpected business writes are detected."""
    digest = hashlib.sha256()
    with _connect(database, readonly=True) as conn:
        table_rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        tables = [
            str(row["name"]) for row in table_rows
            if _is_business_table(str(row["name"]), str(row["sql"] or ""))
        ]
        for table in tables:
            digest.update((table + "\n").encode("utf-8"))
            columns = [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]
            if not columns:
                continue
            projection = ", ".join(f'"{item}"' for item in columns)
            rows = conn.execute(
                f'SELECT {projection} FROM "{table}" ORDER BY {projection}'
            ).fetchall()
            for row in rows:
                digest.update(
                    json.dumps(list(row), ensure_ascii=False, default=str).encode("utf-8")
                )
                digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def clone_production_database(*, production_database: Path, clone_database: Path) -> str:
    """Create a SQLite online-backup clone and never mutate the source database."""
    source = Path(production_database).resolve()
    destination = Path(clone_database).resolve()
    if source == destination:
        raise ValueError("research clone path must differ from production database path")
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = _connect(source, readonly=True)
    destination_conn = _connect(destination)
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    finally:
        source_conn.close()
        destination_conn.close()
    return database_fingerprint(destination)


def _create_clone_session(database: Path):
    with _connect(database) as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = bootstrap_primary_admin(conn)
        material = create_session(
            conn,
            user_id=str(user["user_id"]),
            auth_method="local",
            ip_address="127.0.0.1",
            user_agent="agentfixbpro-policy-v4-holdout/1",
        )
        conn.commit()
    from src.assistant.evals.live_simulation_collection import SessionMaterial
    return SessionMaterial(
        session_id=material.session_id,
        user_id=str(user["user_id"]),
        token=material.session_token,
        csrf=material.csrf_token,
    )


def _revoke_clone_session(database: Path, material: Any, session: requests.Session, headers: dict[str, str], base_url: str) -> bool:
    try:
        response = session.post(base_url + "/api/auth/logout", headers=headers, timeout=(10, 30))
        if response.status_code == 200:
            return True
    except requests.RequestException:
        pass
    try:
        with _connect(database) as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = revoke_session(
                conn, material.session_id, actor_user_id=material.user_id,
                reason="policy_v4_holdout_cleanup",
            )
            conn.commit()
            return bool(result)
    except Exception:
        return False


def _verify_clone_api_binding(database: Path, base_url: str) -> bool:
    """Prove the API reads the clone by authenticating a clone-only session."""
    material = _create_clone_session(database)
    session, headers = _http_session(material)
    revoked = False
    try:
        response = session.get(base_url + "/api/auth/session", headers=headers, timeout=(10, 30))
        response.raise_for_status()
        data = response.json()
        if (
            not isinstance(data, dict)
            or data.get("authenticated") is not True
            or str(data.get("session_id") or "") != material.session_id
            or str(data.get("user_id") or "") != material.user_id
        ):
            raise RuntimeError("Policy-v4 clone API authentication probe did not match clone session")
        return True
    except requests.RequestException as exc:
        raise RuntimeError("Policy-v4 API is not bound to the research clone") from exc
    finally:
        revoked = _revoke_clone_session(database, material, session, headers, base_url)
        session.close()
        if not revoked:
            raise RuntimeError("Policy-v4 clone API probe session cleanup failed")


def _shadow_payload(database: Path, turn_id: str, timeout_seconds: float) -> tuple[dict[str, Any], dict[str, Any]] | None:
    deadline = time.perf_counter() + timeout_seconds
    while time.perf_counter() < deadline:
        with _connect(database, readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM trace_events WHERE event_type='grounding.repair.shadow' "
                "AND turn_id=? ORDER BY created_at DESC, sequence DESC LIMIT 1",
                (turn_id,),
            ).fetchone()
        if row is None:
            time.sleep(0.25)
            continue
        if not all(row[key] for key in (
            "payload_ciphertext", "payload_nonce", "payload_key_version", "payload_hash"
        )):
            return None
        metadata = json.loads(row["metadata_json"] or "{}")
        payload = decrypt_payload(
            row["payload_ciphertext"], row["payload_nonce"], row["payload_key_version"]
        )
        identity = {
            "run_id": str(row["run_id"]), "turn_id": str(row["turn_id"]),
            "trace_id": str(row["trace_id"] or ""), "event_id": str(row["event_id"]),
        }
        return {"metadata": metadata, "payload": payload}, identity
    return None


def _run_attempt(
    *, database: Path, base_url: str, session: requests.Session, headers: dict[str, str],
    question_id: str, payload: dict[str, Any], timeout_seconds: float,
) -> dict[str, Any]:
    thread_id: str | None = None
    archived = False
    event_counts: Counter[str] = Counter()
    response_parts: list[str] = []
    turn_id: str | None = None
    error_code: str | None = None
    shadow: tuple[dict[str, Any], dict[str, Any]] | None = None
    try:
        response = session.post(
            base_url + "/api/assistant/threads", headers=headers,
            json={
                "title": f"[policy-v4-holdout] {question_id}",
                "scope_mode": payload["scope_mode"],
                "project_ids": payload["project_ids"],
                "web_enabled": False,
            }, timeout=(10, 60),
        )
        response.raise_for_status()
        thread_id = str(response.json()["thread_id"])
        with session.post(
            base_url + f"/api/assistant/threads/{thread_id}/turns",
            headers={**headers, "Accept": "text/event-stream"},
            json={
                "client_turn_id": f"policy-v4-{uuid.uuid4().hex}",
                "message": payload["question"],
                "intent": {"type": "aggregate_query"},
            }, stream=True, timeout=(30, timeout_seconds),
        ) as turn_response:
            turn_response.raise_for_status()
            for event, event_payload in _iter_sse(turn_response):
                event_counts[event] += 1
                if isinstance(event_payload, dict) and event_payload.get("turn_id"):
                    turn_id = str(event_payload["turn_id"])
                if event == "text.delta" and isinstance(event_payload, dict):
                    response_parts.append(str(event_payload.get("delta") or ""))
                if event == "turn.failed":
                    error_code = "turn_failed"
        if turn_id:
            shadow = _shadow_payload(database, turn_id, min(60.0, timeout_seconds))
    except Exception as exc:
        error_code = type(exc).__name__
    finally:
        if thread_id:
            try:
                archived = _archive_thread(session, headers, base_url, thread_id)
            except Exception:
                archived = False
    thread_created_count = int(thread_id is not None)
    thread_archived_count = int(thread_id is not None and archived)
    if shadow is None:
        return {
            "complete": False, "archived": archived,
            "thread_created_count": thread_created_count,
            "thread_archived_count": thread_archived_count,
            "error_code": error_code or "shadow_unavailable",
        }
    shadow_data, identity = shadow
    metadata = shadow_data["metadata"]
    lifecycle = trace_lifecycle(
        database, run_id=identity["run_id"],
        timeout_seconds=min(60.0, timeout_seconds),
    )
    response_digest = _answer_digest("".join(response_parts)) if response_parts else None
    invariants = {
        "turn_completed": event_counts.get("turn.completed") == 1,
        "trace_lifecycle_complete": lifecycle["complete"],
        "legacy_answer_hash_matches": bool(
            response_digest and metadata.get("baseline_answer_hash") == response_digest
        ),
        "served_answer_legacy": metadata.get("served_answer_source") == "legacy_baseline",
        "user_visible_unchanged": metadata.get("user_visible_behavior_changed") is False,
        "business_persistence_unchanged": metadata.get("business_persistence_changed") is False,
        "thread_archived": archived,
    }
    complete = all(invariants.values())
    return {
        "complete": complete,
        "archived": archived,
        "thread_created_count": thread_created_count,
        "thread_archived_count": thread_archived_count,
        "error_code": None if complete else "holdout_invariant_failed",
        "repair_triggered": bool(metadata.get("baseline_repair_attempted")),
        "served_answer_legacy": invariants["served_answer_legacy"],
        "user_visible_behavior_unchanged": invariants["user_visible_unchanged"],
        "business_persistence_unchanged": invariants["business_persistence_unchanged"],
        "shadow_payload": {
            **shadow_data["payload"],
            "trace_lifecycle": lifecycle,
        },
        "identity": identity,
    }


def _collect_one(
    *, database: Path, base_url: str, session: requests.Session, headers: dict[str, str],
    entry: Any, index: Any, timeout_seconds: float, max_attempts: int,
) -> HoldoutCase:
    blob = entry.payload
    payload = decrypt_payload(blob.ciphertext, blob.nonce, blob.key_version)
    if normalized_request_hash(payload["question"]) != index.request_hash:
        raise ValueError("decrypted question does not match sealed request hash")
    last: dict[str, Any] = {"complete": False, "archived": False, "error_code": "not_started"}
    thread_created_count = 0
    thread_archived_count = 0
    for attempt in range(1, max_attempts + 1):
        last = _run_attempt(
            database=database, base_url=base_url, session=session, headers=headers,
            question_id=index.question_id, payload=payload, timeout_seconds=timeout_seconds,
        )
        thread_created_count += int(last.get("thread_created_count") or 0)
        thread_archived_count += int(last.get("thread_archived_count") or 0)
        if last["complete"] and thread_created_count == thread_archived_count:
            shadow_payload = last.pop("shadow_payload")
            identity = last.pop("identity")
            case_blob_data = encrypt_payload({
                "question": payload["question"],
                "question_hash": index.question_hash,
                "request_hash": index.request_hash,
                **shadow_payload,
            })
            case_blob = EncryptedBlob.model_validate(case_blob_data)
            return HoldoutCase(
                sealed_order=index.order, question_id=index.question_id,
                question_hash=index.question_hash, request_hash=index.request_hash,
                attempt_count=attempt, complete=True,
                repair_triggered=bool(last["repair_triggered"]),
                encrypted_case_payload=case_blob, case_payload_hash=case_blob.payload_hash,
                run_id_hash=sha256_text(identity["run_id"]),
                turn_id_hash=sha256_text(identity["turn_id"]),
                trace_id_hash=sha256_text(identity["trace_id"]),
                event_id_hash=sha256_text(identity["event_id"]),
                served_answer_legacy=bool(last["served_answer_legacy"]),
                user_visible_behavior_unchanged=bool(last["user_visible_behavior_unchanged"]),
                business_persistence_unchanged=bool(last["business_persistence_unchanged"]),
                thread_created_count=thread_created_count,
                thread_archived_count=thread_archived_count,
                thread_archived=thread_created_count > 0 and thread_created_count == thread_archived_count,
            )
    return HoldoutCase(
        sealed_order=index.order, question_id=index.question_id,
        question_hash=index.question_hash, request_hash=index.request_hash,
        attempt_count=max_attempts, complete=False, repair_triggered=False,
        thread_created_count=thread_created_count,
        thread_archived_count=thread_archived_count,
        thread_archived=thread_created_count > 0 and thread_created_count == thread_archived_count,
        error_code=(
            "thread_cleanup_failed"
            if thread_created_count != thread_archived_count
            else str(last.get("error_code") or "failed")
        ),
    )


def _worker(
    *, database: Path, base_url: str, pairs: list[tuple[Any, Any]], timeout_seconds: float,
    max_attempts: int,
) -> tuple[list[HoldoutCase], bool]:
    material = _create_clone_session(database)
    session, headers = _http_session(material)
    results: list[HoldoutCase] = []
    revoked = False
    try:
        for entry, index in pairs:
            results.append(_collect_one(
                database=database, base_url=base_url, session=session, headers=headers,
                entry=entry, index=index, timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            ))
    finally:
        revoked = _revoke_clone_session(database, material, session, headers, base_url)
        session.close()
    return [item.model_copy(update={"temporary_session_revoked": revoked}) for item in results], revoked


def select_first_200(
    *, experiment_id: str, holdout: HoldoutCollection, generated_at: datetime | None = None
) -> SelectionManifest:
    if not verify_v4(holdout):
        raise ValueError("holdout collection hash is invalid")
    eligible = [item for item in holdout.cases if item.complete and item.repair_triggered]
    selected = eligible[:200]
    return stamp_v4(SelectionManifest(
        experiment_id=experiment_id, generated_at=generated_at or _utcnow(),
        holdout_hash=holdout.content_hash,
        status="complete" if len(eligible) >= 200 else "insufficient_sample",
        eligible_repair_case_count=len(eligible), selected_case_count=len(selected),
        selected_question_hashes=[item.question_hash for item in selected],
        selected_case_payload_hashes=[str(item.case_payload_hash) for item in selected],
    ))


def collect_holdout_v4(
    *, experiment_id: str, production_database: Path, clone_database: Path, base_url: str,
    manifest_path: Path, encrypted_questions_path: Path, preregistration_path: Path,
    output_path: Path, selection_path: Path, concurrency: int = 4,
    timeout_seconds: float = 300.0, max_attempts: int = 2,
) -> tuple[HoldoutCollection, SelectionManifest]:
    production = Path(production_database).resolve()
    database = Path(clone_database).resolve()
    if int(concurrency) < 1:
        raise ValueError("Policy-v4 holdout concurrency must be at least 1")
    if float(timeout_seconds) <= 0:
        raise ValueError("Policy-v4 holdout timeout must be greater than 0")
    if int(max_attempts) < 1:
        raise ValueError("Policy-v4 holdout max_attempts must be at least 1")
    if database == production:
        raise ValueError("Policy-v4 holdout refuses to run against the production database path")
    if not database.is_file():
        raise FileNotFoundError(database)
    manifest = read_model(Path(manifest_path), QuestionManifest)
    encrypted = read_model(Path(encrypted_questions_path), EncryptedQuestionSet)
    prereg = read_model(Path(preregistration_path), Preregistration)
    if not all(verify_v4(item) for item in (manifest, encrypted, prereg)):
        raise ValueError("sealed Policy-v4 input artifact hash is invalid")
    if manifest.content_hash != prereg.question_manifest_hash:
        raise ValueError("preregistration does not reference the sealed question manifest")
    if manifest.encrypted_payload_artifact_hash != encrypted.content_hash:
        raise ValueError("encrypted question artifact lineage mismatch")
    if database_fingerprint(database) != manifest.database_fingerprint:
        raise ValueError("holdout clone fingerprint differs from the sealed question source clone")
    _preflight(base_url)
    clone_probe_revoked = _verify_clone_api_binding(database, base_url)
    before = business_fingerprint(database)
    started = _utcnow()
    pairs = list(zip(encrypted.entries, manifest.questions, strict=True))
    worker_count = max(1, min(int(concurrency), len(pairs)))
    groups = [pairs[index::worker_count] for index in range(worker_count)]
    cases: list[HoldoutCase] = []
    revoked_flags: list[bool] = []
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="policy-v4-holdout") as pool:
        futures = [pool.submit(
            _worker, database=database, base_url=base_url, pairs=group,
            timeout_seconds=timeout_seconds, max_attempts=max_attempts,
        ) for group in groups]
        for future in as_completed(futures):
            group_cases, revoked = future.result()
            cases.extend(group_cases)
            revoked_flags.append(revoked)
    cases.sort(key=lambda item: item.sealed_order)
    after = business_fingerprint(database)
    collection = stamp_v4(HoldoutCollection(
        experiment_id=experiment_id, question_manifest_hash=manifest.content_hash,
        preregistration_hash=prereg.content_hash,
        database_clone_fingerprint=manifest.database_fingerprint,
        started_at=started, completed_at=_utcnow(),
        business_fingerprint_before=before, business_fingerprint_after=after,
        business_persistence_unchanged=before == after,
        temporary_session_count=len(groups) + 1,
        temporary_session_revoked_count=sum(revoked_flags) + int(clone_probe_revoked),
        thread_created_count=sum(item.thread_created_count for item in cases),
        thread_archived_count=sum(item.thread_archived_count for item in cases),
        cases=cases,
    ))
    selection = select_first_200(experiment_id=experiment_id, holdout=collection)
    write_model(Path(output_path), collection)
    write_model(Path(selection_path), selection)
    return collection, selection


__all__ = [
    "business_fingerprint", "clone_production_database", "collect_holdout_v4", "select_first_200",
    "_verify_clone_api_binding",
]
