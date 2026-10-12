"""Opt-in cleanup of disposable logs and reproducible test products."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import defaultdict
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from src.utils.housekeeping import HousekeepingSafetyError, PROTECTED_ARTIFACT_ROOTS, Record, _is_reparse
from src.utils.storage_housekeeping import StorageHousekeeper

GENERATED_SCOPES = {"storage", "logs", "test-products"}
CACHE_NAMES = {".pytest_cache", ".ruff_cache", ".mypy_cache", "__pycache__"}
EXCLUDED_DIRS = {
    ".git", ".worktrees", ".venv", "venv", "env", "node_modules", "site-packages",
    "attachments", "backups", "data", "reports", "fixtures", "evidence", ".uv-cache",
}
LOG_SUFFIX = re.compile(r"\.log(?:\.\d+)?$|(?:^|[.-])(?:stdout|stderr)\.txt$|\.(?:out|err)$", re.I)
ROTATION = re.compile(r"^(?:bot|api)\.log\.[1-9]\d*$", re.I)
LAUNCH_LOG = re.compile(
    r"^(?:bot-launch\.(?:stdout|stderr)\.\d{8}-\d{6}"
    r"|(?:bot-restart|backend-reload)-\d{8}-\d{6}\.(?:stdout|stderr))\.log$", re.I,
)
REFERENCE = re.compile(r"[\w.-]+\.(?:log(?:\.\d+)?|out|err|txt)\b", re.I)
FINAL_LOG = re.compile(r"(?:^|[._-])(?:final[a-z0-9]*|baseline|complete|releasecheck|audit|trace)(?:[._-]|$)", re.I)
META_SUFFIXES = {".md", ".json", ".xml", ".csv"}
META_LIMIT = 8 * 1024 * 1024


class GeneratedHousekeeper(StorageHousekeeper):
    """Expand storage explicitly, never the legacy unattended all scope."""

    def __init__(
        self, repo_root, *, scope="storage", min_age_hours=24, keep_runs=2,
        retention_days=7, log_retention_days=30, keep_log_files=2, now=None,
        commands_probe=None,
    ):
        if scope not in GENERATED_SCOPES:
            raise HousekeepingSafetyError(f"Unsupported generated scope: {scope}")
        if not math.isfinite(log_retention_days) or log_retention_days < 0:
            raise HousekeepingSafetyError("log_retention_days must be finite and non-negative")
        if type(keep_log_files) is not int or keep_log_files < 1:
            raise HousekeepingSafetyError("keep_log_files must be a positive integer")
        super().__init__(
            repo_root, min_age_hours=min_age_hours, keep_runs=keep_runs,
            retention_days=retention_days, now=now, commands_probe=commands_probe,
        )
        self.scope = scope
        self.log_retention_days = log_retention_days
        self.keep_log_files = keep_log_files
        self.log_cutoff = self.now - timedelta(hours=max(min_age_hours, log_retention_days * 24))
        self._generated: dict[str, str] = {}
        self._reference_cache: dict[Path, set[str] | None] = {}
        self._global_references: set[str] | None = set()

    def _fingerprint(self, path: Path) -> tuple:
        value = super()._fingerprint(path)
        if not path.is_dir():
            return value
        digest = hashlib.sha256()
        stack = [path]
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    stat = entry.stat(follow_symlinks=False)
                    if _is_reparse(stat):
                        raise HousekeepingSafetyError("Tree fingerprint contains a reparse point")
                    payload = (Path(entry.path).relative_to(path).as_posix(), stat.st_dev,
                               stat.st_ino, stat.st_size, stat.st_mtime_ns)
                    digest.update(repr(payload).encode("utf-8", errors="surrogateescape"))
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        return (*value, digest.hexdigest())

    @contextmanager
    def _log_age_policy(self):
        original = self.cutoff
        self.cutoff = self.log_cutoff
        try:
            yield
        finally:
            self.cutoff = original

    def _metadata_references(self, directory: Path) -> set[str] | None:
        if directory in self._reference_cache:
            return self._reference_cache[directory]
        references: set[str] | None = set()
        try:
            total = 0
            for path in directory.iterdir():
                if path.suffix.casefold() not in META_SUFFIXES:
                    continue
                if self._reparse_problem(path):
                    references = None
                    break
                if not path.is_file():
                    continue
                size = path.stat().st_size
                total += size
                if size > META_LIMIT or total > 64 * 1024 * 1024:
                    references = None
                    break
                references.update(match.casefold() for match in REFERENCE.findall(path.read_text(encoding="utf-8-sig")))
        except (OSError, UnicodeError):
            references = None
        self._reference_cache[directory] = references
        return references

    def _tree_inventory(self, root: Path) -> tuple:
        inventory, size, newest, problem = super()._tree_inventory(root)
        if not problem:
            for item in inventory:
                nested = (self.root / item).relative_to(root)
                if any(part in {".venv", "venv", "env", "node_modules"} for part in nested.parts[:-1]):
                    problem = "Tree contains an installed environment or dependencies"
                    break
        return inventory, size, newest, problem

    def _load_global_references(self) -> None:
        references = set()
        try:
            for relative in self.tracked:
                path = self.root / relative
                if not (relative.startswith("docs/") or relative in {"CLAUDE.md", "README.md"}):
                    continue
                if path.suffix.casefold() not in META_SUFFIXES:
                    continue
                if self._reparse_problem(path) or path.stat().st_size > META_LIMIT:
                    self._global_references = None
                    return
                references.update(match.casefold() for match in REFERENCE.findall(path.read_text(encoding="utf-8-sig")))
            self._global_references = references
        except (OSError, UnicodeError):
            self._global_references = None

    def _path_in_commands(self, path: Path) -> bool:
        needles = [self._rel(path).casefold(), str(path).replace("\\", "/").casefold()]
        if path.parent not in {self.root, self.root / "artifacts"}:
            needles.extend((self._rel(path.parent).casefold(), str(path.parent).replace("\\", "/").casefold()))
        return any(any(needle in command.replace("\\", "/").casefold() for needle in needles)
                   for command in self._commands)

    def _test_runner_live(self) -> bool:
        return any(cmd.casefold().startswith("uv.exe ")
                   or re.search(r"pytest|run_product_tests|vitest|playwright|\bjest\b|\bruff\b", cmd, re.I)
                   for cmd in self._commands)

    def _context_recent(self, path: Path) -> bool:
        if not self._rel(path).startswith("artifacts/"):
            return False
        current = path.parent
        try:
            while current != self.root / "artifacts":
                for item in current.iterdir():
                    if item.suffix.casefold() not in META_SUFFIXES:
                        continue
                    if self._reparse_problem(item):
                        return True
                    if item.is_file() and item.stat().st_mtime > self.cutoff.timestamp():
                        return True
                current = current.parent
        except OSError:
            return True
        return False

    def _test_problem(self, path: Path) -> str | None:
        relative = self._rel(path)
        artifact = relative.startswith("artifacts/")
        scratch = path.name == ".pytest-tmp" or bool(re.search(r"(?:^|-)pytest-(?:tmp|temp)(?:-|$)", path.name))
        root_output = path.parent == self.root and bool(re.fullmatch(
            r"(?:\.pytest-.*\.txt|pytest_final.*\.txt|.*test_output.*\.txt|first_fail\.txt)", path.name,
        ))
        root_pytest = path.parent == self.root / "artifacts" and bool(re.fullmatch(r"pytest-[\w-]+", path.name))
        if root_pytest:
            entries = list(path.iterdir())
            if not entries or any(
                not item.is_dir() or not re.fullmatch(r"(?:test_[\w.-]+|implicit-runtime\d+)", item.name)
                for item in entries
            ):
                return "Root pytest bundle is not exclusively recognizable temporary test directories"
        allowed = path.name in CACHE_NAMES or scratch or root_output or root_pytest or (artifact and path.name == ".next")
        uv_cache = artifact and path.name == "uv-cache" and path.parent.name == "runtime"
        if not allowed and not uv_cache:
            return "Not an allowlisted test product"
        if uv_cache:
            manifest = path.parent.parent / "ENVIRONMENT.json"
            try:
                if self._reparse_problem(manifest) or manifest.stat().st_size > META_LIMIT:
                    raise ValueError("Unsafe isolated runtime manifest")
                environment = self._read_json(manifest).get("constructed_environment", {})
                declared = environment.get("UV_CACHE_DIR")
                if not isinstance(declared, str) or not Path(declared).is_absolute():
                    raise ValueError("Missing isolated cache path")
                if os.path.normcase(os.path.abspath(declared)) != os.path.normcase(str(path)):
                    raise ValueError("Isolated cache path does not match manifest")
            except (OSError, ValueError, TypeError, AttributeError):
                return "Missing or unsafe isolated runtime manifest"
        if root_output:
            if FINAL_LOG.search(path.name):
                return "Final/baseline/audit test output"
            problem = self._reference_problem(path)
            if problem:
                return problem
        if self._has_keep_marker(path):
            return "Test product keep marker"
        if self._path_in_commands(path) or self._test_runner_live():
            return "Live test/cache runner may use this product"
        if path.name == ".next" and self._is_running(path, build=True):
            return "Node runner may use this archived build"
        if self._context_recent(path):
            return "Associated test evidence newer than grace period"
        return None

    def _log_problem(self, path: Path) -> str | None:
        if not LOG_SUFFIX.search(path.name):
            return "Not a console log"
        if path.parent in {self.root / "logs", self.root / "dashboard/logs"}:
            if not (ROTATION.fullmatch(path.name) or LAUNCH_LOG.fullmatch(path.name)):
                return "Unrotated or unknown service log; retained"
            if ROTATION.fullmatch(path.name) and any("run_bot.py" in cmd.casefold() for cmd in self._commands):
                return "Live service owns this rotation series"
        elif not self._rel(path).startswith("artifacts/"):
            return "Log is outside allowlisted roots"
        if FINAL_LOG.search(path.name):
            return "Final/baseline/audit/trace evidence log"
        if self._has_keep_marker(path) or self._path_in_commands(path):
            return "Log keep marker or active runner"
        if self._test_runner_live():
            return "Live test runner may use console logs"
        return self._reference_problem(path)

    def _reference_problem(self, path: Path) -> str | None:
        if self._global_references is None:
            return "Cannot safely inspect tracked log references"
        current = path.parent
        while True:
            references = self._metadata_references(current)
            if references is None or path.name.casefold() in references:
                return "Log referenced by evidence or reference inspection unavailable"
            if current in {self.root, self.root / "artifacts", self.root / "logs", self.root / "dashboard/logs"}:
                break
            current = current.parent
        if path.name.casefold() in self._global_references:
            return "Log referenced by tracked documentation"
        return None

    def _artifact_inventory(self):
        base = self.root / "artifacts"
        if not base.is_dir() or self._reparse_problem(base):
            return
        stack = [base]
        while stack:
            directory = stack.pop()
            try:
                entries = sorted(directory.iterdir())
                for path in entries:
                    if self._reparse_problem(path):
                        self.skipped.append(Record(self._rel(path), "Artifact path contains a reparse point"))
                        continue
                    if path.is_file():
                        if LOG_SUFFIX.search(path.name):
                            yield path, "logs"
                        continue
                    if not path.is_dir():
                        continue
                    if (path.name in EXCLUDED_DIRS or path.name in PROTECTED_ARTIFACT_ROOTS
                            or self._has_keep_marker(path)):
                        continue
                    scratch = bool(re.search(r"(?:^|-)pytest-(?:tmp|temp)(?:-|$)", path.name))
                    root_pytest = path.parent == base and bool(re.fullmatch(r"pytest-[\w-]+", path.name))
                    if path.name in CACHE_NAMES or scratch or root_pytest or path.name in {".next", ".pytest-tmp"}:
                        yield path, "test-products"
                        continue
                    if path.name == "runtime":
                        cache = path / "uv-cache"
                        if cache.is_dir() and not self._reparse_problem(cache):
                            yield cache, "test-products"
                        continue  # Never traverse installed environments or sandbox user data.
                    stack.append(path)
            except OSError as exc:
                self.skipped.append(Record(self._rel(directory), f"Cannot inspect artifacts: {exc}"))

    def scan(self) -> list:
        self._generated.clear()
        self._reference_cache.clear()
        if self.scope == "storage":
            super().scan()
        else:
            self._reset_scan()
        self._load_global_references()
        inventory = list(self._artifact_inventory())
        if self.scope in {"test-products", "storage"}:
            roots = [self.root / name for name in (".pytest-tmp", ".pytest_cache", ".ruff_cache", ".mypy_cache")]
            for path in roots + [p for p, category in inventory if category == "test-products"]:
                if not path.is_dir():
                    continue
                problem = self._test_problem(path)
                if problem:
                    self.protected.append(Record(self._rel(path), problem))
                    continue
                self._queue_tree(path, "test-products", "Expired reproducible test cache/scratch tree")
                self._generated[self._rel(path)] = "test-products"
            outputs = {self.root / "first_fail.txt"}
            for pattern in (".pytest-*.txt", "pytest_final*.txt", "*test_output*.txt"):
                outputs.update(self.root.glob(pattern))
            for path in sorted(outputs):
                if path.is_file():
                    problem = self._test_problem(path)
                    if problem:
                        self.protected.append(Record(self._rel(path), problem))
                        continue
                    self._queue_file(path, "test-products", "Expired generated pytest console output")
                    self._generated[self._rel(path)] = "test-products"
        if self.scope in {"logs", "storage"}:
            logs = []
            for log_root in (self.root / "logs", self.root / "dashboard/logs"):
                if log_root.is_dir() and not self._reparse_problem(log_root):
                    logs.extend(log_root.iterdir())
            logs.extend(p for p, category in inventory if category == "logs")
            groups = defaultdict(list)
            for path in logs:
                if not path.is_file() or not LOG_SUFFIX.search(path.name):
                    continue
                problem = self._log_problem(path)
                if problem:
                    self.protected.append(Record(self._rel(path), problem))
                    continue
                if ROTATION.fullmatch(path.name):
                    group = (path.parent, path.name.casefold().split(".log.")[0])
                else:
                    name = path.name.casefold()
                    stream = "stderr" if "stderr" in name or name.endswith(".err") else (
                        "stdout" if "stdout" in name or name.endswith(".out") else "console"
                    )
                    group = (path.parent, stream)
                groups[group].append(path)
            with self._log_age_policy():
                for paths in groups.values():
                    ordered = sorted(paths, key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
                    for path in ordered[:self.keep_log_files]:
                        self.protected.append(Record(self._rel(path), "Latest console logs retained"))
                    for path in ordered[self.keep_log_files:]:
                        self._queue_file(path, "logs", "Expired superseded console/rotated log")
                        self._generated[self._rel(path)] = "logs"
        return self._finish_scan()

    def _validate_candidate_now(self, path: Path, kind: str) -> str | None:
        category = self._generated.get(self._rel(path))
        if not category:
            return super()._validate_candidate_now(path, kind)
        if category == "logs":
            with self._log_age_policy():
                problem = self._validate_common_candidate_now(path, kind)
            self._reference_cache.clear()
            return problem or self._log_problem(path)
        self._reference_cache.clear()
        return self._validate_common_candidate_now(path, kind) or self._test_problem(path)

    def _fresh_housekeeper(self):
        return GeneratedHousekeeper(
            self.root, scope=self.scope, min_age_hours=self.min_age_hours,
            keep_runs=self.keep_runs, retention_days=self.retention_days,
            log_retention_days=self.log_retention_days, keep_log_files=self.keep_log_files,
            now=self.now, commands_probe=lambda: self._commands,
        )

    def report_dict(self, *, applied: bool) -> dict:
        report = super().report_dict(applied=applied)
        report["generated_policy"] = {
            "log_retention_days": self.log_retention_days,
            "keep_latest_logs_per_directory_and_stream": self.keep_log_files,
            "test_product_retention_days": self.retention_days,
            "protect_referenced_and_final_logs": True,
            "log_cutoff_utc": self.log_cutoff.isoformat(),
            "jsonl_spools_and_reports": "Never individually eligible; scratch trees are reproducible",
            "installed_environments_and_worktrees": "Never eligible",
        }
        return report
