"""Runtime capability status for optional application features."""

from fastapi import APIRouter

from config import defaults

router = APIRouter(tags=["capabilities"])


@router.get("/capabilities")
def get_capabilities() -> dict:
    """Return effective runtime feature availability."""
    return {
        "draft_workspace": {
            "enabled": defaults.DRAFT_WORKSPACE_ENABLED,
        },
        "personal_assistant": {
            "enabled": defaults.PERSONAL_ASSISTANT_ENABLED,
            "capabilities": {
                "universal_email": defaults.PERSONAL_ASSISTANT_UNIVERSAL_EMAIL_ENABLED,
                "work_items": defaults.PERSONAL_ASSISTANT_WORK_ITEMS_ENABLED,
                "reminders": defaults.PERSONAL_ASSISTANT_REMINDERS_ENABLED,
                "personal_brief": defaults.PERSONAL_ASSISTANT_PERSONAL_BRIEF_ENABLED,
                "inbox_intelligence": defaults.PERSONAL_ASSISTANT_INBOX_INTELLIGENCE_ENABLED,
                "calendar_read": defaults.PERSONAL_ASSISTANT_CALENDAR_READ_ENABLED,
                "meeting_prep": defaults.PERSONAL_ASSISTANT_MEETING_PREP_ENABLED,
                "meeting_ingestion": defaults.PERSONAL_ASSISTANT_MEETING_INGESTION_ENABLED,
                "calendar_write": defaults.PERSONAL_ASSISTANT_CALENDAR_WRITE_ENABLED,
            },
        }
    }
