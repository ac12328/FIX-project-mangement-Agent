"""
Contact Registry — Organisation and Contact repositories.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone


def _gen_id(prefix: str = "") -> str:
    """Generate a short unique ID."""
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def create_organisation(
    conn: sqlite3.Connection,
    org_name: str,
    domains: str,
    org_type: str = "client",
) -> str:
    """Create an organisation. Returns org_id."""
    now = datetime.now(timezone.utc).isoformat()
    org_id = _gen_id("org_")
    conn.execute(
        """INSERT INTO organisations (org_id, org_name, domains, org_type, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (org_id, org_name, domains, org_type, now),
    )
    conn.commit()
    return org_id


def get_organisation_by_domain(
    conn: sqlite3.Connection, domain: str
) -> dict | None:
    """Find an organisation by matching a domain in its domains list."""
    domain = domain.lower().strip()
    rows = conn.execute("SELECT * FROM organisations").fetchall()
    for row in rows:
        d = dict(row)
        org_domains = [x.strip().lower() for x in (d.get("domains") or "").split(",")]
        if domain in org_domains:
            return d
    return None


def create_contact(
    conn: sqlite3.Connection,
    display_name: str,
    email_addresses: str,
    primary_email: str | None = None,
    *,
    organisation_id: str | None = None,
    role: str | None = None,
    contact_type: str = "person",
    department: str | None = None,
    expertise_tags: str | None = None,
    responsiveness_score: float | None = None,
    preferred_contact_method: str | None = None,
) -> str:
    """Create a contact. Returns contact_id."""
    now = datetime.now(timezone.utc).isoformat()
    contact_id = _gen_id("ct_")
    conn.execute(
        """INSERT INTO contacts
           (contact_id, display_name, email_addresses, primary_email,
            organisation_id, role, contact_type, department, expertise_tags,
            responsiveness_score, preferred_contact_method, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (contact_id, display_name, email_addresses, primary_email,
         organisation_id, role, contact_type, department,
         expertise_tags or "[]", responsiveness_score,
         preferred_contact_method, now),
    )
    conn.commit()
    return contact_id


def upsert_contact_for_project(
    conn: sqlite3.Connection,
    email: str,
    display_name: str,
    project_id: str | None = None,
    *,
    role: str | None = None,
) -> dict:
    """Create or update a contact, adding project_id to cross_project_history.

    Always marks the contact as operator_confirmed.
    Returns {"contact_id": ..., "action": "created"|"updated", "email": email}.
    """
    email = email.lower().strip()
    existing = find_contact_by_email(conn, email)

    if existing:
        contact_id = existing["contact_id"]
        # Add project to cross_project_history if not already present
        if project_id:
            try:
                history = json.loads(existing.get("cross_project_history") or "[]")
            except json.JSONDecodeError:
                history = []
            if project_id not in history:
                history.append(project_id)
                conn.execute(
                    "UPDATE contacts SET cross_project_history = ? WHERE contact_id = ?",
                    (json.dumps(history), contact_id),
                )
        # Promote to operator_confirmed + update name/role if provided
        updates = ["created_by = 'operator_confirmed'"]
        params: list = []
        if display_name:
            updates.append("display_name = ?")
            params.append(display_name)
        if role:
            updates.append("role = ?")
            params.append(role)
        params.append(contact_id)
        conn.execute(
            f"UPDATE contacts SET {', '.join(updates)} WHERE contact_id = ?",
            params,
        )
        conn.commit()
        return {"contact_id": contact_id, "action": "updated", "email": email}

    # Create new contact
    history_json = json.dumps([project_id]) if project_id else "[]"
    now = datetime.now(timezone.utc).isoformat()
    contact_id = _gen_id("ct_")
    conn.execute(
        """INSERT INTO contacts
           (contact_id, display_name, email_addresses, primary_email,
            cross_project_history, role, created_by, confidence, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'operator_confirmed', 0.9, ?)""",
        (contact_id, display_name, email, email, history_json, role, now),
    )
    conn.commit()
    return {"contact_id": contact_id, "action": "created", "email": email}


def find_contact_by_email(
    conn: sqlite3.Connection, email: str
) -> dict | None:
    """Find a contact by any of their email addresses."""
    email = email.lower().strip()
    rows = conn.execute("SELECT * FROM contacts").fetchall()
    for row in rows:
        d = dict(row)
        emails = [e.strip().lower() for e in (d.get("email_addresses") or "").split(",")]
        if email in emails:
            return d
    return None


def get_primary_contact_for_project(
    conn: sqlite3.Connection, project_id: str
) -> str | None:
    """Return the contact_id of the primary external contact for a project.

    Looks for operator-confirmed contacts first, then auto-resolved ones.
    Returns None if no contact is associated with the project.
    """
    row = conn.execute(
        """SELECT contact_id FROM contacts
           WHERE cross_project_history LIKE ?
             AND contact_type NOT IN ('internal', 'department')
           ORDER BY
             CASE WHEN created_by = 'operator_confirmed' THEN 0 ELSE 1 END,
             confidence DESC
           LIMIT 1""",
        (f'%"{project_id}"%',),
    ).fetchone()
    return row["contact_id"] if row else None


def get_contacts_by_organisation(
    conn: sqlite3.Connection, organisation_id: str
) -> list[dict]:
    """Get all contacts belonging to an organisation."""
    rows = conn.execute(
        "SELECT * FROM contacts WHERE organisation_id = ?",
        (organisation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _tokenize(text: str) -> set[str]:
    """Normalize text into searchable tokens (space/underscore/hyphen separated)."""
    import re
    tokens = re.split(r"[\s_—–-]+", text.lower())
    return {t for t in tokens if t}


def find_internal_contacts_by_expertise(
    conn: sqlite3.Connection,
    expertise_needed: str,
    limit: int = 5,
) -> list[dict]:
    """
    Find internal contacts matching an expertise need, ranked by tag relevance
    then responsiveness_score (per Blueprint Section 15.4).

    expertise_needed: free-text string (e.g. "KYC review", "network access")
    Returns: list of dicts with contact_id, display_name, department,
             expertise_tags (as list), responsiveness_score, primary_email
    """
    rows = conn.execute(
        """SELECT contact_id, display_name, primary_email, department,
                  expertise_tags, responsiveness_score
           FROM contacts
           WHERE contact_type = 'internal'"""
    ).fetchall()

    expertise_tokens = _tokenize(expertise_needed)
    scored: list[tuple[int, float, dict]] = []

    for row in rows:
        d = dict(row)
        raw_tags = d.get("expertise_tags") or "[]"
        try:
            tags = json.loads(raw_tags) if isinstance(raw_tags, str) else raw_tags
        except json.JSONDecodeError:
            tags = []

        # Score: count of tags that share any token with expertise_needed (case-insensitive)
        score = 0
        for tag in tags:
            if isinstance(tag, str):
                tag_tokens = _tokenize(tag)
                score += len(tag_tokens & expertise_tokens)

        if score > 0:
            scored.append((score, d.get("responsiveness_score") or 0.0, d))

    # Sort: tag relevance DESC, then responsiveness DESC
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    results: list[dict] = []
    for score, _, d in scored[:limit]:
        raw_tags = d.get("expertise_tags") or "[]"
        try:
            tags = json.loads(raw_tags) if isinstance(raw_tags, str) else raw_tags
        except json.JSONDecodeError:
            tags = []
        results.append({
            "contact_id": d.get("contact_id"),
            "display_name": d.get("display_name"),
            "department": d.get("department"),
            "expertise_tags": tags,
            "responsiveness_score": d.get("responsiveness_score"),
            "primary_email": d.get("primary_email"),
        })

    return results
