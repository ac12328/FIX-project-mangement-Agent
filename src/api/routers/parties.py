"""Party graph API — clients, vendors, project memberships."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.db.connection import scoped_read, scoped_write
from src.repositories import party_repo

router = APIRouter(tags=["parties"])


class OrgSummary(BaseModel):
    org_id: str
    org_name: str
    org_type: str | None = None
    domains: str | None = None


class LinkPartyRequest(BaseModel):
    org_id: str
    party_role: str = Field(default="other")
    is_primary: bool = False
    notes: str | None = None


class LinkContactRequest(BaseModel):
    contact_id: str
    role_on_project: str = "other"
    is_primary: bool = False


@router.get("/parties")
def list_parties(
    org_type: str | None = Query(None, description="client|vendor|regulator|internal|unknown"),
    limit: int = Query(100, ge=1, le=500),
):
    with scoped_read() as conn:
        orgs = party_repo.list_orgs_by_type(conn, org_type, limit=limit)
    return {"items": orgs, "count": len(orgs)}


@router.get("/parties/{org_id}")
def get_party(org_id: str):
    with scoped_read() as conn:
        org = party_repo.get_org(conn, org_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organisation not found")
        projects = party_repo.list_org_projects(conn, org_id)
        contacts = party_repo.list_org_contacts(conn, org_id)
    return {"organisation": org, "projects": projects, "contacts": contacts}


@router.get("/projects/{project_id}/parties")
def get_project_parties(project_id: str):
    with scoped_read() as conn:
        parties = party_repo.list_project_parties(conn, project_id)
    return {"project_id": project_id, "parties": parties}


@router.post("/projects/{project_id}/parties")
def post_project_party(project_id: str, body: LinkPartyRequest):
    with scoped_write() as conn:
        row = party_repo.link_org_to_project(
            conn,
            project_id,
            body.org_id,
            body.party_role,
            is_primary=body.is_primary,
            notes=body.notes,
        )
        conn.commit()
    return {"ok": True, "membership": row}


@router.post("/projects/{project_id}/contacts")
def post_project_contact(project_id: str, body: LinkContactRequest):
    with scoped_write() as conn:
        row = party_repo.link_contact_to_project(
            conn,
            body.contact_id,
            project_id,
            role_on_project=body.role_on_project,
            is_primary=body.is_primary,
            source="api",
        )
        conn.commit()
    return {"ok": True, "link": row}
