"""
Shared prompt loader — loads .md prompt files from the prompts/ directory.
All prompt files are cached after first read (per process lifetime).
"""

from functools import lru_cache
from pathlib import Path

from config.settings import BASE_DIR

_PROMPTS_DIR = BASE_DIR / "prompts"


@lru_cache(maxsize=32)
def load_prompt(name: str) -> str:
    """
    Load a prompt file by relative path under prompts/.

    Examples:
        load_prompt("routing/email_classifier.md")
        load_prompt("email_agent.md")
    """
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")
