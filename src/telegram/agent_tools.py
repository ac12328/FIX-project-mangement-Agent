"""
Agent tools helpers for Telegram chat updates.

After the confirmation flow removal (2026-04-06), the staging and confirmed-update
helpers (_stage_update_confirm, _run_confirmed_update) were deleted.  The
update_project tool now calls safe_write_project() directly via chat_handlers.py.

This file is intentionally empty.  It is kept to avoid breaking any stale imports
that may exist in tests or migration scripts — they will get a clean ImportError
with a traceable location.
"""
