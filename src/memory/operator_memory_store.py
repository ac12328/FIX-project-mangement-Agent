"""Facade for loading and writing operator memory."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from src.models.operator_memory import MemoryType, OperatorMemoryEntry
from src.repositories import operator_memory_repo as repo


class OperatorMemoryStore:
    """LangGraph-inspired namespace store backed by SQLite."""

    def __init__(self, conn: sqlite3.Connection, operator_id: str):
        self._conn = conn
        self._operator_id = operator_id

    @property
    def operator_id(self) -> str:
        return self._operator_id

    def put(
        self,
        *,
        scope: str,
        scope_id: str,
        memory_type: MemoryType,
        memory_key: str,
        memory_value: dict[str, Any],
        source: str = "operator",
    ) -> None:
        repo.upsert_memory(
            self._conn,
            OperatorMemoryEntry(
                operator_id=self._operator_id,
                scope=scope,  # type: ignore[arg-type]
                scope_id=scope_id,
                memory_type=memory_type,
                memory_key=memory_key,
                memory_value=memory_value,
                source=source,
            ),
        )

    def put_forbidden(
        self,
        *,
        project_id: str,
        rule_key: str,
        description: str,
        blocked_fields: dict[str, list[str]] | None = None,
        source: str = "operator",
    ) -> None:
        self.put(
            scope="project",
            scope_id=project_id,
            memory_type=MemoryType.FORBIDDEN,
            memory_key=rule_key,
            memory_value={
                "description": description,
                "blocked_fields": blocked_fields or {},
            },
            source=source,
        )

    def list_for_context(
        self,
        *,
        project_id: str | None = None,
        chat_id: int | None = None,
        session_id: str | None = None,
        entity_refs: list[tuple[str, str]] | None = None,
        memory_request=None,
    ) -> list[dict]:
        from src.learning.legacy_retirement import legacy_injection_allowed
        if not legacy_injection_allowed(self._conn, "operator_memory"):
            return []
        rows: list[dict] = []
        rows.extend(
            repo.list_memories(
                self._conn, self._operator_id, scope="global", scope_id="*",
            )
        )
        if chat_id is not None:
            rows.extend(
                repo.list_memories(
                    self._conn,
                    self._operator_id,
                    scope="chat",
                    scope_id=str(chat_id),
                )
            )
        if session_id and session_id != (str(chat_id) if chat_id is not None else None):
            rows.extend(
                repo.list_memories(
                    self._conn,
                    self._operator_id,
                    scope="chat",
                    scope_id=session_id,
                )
            )
        if project_id:
            rows.extend(
                repo.list_memories(
                    self._conn,
                    self._operator_id,
                    scope="project",
                    scope_id=project_id,
                )
            )
        seen_refs: set[tuple[str, str]] = set()
        for scope, scope_id in entity_refs or []:
            ref = (scope, scope_id)
            if not scope_id or ref in seen_refs:
                continue
            seen_refs.add(ref)
            rows.extend(
                repo.list_memories(
                    self._conn,
                    self._operator_id,
                    scope=scope,
                    scope_id=scope_id,
                )
            )
        owners = {row["legacy_key"]: (row["fact_id"], row["fact_version"]) for row in self._conn.execute(
            "SELECT legacy_key,fact_id,fact_version FROM memory_projection_ownership WHERE target='operator_memory'")}
        authorized = set()
        if owners and memory_request is not None and memory_request.operator_id == self._operator_id:
            from src.memory.retriever import MemoryRetriever
            authorized = MemoryRetriever().authorized_versions(self._conn, memory_request)
        from src.memory.projection_authority import operator_projection_key
        return [row for row in rows if owners.get(operator_projection_key(row)) is None
                or owners[operator_projection_key(row)] in authorized]

    def format_for_prompt(
        self,
        *,
        project_id: str | None = None,
        chat_id: int | None = None,
        session_id: str | None = None,
        entity_refs: list[tuple[str, str]] | None = None,
        memory_request=None,
    ) -> str:
        rows = self.list_for_context(
            project_id=project_id,
            chat_id=chat_id,
            session_id=session_id,
            entity_refs=entity_refs,
            memory_request=memory_request,
        )
        if not rows:
            return ""
        lines: list[str] = []
        for row in rows:
            val = row.get("memory_value")
            if val is None:
                val = {}
            if isinstance(val, dict):
                desc = val.get("description") or val.get("text") or json.dumps(val, ensure_ascii=False)
            else:
                desc = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
            lines.append(
                f"- [{row['memory_type']}] {row['scope']}/{row['scope_id']}"
                f" {row['memory_key']}: {desc}"
            )
        return "<operator-memory>\n" + "\n".join(lines) + "\n</operator-memory>"

    def list_forbidden_for_project(self, project_id: str) -> list[dict]:
        global_rows = repo.list_memories(
            self._conn,
            self._operator_id,
            scope="global",
            scope_id="*",
            memory_type=MemoryType.FORBIDDEN.value,
        )
        project_rows = repo.list_memories(
            self._conn,
            self._operator_id,
            scope="project",
            scope_id=project_id,
            memory_type=MemoryType.FORBIDDEN.value,
        )
        return global_rows + project_rows