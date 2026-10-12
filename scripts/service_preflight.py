"""Credential-safe preflight for the local Bot/API service."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.runtime_environment import hydrate_persisted_runtime_environment


def main() -> int:
    hydrated = hydrate_persisted_runtime_environment()

    from config.settings import validate_config

    errors = validate_config(include_load_error=True)
    if errors:
        print("[ERROR] Bot+API preflight failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        print(
            "Existing services were not stopped. Fix the reported configuration and retry.",
            file=sys.stderr,
        )
        return 1

    suffix = f" ({len(hydrated)} persisted variable(s) hydrated)" if hydrated else ""
    print(f"Bot+API preflight OK{suffix}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
