# Memory Ownership

Canonical ownership of memory layers in AgentFIXBPRO.
Related: [VOCABULARY.md](./VOCABULARY.md), `src/models/context_layers.py`, `src/models/context_frame.py`.

**Assembly path:** `ContextEngine` → `ContextFrame` (sole production assembly output).
Layer models `WorkingMemoryContext` / `RelationshipContext` live in `src/models/context_layers.py`.
`ContextPacket` is deprecated (compat re-export only).

---

## Ownership table

| Layer | Table/store | Write when | Read by | Lifecycle |
|-------|-------------|------------|---------|-----------|
| Working memory | `working_memory` | Session state, active project, pending questions | Context L2, anchors | Stale after `WORKING_MEMORY_STALENESS_DAYS` |
| Operator memory | `operator_memory` | Explicit remember / flush / prefs | Memory guard, context | `active` flag |
| Agent notes | `agent_notes` | Curated personal KB | Context knowledge | Manual |
| Project corrections | `project_corrections` | Agent self-correction on field errors | Context / write guards | Append-only |
| Session summaries | `session_summaries` | End of Telegram session | Next session bootstrap | Retention TBD |
| Wiki | wiki graph | Curator / tools | Knowledge slot | Reviewed pages |

---

## Rules of thumb for writers

| Intent | Store |
|--------|--------|
| Preferences / forbidden phrases | `operator_memory` (**not** `working_memory`) |
| Ephemeral session state | `working_memory` |
| Field value corrections | `project_corrections` |
| Curated long-lived notes | `agent_notes` |
| Cross-session narrative | `session_summaries` |
| Shared / reviewed knowledge | wiki graph |

### Soft enforcement

`src/memory/write_policy.py` logs a warning when a preference-like key
(`pref_`, `preference_`, `forbidden_`, `avoid_`) is written to
`working_memory`. Prefer `operator_memory` for those keys.

---

## Layer notes

### Working memory (`working_memory`)

- Per-project key/value store for short-lived operational state.
- Keys commonly mapped into `WorkingMemoryContext`: `recent_thread_summary`,
  `latest_operator_instruction`, `pending_question*`, `open_decision*`.
- Staleness computed in `working_memory_repo` via `WORKING_MEMORY_STALENESS_DAYS`
  (`config/defaults.py`, default 7 days).

### Operator memory (`operator_memory`)

- Operator-scoped preferences and explicit remember/flush entries.
- Filtered by `active` flag; scopes may be global or project-scoped.
- Do not dump preference material into `working_memory`.

### Agent notes (`agent_notes`)

- Curated personal knowledge base entries (`note_type` classified).
- Manual lifecycle; surfaced into the context knowledge slot.

### Project corrections (`project_corrections`)

- Append-only self-correction log when the agent mis-writes a field.
- Consumed by context / write guards to avoid repeating the same mistake.

### Session summaries (`session_summaries`)

- Written at Telegram session end; loaded on next session bootstrap.
- FTS index available for search; retention policy TBD.

### Wiki

- Typed knowledge graph under `src/wiki/`.
- Curator jobs and agent tools write; reviewed pages feed the knowledge slot.
