"""Conservative, allowlisted housekeeping for generated repository data."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

SCOPES = {"all", "caches", "artifacts", "runtime"}
KEEP_MARKER = ".housekeeping-keep"
PROTECTED_ARTIFACT_ROOTS = {
    "ai-workspace-eval",
    "observability",
    "observability-evidence-fixtures",
    "housekeeping",
}

class HousekeepingSafetyError(RuntimeError):
    pass

@dataclass(frozen=True)
class Candidate:
    path: str
    scope: str
    kind: str
    size_bytes: int
    reason: str

@dataclass(frozen=True)
class Record:
    path: str
    reason: str

@dataclass(frozen=True)
class ArtifactFamily:
    parent: str
    pattern: str
    group: str

ARTIFACT_FAMILIES = (
    ArtifactFamily("artifacts/grounding-repair", "*", "grounding-repair"),
    ArtifactFamily("artifacts/ai-workspace-latency", "*", "latency"),
    ArtifactFamily("artifacts/observability-runtime-smoke", "*", "observability-runtime"),
    ArtifactFamily("artifacts", "tool-centric-query-v2*", "query-v2"),
    ArtifactFamily("artifacts", "tool-centric-shadow-router*", "shadow-router"),
    ArtifactFamily("artifacts", "tool-centric-unified-serving*", "unified-serving"),
)

class Housekeeper:

    def __init__(
        self,
        repo_root: Path | str,
        *,
        scope: str = "all",
        min_age_hours: float = 24,
        now: datetime | None = None,
        pid_is_live: Callable[[int], bool] | None = None,
    ) -> None:
        self.root = Path(repo_root).resolve()
        if scope not in SCOPES:
            raise HousekeepingSafetyError(f"Unsupported scope: {scope}")
        if min_age_hours < 0:
            raise HousekeepingSafetyError("min_age_hours must be non-negative")
        if not (self.root / ".git").exists() or not (self.root / "CLAUDE.md").is_file():
            raise HousekeepingSafetyError(f"Not an AgentFIXBPRO repository root: {self.root}")
        self.scope = scope
        self.min_age_hours = min_age_hours
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.cutoff = self.now - timedelta(hours=min_age_hours)
        self._pid_is_live = pid_is_live or _pid_is_live
        self.tracked = self._load_tracked_paths()
        self.candidates: list[Candidate] = []
        self.skipped: list[Record] = []
        self.protected: list[Record] = []
        self.deleted: list[Candidate] = []
        self.errors: list[Record] = []
        self._candidate_paths: set[str] = set()

    def scan(self) -> list[Candidate]:
        self.candidates.clear()
        self.skipped.clear()
        self.protected.clear()
        self.deleted.clear()
        self.errors.clear()
        self._candidate_paths.clear()
        if self.scope in {"all", "caches"}:
            self._scan_caches()
        if self.scope in {"all", "artifacts"}:
            self._scan_artifacts()
        if self.scope in {"all", "runtime"}:
            self._scan_runtime()
        self.candidates.sort(key=lambda item: item.path.casefold())
        return list(self.candidates)

    def apply(self) -> None:
        for candidate in list(self.candidates):
            path = self.root / candidate.path
            try:
                if not path.exists():
                    continue
                problem = self._validate_candidate_now(path, candidate.kind)
                if problem:
                    raise HousekeepingSafetyError(problem)
                if candidate.kind == "tree":
                    self._remove_tree(path)
                else:
                    path.unlink()
                self.deleted.append(candidate)
            except Exception as exc:  # continue so the audit contains all failures
                self.errors.append(Record(candidate.path, str(exc)))

    def report_dict(self, *, applied: bool) -> dict:
        return {
            "schema_version": 1,
            "mode": "apply" if applied else "dry-run",
            "repo_root": str(self.root),
            "scope": self.scope,
            "min_age_hours": self.min_age_hours,
            "started_at_utc": self.now.isoformat(),
            "cutoff_utc": self.cutoff.isoformat(),
            "candidate_count": len(self.candidates),
            "candidate_bytes": sum(item.size_bytes for item in self.candidates),
            "deleted_count": len(self.deleted),
            "deleted_bytes": sum(item.size_bytes for item in self.deleted),
            "candidates": [asdict(item) for item in self.candidates],
            "deleted": [asdict(item) for item in self.deleted],
            "protected": [asdict(item) for item in self.protected],
            "skipped": [asdict(item) for item in self.skipped],
            "errors": [asdict(item) for item in self.errors],
        }

    def write_report(self, *, applied: bool) -> Path:
        report_dir = self.root / "artifacts" / "housekeeping"
        report_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.now.strftime("%Y%m%dT%H%M%S.%fZ")
        mode = "apply" if applied else "dry-run"
        path = report_dir / f"{stamp}-{mode}.json"
        path.write_text(json.dumps(self.report_dict(applied=applied), indent=2), encoding="utf-8")
        return path

    def _load_tracked_paths(self) -> set[str]:
        proc = subprocess.run(
            ["git", "-C", str(self.root), "ls-files", "-z"],
            check=False,
            capture_output=True,
        )
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", errors="replace").strip()
            raise HousekeepingSafetyError(f"Cannot enumerate tracked files: {message}")
        return {
            item.replace("\\", "/")
            for item in proc.stdout.decode("utf-8", errors="surrogateescape").split("\0")
            if item
        }

    def _scan_caches(self) -> None:
        for relative in (".pytest-tmp", ".pytest_cache", "dashboard/.next"):
            self._queue_tree(self.root / relative, "caches", "generated cache tree")
        for pattern in (".pytest-*.txt", "pytest_final*.txt", "*test_output*.txt"):
            for path in self.root.glob(pattern):
                self._queue_file(path, "caches", "generated pytest output")
        self._queue_file(self.root / "first_fail.txt", "caches", "generated pytest output")

        excluded = [self.root / ".pytest-tmp", self.root / ".pytest_cache", self.root / "dashboard/.next"]
        search_roots = ("src", "scripts", "tests", "config", "artifacts")
        for search_root in search_roots:
            base = self.root / search_root
            if not base.exists():
                continue
            for path in base.rglob("__pycache__"):
                if not any(_is_within(path, item) for item in excluded):
                    self._queue_tree(path, "caches", "Python bytecode cache")
        self._queue_tree(self.root / "__pycache__", "caches", "Python bytecode cache")

    def _scan_artifacts(self) -> None:
        keepers: set[Path] = set()
        family_bundles: list[Path] = []
        for family in ARTIFACT_FAMILIES:
            parent = self.root / family.parent
            bundles = [path for path in parent.glob(family.pattern) if path.is_dir()]
            family_bundles.extend(bundles)
            grouped: dict[str, list[Path]] = {}
            for bundle in bundles:
                grouped.setdefault(self._bundle_group(family.group, bundle.name), []).append(bundle)
            for group_bundles in grouped.values():
                complete = [bundle for bundle in group_bundles if self._bundle_has_raw_payload(bundle)]
                if complete:
                    keepers.add(max(complete, key=self._keeper_rank))

        for bundle in family_bundles:
            if (bundle / KEEP_MARKER).exists():
                keepers.add(bundle)
            if any(bundle == keeper for keeper in keepers):
                self.protected.append(Record(self._rel(bundle), "complete evidence bundle keeper"))
                continue
            for path in self._iter_files(bundle):
                if _is_raw_artifact(path.name):
                    self._queue_file(path, "artifacts", "raw payload from superseded evidence bundle")

        artifact_root = self.root / "artifacts"
        recognized = set(PROTECTED_ARTIFACT_ROOTS)
        recognized.update({"grounding-repair", "ai-workspace-latency", "observability-runtime-smoke"})
        if artifact_root.exists():
            for child in artifact_root.iterdir():
                if not child.is_dir():
                    continue
                if child.name in PROTECTED_ARTIFACT_ROOTS:
                    self.protected.append(Record(self._rel(child), "protected artifact root"))
                elif child.name in recognized or child.name.startswith(
                    ("tool-centric-query-v2", "tool-centric-shadow-router", "tool-centric-unified-serving")
                ):
                    continue
                else:
                    self.skipped.append(Record(self._rel(child), "unknown artifact family; report only"))

    def _scan_runtime(self) -> None:
        pid_paths = list((self.root / "artifacts").glob("*.pid"))
        unified = self.root / "artifacts" / "tool-centric-unified-serving"
        if unified.exists():
            pid_paths.extend(unified.glob("*.pid"))
        for path in pid_paths:
            try:
                pid = int(path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                self.skipped.append(Record(self._rel(path), "invalid PID marker"))
                continue
            if self._pid_is_live(pid):
                self.protected.append(Record(self._rel(path), f"live PID {pid}"))
            else:
                self._queue_file(path, "runtime", f"stale PID marker for {pid}")

        for relative in (
            "artifacts/bot-api.lock",
            "data/engineering-traces.jsonl",
            "artifacts/observability/otel-traces.jsonl",
        ):
            path = self.root / relative
            if path.exists():
                self.protected.append(Record(relative, "active runtime file"))

        for relative in ("backups", "data/backups"):
            base = self.root / relative
            if not base.exists():
                continue
            for suffix in ("*.db-wal", "*.db-shm"):
                for sidecar in base.glob(suffix):
                    database = Path(str(sidecar).removesuffix("-wal").removesuffix("-shm"))
                    if database.exists():
                        self.protected.append(Record(self._rel(sidecar), "sidecar has a matching database"))
                    else:
                        self._queue_file(sidecar, "runtime", "orphaned SQLite backup sidecar")

    def _queue_file(self, path: Path, scope: str, reason: str) -> None:
        if not path.is_file():
            return
        relative = self._safe_relative(path)
        if relative is None:
            return
        if relative in self.tracked:
            self.skipped.append(Record(relative, "tracked file"))
            return
        problem = self._reparse_problem(path)
        if problem:
            self.skipped.append(Record(relative, problem))
            return
        if path.stat().st_mtime > self.cutoff.timestamp():
            self.skipped.append(Record(relative, "newer than grace period"))
            return
        self._add_candidate(Candidate(relative, scope, "file", path.stat().st_size, reason))

    def _queue_tree(self, path: Path, scope: str, reason: str) -> None:
        if not path.is_dir():
            return
        relative = self._safe_relative(path)
        if relative is None:
            return
        inventory, size, newest, problem = self._tree_inventory(path)
        if problem:
            self.skipped.append(Record(relative, problem))
            return
        tracked = sorted(item for item in inventory if item in self.tracked)
        if tracked:
            self.skipped.append(Record(relative, f"tree contains tracked file: {tracked[0]}"))
            return
        if newest > self.cutoff.timestamp():
            self.skipped.append(Record(relative, "tree contains data newer than grace period"))
            return
        self._add_candidate(Candidate(relative, scope, "tree", size, reason))

    def _add_candidate(self, candidate: Candidate) -> None:
        if candidate.path not in self._candidate_paths:
            self._candidate_paths.add(candidate.path)
            self.candidates.append(candidate)

    def _safe_relative(self, path: Path) -> str | None:
        try:
            absolute = Path(os.path.abspath(path))
            absolute.relative_to(self.root)
            resolved = absolute.resolve(strict=False)
            resolved.relative_to(self.root)
        except (OSError, ValueError):
            display = str(path)
            self.skipped.append(Record(display, "path escapes repository root"))
            return None
        return absolute.relative_to(self.root).as_posix()

    def _reparse_problem(self, path: Path) -> str | None:
        relative = Path(os.path.abspath(path)).relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current /= part
            try:
                stat_result = current.lstat()
            except FileNotFoundError:
                return "path disappeared during scan"
            if _is_reparse(stat_result):
                return "path contains a symlink or reparse point"
        return None

    def _tree_inventory(self, root: Path) -> tuple[list[str], int, float, str | None]:
        problem = self._reparse_problem(root)
        if problem:
            return [], 0, 0, problem
        files: list[str] = []
        size = 0
        newest = root.stat().st_mtime
        stack = [root]
        while stack:
            directory = stack.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError as exc:
                return [], 0, 0, f"cannot inspect tree: {exc}"
            for entry in entries:
                try:
                    stat_result = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    return [], 0, 0, f"cannot inspect tree entry: {exc}"
                if _is_reparse(stat_result):
                    return [], 0, 0, "tree contains a symlink or reparse point"
                newest = max(newest, stat_result.st_mtime)
                entry_path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry_path)
                else:
                    relative = self._safe_relative(entry_path)
                    if relative is None:
                        return [], 0, 0, "tree contains a path outside repository root"
                    files.append(relative)
                    size += stat_result.st_size
        return files, size, newest, None

    def _validate_candidate_now(self, path: Path, kind: str) -> str | None:
        relative = self._safe_relative(path)
        if relative is None:
            return "path escapes repository root"
        if kind == "file":
            if relative in self.tracked:
                return "tracked file"
            problem = self._reparse_problem(path)
            if problem:
                return problem
            if path.stat().st_mtime > self.cutoff.timestamp():
                return "file became newer than grace period"
            return None
        inventory, _, newest, problem = self._tree_inventory(path)
        if problem:
            return problem
        if any(item in self.tracked for item in inventory):
            return "tree contains a tracked file"
        if newest > self.cutoff.timestamp():
            return "tree contains data newer than grace period"
        return None

    def _remove_tree(self, root: Path) -> None:
        directories: list[Path] = []
        stack = [root]
        while stack:
            directory = stack.pop()
            directories.append(directory)
            for entry in os.scandir(directory):
                stat_result = entry.stat(follow_symlinks=False)
                if _is_reparse(stat_result):
                    raise HousekeepingSafetyError("tree changed to contain a reparse point")
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                else:
                    path.unlink()
        for directory in reversed(directories):
            directory.rmdir()

    def _bundle_has_raw_payload(self, bundle: Path) -> bool:
        return any(_is_raw_artifact(path.name) for path in self._iter_files(bundle))

    def _iter_files(self, root: Path) -> Iterable[Path]:
        for directory, names, files in os.walk(root, followlinks=False):
            safe_names = []
            for name in names:
                path = Path(directory) / name
                try:
                    if not _is_reparse(path.lstat()):
                        safe_names.append(name)
                except OSError:
                    continue
            names[:] = safe_names
            for name in files:
                yield Path(directory) / name

    def _keeper_rank(self, bundle: Path) -> tuple[int, float, str]:
        return (1 if "final" in bundle.name.casefold() else 0, bundle.stat().st_mtime, bundle.name.casefold())

    def _bundle_group(self, family: str, name: str) -> str:
        if family != "latency":
            return family
        lowered = name.casefold()
        for scenario in ("contention-15", "contention-70", "baseline", "runtime-surfaces"):
            if scenario in lowered:
                return f"latency:{scenario}"
        return f"latency:{lowered}"

    def _rel(self, path: Path) -> str:
        return Path(os.path.abspath(path)).relative_to(self.root).as_posix()

def _is_raw_artifact(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered.endswith((".db", ".db-wal", ".db-shm"))
        or "engineering-traces.jsonl" in lowered
        or lowered in {"otel-traces.jsonl", "sse-events.jsonl"}
    )

def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    reparse_flag = getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag) or bool(stat_result.st_mode & 0o120000 == 0o120000)

def _is_within(path: Path, parent: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(parent)))
        return True
    except ValueError:
        return False

def _pid_is_live(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True

def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"
