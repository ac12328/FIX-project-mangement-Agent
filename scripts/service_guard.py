"""Conservative Windows service ownership and passive readiness checks.

Only listeners descended from this workspace launchers are eligible to stop.
An unknown port owner is an error, never a reason to kill a process.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

if __package__:
    from .service_shutdown import open_cleanup_confirmation, open_process_exit_handle, request_shutdown
else:  # Executed directly by services.bat.
    from service_shutdown import open_cleanup_confirmation, open_process_exit_handle, request_shutdown


_PORT_LAUNCHERS = {8000: "_start_bot.cmd", 3000: "_start_ui.cmd"}


class OwnershipError(RuntimeError):
    """A port owner cannot be proved to belong to this workspace."""


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    parent_pid: int
    name: str
    command_line: str


def _powershell_json(command: str):
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); '
         "$ErrorActionPreference='Stop'; " + command],
        capture_output=True, timeout=20, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(result.stdout.decode("utf-8"))


def listening_pids(port: int) -> set[int]:
    if type(port) is not int or not 0 < port < 65536:
        raise ValueError("Invalid TCP port")
    # Query only the requested port. An unavailable provider is not an empty port.
    data = _powershell_json(
        f"try {{$rows=@(Get-NetTCPConnection -State Listen -LocalPort {port} "
        "-ErrorAction Stop)} catch { "
        "if ($_.FullyQualifiedErrorId -notlike 'CmdletizationQuery_NotFound*') {throw}; "
        "$rows=@() }; "
        "ConvertTo-Json -InputObject @($rows | Select-Object -ExpandProperty OwningProcess) -Compress"
    )
    if not isinstance(data, list) or any(type(pid) is not int or pid <= 0 for pid in data):
        raise ValueError("Invalid listener inventory")
    return set(data)


def process_snapshot(pids: set[int], root: Path) -> dict[int, ProcessInfo]:
    """Read only the requested PIDs and their bounded launcher ancestry."""
    if any(type(pid) is not int or pid <= 0 for pid in pids):
        raise ValueError("Invalid process inventory seed")
    if not pids:
        return {}
    markers = "|".join(re.escape(str((root / name).resolve()))
                       for name in sorted(set(_PORT_LAUNCHERS.values())))
    pattern = r'(?:^|\s)/[kc]\s+"?(?:' + markers + r')(?=$|[\s"&])'
    pattern = pattern.replace("'", "''")
    seeds = ",".join(map(str, sorted(pids)))
    # Stop at the controller or its parent if no launcher is found. Never walk
    # past our calling context or enumerate siblings/children of an unknown PID.
    boundaries = f"{os.getpid()},{os.getppid()}"
    data = _powershell_json(
        f"$seeds=@({seeds}); $boundaries=@({boundaries}); $pattern='{pattern}'; "
        r"""
        $rows=@{};
        foreach ($seed in $seeds) {
          $current=[int]$seed; $seen=@{};
          for ($depth=0; $depth -lt 16; $depth++) {
            if ($current -le 0 -or $seen.ContainsKey($current)) {break};
            $seen[$current]=$true;
            if ($rows.ContainsKey($current)) {$row=$rows[$current]} else {
              $item=Get-CimInstance Win32_Process -Filter ('ProcessId='+$current) -ErrorAction Stop;
              if ($null -eq $item) {break};
              if ([int]$item.ProcessId -ne $current) {throw 'Unexpected process inventory PID'};
              $row=[PSCustomObject]@{
                ProcessId=[int]$item.ProcessId; ParentProcessId=[int]$item.ParentProcessId;
                Name=[string]$item.Name; CommandLine=[string]$item.CommandLine;
                CreationFileTime=[string]$item.CreationDate.ToUniversalTime().ToFileTimeUtc()
              };
              $rows[$current]=$row;
            }
            if (($row.Name -ieq 'cmd.exe' -and $row.CommandLine -imatch $pattern) -or
                $current -in $boundaries) {break};
            $current=[int]$row.ParentProcessId;
          }
        }
        ConvertTo-Json -InputObject @($rows.Values) -Compress
        """
    )
    if not isinstance(data, list):
        raise ValueError("Invalid process inventory")
    return {
        int(row["ProcessId"]): ProcessInfo(
            pid=int(row["ProcessId"]), parent_pid=int(row.get("ParentProcessId") or 0),
            name=str(row.get("Name") or ""), command_line=str(row.get("CommandLine") or ""),
        )
        for row in data if isinstance(row, dict) and row.get("ProcessId") is not None
    }


def _launcher_pid(pid: int, port: int, root: Path,
                  processes: dict[int, ProcessInfo]) -> int | None:
    marker = str((root / _PORT_LAUNCHERS[port]).resolve()).casefold()
    launcher_pattern = re.compile(r"(?:^|\s)/[kc]\s+\"?" + re.escape(marker) + r"(?=$|[\s\"&])", re.I)
    current = pid
    visited: set[int] = set()
    for _ in range(16):
        if current <= 0 or current in visited:
            return None
        visited.add(current)
        info = processes.get(current)
        if info is None:
            return None
        # The exact absolute launcher path and its cmd.exe process must agree.
        # A matching basename in another checkout does not confer ownership.
        if info.name.casefold() == "cmd.exe" and launcher_pattern.search(info.command_line.casefold()):
            return current
        current = info.parent_pid
    return None


def owned_launchers(port: int, root: Path) -> dict[int, int]:
    if port not in _PORT_LAUNCHERS:
        raise ValueError(f"Unmanaged service port: {port}")
    pids = listening_pids(port)
    processes = process_snapshot(pids, root) if pids else {}
    owners = {}
    for pid in pids:
        launcher = _launcher_pid(pid, port, root, processes)
        if launcher is None:
            raise OwnershipError(f"Port {port} belongs to unverified PID {pid}; leaving it untouched")
        owners[pid] = launcher
    return owners


def verify_workspace_ports(root: Path) -> None:
    for port in _PORT_LAUNCHERS:
        owned_launchers(port, root)


def stop_port(
    port: int, root: Path, *, timeout: float = 90, force_after_timeout: bool = False,
) -> None:
    if port != 8000 and _PORT_LAUNCHERS.get(port) != "_start_bot.cmd":
        _stop_ui_port(port, root, timeout=timeout)
        return
    owners = owned_launchers(port, root)
    deadline = time.monotonic() + timeout
    forced = False
    for pid, launcher in owners.items():
        # Retain both kernel objects across process exit; neither PID reuse nor
        # port release constitutes a successful resource cleanup.
        with open_process_exit_handle(pid, allow_terminate=force_after_timeout) as process:
            if owned_launchers(port, root).get(pid) != launcher:
                raise OwnershipError(f"Port {port} changed owner during shutdown")
            try:
                confirmation = open_cleanup_confirmation(root, pid)
            except OSError as exc:
                raise OwnershipError(
                    f"Owned backend PID {pid} has no cleanup confirmation channel; leaving it running"
                ) from exc
            with confirmation:
                if confirmation.wait(0):
                    raise OwnershipError(f"Owned backend PID {pid} has a stale cleanup confirmation")
                try:
                    request_shutdown(root, pid)
                except OSError as exc:
                    raise OwnershipError(
                        f"Owned backend PID {pid} has no cooperative shutdown channel; leaving it running"
                    ) from exc
                if process.wait(max(0, deadline - time.monotonic())):
                    if not confirmation.wait(0):
                        raise OwnershipError(
                            f"Owned backend PID {pid} exited but cleanup was not confirmed"
                        )
                    continue
                if not force_after_timeout:
                    raise OwnershipError(f"Owned backend PID {pid} did not exit after shutdown request")
                if owned_launchers(port, root).get(pid) != launcher:
                    raise OwnershipError(f"Port {port} changed owner during shutdown")
                # Explicit operator opt-in only. This path is never a graceful result.
                if process.wait(0):
                    if not confirmation.wait(0):
                        raise OwnershipError(
                            f"Owned backend PID {pid} exited but cleanup was not confirmed"
                        )
                    continue
                process.terminate()  # Pinned handle, never a potentially recycled PID.
                forced = True
                if not process.wait(10):
                    raise OwnershipError(f"Owned backend PID {pid} survived forced shutdown")
    remaining = owned_launchers(port, root)
    if remaining:
        raise OwnershipError(f"Port {port} remains occupied after backend shutdown")
    if listening_pids(port):
        raise OwnershipError(f"Port {port} changed owner during shutdown")
    if forced:
        raise OwnershipError("Owned backend forcibly terminated; graceful cleanup was not confirmed")


def _stop_ui_port(port: int, root: Path, *, timeout: float) -> None:
    owners = owned_launchers(port, root)
    # Retain launcher process objects across both taskkill calls. A verified PID
    # must not be reused for another process between ownership checks and kill.
    with ExitStack() as handles:
        pinned = {}
        for launcher in set(owners.values()):
            try:
                pinned[launcher] = handles.enter_context(open_process_exit_handle(launcher))
            except OSError as exc:
                raise OwnershipError(
                    f"Owned UI launcher PID {launcher} cannot be pinned; leaving it running"
                ) from exc
        for pid, launcher in owners.items():
            if owned_launchers(port, root).get(pid) != launcher:
                raise OwnershipError(f"Port {port} changed owner during shutdown")
            if pinned[launcher].wait(0):
                raise OwnershipError(f"Owned UI launcher PID {launcher} exited before shutdown")
            # This output is unused. Keep localized Windows bytes undecoded.
            subprocess.run(
                ["taskkill", "/PID", str(launcher), "/T"],
                capture_output=True, timeout=10, check=False,
            )
        deadline = time.monotonic() + timeout
        grace_deadline = min(deadline, time.monotonic() + 2)
        while listening_pids(port) and time.monotonic() < grace_deadline:
            time.sleep(0.5)

        remaining = owned_launchers(port, root)
        if any(owners.get(pid) != launcher for pid, launcher in remaining.items()):
            raise OwnershipError(f"Port {port} changed owner during shutdown")
        for launcher in set(remaining.values()):
            if pinned[launcher].wait(0):
                raise OwnershipError(f"Owned UI launcher PID {launcher} exited while its listener remained")
            # Escalate only while the same pinned launcher still owns a listener.
            subprocess.run(
                ["taskkill", "/F", "/PID", str(launcher), "/T"],
                capture_output=True, timeout=10, check=False,
            )
        while listening_pids(port):
            if time.monotonic() >= deadline:
                raise OwnershipError(f"Port {port} is still occupied after owned-process shutdown")
            time.sleep(0.5)
        # Port release alone does not prove that the verified launcher has
        # exited. In particular, cmd /k can remain at a batch-file pause after
        # its child exits. Wait on the pinned process object, not its reusable PID.
        for launcher, process in pinned.items():
            if not process.wait(max(0, deadline - time.monotonic())):
                raise OwnershipError(
                    f"Owned UI launcher PID {launcher} remains running after port {port} closed"
                )

def passive_backend_ready(port: int = 8000) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            if response.status != 200:
                return False
            payload = json.load(response)
    except (OSError, ValueError, TimeoutError):
        return False
    if not isinstance(payload, dict) or payload.get("db_ok") is False:
        return False
    return payload.get("status") == "ok" or (
        payload.get("status") == "llm_unavailable" and payload.get("db_ok") is True
    )


def wait_backend_ready(root: Path, *, timeout: float, port: int = 8000) -> None:
    deadline = time.monotonic() + timeout
    while True:
        owners = owned_launchers(port, root)
        if owners:
            # Keep both listeners and their launchers alive across the HTTP
            # probe. Equal PID mappings alone cannot detect PID reuse.
            with ExitStack() as handles:
                pinned = {}
                for pid in set(owners) | set(owners.values()):
                    try:
                        pinned[pid] = handles.enter_context(open_process_exit_handle(pid))
                    except OSError as exc:
                        raise OwnershipError(
                            f"Port {port} owner PID {pid} cannot be pinned during backend readiness"
                        ) from exc
                if any(process.wait(0) for process in pinned.values()):
                    raise OwnershipError(f"Port {port} changed owner during backend readiness")
                if passive_backend_ready(port):
                    if owned_launchers(port, root) != owners or any(
                        process.wait(0) for process in pinned.values()
                    ):
                        raise OwnershipError(f"Port {port} changed owner during backend readiness")
                    return
        if time.monotonic() >= deadline:
            raise OwnershipError("Backend did not pass owned-process and passive DB readiness checks")
        time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "stop", "ready"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, choices=tuple(_PORT_LAUNCHERS))
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument(
        "--force-after-timeout", action="store_true",
        help="Opt in to terminating an owned backend after cooperative shutdown times out",
    )
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        if args.action == "verify":
            verify_workspace_ports(root)
        elif args.action == "stop":
            if args.port is None:
                parser.error("stop requires --port")
            stop_port(
                args.port, root, timeout=args.timeout,
                force_after_timeout=args.force_after_timeout,
            )
        else:
            wait_backend_ready(root, timeout=args.timeout, port=args.port or 8000)
    except (OwnershipError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"[service guard] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
