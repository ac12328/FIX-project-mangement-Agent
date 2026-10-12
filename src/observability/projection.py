"""SQLite projection for sanitized engineering trace spool records."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def project_record(conn: sqlite3.Connection, record: dict[str, Any]) -> bool:
    """Project one record, returning True when it was already present."""
    event = record["event"]
    # Replaying a start must not reopen a finished run or restore stale scope.
    if conn.execute("SELECT 1 FROM trace_events WHERE event_id=?", (event["event_id"],)).fetchone():
        return True
    metadata = dict(event.get("metadata") or {})
    if event["event_type"] == "run.start":
        metadata["initial_project_id"] = metadata.get("project_id")
        metadata["initial_project_ids"] = metadata.get("project_ids", metadata.get("scope_project_ids", []))
    run_id = event["run_id"]
    timestamp = event["timestamp"]
    conn.execute(
        """INSERT OR IGNORE INTO trace_runs(
               run_id,schema_version,trace_id,turn_id,thread_id,surface,task,project_id,
               session_id,chat_id,operator_id,status,started_at,metadata_json,implicit,
               component,component_version
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run_id, event.get("schema_version", 1), event["trace_id"], event.get("turn_id"),
            event.get("thread_id"), metadata.get("surface", "background"),
            metadata.get("task", "background"), metadata.get("project_id"),
            metadata.get("session_id"), metadata.get("chat_id"), metadata.get("operator_id"),
            "running", timestamp, json.dumps(metadata, ensure_ascii=False, default=str),
            int(bool(metadata.get("implicit"))), event.get("component"), event.get("component_version"),
        ),
    )
    if event["event_type"] == "run.start":
        conn.execute(
            """UPDATE trace_runs SET schema_version=?,trace_id=?,turn_id=?,thread_id=?,surface=?,task=?,
                   project_id=?,session_id=?,chat_id=?,operator_id=?,status='running',started_at=?,
                   metadata_json=?,implicit=?,component=?,component_version=? WHERE run_id=? AND ended_at IS NULL""",
            (
                event.get("schema_version", 1), event["trace_id"], event.get("turn_id"), event.get("thread_id"),
                metadata.get("surface", "background"), metadata.get("task", "background"), metadata.get("project_id"),
                metadata.get("session_id"), metadata.get("chat_id"), metadata.get("operator_id"), timestamp,
                json.dumps(metadata, ensure_ascii=False, default=str), int(bool(metadata.get("implicit"))),
                event.get("component"), event.get("component_version"), run_id,
            ),
        )
    llm_call_id = event.get("llm_call_id")
    if llm_call_id:
        _upsert_llm_trace(conn, event, metadata)
    envelope = record.get("payload") or {}
    cursor = conn.execute(
        """INSERT OR IGNORE INTO trace_events(
               event_id,run_id,schema_version,trace_id,turn_id,thread_id,sequence,event_type,
               round_index,llm_trace_id,llm_call_id,tool_call_id,tool_name,status,duration_ms,
               component,component_version,parent_event_id,parent_span_id,metadata_json,
               payload_ciphertext,payload_nonce,payload_key_version,payload_hash,created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event["event_id"], run_id, event.get("schema_version", 1), event["trace_id"], event.get("turn_id"),
            event.get("thread_id"), event["sequence"], event["event_type"], event.get("round_index"),
            llm_call_id, llm_call_id, event.get("tool_call_id"), metadata.get("tool_name"), event.get("status", "ok"),
            event.get("duration_ms"), event.get("component"), event.get("component_version"),
            event.get("parent_event_id"), event.get("parent_span_id"),
            json.dumps(metadata, ensure_ascii=False, default=str), envelope.get("ciphertext"), envelope.get("nonce"),
            envelope.get("key_version"), envelope.get("payload_hash"), timestamp,
        ),
    )
    if event["event_type"] == "run.finish":
        from src.observability.project_scope_projection import finish_project_scope

        finish_project_scope(conn, run_id, metadata)
        conn.execute(
            """UPDATE trace_runs SET status=?,exit_reason=?,anomaly_flags_json=?,ended_at=?,
                   duration_ms=?,persistence_status=? WHERE run_id=?""",
            (
                event.get("status", "completed"), metadata.get("exit_reason"),
                json.dumps(metadata.get("anomaly_flags") or []), timestamp,
                int(event.get("duration_ms") or 0), metadata.get("persistence_status"), run_id,
            ),
        )
    return cursor.rowcount == 0


def _upsert_llm_trace(conn: sqlite3.Connection, event: dict[str, Any], metadata: dict[str, Any]) -> None:
    llm_call_id = str(event["llm_call_id"])
    conn.execute(
        """INSERT INTO llm_traces(
               trace_id,llm_call_id,task,model,tokens_in,tokens_out,latency_ms,output_valid,
               error_class,error_message,decision_outcome,prompt_hash,per_guard_results,
               trace_metadata,run_id,turn_id,round_index,ttft_ms,retry_count,failover_from,
               cache_hit,cache_read_tokens,cache_write_tokens,request_event_id,response_event_id,created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(trace_id) DO UPDATE SET
               model=excluded.model,tokens_in=MAX(llm_traces.tokens_in,excluded.tokens_in),
               tokens_out=MAX(llm_traces.tokens_out,excluded.tokens_out),
               latency_ms=MAX(llm_traces.latency_ms,excluded.latency_ms),
               output_valid=excluded.output_valid,error_class=COALESCE(excluded.error_class,llm_traces.error_class),
               error_message=COALESCE(excluded.error_message,llm_traces.error_message),
               decision_outcome=COALESCE(excluded.decision_outcome,llm_traces.decision_outcome),
               trace_metadata=excluded.trace_metadata,ttft_ms=COALESCE(excluded.ttft_ms,llm_traces.ttft_ms),
               retry_count=MAX(llm_traces.retry_count,excluded.retry_count),
               failover_from=COALESCE(excluded.failover_from,llm_traces.failover_from),
               cache_hit=COALESCE(excluded.cache_hit,llm_traces.cache_hit),
               cache_read_tokens=MAX(llm_traces.cache_read_tokens,excluded.cache_read_tokens),
               cache_write_tokens=MAX(llm_traces.cache_write_tokens,excluded.cache_write_tokens),
               request_event_id=COALESCE(llm_traces.request_event_id,excluded.request_event_id),
               response_event_id=COALESCE(excluded.response_event_id,llm_traces.response_event_id)""",
        (
            llm_call_id, llm_call_id, metadata.get("task", "unknown"), metadata.get("model", "unknown"),
            int(metadata.get("tokens_in") or 0), int(metadata.get("tokens_out") or 0),
            int(event.get("duration_ms") or metadata.get("latency_ms") or 0),
            int(event.get("event_type") != "llm.error"), metadata.get("error_class"), metadata.get("error_message"),
            metadata.get("decision_outcome"), metadata.get("prompt_hash"),
            json.dumps(metadata.get("guard_results"), ensure_ascii=False) if metadata.get("guard_results") else None,
            json.dumps(metadata, ensure_ascii=False, default=str), event["run_id"], event.get("turn_id"),
            event.get("round_index"), metadata.get("ttft_ms"), int(metadata.get("retry_count") or 0),
            metadata.get("failover_from"), None if metadata.get("cache_hit") is None else int(bool(metadata.get("cache_hit"))),
            int(metadata.get("cache_read_tokens") or 0), int(metadata.get("cache_write_tokens") or 0),
            event["event_id"] if event["event_type"] == "llm.request" else None,
            event["event_id"] if event["event_type"] in {"llm.response", "llm.error"} else None,
            event["timestamp"],
        ),
    )
