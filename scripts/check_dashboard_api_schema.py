"""Fail when the committed Dashboard OpenAPI types differ from the current API."""

from __future__ import annotations

import difflib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
COMMITTED = DASHBOARD / "src" / "lib" / "generated" / "api-schema.ts"


def main() -> int:
    npx = shutil.which("npx")
    if npx is None:
        print("npx is required to check Dashboard API schema drift", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="agentfix-openapi-") as directory:
        openapi = Path(directory) / "openapi.json"
        generated = Path(directory) / "api-schema.ts"
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "export_dashboard_openapi.py"), str(openapi)],
            cwd=ROOT, check=True,
        )
        subprocess.run(
            [npx, "--no-install", "openapi-typescript", str(openapi), "-o", str(generated)],
            cwd=DASHBOARD, check=True,
        )
        actual = generated.read_text(encoding="utf-8")

    expected = COMMITTED.read_text(encoding="utf-8")
    if actual == expected:
        print("Dashboard OpenAPI types are current")
        return 0

    diff = difflib.unified_diff(
        expected.splitlines(), actual.splitlines(),
        fromfile=str(COMMITTED.relative_to(ROOT)), tofile="generated", lineterm="",
    )
    for line in list(diff)[:100]:
        print(line)
    print("Regenerate with: cd dashboard; npm run api:generate", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
