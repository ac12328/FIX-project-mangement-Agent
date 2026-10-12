"""Workspace-scoped Windows event for cooperative Bot/API process shutdown.

Only the owning process creates the event. The guard first verifies the listener's
launcher and retains a process handle while signalling and waiting for exit.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import sys
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Iterator

_EVENT_MODIFY_STATE = 0x0002
_SYNCHRONIZE = 0x00100000
_PROCESS_TERMINATE = 0x0001
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 0x102
_ERROR_ALREADY_EXISTS = 183


def _kernel32():
    if sys.platform != "win32":
        raise OSError("Windows service shutdown events require Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateEventW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.OpenEventW.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.SetEvent.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


def _event_name(root: Path, pid: int) -> str:
    workspace = str(root.resolve()).casefold().encode("utf-8")
    digest = hashlib.sha256(workspace).hexdigest()[:24]
    return f"Local\\AgentFIXBPRO-stop-{digest}-{pid}"


def _completion_name(root: Path, pid: int) -> str:
    return _event_name(root, pid) + "-cleanup-complete"


class _ShutdownChannel:
    def __init__(self, stop: "_Handle", completion: "_Handle"):
        self.stop = stop
        self.completion = completion

    def wait(self, timeout: float) -> bool:
        return self.stop.wait(timeout)

    def confirm_cleanup(self) -> None:
        if not self.completion.kernel.SetEvent(self.completion.handle):
            raise ctypes.WinError(ctypes.get_last_error())


class _Handle:
    def __init__(self, kernel, handle):
        self.kernel = kernel
        self.handle = handle

    def wait(self, timeout: float) -> bool:
        milliseconds = min(max(0, int(timeout * 1000)), 0xFFFFFFFE)
        outcome = self.kernel.WaitForSingleObject(self.handle, milliseconds)
        if outcome == _WAIT_OBJECT_0:
            return True
        if outcome == _WAIT_TIMEOUT:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self) -> None:
        if not self.kernel.TerminateProcess(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.kernel.CloseHandle(self.handle)


def _create_event(kernel, name: str) -> _Handle:
    handle = kernel.CreateEventW(None, True, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel.CloseHandle(handle)
        raise OSError("Shutdown event already exists for this process and workspace")
    return _Handle(kernel, handle)


@contextmanager
def cooperative_shutdown_event(root: Path) -> Iterator[_ShutdownChannel]:
    """Create fresh stop and cleanup events for this workspace and process."""
    kernel = _kernel32()
    with _create_event(kernel, _event_name(root, os.getpid())) as stop:
        with _create_event(kernel, _completion_name(root, os.getpid())) as completion:
            yield _ShutdownChannel(stop, completion)


def open_cleanup_confirmation(root: Path, pid: int) -> _Handle:
    """Keep the confirmation event alive across the owner's process exit."""
    kernel = _kernel32()
    handle = kernel.OpenEventW(_SYNCHRONIZE, False, _completion_name(root, pid))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return _Handle(kernel, handle)


def request_shutdown(root: Path, pid: int) -> None:
    """Signal a verified listener; absence of a cooperative target is an error."""
    kernel = _kernel32()
    handle = kernel.OpenEventW(_EVENT_MODIFY_STATE, False, _event_name(root, pid))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    with _Handle(kernel, handle):
        if not kernel.SetEvent(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def open_process_exit_handle(pid: int, *, allow_terminate: bool = False) -> _Handle:
    """Pin the listener's process object before signalling; PID reuse is not success."""
    kernel = _kernel32()
    rights = _SYNCHRONIZE | (_PROCESS_TERMINATE if allow_terminate else 0)
    handle = kernel.OpenProcess(rights, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return _Handle(kernel, handle)
