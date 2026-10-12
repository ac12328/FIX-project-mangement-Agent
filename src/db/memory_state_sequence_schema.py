"""Give equal-time memory state events a durable append-order tie breaker."""
TABLE = """CREATE TABLE {name} (
 event_seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 fact_id TEXT NOT NULL REFERENCES memory_facts(fact_id),
 state TEXT NOT NULL CHECK(state IN ('active','superseded','revoked')),
 actor_id TEXT NOT NULL, reason TEXT NOT NULL, effective_at TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT(datetime('now'))
)"""
HEAD_TRIGGER = """CREATE TRIGGER trg_memory_fact_heads_binding BEFORE INSERT ON memory_fact_heads
WHEN NOT EXISTS (SELECT 1 FROM memory_facts f WHERE f.fact_id=NEW.fact_id AND f.fact_key=NEW.fact_key AND f.fact_version=NEW.fact_version)
OR NOT EXISTS (SELECT 1 FROM memory_fact_state_events e WHERE e.fact_id=NEW.fact_id AND e.state='active'
 AND e.event_id=(SELECT event_id FROM memory_fact_state_events WHERE fact_id=NEW.fact_id
 ORDER BY effective_at DESC,created_at DESC,event_seq DESC LIMIT 1))
BEGIN SELECT RAISE(ABORT,'memory_fact_heads binding invalid'); END"""


def ensure_memory_event_sequence(conn):
    """Atomic upgrade; preserve legacy row insertion order without rewriting events."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_fact_state_events)")}
    if not columns:
        return
    started = not conn.in_transaction
    if started:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT memory_state_sequence")
    try:
        conn.execute("DROP TRIGGER IF EXISTS trg_memory_fact_heads_binding")
        if "event_seq" not in columns:
            conn.execute(TABLE.format(name="memory_fact_state_events_v118"))
            conn.execute("""INSERT INTO memory_fact_state_events_v118
                (event_seq,event_id,fact_id,state,actor_id,reason,effective_at,created_at)
                SELECT rowid,event_id,fact_id,state,actor_id,reason,effective_at,created_at
                FROM memory_fact_state_events ORDER BY rowid""")
            conn.execute("DROP TRIGGER IF EXISTS trg_memory_fact_state_no_update")
            conn.execute("DROP TRIGGER IF EXISTS trg_memory_fact_state_no_delete")
            conn.execute("DROP TABLE memory_fact_state_events")
            conn.execute("ALTER TABLE memory_fact_state_events_v118 RENAME TO memory_fact_state_events")
        conn.execute(HEAD_TRIGGER)
        for action in ("update", "delete"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS trg_memory_fact_state_no_{action} BEFORE {action.upper()} ON memory_fact_state_events BEGIN SELECT RAISE(ABORT,'memory_fact_state_events is append-only'); END")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_state_latest ON memory_fact_state_events(fact_id,effective_at,created_at,event_seq)")
        conn.execute("RELEASE SAVEPOINT memory_state_sequence")
        if started:
            conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT memory_state_sequence")
        conn.execute("RELEASE SAVEPOINT memory_state_sequence")
        if started:
            conn.rollback()
        raise
