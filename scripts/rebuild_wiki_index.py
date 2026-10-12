"""Rebuild wiki/index.md from all content pages."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.wiki.index_sync import list_wiki_pages, rebuild_index  # noqa: E402


def main() -> int:
    wiki_dir = ROOT / "wiki"
    content = rebuild_index(wiki_dir)
    pages = list_wiki_pages(wiki_dir)
    print(f"Rebuilt index.md with {len(pages)} pages ({len(content)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
