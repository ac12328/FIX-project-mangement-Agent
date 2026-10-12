"""Party graph repositories — organisations on projects, people on projects."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any


def link_org_to_project(
    conn: sqlite3.Connection,
    project_id: str,
    org_id: str,
    party_role: str = "other",
    *,
    is_primary: bool = False,
    notes: str | None = None,
) -> dict[str, Any]:
    """Ensure organisation is a party on the project. Returns membership row."""
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        """SELECT id, is_primary FROM project_parties
           WHERE project_id = ? AND org_id = ? AND party_role = ?""",
        (project_id, org_id, party_role),
    ).fetchone()
    if existing:
        new_primary = 1 if (is_primary or existing["is_primary"]) else 0
        conn.execute(
            """UPDATE project_parties
               SET is_primary = ?, notes = COALESCE(?, notes), updated_at = ?
               WHERE id = ?""",
            (new_primary, notes, now, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO project_parties
               (project_id, org_id, party_role, is_primary, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (project_id, org_id, party_role, 1 if is_primary else 0, notes, now, now),
        )
    row = conn.execute(
        """SELECT * FROM project_parties
           WHERE project_id = ? AND org_id = ? AND party_role = ?""",
        (project_id, org_id, party_role),
    ).fetchone()
    return dict(row) if row else {}


def link_contact_to_project(
    conn: sqlite3.Connection,
    contact_id: str,
    project_id: str,
    *,
    role_on_project: str = "other",
    source: str = "identity_confirm",
    is_primary: bool = False,
) -> dict[str, Any]:
    """Ensure person is linked to project. Returns link row."""
    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        """SELECT id, is_primary FROM contact_project_links
           WHERE contact_id = ? AND project_id = ?""",
        (contact_id, project_id),
    ).fetchone()
    if existing:
        new_primary = 1 if (is_primary or existing["is_primary"]) else 0
        conn.execute(
            """UPDATE contact_project_links
               SET role_on_project = COALESCE(?, role_on_project),
                   is_primary = ?
               WHERE id = ?""",
            (role_on_project, new_primary, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO contact_project_links
               (contact_id, project_id, role_on_project, is_primary, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                contact_id,
                project_id,
                role_on_project,
                1 if is_primary else 0,
                source,
                now,
            ),
        )
    # Keep denormalized history in sync
    row = conn.execute(
        "SELECT cross_project_history FROM contacts WHERE contact_id = ?",
        (contact_id,),
    ).fetchone()
    if row is not None:
        import json

        try:
            history = json.loads(row["cross_project_history"] or "[]")
        except json.JSONDecodeError:
            history = []
        if not isinstance(history, list):
            history = []
        if project_id not in history:
            history.append(project_id)
            conn.execute(
                "UPDATE contacts SET cross_project_history = ? WHERE contact_id = ?",
                (json.dumps(sorted(str(x) for x in history)), contact_id),
            )
    link = conn.execute(
        """SELECT * FROM contact_project_links
           WHERE contact_id = ? AND project_id = ?""",
        (contact_id, project_id),
    ).fetchone()
    return dict(link) if link else {}


def list_orgs_by_type(
    conn: sqlite3.Connection,
    org_type: str | None = None,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if org_type:
        rows = conn.execute(
            """SELECT * FROM organisations
               WHERE org_type = ?
               ORDER BY org_name LIMIT ?""",
            (org_type, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM organisations ORDER BY org_name LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_project_parties(
    conn: sqlite3.Connection, project_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT pp.*, o.org_name, o.org_type, o.domains
           FROM project_parties pp
           JOIN organisations o ON o.org_id = pp.org_id
           WHERE pp.project_id = ?
           ORDER BY pp.party_role, o.org_name""",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_org_projects(
    conn: sqlite3.Connection, org_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT pp.*, p.client_name, p.project_status, p.current_milestone
           FROM project_parties pp
           JOIN projects p ON p.project_id = pp.project_id
           WHERE pp.org_id = ?
           ORDER BY pp.updated_at DESC""",
        (org_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_contact_projects(
    conn: sqlite3.Connection, contact_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT cpl.*, p.client_name, p.project_status
           FROM contact_project_links cpl
           JOIN projects p ON p.project_id = cpl.project_id
           WHERE cpl.contact_id = ?
           ORDER BY cpl.created_at DESC""",
        (contact_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_org(
    conn: sqlite3.Connection, org_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM organisations WHERE org_id = ?", (org_id,)
    ).fetchone()
    return dict(row) if row else None


def list_org_contacts(
    conn: sqlite3.Connection, org_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM contacts
           WHERE organisation_id = ?
           ORDER BY display_name""",
        (org_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_counterparty_org_id(
    conn: sqlite3.Connection,
    *,
    counterparty: str | None = None,
    reporter_email: str | None = None,
) -> str | None:
    """Map free-text counterparty / reporter email to organisations.org_id.

    Resolution order:
      1. counterparty equals a known email domain
      2. reporter_email domain matches an organisation
      3. counterparty equals org_name (case-insensitive)
      4. counterparty contains a known domain token (e.g. 'Demo Client / Demo Vendor')
    """
    from src.repositories.contact_repo import get_organisation_by_domain

    raw = (counterparty or "").strip()
    email = (reporter_email or "").strip().lower()

    # 1) bare domain string
    if raw and "@" not in raw and "." in raw and " " not in raw:
        org = get_organisation_by_domain(conn, raw.lower())
        if org:
            return str(org["org_id"])

    # 2) reporter email domain
    if "@" in email:
        domain = email.split("@", 1)[1]
        org = get_organisation_by_domain(conn, domain)
        if org:
            return str(org["org_id"])

    # 3) exact org name
    if raw:
        row = conn.execute(
            """SELECT org_id FROM organisations
               WHERE lower(org_name) = lower(?)
               LIMIT 1""",
            (raw,),
        ).fetchone()
        if row:
            return str(row["org_id"])

    # 4) domain token appears inside free-text counterparty
    if raw:
        for o in conn.execute("SELECT org_id, domains, org_name FROM organisations"):
            for dom in (o["domains"] or "").split(","):
                dom = dom.strip().lower()
                if dom and dom in raw.lower():
                    return str(o["org_id"])
            name = (o["org_name"] or "").strip()
            if name and len(name) >= 3 and name.lower() in raw.lower():
                return str(o["org_id"])

    return None


def guess_org_type_for_domain(
    conn: sqlite3.Connection, domain: str
) -> str:
    """Heuristic: internal/company → vendor list → wiki → DB → multi-project.

    Domains in CONFIG.company_domains are always internal, never
    vendor — even when they appear on many projects.
    """
    domain = (domain or "").lower().strip()
    if not domain:
        return "unknown"

    try:
        from config.settings import CONFIG

        company = {d.lower() for d in getattr(CONFIG, "company_domains", set())}
        if domain in company:
            return "internal"
    except Exception:
        pass

    try:
        from config.defaults import VENDOR_EMAIL_DOMAINS

        if domain in VENDOR_EMAIL_DOMAINS:
            return "vendor"
    except Exception:
        pass

    from src.wiki.domain_index import get_wiki_domain_index

    wiki_type = get_wiki_domain_index().org_type_for(domain)
    if wiki_type and wiki_type != "unknown":
        return wiki_type

    existing = None
    try:
        from src.repositories.contact_repo import get_organisation_by_domain
        existing = get_organisation_by_domain(conn, domain)
    except Exception:
        existing = None
    if existing and existing.get("org_type") and existing["org_type"] != "unknown":
        return str(existing["org_type"])

    # Domain appears on multiple project client_emails → likely vendor serving many
    try:
        from src.triage.client_emails import project_ids_for_sender_domain

        pids = project_ids_for_sender_domain(
            conn, f"user@{domain}", active_only=False
        )
        if len(pids) >= 2:
            return "vendor"
    except Exception:
        pass
    return "unknown"
