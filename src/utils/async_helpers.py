"""Shared async utilities — extracted from card_push.py and review_push.py."""

import asyncio


def has_running_loop() -> bool:
    """Return True if there is a currently running asyncio event loop."""
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False
