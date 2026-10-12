"""Opt-in retention for archived evaluation databases and isolated UI builds."""
from __future__ import annotations

import json
import os
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from src.utils.housekeeping import Housekeeper, HousekeepingSafetyError, Record

EMAIL_PREFIX = "email-project-truth-abr"
TERMINAL_STATUSES = {"completed", "failed"}


def process_commands() -> list[str]:
    """Inspect only likely Python/Node runners; fail closed on provider errors."""
    if os.name != "nt":
        raise HousekeepingSafetyError("Storage cleanup requires an explicit process probe on this OS")
    command = (
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
        "$ErrorActionPreference='Stop'; "
        "$rows=@(Get-CimInstance Win32_Process -Filter "
        "\"Name='node.exe' OR Name='python.exe' OR Name='pythonw.exe' "
        "OR Name='py.exe' OR Name='uv.exe' OR Name='ruff.exe' OR Name='pytest.exe'\"); "
        "ConvertTo-Json -InputObject @($rows | Select-Object Name,CommandLine) -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, check=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        rows = json.loads(result.stdout.decode("utf-8-sig"))
        if not isinstance(rows, list) or any(not row.get("CommandLine") for row in rows):
            raise ValueError("Incomplete process inventory")
        return [str(row["Name"]) + " " + str(row["CommandLine"]) for row in rows]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise HousekeepingSafetyError(f"Cannot prove storage targets inactive: {exc}") from exc


class StorageHousekeeper(Housekeeper):
    """Never delete reports, runtime data, worktrees, or the default .next tree."""

    def __init__(
        self, repo_root: Path | str, *, scope: str = "storage",
        min_age_hours: float = 24, keep_runs: int = 2, retention_days: float = 7,
        now: datetime | None = None, commands_probe: Callable[[], list[str]] | None = None,
    ) -> None:
        if scope != "storage" or type(keep_runs) is not int or keep_runs < 1:
            raise HousekeepingSafetyError("Storage scope requires keep_runs >= 1")
        if not 0 <= retention_days < float("inf"):
            raise HousekeepingSafetyError("retention_days must be finite and non-negative")
        super().__init__(repo_root, scope="artifacts", min_age_hours=min_age_hours, now=now)
        self.scope = "storage"
        self.keep_runs = keep_runs
        self.retention_days = retention_days
        self.cutoff = min(self.cutoff, self.now - timedelta(days=retention_days))
        self._commands_probe = commands_probe or process_commands
        self._commands: list[str] = []
        self._fingerprints: dict[str, tuple] = {}
        self._manifests: dict[str, tuple] = {}
        self._eligible_bundles: set[str] = set()
        self._apply_eligible: set[str] | None = None

    @staticmethod
    def _fingerprint(path: Path) -> tuple:
        value = path.stat()
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    def _read_json(self, path: Path) -> dict:
        if self._reparse_problem(path):
            raise ValueError("Manifest contains a reparse point")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Manifest is not an object")
        return value

    def _run_info(self, bundle: Path, *, require_payload: bool = False) -> tuple:
        if self._reparse_problem(bundle):
            raise ValueError("Bundle contains a reparse point")
        aggregate = self._read_json(bundle / "aggregate.json")
        progress = self._read_json(bundle / "progress.json")
        cohort = self._read_json(bundle / "cohort.json")
        report = bundle / "report.md"
        if not report.is_file() or self._reparse_problem(report):
            raise ValueError("Missing safe human-readable report")
        cases = progress.get("cases")
        expected = aggregate.get("expected_cases")
        completed = aggregate.get("completed_cases")
        failed = aggregate.get("failed_cases")
        if (type(expected) is not int or expected < 1
                or type(completed) is not int or type(failed) is not int
                or completed < 0 or failed < 0 or completed + failed != expected
                or not isinstance(cases, dict) or len(cases) != expected
                or cohort.get("research_only") is not True):
            raise ValueError("Run is incomplete or not research-only")
        statuses = [case.get("status") for case in cases.values() if isinstance(case, dict)]
        if (len(statuses) != expected or any(s not in TERMINAL_STATUSES for s in statuses)
                or statuses.count("completed") != completed or statuses.count("failed") != failed
                or sorted(cohort.get("selected_case_ids", [])) != sorted(cases)):
            raise ValueError("Terminal case manifests disagree")
        if require_payload:
            databases = [bundle / "research-clone.db"]
            databases.extend(bundle / "case-databases" / (case_id + ".db") for case_id in cases)
            if any(not db.is_file() or db.stat().st_size == 0 or self._reparse_problem(db)
                   for db in databases):
                raise ValueError("Missing safe complete database payload")
        stamp = datetime.fromisoformat(str(aggregate["generated_at"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Run timestamp lacks a timezone")
        group = ("gemma4" if "gemma4" in bundle.name.casefold() else "standard", tuple(sorted(cases)))
        fingerprint = tuple(self._fingerprint(bundle / name) for name in (
            "aggregate.json", "progress.json", "cohort.json", "report.md",
        ))
        return group, stamp.astimezone(timezone.utc).timestamp(), failed == 0, fingerprint

    def _has_keep_marker(self, path: Path) -> bool:
        current = path if path.is_dir() else path.parent
        while True:
            marker = current / ".housekeeping-keep"
            if marker.exists() or marker.is_symlink():
                return True
            if current == self.root:
                return False
            current = current.parent

    def _tree_inventory(self, root: Path) -> tuple[list[str], int, float, str | None]:
        inventory, size, newest, problem = super()._tree_inventory(root)
        if not problem and any(Path(item).name == ".housekeeping-keep" for item in inventory):
            problem = "Tree contains a keep marker"
        return inventory, size, newest, problem

    def _is_running(self, path: Path, *, build: bool = False) -> bool:
        if build:
            # A custom NEXT_DIST_DIR may live only in the environment: do not guess.
            return any(cmd.casefold().startswith("node.exe ") for cmd in self._commands)
        relative = self._rel(path).replace("\\", "/").casefold()
        absolute = str(path).replace("\\", "/").casefold()
        for command in self._commands:
            normalized = command.replace("\\", "/").casefold()
            if relative in normalized or absolute in normalized:
                return True
            if "run_email_project_truth_abr" in normalized:
                return True
        return False

    def _reset_scan(self) -> None:
        self.candidates.clear()
        self.skipped.clear()
        self.protected.clear()
        self.deleted.clear()
        self.errors.clear()
        self._candidate_paths.clear()
        self._fingerprints.clear()
        self._manifests.clear()
        self._eligible_bundles.clear()
        self._apply_eligible = None
        self._commands = self._commands_probe()

    def _finish_scan(self) -> list:
        for item in self.candidates:
            self._fingerprints[item.path] = self._fingerprint(self.root / item.path)
        self.candidates.sort(key=lambda item: item.path.casefold())
        return list(self.candidates)

    def scan(self) -> list:
        self._reset_scan()
        groups: dict[tuple, list[tuple]] = defaultdict(list)
        for bundle in sorted((self.root / "artifacts").glob(EMAIL_PREFIX + "*")):
            if not bundle.is_dir():
                continue
            try:
                group, stamp, success, fingerprint = self._run_info(bundle, require_payload=True)
                groups[group].append((stamp, bundle.name, bundle, success, fingerprint))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.protected.append(Record(self._rel(bundle), f"Unfinished/invalid evidence: {exc}"))
        for runs in groups.values():
            ordered = sorted(runs, reverse=True, key=lambda item: (item[0], item[1]))
            keepers = {item[2] for item in ordered[:self.keep_runs]}
            successful = [item for item in ordered if item[3]]
            if successful:
                keepers.add(successful[0][2])
            for stamp, _, bundle, _, fingerprint in ordered:
                relative = self._rel(bundle)
                if (stamp > self.cutoff.timestamp()
                        or any(item[3] / 1_000_000_000 > self.cutoff.timestamp() for item in fingerprint)):
                    self.protected.append(Record(relative, "Evidence newer than grace period"))
                    continue
                if bundle in keepers or self._has_keep_marker(bundle) or self._is_running(bundle):
                    self.protected.append(Record(relative, "Retained evidence, keep marker, or active runner"))
                    continue
                self._eligible_bundles.add(relative)
                self._manifests[relative] = fingerprint
                payloads = [bundle / "research-clone.db"]
                payloads.extend(sorted((bundle / "case-databases").glob("*.db")))
                for database in payloads:
                    for suffix in ("", "-wal", "-shm"):
                        payload = Path(str(database) + suffix)
                        if self._has_keep_marker(payload):
                            self.protected.append(Record(self._rel(payload), "Payload keep marker"))
                        else:
                            self._queue_file(payload, "storage", "Superseded evaluation database; reports retained")
        for build in sorted((self.root / "dashboard").glob(".next-*")):
            if self._has_keep_marker(build) or self._is_running(build, build=True):
                self.protected.append(Record(self._rel(build), "Keep marker or Node runner may use this build"))
            else:
                self._queue_tree(build, "storage", "Expired isolated Next.js build")
        return self._finish_scan()

    def _validate_common_candidate_now(self, path: Path, kind: str) -> str | None:
        problem = super()._validate_candidate_now(path, kind)
        if problem:
            return problem
        relative = self._rel(path)
        if self._apply_eligible is not None and relative not in self._apply_eligible:
            return "Storage retention/protection changed after discovery"
        if self._fingerprints.get(relative) != self._fingerprint(path):
            return "Storage candidate changed after discovery"
        if self._has_keep_marker(path):
            return "Keep marker appeared after discovery"
        return None

    def _validate_candidate_now(self, path: Path, kind: str) -> str | None:
        problem = self._validate_common_candidate_now(path, kind)
        if problem:
            return problem
        relative = self._rel(path)
        if kind == "tree":
            if path.parent != self.root / "dashboard" or not path.name.startswith(".next-"):
                return "Not an allowlisted isolated build"
            if self._is_running(path, build=True):
                return "Node runner may use this build"
        else:
            bundle = self.root / "/".join(relative.split("/")[:2])
            key = self._rel(bundle)
            if key not in self._eligible_bundles or self._is_running(bundle):
                return "Evaluation bundle is not inactive and superseded"
            try:
                if self._run_info(bundle)[3] != self._manifests[key]:
                    return "Evaluation evidence changed after discovery"
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return f"Evaluation evidence no longer valid: {exc}"
        return None

    def _fresh_housekeeper(self) -> StorageHousekeeper:
        return StorageHousekeeper(
            self.root, min_age_hours=self.min_age_hours, keep_runs=self.keep_runs,
            retention_days=self.retention_days, now=self.now,
            commands_probe=lambda: self._commands,
        )

    def apply(self) -> None:
        self._commands = self._commands_probe()
        self.tracked = self._load_tracked_paths()
        fresh = self._fresh_housekeeper()
        self._apply_eligible = {item.path for item in fresh.scan()}
        super().apply()

    def report_dict(self, *, applied: bool) -> dict:
        report = super().report_dict(applied=applied)
        report["storage_policy"] = {
            "keep_runs_per_route_and_case_set": self.keep_runs,
            "retention_days": self.retention_days,
            "preserve_latest_successful_run": True,
            "worktrees": "Review only; remove separately with verified git worktree remove",
            "default_next_and_runtime_data": "Never eligible",
        }
        return report
