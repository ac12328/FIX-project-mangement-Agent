"""Public migration decisions fail closed without an operator-reviewed catalog."""
from __future__ import annotations
from pathlib import Path


def build_reviewed_manifest(wiki_dir: Path) -> dict:
    """Private reviewed migration decisions are intentionally not distributed."""
    raise RuntimeError(
        "No reviewed migration catalog is bundled. Author and review a local "
        "manifest; do not infer approval or project destinations from this demo."
    )
