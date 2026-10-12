"""Unified project intake endpoint."""

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from src.api.deps import get_conn, get_write_conn
from src.api.schemas.intake import IntakeRequest, IntakeResponse
from src.portfolio.project_intake import (
    IntakeProposal,
    confirm_intake,
    create_intake_from_structured,
    create_intake_from_text,
)

router = APIRouter(tags=["intake"])
logger = logging.getLogger(__name__)


@router.post("/projects/intake", response_model=IntakeResponse)
def project_intake(
    body: IntakeRequest,
    conn: sqlite3.Connection = Depends(get_write_conn),
):
    proposal = _build_proposal(body)
    if proposal is None:
        raise HTTPException(400, "Could not build intake proposal from the supplied payload")

    committed = False
    message = "Intake proposal created. Confirmation required before commit."
    if body.confirm:
        try:
            committed, message = confirm_intake(conn, proposal)
        except sqlite3.OperationalError as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error("Intake DB error for %s: %s", body.project_id, exc)
            raise HTTPException(503, "Database temporarily unavailable — please retry in a few seconds")
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.error("Intake unexpected error for %s: %s", body.project_id, exc)
            raise
        if not committed:
            raise HTTPException(409, message)

    return IntakeResponse(
        project_id=proposal.project_id,
        client_name=proposal.client_name,
        client_email=proposal.client_email,
        market=proposal.market,
        connection_type=proposal.connection_type,
        target_go_live=proposal.target_go_live,
        notes=proposal.notes,
        source=proposal.source,
        confidence=proposal.confidence,
        needs_confirmation=proposal.needs_confirmation,
        extraction_warnings=proposal.extraction_warnings,
        committed=committed,
        message=message,
    )


def _build_proposal(body: IntakeRequest) -> IntakeProposal | None:
    source = body.source or "web_form"

    if body.free_text:
        extracted = body.extracted_fields.model_dump(exclude_none=True) if body.extracted_fields else None
        llm_extract_fn = (lambda _: extracted) if extracted else None
        return create_intake_from_text(body.free_text, llm_extract_fn=llm_extract_fn)

    if not body.project_id or not body.client_name:
        return None

    return create_intake_from_structured(
        project_id=body.project_id,
        client_name=body.client_name,
        client_email=body.client_email,
        market=body.market,
        connection_type=body.connection_type,
        target_go_live=body.target_go_live,
        source=source,
    )
