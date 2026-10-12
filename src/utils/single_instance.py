"""Cross-process single-instance lock for long-lived local services."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class InstanceAlreadyRunningError(RuntimeError):
    """Raised when another process owns the requested service lock."""


class ProcessInstanceLock:
    """Hold an OS-backed advisory lock until explicitly released."""

    def __init__(self, path: Path, handle: BinaryIO) -> None:
        self.path = path
        self._handle = handle
        self._released = False

    @classmethod
    def acquire(cls, path: str | Path) -> "ProcessInstanceLock":
        lock_path = Path(path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            _lock_handle(handle)
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n".encode("ascii"))
            handle.flush()
            handle.seek(0)
        except OSError as exc:
            handle.close()
            raise InstanceAlreadyRunningError(
                f"Another process owns the service lock: {lock_path}"
            ) from exc
        return cls(lock_path, handle)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._handle.seek(0)
            _unlock_handle(self._handle)
        finally:
            self._handle.close()

    def __enter__(self) -> "ProcessInstanceLock":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def _lock_handle(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_handle(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
