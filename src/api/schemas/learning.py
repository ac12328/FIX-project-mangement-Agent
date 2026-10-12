"""Pydantic models for learning endpoints."""

from pydantic import BaseModel


class SourceEmail(BaseModel):
    email_id: str
    sender_email: str
    subject: str
    direction: str
    processed_at: str


class KnowledgeEntry(BaseModel):
    entry_id: str
    created_at: str
    thread_id: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    ticket_id: str | None = None
    knowledge_type: str
    title: str
    content: str
    confidence: str
    visibility: str
    wiki_path: str | None = None
    source_email_count: int = 0


class KnowledgeEntryDetail(KnowledgeEntry):
    source_emails: list[SourceEmail] = []


class MiningReportDict(BaseModel):
    run_id: str
    started_at: str
    threads_processed: int
    entries_created: int
    wiki_pages_written: int
    errors: list[str] = []


class LearningStats(BaseModel):
    total_entries: int
    by_type: dict[str, int]
    by_visibility: dict[str, int]
    by_confidence: dict[str, int]
    last_run: MiningReportDict | None = None
