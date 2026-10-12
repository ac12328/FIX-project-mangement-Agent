"""Run the deterministic product suite with a pre-pytest Python egress guard.

This is not an OS firewall. Node children inherit a loopback guard, but
other non-Python children, stripped environments, and native transports still
require runner isolation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "tests" / "network_bootstrap"


def guarded_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    # Both consumers must use the cache prepared before socket isolation.
    cache = env.get("CUSTOM_TIKTOKEN_CACHE_DIR") or env.get("TIKTOKEN_CACHE_DIR")
    cache = cache or str(ROOT / ".venv" / "test-tokenizers")
    cache = str(Path(cache).resolve())
    env["TIKTOKEN_CACHE_DIR"] = cache
    env["CUSTOM_TIKTOKEN_CACHE_DIR"] = cache
    prior = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(BOOTSTRAP), str(ROOT), *([prior] if prior else [])]
    )
    env["AGENTFIX_DEMO_TEST_NETWORK_GUARD"] = "1"
    # Node children inherit the loopback-only guard; do not trust caller options.
    env["NODE_OPTIONS"] = f'--require="{(BOOTSTRAP / "node_loopback_guard.cjs").as_posix()}"'
    return env


def main(argv: list[str] | None = None) -> int:
    env = guarded_environment()
    # sitecustomize exceptions can be ignored by Python startup. Validate the
    # installed guard before allowing pytest or any plugin to import.
    preflight = subprocess.run(
        [sys.executable, "-c", "import socket\n"
         "if 'NetworkTransportGuard.install' not in socket.getaddrinfo.__qualname__:\n"
         "    raise RuntimeError('early network guard missing')\n"],
        cwd=ROOT, env=env, check=False,
    )
    if preflight.returncode:
        return preflight.returncode
    if shutil.which("node"):
        node_preflight = subprocess.run(
            ["node", "-e", "const dns = require('node:dns'); "
             "if (!dns.lookup.toString().includes('refuse(host)')) "
             "throw new Error('early Node guard missing'); "
             "try { dns.lookup('example.invalid', () => {}); "
             "throw new Error('Node guard did not deny egress'); } "
             "catch (error) { if (!/Isolated test blocked Node egress/.test(error.message)) "
             "throw error; }"],
            cwd=ROOT, env=env, check=False,
        )
        if node_preflight.returncode:
            return node_preflight.returncode
    return subprocess.run(
        [sys.executable, "-m", "pytest", *(sys.argv[1:] if argv is None else argv)],
        cwd=ROOT, env=env, check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
