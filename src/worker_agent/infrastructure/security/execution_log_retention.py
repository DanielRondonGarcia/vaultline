"""Focused execution-log retention component (S3 in
``odd/tasks/persistent-worker-execution-logs.md``).

Contains the 30-day managed-log pruning sweep: it deletes managed job logs
older than the retention window under the fixed host structure, collapses
the emptied managed directories and reports what it did as a summary — no
capture, redaction, spawn or Docker logic lives here, so the sibling
``execution_log_capture`` module stays the single capture entrypoint and
this slice stays a small, self-contained module that imports nothing from
the capture module or the Docker SDK. The Windows-only handle/ctypes
deletion primitives it uses live in the sibling
``execution_log_windows`` module (the lazy cached loader ``_windows_api``),
and the POSIX descriptor-relative containment half (the pinned root
descriptor, the ``O_NOFOLLOW`` component walk, the contained unlink and
the contained empty-directory collapse) lives in the sibling
``execution_log_posix`` module.

Retained invariants (unchanged from the behavior this module was extracted
from): the retention window and the root default come from the committed
path policy (:class:`ExecutionLogPaths`, the single source, so the default
stays «30 días (recomendado)» and any ``WORKER_EXECUTION_LOG_RETENTION_DAYS``
override is clamped to the supported 1..365 window); only logs matching the
exact managed layout the path policy produces are ever deleted, no matter
what root value is supplied; the sweep never follows symbolic links and
rejects every directory reparse point (NTFS junctions included): a reparse
point is never descended into, never pruned, never collapsed and never
removed, and any entry whose attributes cannot be established fails closed
(no traversal, no deletion, only a reported failure); and :meth:`prune`
never raises (fail-open semantics, S4) — every failure is counted in the
returned summary for the caller to report and alert.

Race containment for deletion (independent verification reproduced a
critical Windows escape): verifying only the leaf's metadata and unlinking
by path is not sufficient, because an ancestor managed directory replaced
by an NTFS junction (or a POSIX symlink) between the sweep and the deletion
redirects every path resolution below it; the stale log outside the
configured root that the path now names would be deleted. Candidates
additionally carry sweep-time identity captured from the file object itself
(Windows ``DirEntry`` enumeration exposes no ``st_ino``/``st_dev``, so the
leaf is stat'ed once non-following at sweep time; identity that cannot be
captured fails closed). Deletion is
therefore verified contained and executed through objects, not paths:

* Windows: the leaf is opened by handle and the handle's true final path
  (``GetFinalPathNameByHandleW``) must be strictly inside the capture
  root's own true final path; the verified object is then deleted through
  that same handle (``SetFileInformationByHandle``/``FileDispositionInfo``),
  so a concurrent ancestor replacement can never redirect a deletion that
  was already verified.
* POSIX: the configured root directory object is pinned once per
  ``prune`` as a file descriptor; the ancestry is opened component by
  component with ``O_NOFOLLOW`` relative to that pinned descriptor and the
  leaf is unlinked relative to its pinned parent descriptor, so no link,
  junction or reparse redirect placed in a replaced ancestor - or in the
  root path itself - can ever redirect the walk (bind mounts are not a
  reparse/symlink mechanism and are not blocked by ``O_NOFOLLOW``;
  containing them is outside this mechanism's guarantee and the leaf's
  identity is re-verified after the open): renames of the pinned root keep
  working (still the same configured object) and a replacement root is
  simply never opened. The leaf is opened non-blocking so a FIFO or device
  swapped into the swept candidate's place can never hang the caller, and
  a nonregular leaf is never removed. Candidate identity checks (device,
  inode, staleness and regularity) stop a mid-sweep swap from deleting a
  different file, including one sharing the candidate's inode through a
  hard link in a replacement tree.
* Any platform where one of these mechanisms is unavailable fails closed:
  nothing is deleted through unverified paths and every candidate is
  reported as a failed deletion.
"""

from __future__ import annotations

import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from .execution_log_paths import ExecutionLogPaths
    from .execution_log_posix import ExecutionLogPosix
    from .execution_log_windows import _windows_api
except ImportError:  # source-mode child ran as a directly executed script:
    # containing directory sys.path[0] keeps the sibling imports working
    # from any cwd; frozen workers package the same module set.
    from execution_log_paths import ExecutionLogPaths  # type: ignore[no-redef]
    from execution_log_posix import ExecutionLogPosix  # type: ignore[no-redef]
    from execution_log_windows import _windows_api  # type: ignore[no-redef]


# The Windows-only handle/ctypes deletion primitives (``_WindowsDeletion``
# and the lazy ``kernel32`` glue) live in the sibling
# ``execution_log_windows`` module and are obtained through the cached,
# fail-closed loader ``_windows_api`` imported above; the POSIX
# descriptor-relative containment half lives in the sibling
# ``execution_log_posix`` module. Nothing about the contained deletion's
# behavior changed in the extraction.


class ExecutionLogRetention:
    """Managed-log pruning for the fixed execution-log structure."""

    # Reparse-point handling: every directory reparse point (NTFS junctions
    # included, plus mounted volumes and any other tag) may redirect anywhere
    # on the volume, so a reparse point is never followed, never collapsed and
    # never removed, even when its relative layout matches the managed
    # structure. Windows non-following stat exposes ``st_reparse_tag``;
    # other platforms default to no tag so symlink-free POSIX behavior is
    # preserved through the same single check.

    # Retention-policy constants stay delegated to the committed path policy.
    RETENTION_ENV = ExecutionLogPaths.RETENTION_ENV
    DEFAULT_RETENTION_DAYS = ExecutionLogPaths.DEFAULT_RETENTION_DAYS
    MIN_RETENTION_DAYS = ExecutionLogPaths.MIN_RETENTION_DAYS
    MAX_RETENTION_DAYS = ExecutionLogPaths.MAX_RETENTION_DAYS
    # Retention sweep bounds: a bounded deletion batch and a bounded walk
    # depth keep an oversized or hostile root from stalling pruning forever.
    MAX_PRUNE_DELETIONS = 1000
    MAX_WALK_DEPTH = 6

    @classmethod
    def default_root(cls) -> str:
        return ExecutionLogPaths.default_root()

    @classmethod
    def retention_days(cls) -> int:
        return ExecutionLogPaths.retention_days()

    @classmethod
    def prune(
        cls,
        root: object = None,
        *,
        now: Optional[datetime] = None,
        retention_days: Optional[int] = None,
    ) -> Dict[str, object]:
        """Delete managed logs older than the retention window; never raises."""
        if retention_days is None:
            days = cls.retention_days()
        else:
            if isinstance(retention_days, bool) or not isinstance(retention_days, int):
                raise ValueError("retention days must be an integer")
            days = min(cls.MAX_RETENTION_DAYS, max(cls.MIN_RETENTION_DAYS, retention_days))
        root_path = Path(root) if root is not None else Path(cls.default_root())
        moment = (now if now is not None else datetime.now(timezone.utc)).astimezone(timezone.utc)
        summary: Dict[str, object] = {
            "retention_days": days,
            "inspected": 0,
            "deleted": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }
        if not root_path.is_dir():
            return summary
        # The containment boundary is established once per prune: on Windows
        # from the root's true final path obtained via a handle, on POSIX by
        # pinning the root directory object itself; when the boundary cannot
        # be established, deletion fails closed for every candidate.
        root_fd: Optional[int] = None
        root_final: Optional[str] = None
        if sys.platform == "win32":
            root_final = cls._root_final_path(root_path)
        else:
            if ExecutionLogPosix.CONTAINED:
                try:
                    root_fd = ExecutionLogPosix.pin_root(root_path)
                except OSError:
                    # The configured root disappeared before it could be
                    # pinned; fail closed without touching anything.
                    return summary
        cutoff = (moment - timedelta(days=days)).timestamp()
        try:
            try:
                candidates, collapsible = cls._sweep(root_path, cutoff, summary)
            except OSError:
                summary["failed"] = summary["inspected"]
                summary["errors"].append("retention sweep failed to walk the capture root")
                return summary
            for parts, info in candidates[: cls.MAX_PRUNE_DELETIONS]:
                status, message = cls._delete_candidate(
                    root_path, root_fd, root_final, parts, info, cutoff
                )
                if status == "deleted":
                    summary["deleted"] += 1
                elif status == "failed":
                    summary["failed"] += 1
                    if message and len(summary["errors"]) < 5:
                        summary["errors"].append(message)
                else:
                    summary["skipped"] += 1
            cls._collapse_directories(root_path, root_fd, root_final, collapsible)
            return summary
        finally:
            if root_fd is not None:
                os.close(root_fd)

    @classmethod
    def _is_reparse_point(cls, info: os.stat_result) -> bool:
        """Whether the non-following stat names a reparse point (symlink,
        junction or any other tag): such entries are never trusted."""
        return getattr(info, "st_reparse_tag", 0) != 0

    @classmethod
    def _sweep(
        cls,
        root: Path,
        cutoff: float,
        summary: Dict[str, object],
    ) -> Tuple[List[Tuple[Tuple[str, ...], os.stat_result]], List[Tuple[Tuple[str, ...], int]]]:
        """Walk the managed structure and return the stale candidates and the
        collapsible managed directories. Candidates carry their relative
        parts and the sweep-time attributes so deletion can re-verify the
        exact object it is about to remove inside the containment boundary."""
        candidates: List[Tuple[Tuple[str, ...], os.stat_result]] = []
        collapsible: List[Tuple[Tuple[str, ...], int]] = []
        pending: List[Tuple[Path, int, Tuple[str, ...]]] = [(root, 0, ())]
        while pending:
            directory, depth, relative = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        child_parts = relative + (entry.name,)
                        child_path = Path(entry.path)
                        try:
                            # Non-following stat is the single source of the
                            # entry shape: it never traverses links or
                            # junctions and it exposes the reparse tag.
                            info = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            # Fail closed: attributes could not be established,
                            # so the entry must never be trusted for traversal,
                            # pruning or collapse; the failure is reported (S4).
                            if (
                                ExecutionLogPaths.managed_log_relative_parts(child_parts)
                                or ExecutionLogPaths.managed_directory_prefix(child_parts)
                            ):
                                summary["failed"] += 1
                                if len(summary["errors"]) < 5:
                                    summary["errors"].append(
                                        f"{child_parts[-1]}: {exc.__class__.__name__}"
                                    )
                            continue
                        if cls._is_reparse_point(info):
                            # Never follow, prune, collapse or remove a
                            # reparse point; its target may be anywhere.
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if ExecutionLogPaths.managed_directory_prefix(child_parts):
                                collapsible.append((child_parts, depth + 1))
                            if depth < cls.MAX_WALK_DEPTH:
                                pending.append((child_path, depth + 1, child_parts))
                            continue
                        if not stat.S_ISREG(info.st_mode) or not entry.name.endswith(".log"):
                            continue
                        if not ExecutionLogPaths.managed_log_relative_parts(child_parts):
                            continue
                        summary["inspected"] += 1
                        if info.st_mtime >= cutoff:
                            continue
                        # Reliable sweep-time identity: on Windows
                        # ``DirEntry.stat`` carries no ``st_ino``/``st_dev``
                        # (FindFirstFile data), so the leaf's file object is
                        # stat'ed non-following once here; deletion then only
                        # removes the very file the sweep measured.
                        identity = cls._sweep_identity(child_path, info)
                        if identity is None:
                            # Fail closed: identity could not be established,
                            # so the candidate can never be deleted safely.
                            summary["failed"] += 1
                            if len(summary["errors"]) < 5:
                                summary["errors"].append(
                                    f"{entry.name}: candidate identity could not be established"
                                )
                            continue
                        if identity.st_mtime < cutoff:
                            candidates.append((child_parts, identity))
            except OSError:
                if len(summary["errors"]) < 5:
                    summary["errors"].append(f"{directory}: scan failed")
        return candidates, collapsible

    # --- contained deletion -------------------------------------------------

    @staticmethod
    def _sweep_identity(
        child_path: Path,
        info: os.stat_result,
    ) -> Optional[os.stat_result]:
        """Reliable sweep-time identity of a deletion candidate. Windows
        directory enumeration (``DirEntry.stat``) exposes no ``st_ino`` or
        ``st_dev`` (FindFirstFile data), so a plain non-following stat of
        the leaf re-establishes identity from the file object itself (it
        never follows the leaf's own reparse points; a swapped ancestor is
        later rejected at deletion by the containment boundary or the
        identity mismatch). Returns ``None`` when identity cannot be
        obtained: fail closed, the candidate is never deletable."""
        if os.name != "nt":
            return info
        try:
            identity = os.lstat(child_path)
        except OSError:
            return None
        if identity.st_ino == 0:
            return None
        return identity

    @classmethod
    def _root_final_path(cls, root: Path) -> Optional[str]:
        """True final path of the trusted containment boundary (the capture
        root), or ``None`` when it cannot be established: deletion then fails
        closed for every candidate."""
        if sys.platform != "win32":
            return None
        api = _windows_api()
        if api is None:
            return None
        try:
            handle = api.open_query(str(root))
        except OSError:
            return None
        if handle is None:
            return None
        try:
            return api.final_path(handle)
        finally:
            api.close(handle)

    @staticmethod
    def _contained(final: str, root_final: str) -> bool:
        """Whether a true final path is strictly inside the root boundary."""
        boundary = root_final.rstrip("\\") + "\\"
        return final.casefold().startswith(boundary.casefold())

    @classmethod
    def _delete_candidate(
        cls,
        root: Path,
        root_fd: Optional[int],
        root_final: Optional[str],
        parts: Tuple[str, ...],
        info: os.stat_result,
        cutoff: float,
    ) -> Tuple[str, Optional[str]]:
        """Delete one swept candidate only after verifying the removal is
        contained in the pinned capture-root boundary. Returns
        ``(status, error)`` with status ``deleted``, ``skipped`` or
        ``failed``; a ``failed`` status covers containment violations and
        unavailable mechanisms (fail closed, S4)."""
        if sys.platform == "win32":
            return cls._windows_delete_file(root, root_final, parts, info, cutoff)
        return ExecutionLogPosix.delete_file(root_fd, parts, info, cutoff)

    @classmethod
    def _windows_delete_file(
        cls,
        root: Path,
        root_final: Optional[str],
        parts: Tuple[str, ...],
        info: os.stat_result,
        cutoff: float,
    ) -> Tuple[str, Optional[str]]:
        api = _windows_api()
        if api is None:
            return "failed", f"{parts[-1]}: handle-based deletion unavailable"
        if root_final is None:
            return "failed", f"{parts[-1]}: capture root containment could not be established"
        path = os.path.join(str(root), *parts)
        try:
            attributes = api.attributes(path)
            if attributes is None:
                return "failed", f"{parts[-1]}: attributes could not be established"
            if attributes & (api.FILE_ATTRIBUTE_REPARSE_POINT | api.FILE_ATTRIBUTE_DIRECTORY):
                return "skipped", None
            handle = api.open_object(path, directory=False)
            if handle is None:
                return "failed", f"{parts[-1]}: managed log could not be opened for deletion"
            try:
                current = api.handle_information(handle)
                if current is None:
                    return "failed", f"{parts[-1]}: handle information could not be established"
                if current.dwFileAttributes & api.FILE_ATTRIBUTE_REPARSE_POINT:
                    return "skipped", None
                if current.dwFileAttributes & api.FILE_ATTRIBUTE_DIRECTORY:
                    return "skipped", None
                final = api.final_path(handle)
                if final is None:
                    return "failed", f"{parts[-1]}: true path of the managed log could not be established"
                if not cls._contained(final, root_final):
                    # Containment always wins over skip or delete decisions.
                    return "failed", f"{parts[-1]}: {final} is outside the capture root"
                swept_index = int(getattr(info, "st_ino", 0) or 0)
                if swept_index and api.index(current) != swept_index:
                    return "skipped", None
                # Volume identity is compared on the volume-serial (32-bit)
                # domain on both sides: the handle's ``dwVolumeSerialNumber``
                # is a DWORD by definition, while Windows Python 3.12+ fills
                # ``lstat().st_dev`` with the full 64-bit volume identity
                # (its low 32 bits are the same serial), so a raw equality
                # against the wider swept value would skip every ordinary
                # same-volume deletion. The full-width high bits never
                # decide anything, and a genuinely different volume still
                # differs after masking; the file identity itself (index,
                # creation time, size) is checked unchanged and at full
                # width, so identity preservation stays unchanged in kind
                # and strength (fail closed).
                swept_volume = int(getattr(info, "st_dev", 0) or 0)
                if swept_volume and (api.volume(current) & 0xFFFFFFFF) != (
                    swept_volume & 0xFFFFFFFF
                ):
                    return "skipped", None
                swept_creation = getattr(info, "st_ctime_ns", None)
                if swept_creation is not None and int(swept_creation) != api.creation_ns(current):
                    return "skipped", None
                if current.nFileSizeHigh << 32 | current.nFileSizeLow != int(getattr(info, "st_size", -1)):
                    return "skipped", None
                if not api.is_stale(current, cutoff):
                    return "skipped", None
                if not api.mark_for_delete(handle):
                    return "failed", f"{parts[-1]}: deletion could not be scheduled"
                return "deleted", None
            finally:
                api.close(handle)
        except OSError as exc:
            return "failed", f"{parts[-1]}: {exc.__class__.__name__}"

    # --- collapses ----------------------------------------------------------

    @classmethod
    def _collapse_directories(
        cls,
        root: Path,
        root_fd: Optional[int],
        root_final: Optional[str],
        collapsible: List[Tuple[Tuple[str, ...], int]],
    ) -> None:
        """Best-effort removal of emptied managed directories, deepest first,
        through the same containment as file deletion: only a real (non-reparse)
        directory whose verified location is inside the pinned boundary is ever
        removed."""
        for parts, _ in sorted(collapsible, key=lambda item: item[1], reverse=True):
            if sys.platform == "win32":
                cls._windows_remove_directory(root, root_final, parts)
            else:
                ExecutionLogPosix.remove_directory(root_fd, parts)

    @classmethod
    def _windows_remove_directory(
        cls,
        root: Path,
        root_final: Optional[str],
        parts: Tuple[str, ...],
    ) -> bool:
        if root_final is None:
            return False
        api = _windows_api()
        if api is None:
            return False
        path = os.path.join(str(root), *parts)
        try:
            attributes = api.attributes(path)
            if attributes is None:
                return False
            if (attributes & api.FILE_ATTRIBUTE_REPARSE_POINT) or not (
                attributes & api.FILE_ATTRIBUTE_DIRECTORY
            ):
                return False
            handle = api.open_object(path, directory=True)
            if handle is None:
                return False
            try:
                current = api.handle_information(handle)
                if current is None:
                    return False
                if current.dwFileAttributes & api.FILE_ATTRIBUTE_REPARSE_POINT:
                    return False
                if not (current.dwFileAttributes & api.FILE_ATTRIBUTE_DIRECTORY):
                    return False
                final = api.final_path(handle)
                if final is None or not cls._contained(final, root_final):
                    return False
                if not api.mark_for_delete(handle):
                    return False
                return True
            finally:
                api.close(handle)
        except OSError:
            return False


__all__ = ["ExecutionLogRetention"]