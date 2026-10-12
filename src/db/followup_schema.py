"""Additive followup action metadata; never infer legacy owners or promises."""

def ensure_followup_action_columns(conn):
    columns={row[1] for row in conn.execute("PRAGMA table_info(internal_followups)")}
    if not columns:
        return
    additions={
        "reminder_due_at":"TEXT",
        "action_revision":"INTEGER NOT NULL DEFAULT 0",
        "owner_side":"TEXT CHECK(owner_side IN ('Internal','External','Shared'))",
        "due_date_basis":"TEXT NOT NULL DEFAULT 'legacy_unknown' CHECK(due_date_basis IN ('legacy_unknown','operator_entered','reported_commitment','none'))",
    }
    for name,definition in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE internal_followups ADD COLUMN {name} {definition}")
