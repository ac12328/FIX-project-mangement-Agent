"""Project intake request/response schemas."""

from pydantic import BaseModel, Field


class IntakeExtractedFields(BaseModel):
    project_id: str | None = None
    client_name: str | None = None
    client_email: str | None = None
    market: str | None = None
    connection_type: str | None = None
    target_go_live: str | None = None
    notes: str | None = None
    confidence: float | None = None


class IntakeRequest(BaseModel):
    source: str = "web_form"
    project_id: str | None = None
    client_name: str | None = None
    client_email: str | None = None
    market: str | None = None
    connection_type: str = "FIX 4.2"
    target_go_live: str | None = None
    free_text: str | None = None
    extracted_fields: IntakeExtractedFields | None = None
    confirm: bool = False


class IntakeResponse(BaseModel):
    project_id: str
    client_name: str
    client_email: str | None = None
    market: str | None = None
    connection_type: str = "FIX 4.2"
    target_go_live: str | None = None
    notes: str | None = None
    source: str
    confidence: float
    needs_confirmation: bool = True
    extraction_warnings: list[str] = Field(default_factory=list)
    committed: bool = False
    message: str
