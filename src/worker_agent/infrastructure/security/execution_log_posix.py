"""POSIX descriptor-relative containment for execution-log deletion (S1-S6
in ``odd/tasks/persistent-worker-execution-logs.md``).

Standalone sibling of :mod:`execution_log_retention` (review slice):
exclusively the POSIX half of the race containment that binds every
deletion and collapse to objects, never to paths. Stdlib only; the module
imports cleanly on every platform and every entry point fails closed
wherever the mechanism is unavailable.

The configured root directory object is pinned once per sweep as a file
descriptor; the ancestry is opened component by component with
``O_NOFOLLOW`` relative to that pinned descriptor and the leaf is unlinked
relative to its pinned parent descriptor, so no link, junction or reparse
redirect placed in a replaced ancestor - or in the root path itself - can
ever redirect the walk: renames of the pinned root keep working (still the
same configured object) and a replacement root is simply never opened.
Bind mounts are not a reparse/symlink mechanism and are NOT blocked by
``O_NOFOLLOW``: a mounted subtree reached through a verified real
directory is opened transparently, which is why the candidate identity
checks below also compare the device identity of the live object.
Candidate identity checks (device, inode, staleness and regularity) stop a
mid-sweep swap from deleting a different file, including one sharing the
candidate's inode through a hard link in a replacement tree, and stop a
swapped-in FIFO, directory or device: the leaf is opened non-blocking and
a nonregular leaf is never removed, so a planted FIFO or character device
cannot hang or be deleted through a verified traversal.

Any platform where one of these mechanisms is unavailable fails closed:
nothing is deleted through unverified paths and every candidate is reported
as a failed deletion by the retention caller.
"""

from __future__ import annotations

import os
import stat
from typing import Optional, Tuple

__all__ = ["ExecutionLogPosix"]


class ExecutionLogPosix:
    """Descriptor-relative containment for managed-log deletion on POSIX."""

    # Contained deletion requires descriptor-relative opens and removal plus
    # O_NOFOLLOW, and a non-blocking leaf open: a FIFO (or device) replaced
    # in the swept leaf's place must never hang the pruning caller, so the
    # capability without a non-blocking open fails closed too. The same
    # single check used to live in the retention caller and keeps its exact
    # shape.

    CONTAINED = (
        hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NONBLOCK")
        and os.open in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
    )

    @classmethod
    def pin_root(cls, root: object) -> Optional[int]:
        """Pin the configured root directory object as a descriptor: the
        containment boundary established once per prune. Returns ``None``
        when the mechanism is unavailable: deletion then fails closed for
        every candidate. Raises ``OSError`` when the configured root cannot
        be opened, for the caller to fail closed without touching anything."""
        if not cls.CONTAINED:
            return None
        # No O_NOFOLLOW on purpose: an operator-configured symlinked root is
        # the configured boundary, and replacement of the root path later in
        # the prune cannot migrate the deletion because nothing re-opens
        # this path again.
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    @classmethod
    def open_directory_chain(cls, root_fd: int, parts: Tuple[str, ...]) -> int:
        """Open each named directory below the pinned root with ``O_NOFOLLOW``
        and return the deepest descriptor: every traversal step is verified a
        real directory, so a link or junction replacing an ancestor can never
        redirect the walk. Raises ``OSError`` otherwise. The caller closes the
        returned descriptor unless it is ``root_fd`` itself."""
        fd = root_fd
        owned = False
        try:
            for part in parts:
                opened = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                if owned:
                    os.close(fd)
                fd, owned = opened, True
            return fd
        except BaseException:
            if owned:
                os.close(fd)
            raise

    @classmethod
    def delete_file(
        cls,
        root_fd: Optional[int],
        parts: Tuple[str, ...],
        info,
        cutoff: float,
    ) -> Tuple[str, Optional[str]]:
        """Delete one swept candidate only after verifying, inside the pinned
        boundary, that the live object still matches the swept identity and
        is still stale. Returns ``(status, error)`` with status ``deleted``,
        ``skipped`` or ``failed``; a ``failed`` status covers containment
        violations and unavailable mechanisms (fail closed, S4)."""
        if not cls.CONTAINED or root_fd is None:
            return "failed", f"{parts[-1]}: no pinned root descriptor; deletion fails closed"
        try:
            parent_fd = cls.open_directory_chain(root_fd, parts[:-1])
        except OSError as exc:
            return "failed", f"{parts[-1]}: {exc.__class__.__name__}"
        try:
            try:
                leaf_fd = os.open(
                    parts[-1],
                    # Non-blocking on purpose: a FIFO or device swapped into
                    # the swept leaf's place would otherwise hang the caller
                    # on open; with O_NONBLOCK the open returns at once and
                    # the regularity check below fails closed (skipped)
                    # without removing the nonregular object.
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                return "failed", f"{parts[-1]}: {exc.__class__.__name__}"
            try:
                current = os.fstat(leaf_fd)
            finally:
                os.close(leaf_fd)
            if not stat.S_ISREG(current.st_mode):
                return "skipped", None
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                # The swept managed log is no longer this exact file.
                return "skipped", None
            if current.st_size != info.st_size:
                # A replacement reused the swept file's inode number.
                return "skipped", None
            if cls._ctime_changed(current, info):
                # A replacement reused the swept file's inode number.
                return "skipped", None
            if current.st_mtime >= cutoff:
                # Touched back into the retention window meanwhile.
                return "skipped", None
            try:
                os.unlink(parts[-1], dir_fd=parent_fd)
            except OSError as exc:
                return "failed", f"{parts[-1]}: {exc.__class__.__name__}"
            return "deleted", None
        finally:
            if parent_fd != root_fd:
                os.close(parent_fd)

    @classmethod
    def remove_directory(cls, root_fd: Optional[int], parts: Tuple[str, ...]) -> bool:
        """Best-effort removal of one emptied managed directory through the
        same pinned-boundary containment; ``False`` leaves it untouched."""
        if not cls.CONTAINED or root_fd is None:
            return False
        try:
            parent_fd = cls.open_directory_chain(root_fd, parts[:-1])
        except OSError:
            return False
        try:
            try:
                os.rmdir(parts[-1], dir_fd=parent_fd)
                return True
            except OSError:
                return False
        finally:
            if parent_fd != root_fd:
                os.close(parent_fd)

    @staticmethod
    def _ctime_changed(current, info) -> bool:
        """Whether the file's metadata-change time moved since the sweep;
        catches a replacement that reused the swept file's inode number
        (common on filesystems that recycle inodes immediately)."""
        try:
            return int(getattr(info, "st_ctime_ns")) != int(getattr(current, "st_ctime_ns"))
        except (AttributeError, TypeError, ValueError):
            return False