"""Windows Credential Manager adapter for FIX session secrets.

The implementation deliberately keeps plaintext out of SQLite and LLM paths.
Tests should inject an in-memory implementation of ``SessionSecretStore``.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
from abc import ABC, abstractmethod


class SessionSecretStore(ABC):
    @abstractmethod
    def put(self, reference: str, value: str) -> None: ...

    @abstractmethod
    def get(self, reference: str) -> str | None: ...

    @abstractmethod
    def delete(self, reference: str) -> None: ...


class InMemorySessionSecretStore(SessionSecretStore):
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def put(self, reference: str, value: str) -> None:
        self._values[reference] = value

    def get(self, reference: str) -> str | None:
        return self._values.get(reference)

    def delete(self, reference: str) -> None:
        self._values.pop(reference, None)


class WindowsCredentialManagerStore(SessionSecretStore):
    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    class _CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", ctypes.wintypes.DWORD),
            ("Type", ctypes.wintypes.DWORD),
            ("TargetName", ctypes.wintypes.LPWSTR),
            ("Comment", ctypes.wintypes.LPWSTR),
            ("LastWritten", ctypes.wintypes.FILETIME),
            ("CredentialBlobSize", ctypes.wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", ctypes.wintypes.DWORD),
            ("AttributeCount", ctypes.wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", ctypes.wintypes.LPWSTR),
            ("UserName", ctypes.wintypes.LPWSTR),
        ]

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows Credential Manager is only available on Windows")
        self._advapi = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi.CredWriteW.argtypes = [ctypes.POINTER(self._CREDENTIALW), ctypes.wintypes.DWORD]
        self._advapi.CredWriteW.restype = ctypes.wintypes.BOOL
        self._advapi.CredReadW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(self._CREDENTIALW))]
        self._advapi.CredReadW.restype = ctypes.wintypes.BOOL
        self._advapi.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi.CredDeleteW.argtypes = [ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD]
        self._advapi.CredDeleteW.restype = ctypes.wintypes.BOOL

    def put(self, reference: str, value: str) -> None:
        blob = value.encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        cred = self._CREDENTIALW()
        cred.Type = self.CRED_TYPE_GENERIC
        cred.TargetName = reference
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = "AgentFIXBPRO"
        if not self._advapi.CredWriteW(ctypes.byref(cred), 0):
            raise ctypes.WinError()

    def get(self, reference: str) -> str | None:
        ptr = ctypes.POINTER(self._CREDENTIALW)()
        if not self._advapi.CredReadW(reference, self.CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
            error = ctypes.get_last_error()
            if error == 1168:
                return None
            raise ctypes.WinError(error)
        try:
            cred = ptr.contents
            raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            return raw.decode("utf-16-le")
        finally:
            self._advapi.CredFree(ptr)

    def delete(self, reference: str) -> None:
        if not self._advapi.CredDeleteW(reference, self.CRED_TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            if error != 1168:
                raise ctypes.WinError(error)


def make_secret_reference(project_id: str, environment: str, session_key: str, name: str, version: int | str) -> str:
    return f"AgentFIXBPRO/FIX/{project_id}/{environment}/{session_key}/{name}/v{version}"
