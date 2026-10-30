"""Windows-only handle/ctypes deletion primitives for contained retention.

Standalone sibling of :mod:`execution_log_retention` (review slice): every
primitive here uses a handle so that a deletion decision is bound to the
object it inspected rather than to a path that a concurrent ancestor
replacement could redirect. Stdlib only; ``ctypes`` is loaded lazily so the
module imports cleanly on every platform and the loader (``_windows_api``)
fails closed (``None``) when the Windows API cannot be constructed.

Retained invariants, unchanged from the behavior this code was extracted
from:

* ``open_object`` opens with ``DELETE`` access and (for directories)
  ``FILE_FLAG_BACKUP_SEMANTICS``; every open carries
  ``FILE_FLAG_OPEN_REPARSE_POINT``, so the handle refers to the reparse
  point itself when the leaf is one — a swapped-in link is inspected, never
  followed past the leaf (ancestor traversal is contained by the caller's
  final-path verification).
* ``final_path`` returns the handle's true final path
  (``GetFinalPathNameByHandleW``), the containment boundary the caller
  compares against the capture root's own final path.
* ``mark_for_delete`` schedules deletion through
  ``SetFileInformationByHandle``/``FileDispositionInfo``, so a concurrent
  ancestor replacement can never redirect a deletion that was already
  verified.
* The lazy singleton caches the constructed glue and returns ``None``
  instead of raising when the API is unavailable: any platform without the
  mechanism fails closed (nothing is deleted through unverified paths).
"""

from __future__ import annotations

from typing import Optional

__all__ = ["_WindowsDeletion", "_windows_api"]


class _WindowsDeletion:
    """Handle-based deletion primitives for Windows (ctypes, stdlib only).

    Every primitive uses a handle so that a deletion decision is bound to
    the object it inspected rather than to a path that a concurrent
    ancestor replacement could redirect."""

    DELETE_ACCESS = 0x00010000
    FILE_SHARE_ALL = 0x00000001 | 0x00000002 | 0x00000004
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    FILE_DISPOSITION_INFO_CLASS = 4
    _EPOCH_OFFSET = 116444736000000000
    _MAXIMUM_FINAL_PATH = 0x8000

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        self._ctypes = ctypes
        self._file_information = _ByHandleFileInformation
        self._invalid_handle = ctypes.c_void_p(-1).value
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateFileW.restype = wintypes.HANDLE
        self._kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.GetFileAttributesW.restype = wintypes.DWORD
        self._kernel32.GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel32.GetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        self._kernel32.GetFinalPathNameByHandleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
        self._kernel32.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]

    # The returned handle refers to the reparse point itself when the leaf is
    # one, so a swapped-in link is inspected, never followed past the leaf;
    # ancestor traversal is contained by the final-path verification.

    def open_object(self, path: str, *, directory: bool) -> Optional[int]:
        flags = self.FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= self.FILE_FLAG_BACKUP_SEMANTICS
        handle = self._kernel32.CreateFileW(
            path,
            self.DELETE_ACCESS,
            self.FILE_SHARE_ALL,
            None,
            self.OPEN_EXISTING,
            flags,
            None,
        )
        if handle in (self._invalid_handle, None):
            return None
        return int(handle)

    def open_query(self, path: str) -> Optional[int]:
        """Directory handle for establishing the root boundary (no delete)."""
        handle = self._kernel32.CreateFileW(
            path,
            0,
            self.FILE_SHARE_ALL,
            None,
            self.OPEN_EXISTING,
            self.FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if handle in (self._invalid_handle, None):
            return None
        return int(handle)

    def close(self, handle: int) -> None:
        self._kernel32.CloseHandle(handle)

    def attributes(self, path: str) -> Optional[int]:
        attributes = self._kernel32.GetFileAttributesW(path)
        if attributes == 0xFFFFFFFF:
            return None
        return int(attributes)

    def handle_information(self, handle: int):
        information = self._file_information()
        if not self._kernel32.GetFileInformationByHandle(handle, self._ctypes.byref(information)):
            return None
        return information

    def final_path(self, handle: int) -> Optional[str]:
        size = 512
        while size <= self._MAXIMUM_FINAL_PATH:
            buffer = self._ctypes.create_unicode_buffer(size)
            written = self._kernel32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
            if written == 0:
                return None
            if written < size:
                return buffer.value
            size = int(written) + 1
        return None

    def is_stale(self, information, cutoff: float) -> bool:
        stamp = information.ftLastWriteTime.dwHighDateTime
        stamp = ((stamp << 32) | information.ftLastWriteTime.dwLowDateTime) - self._EPOCH_OFFSET
        return (stamp / 10**7) < cutoff

    def index(self, information) -> int:
        return (information.nFileIndexHigh << 32) | information.nFileIndexLow

    def volume(self, information) -> int:
        return int(information.dwVolumeSerialNumber)

    def creation_ns(self, information) -> int:
        stamp = (information.ftCreationTime.dwHighDateTime << 32) | (
            information.ftCreationTime.dwLowDateTime
        )
        return (stamp - self._EPOCH_OFFSET) * 100

    def mark_for_delete(self, handle: int) -> bool:
        flag = self._ctypes.c_ubyte(1)
        return bool(
            self._kernel32.SetFileInformationByHandle(
                handle,
                self.FILE_DISPOSITION_INFO_CLASS,
                self._ctypes.byref(flag),
                self._ctypes.sizeof(flag),
            )
        )


_WINAPI: Optional[_WindowsDeletion] = None


def _windows_api() -> Optional[_WindowsDeletion]:
    global _WINAPI
    if _WINAPI is None:
        try:
            _WINAPI = _WindowsDeletion()
        except Exception:  # pragma: no cover - defensive: fail closed instead
            return None
    return _WINAPI