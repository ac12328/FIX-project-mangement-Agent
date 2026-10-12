"""Process-crash reconciliation for journaled draft attachment API mutations.

A sidecar precedes the filesystem change. A receipt in the same transaction
as the draft revision is the sole committed/rolled-back decision.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from src.email.attachments import AttachmentError, MAX_TOTAL_SIZE


class AttachmentRecoveryError(RuntimeError):
    """The attachment state cannot be reconciled without an operator."""


class AttachmentCleanupError(OSError):
    """A converged file state could not be physically cleaned up yet."""


class AttachmentBusyError(AttachmentRecoveryError):
    """A concurrent mutation owns the attachment manifest."""


_unresolved = threading.Event()


# The integrated runtime uses one SQLite writer, but read-only API workers or
# another process can otherwise observe a visible link before its DB decision.
# Keep the in-process RLock for thread coordination and a stable advisory file
# lock for cooperating processes. Never unlink the lock file: replacing its
# inode could let two processes hold different locks for the same directory.
_visibility_lock = threading.RLock()
_visibility_owner = threading.local()


def _lock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def attachment_visibility_lock(base_dir: str):
    """Fail closed if another thread/process is changing the same manifest.

    The lock must be nonblocking: a caller may already hold SQLite's write
    lease, while another writer holds visibility and waits for that lease.
    Nested calls from the owning thread share the OS handle for one root.
    """
    root = Path(base_dir).resolve()
    if not _visibility_lock.acquire(blocking=False):
        raise AttachmentBusyError("Attachment mutation in progress; retry after it completes")
    handle = None
    nested = getattr(_visibility_owner, "depth", 0) > 0
    try:
        if nested:
            if _visibility_owner.root != root:
                raise AttachmentRecoveryError("Nested attachment roots do not match")
            _visibility_owner.depth += 1
        else:
            root.mkdir(parents=True, exist_ok=True)
            lock_path = root / ".attachment-visibility.lock"
            if lock_path.is_symlink():
                raise AttachmentRecoveryError("Attachment visibility lock is a symlink")
            handle = lock_path.open("a+b")
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            try:
                _lock_file(handle)
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK}:
                    raise AttachmentBusyError(
                        "Attachment mutation in another process; retry after it completes"
                    ) from exc
                raise
            _visibility_owner.root = root
            _visibility_owner.depth = 1
        try:
            yield
        finally:
            _visibility_owner.depth -= 1
            if not _visibility_owner.depth:
                del _visibility_owner.root
                del _visibility_owner.depth
                _unlock_file(handle)
    finally:
        if handle is not None:
            handle.close()
        _visibility_lock.release()


def require_reconciled() -> None:
    """Prevent reads and sends after an in-process compensation failure."""
    if _unresolved.is_set():
        raise AttachmentRecoveryError("Attachment recovery is required before reading or sending")


def mark_unresolved() -> None:
    _unresolved.set()


def reset_recovery_state() -> None:
    """Test-only reset; production clears the latch only after startup recovery."""
    _unresolved.clear()


@dataclass(frozen=True)
class AttachmentIntent:
    operation_id: str
    kind: str
    draft_id: str
    filename: str
    storage_name: str
    size: int
    sha256: str
    device: int
    inode: int
    version: int = 1


def _root(base_dir: str) -> Path:
    return Path(base_dir) / "attachments"


def _validate_name(name: str) -> None:
    if (
        not name or name in {".", "..", ".staging"}
        or Path(name).name != name or "/" in name or "\\" in name
    ):
        raise AttachmentRecoveryError("Invalid attachment journal path component")


def _paths(base_dir: str, intent: AttachmentIntent) -> tuple[Path, Path, Path]:
    if intent.version != 1 or intent.kind not in {"upload", "delete"}:
        raise AttachmentRecoveryError("Unsupported attachment journal version or kind")
    if len(intent.operation_id) != 32 or any(c not in "0123456789abcdef" for c in intent.operation_id):
        raise AttachmentRecoveryError("Invalid attachment operation ID")
    for value in (intent.draft_id, intent.filename, intent.storage_name):
        _validate_name(value)
    if intent.size < 0 or len(intent.sha256) != 64 or any(
        c not in "0123456789abcdef" for c in intent.sha256
    ):
        raise AttachmentRecoveryError("Invalid attachment journal content identity")
    root = _root(base_dir)
    target = root / intent.draft_id / intent.filename
    stored = root / ".staging" / intent.storage_name
    journal = root / ".staging" / "operations" / f"{intent.operation_id}.json"
    for directory in (root, target.parent, stored.parent, journal.parent):
        if directory.is_symlink():
            raise AttachmentRecoveryError("Symlink in attachment storage path")
    return target, stored, journal


def _identity(path: Path) -> tuple[int, int]:
    stat = path.lstat()
    if path.is_symlink() or not path.is_file():
        raise AttachmentRecoveryError("Attachment path is not a regular file")
    return stat.st_dev, stat.st_ino


def _verify(path: Path, intent: AttachmentIntent) -> None:
    try:
        if _identity(path) != (intent.device, intent.inode):
            raise AttachmentRecoveryError("Attachment file identity changed")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if path.stat().st_size != intent.size or digest.hexdigest() != intent.sha256:
            raise AttachmentRecoveryError("Attachment content changed")
    except OSError as exc:
        raise AttachmentRecoveryError("Attachment could not be verified") from exc


def _cleanup(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise AttachmentCleanupError(f"Could not clean attachment path: {path.name}") from exc


def _persist(base_dir: str, intent: AttachmentIntent) -> None:
    _, _, journal = _paths(base_dir, intent)
    journal.parent.mkdir(parents=True, exist_ok=True)
    temporary = journal.with_suffix(".tmp")
    with temporary.open("xb") as handle:
        handle.write(json.dumps(asdict(intent), sort_keys=True).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.link(temporary, journal)  # No replacement of a competing intent.
    finally:
        temporary.unlink(missing_ok=True)


def prepare_upload(
    base_dir: str, draft_id: str, filename: str, staged: Path, size: int, sha256: str
) -> AttachmentIntent:
    """Choose a free name and persist the intent before making a file visible."""
    _validate_name(draft_id)
    safe_name = filename.replace("/", "_").replace("\\", "_")
    _validate_name(safe_name)
    draft_dir = _root(base_dir) / draft_id
    draft_dir.mkdir(parents=True, exist_ok=True)
    if sum(f.stat().st_size for f in draft_dir.iterdir() if f.is_file()) + size > MAX_TOTAL_SIZE:
        raise AttachmentError(f"Total attachments exceed {MAX_TOTAL_SIZE} bytes")
    stem, extension = Path(safe_name).stem, Path(safe_name).suffix
    for counter in range(10000):
        candidate = safe_name if counter == 0 else f"{stem}_{counter}{extension}"
        if (draft_dir / candidate).exists():
            continue
        dev, inode = _identity(staged)
        intent = AttachmentIntent(
            uuid.uuid4().hex, "upload", draft_id, candidate, staged.name,
            size, sha256, dev, inode,
        )
        _persist(base_dir, intent)
        return intent
    raise AttachmentError("Too many attachments with the same filename")


def prepare_delete(
    base_dir: str, draft_id: str, filename: str, size: int, sha256: str
) -> AttachmentIntent:
    """Persist an intent before moving the original into quarantine."""
    _validate_name(draft_id)
    _validate_name(filename)
    original = _root(base_dir) / draft_id / filename
    dev, inode = _identity(original)
    operation_id = uuid.uuid4().hex
    intent = AttachmentIntent(
        operation_id, "delete", draft_id, filename, f"{operation_id}.delete",
        size, sha256, dev, inode,
    )
    _persist(base_dir, intent)
    return intent


def abandon_upload_collision(base_dir: str, intent: AttachmentIntent) -> None:
    """Discard an unpublished intent after an exclusive link lost a name race.

    The competing target is never touched. A crash before this cleanup may
    require manual reconciliation because its provenance cannot be inferred.
    """
    if intent.kind != "upload":
        raise AttachmentRecoveryError("Only upload name collisions can be abandoned")
    target, stored, journal = _paths(base_dir, intent)
    _verify(stored, intent)
    if not (target.exists() or target.is_symlink()) or target.samefile(stored):
        raise AttachmentRecoveryError("Upload collision cannot be identified safely")
    _cleanup(journal)


def apply_file_change(base_dir: str, intent: AttachmentIntent) -> None:
    target, stored, _ = _paths(base_dir, intent)
    _verify(stored if intent.kind == "upload" else target, intent)
    if intent.kind == "upload":
        os.link(stored, target)  # Exclusive; never replace another upload.
    else:
        os.link(target, stored)  # Exclusive, including across another writer's race.
        target.unlink()  # Both links are recognizable after a crash between steps.


def record_committed_decision(conn: sqlite3.Connection, intent: AttachmentIntent) -> None:
    conn.execute(
        "INSERT INTO draft_attachment_operations "
        "(operation_id, draft_id, kind, filename, size, sha256) VALUES (?,?,?,?,?,?)",
        (intent.operation_id, intent.draft_id, intent.kind, intent.filename,
         intent.size, intent.sha256),
    )


def reconcile_one(conn: sqlite3.Connection, base_dir: str, intent: AttachmentIntent) -> None:
    """Converge one sidecar from the SQLite decision; never overwrite unknown bytes."""
    target, stored, journal = _paths(base_dir, intent)
    receipt = conn.execute(
        "SELECT draft_id, kind, filename, size, sha256 "
        "FROM draft_attachment_operations WHERE operation_id=?", (intent.operation_id,),
    ).fetchone()
    committed = receipt is not None
    if committed and tuple(receipt) != (
        intent.draft_id, intent.kind, intent.filename, intent.size, intent.sha256
    ):
        raise AttachmentRecoveryError("Attachment receipt disagrees with journal")
    visible, hidden = target.exists() or target.is_symlink(), stored.exists() or stored.is_symlink()
    if intent.kind == "upload":
        if hidden:
            _verify(stored, intent)
        if visible:
            _verify(target, intent)
        if committed:
            if not visible:
                raise AttachmentRecoveryError("Committed upload is not visible")
        elif visible:
            if not hidden or not target.samefile(stored):
                raise AttachmentRecoveryError("Uncommitted upload cannot be identified")
            target.unlink()
        if hidden:
            _cleanup(stored)
    elif committed:
        if visible:
            raise AttachmentRecoveryError("Committed deletion still has a visible path")
        if hidden:
            _verify(stored, intent)
            _cleanup(stored)
    else:
        if hidden:
            _verify(stored, intent)
            if visible:
                _verify(target, intent)
                if not target.samefile(stored):
                    raise AttachmentRecoveryError("Competing file blocks deletion rollback")
            else:
                os.link(stored, target)  # Restart-safe exclusive restore.
            _cleanup(stored)
        elif visible:
            _verify(target, intent)
        else:
            raise AttachmentRecoveryError("Uncommitted deletion has no recoverable file")
    _cleanup(journal)


def recover_pending(conn: sqlite3.Connection, base_dir: str) -> int:
    """Reconcile known intents under the shared visibility guard."""
    with attachment_visibility_lock(base_dir):
        return _recover_pending_locked(conn, base_dir)


def _recover_pending_locked(conn: sqlite3.Connection, base_dir: str) -> int:
    directory = _root(base_dir) / ".staging" / "operations"
    if directory.is_symlink():
        raise AttachmentRecoveryError("Attachment journal directory is a symlink")
    if not directory.exists():
        _unresolved.clear()
        return 0
    count = 0
    for journal in sorted(directory.iterdir()):
        if journal.suffix == ".tmp":
            continue  # Never published an intent or a visible file.
        if journal.suffix != ".json" or journal.is_symlink():
            raise AttachmentRecoveryError("Unknown attachment journal entry")
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
            intent = AttachmentIntent(**payload)
            _, _, expected = _paths(base_dir, intent)
            if journal != expected:
                raise AttachmentRecoveryError("Attachment journal name mismatch")
        except (OSError, TypeError, ValueError) as exc:
            raise AttachmentRecoveryError(f"Invalid attachment journal: {journal.name}") from exc
        reconcile_one(conn, base_dir, intent)
        count += 1
    _unresolved.clear()
    return count
