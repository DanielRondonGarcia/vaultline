import subprocess
import os
import sys
import json
import errno
import logging
import re
import shutil
import stat
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from src.app.domain.models import BackupConfig, BackupResult, RestoreConfig, RestoreResult
from src.app.application.ports.ports import BackupStrategy, RestoreStrategy
from src.app.domain.restore_ownership import RestoreOwnershipPolicy, parse_uid_gid
from src.restic_retention import build_restic_forget_command
from src.restic_executable import (
    resolve_restic_executable,
    restic_command_with_backend_options,
    restic_subprocess_environment,
)
from src.rclone_executable import resolve_rclone_executable

logger = logging.getLogger(__name__)

SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
UNSUPPORTED_METADATA = ["acl", "xattr", "timestamps", "special metadata"]
RESTIC_LOCK_MARKERS = ("repository is already locked", "unable to create lock in backend")
RESTIC_LOCK_STILL_HELD_HINT = (
    "'restic unlock' only removes locks that restic considers stale; "
    "a lock held by a live restic process on this or another host must finish or be released there"
)
_RESTIC_LOCK_AGE = re.compile(r"\(([^()]+) ago\)")


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def describe_restic_lock(stderr: Any, return_code: int) -> Optional[str]:
    """Return a short description when a failed restic command hit a repository lock, else None."""
    if return_code == 0 or not isinstance(stderr, str):
        return None
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not any(marker in line.casefold() for line in lines for marker in RESTIC_LOCK_MARKERS):
        return None
    for line in lines:
        age = _RESTIC_LOCK_AGE.search(line)
        if "lock was created at" in line.casefold() and age:
            return f"lock created {age.group(1)} ago"
    return "lock age unknown"


def _require_executable(name: str) -> str:
    if name == "restic" and getattr(sys, "frozen", False):
        return resolve_restic_executable()
    if name == "rclone" and getattr(sys, "frozen", False):
        return resolve_rclone_executable()
    if shutil.which(name) is None:
        raise FileNotFoundError(f"required executable '{name}' is not available on PATH")
    return name


class OwnershipNormalizationError(RuntimeError):
    def __init__(self, message: str, report: dict[str, Any]):
        self.report = report
        self.category = report.get("category", "ownership_normalization_failed")
        super().__init__(message)


class NativeSourceSelectionError(ValueError):
    """Raised when the exact configured native source cannot be restored
    through a validated restic snapshot subtree; fail closed before mutation."""

    def __init__(self, message: str, category: str):
        super().__init__(message)
        self.category = category


class ReadOnlyRestoreTargetError(ValueError):
    category = "readonly_target"


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _ownership_policy(config: RestoreConfig) -> RestoreOwnershipPolicy:
    value = getattr(config, "restore_ownership", None)
    if value is None and config.chown:
        return RestoreOwnershipPolicy(mode="map", default_mapping=config.chown, confirmation="confirmed", source="RESTORE_CHOWN")
    if value is None:
        return RestoreOwnershipPolicy()
    policy = value if isinstance(value, RestoreOwnershipPolicy) else RestoreOwnershipPolicy.from_dict(value)
    return policy.require_confirmation()


def _normalization_targets(config: RestoreConfig, policy: RestoreOwnershipPolicy) -> list[tuple[str, str, str]]:
    if policy.mode != "map":
        return []
    targets: list[tuple[str, str, str]] = []
    for scope in getattr(config, "volume_scopes", ()) or ():
        key = _field(scope, "volume_key") or _field(scope, "stable_key")
        path = _field(scope, "target_path") or _field(scope, "destination")
        mapping = policy.mapping_text(key) if key else None
        if key and path and mapping:
            targets.append((str(path), mapping, str(key)))
    if targets:
        return targets
    if policy.default_mapping:
        return [(config.target_path, policy.default_mapping, "default")]
    return [(config.target_path, next(iter(policy.mappings.values())), "direct")] if len(policy.mappings) == 1 and not _should_use_backup_dir_layout(config) else []


def _apply_chown(target_path: str, chown: str) -> dict[str, Any]:
    uid, gid = parse_uid_gid(chown)
    report: dict[str, Any] = {"state": "running", "changed_count": 0, "unsupported_metadata": list(UNSUPPORTED_METADATA)}
    pending = [Path(target_path)]
    current_path = target_path
    try:
        while pending:
            current = pending.pop()
            current_path = str(current)
            current_stat = os.lstat(current)
            if not report["changed_count"] and stat.S_ISLNK(current_stat.st_mode):
                raise ValueError("refusing to normalize a symlink target")
            os.lchown(current_path, uid, gid)
            report["changed_count"] += 1
            if stat.S_ISDIR(current_stat.st_mode):
                with os.scandir(current) as entries:
                    pending.extend(Path(entry.path) for entry in entries)
    except (AttributeError, OSError, ValueError) as exc:
        report.update({"state": "partial" if report["changed_count"] else "failed", "category": "ownership_normalization_failed", "failed_path": current_path, "detail": str(exc)})
        raise OwnershipNormalizationError(f"Ownership normalization failed at {current_path}: {exc}", report) from exc
    report.update({"state": "complete", "changed": bool(report["changed_count"])})
    return report


def normalize_ownership(config: RestoreConfig, policy: RestoreOwnershipPolicy, created_paths: Optional[list[Path]] = None) -> dict[str, Any]:
    base = {"changed_count": 0, "changed": False, "unsupported_metadata": list(UNSUPPORTED_METADATA)}
    if policy.mode != "map" or (not policy.mappings and not policy.default_mapping):
        return {**base, "state": "preserved", "category": "preserved"}
    if created_paths is not None:
        # Preserve mode: the mapping may only normalize ownership of entries the
        # restore just created; preserved files and directories are never chowned.
        if not created_paths:
            return {**base, "state": "preserved", "category": "preserved"}
        targets = _normalization_targets(config, policy)
        if not targets:
            raise OwnershipNormalizationError(
                "No stable target scope is available for ownership mapping of restored files",
                {**base, "state": "failed", "category": "unknown_volume_scope"},
            )
        changed_count = 0
        for created in created_paths:
            if Path(created).is_symlink():
                continue
            match = next(
                ((scope_path, mapping) for scope_path, mapping, _key in targets if _path_is_within(str(created), scope_path)),
                None,
            )
            if match is None:
                raise OwnershipNormalizationError(
                    f"Restored path {created} is outside the mapped ownership scopes",
                    {**base, "state": "failed", "category": "unknown_volume_scope", "failed_path": str(created)},
                )
            report = _apply_chown(str(created), match[1])
            changed_count += report["changed_count"]
        base["changed_count"] = changed_count
        return {**base, "state": "complete", "category": "ok", "changed": bool(changed_count)}
    targets = _normalization_targets(config, policy)
    if not targets:
        raise OwnershipNormalizationError("No stable target scope is available for ownership mapping", {**base, "state": "failed", "category": "unknown_volume_scope"})
    for path, mapping, _ in targets:
        try:
            report = _apply_chown(path, mapping)
        except OwnershipNormalizationError as exc:
            exc.report["changed_count"] += base["changed_count"]
            raise
        base["changed_count"] += report["changed_count"]
    return {**base, "state": "complete", "category": "ok", "changed": bool(base["changed_count"])}


def _attach_restore_evidence(result: RestoreResult, policy: RestoreOwnershipPolicy, normalization: dict[str, Any], destructive_state: str, category: str) -> RestoreResult:
    policy_data = policy.to_dict()
    policy_data.update({key: value for key, value in (("source", policy.source), ("default_mapping", policy.default_mapping)) if value})
    result.category, result.policy, result.normalization = category, policy_data, normalization
    result.unsupported_metadata = normalization.get("unsupported_metadata", list(UNSUPPORTED_METADATA))
    result.destructive_state, result.partial = destructive_state, destructive_state in {"partial", "unknown"}
    return result

def _clear_target_contents(target_path: str) -> None:
    target = Path(target_path)
    if str(target.resolve()) == "/":
        raise ValueError("Refusing to restore into filesystem root")
    if target.is_symlink():
        raise ValueError("Refusing to restore through a symlink target")
    target.mkdir(parents=True, exist_ok=True)
    for child in target.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

def _sqlite_warnings(target_path: str) -> list[str]:
    warnings = []
    for root, _, files in os.walk(target_path):
        for name in files:
            if name.lower().endswith(SQLITE_SUFFIXES):
                db_path = os.path.join(root, name)
                warnings.append(
                    f"SQLite-like file restored at {db_path}; ensure the DB file and parent directory "
                    "are writable by the runtime UID/GID for journal, WAL, and temp files."
                )
    return warnings

def _should_use_backup_dir_layout(config: RestoreConfig) -> bool:
    layout = (config.layout or "auto").lower()
    if layout == "backup-dir":
        return True
    if layout == "auto":
        return os.path.normpath(config.target_path) == "/backup"
    return False

def _replace_path(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()

    if source.is_symlink():
        target_is_directory = stat.S_ISDIR(os.lstat(source).st_mode)
        os.symlink(os.readlink(source), destination, target_is_directory=target_is_directory)
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)

def _copy_directory_contents(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        _replace_path(item, target_dir / item.name)

_PRESERVE_ROOT_PATTERN = re.compile(r"^[A-Za-z]:\\\\?$")


def _ensure_safe_preserve_target(target_dir: Path) -> None:
    """Guard for the preserve restore target: never a filesystem or drive root,
    never a symlink (existing or broken), and only a directory when it exists.
    Preserve mode never clears, so this guard must reject before any write."""
    absolute = os.path.abspath(str(target_dir))
    if absolute == os.path.abspath(os.sep) or _PRESERVE_ROOT_PATTERN.fullmatch(absolute):
        raise ValueError("Refusing to restore into a filesystem root")
    if os.path.lexists(absolute):
        target_stat = os.lstat(absolute)
        if stat.S_ISLNK(target_stat.st_mode):
            raise ValueError(f"Refusing to restore through a symlink target: {target_dir}")
        if not stat.S_ISDIR(target_stat.st_mode):
            raise ValueError(f"Preserve restore target must be a directory: {target_dir}")


def _copy_missing_file_exclusively(source: Path, destination: Path) -> int:
    """Create the destination exclusively (O_CREAT | O_EXCL, with O_NOFOLLOW where
    supported); an existing file, symlink, or competitor-created entry is left
    untouched and reported as preserved. Copies content and restores the source's
    timestamps and permission bits for the created entry only. Returns 1 when the
    file was created, 0 when an existing entry won the race."""
    if os.name == "nt":
        # os.O_NOFOLLOW is unavailable on Windows; lstat narrows the broken-symlink
        # window, and O_EXCL below covers plain existing entries.
        try:
            os.lstat(str(destination))
            return 0
        except FileNotFoundError:
            pass
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(
            str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o666
        )
    except FileExistsError:
        return 0
    except OSError as exc:
        if exc.errno in (errno.EEXIST, errno.ELOOP):
            return 0
        raise
    with os.fdopen(descriptor, "wb") as handle:
        with open(str(source), "rb") as source_handle:
            shutil.copyfileobj(source_handle, handle)
    shutil.copystat(str(source), str(destination), follow_symlinks=False)
    return 1

def _restore_missing_only(
    source_dir: Path,
    target_dir: Path,
    actions: Optional[list[str]] = None,
    created_paths: Optional[list[Path]] = None,
    *,
    _covered: bool = False,
) -> tuple[int, int]:
    """Copy only items that do not exist under target_dir, without following or
    replacing existing entries (including broken symlinks and file/dir collisions).
    Every creation uses exclusive primitives (O_EXCL file creation, exclusive
    mkdir, symlink creation that fails on existing entries), so a competitor that
    wins the check/create race is preserved instead of overwritten. Recurses into
    matching pairs of existing directories so missing files inside them are
    restored. created_paths records only the topmost created roots (ownership
    normalization walks each created subtree itself)."""
    if created_paths is None:
        created_paths = []
    if actions is None:
        actions = []
    if not _covered:
        _ensure_safe_preserve_target(target_dir)
        if not os.path.lexists(str(target_dir)):
            os.makedirs(str(target_dir))
            root_stat = os.lstat(str(target_dir))
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                raise ValueError(f"Preserve restore target must be a directory: {target_dir}")
            created_paths.append(target_dir)
    restored_count = 0
    kept_count = 0
    for item in sorted(source_dir.iterdir()):
        destination = target_dir / item.name
        item_is_dir = item.is_dir() and not item.is_symlink()
        destination_is_plain_dir = False
        try:
            destination_stat = os.lstat(str(destination))
            destination_is_plain_dir = (
                stat.S_ISDIR(destination_stat.st_mode)
                and not stat.S_ISLNK(destination_stat.st_mode)
            )
        except FileNotFoundError:
            destination_is_plain_dir = False
        if destination_is_plain_dir and item_is_dir:
            restored, kept = _restore_missing_only(
                item, destination, actions, created_paths, _covered=_covered
            )
            restored_count += restored
            kept_count += kept
            continue
        if os.path.lexists(str(destination)):
            kept_count += 1
            logger.debug("Preserving existing target entry untouched: %s", destination)
            continue
        if item.is_symlink():
            target_is_directory = stat.S_ISDIR(os.lstat(item).st_mode)
            try:
                os.symlink(os.readlink(item), destination, target_is_directory=target_is_directory)
            except FileExistsError:
                kept_count += 1
                continue
            if not _covered:
                created_paths.append(destination)
            restored_count += 1
            logger.info("Restored missing entry: %s", destination)
        elif item_is_dir:
            try:
                os.mkdir(str(destination))
            except FileExistsError:
                kept_count += 1
                continue
            if not _covered:
                created_paths.append(destination)
            restored, kept = _restore_missing_only(
                item, destination, actions, created_paths, _covered=True
            )
            # Directory metadata (timestamps, permissions) is applied after the
            # children exist; existing entries were never touched.
            shutil.copystat(str(item), str(destination), follow_symlinks=False)
            restored_count += 1
            kept_count += kept
            logger.info("Restored missing entry: %s", destination)
        else:
            if _copy_missing_file_exclusively(item, destination) == 0:
                kept_count += 1
                continue
            if not _covered:
                created_paths.append(destination)
            restored_count += 1
            logger.info("Restored missing entry: %s", destination)
    return restored_count, kept_count

def _resolve_backup_root(restore_root: str) -> Path:
    root = Path(restore_root)
    backup_root = root / "backup"
    if backup_root.is_dir():
        return backup_root
    return root


def _path_is_within(path: str | Path, root: str | Path) -> bool:
    try:
        normalized_path = os.path.normcase(os.path.abspath(os.fspath(path)))
        normalized_root = os.path.normcase(os.path.abspath(os.fspath(root)))
        return os.path.commonpath((normalized_path, normalized_root)) == normalized_root
    except (TypeError, ValueError):
        return False


_WINDOWS_SOURCE_SHAPE = re.compile(r"^[A-Za-z]:[\\/]")


def _canonical_native_path(value: str) -> tuple[str, str]:
    """OS-appropriate comparison key for a configured native path (Windows paths
    are compared case-insensitively, POSIX paths case-sensitively)."""
    text = str(value).strip()
    flavor = "windows" if (_WINDOWS_SOURCE_SHAPE.match(text) or text.startswith("\\\\")) else "posix"
    comparable = re.sub(r"/{2,}", "/", text.replace("\\", "/"))
    if flavor == "windows":
        comparable = comparable.casefold()
    return flavor, comparable.rstrip("/")


def _native_snapshot_subtree(source: str) -> str:
    """Convert a configured native source into the restic snapshot subtree path.
    Windows drives: C:\\\\foo -> /C/foo (confirmed against restic 0.18.1 on Windows).
    POSIX sources are stored verbatim. UNC sources have no confirmed representation
    and fail closed instead of restoring a wrong subtree."""
    text = str(source).strip()
    comparable = text.replace("\\", "/")
    if comparable.startswith("//"):
        raise NativeSourceSelectionError(
            f"restic snapshot subtree for UNC source {source} is not supported; "
            "refusing to substitute a subtree for the original-path restore",
            "native_subtree_unsupported",
        )
    if _WINDOWS_SOURCE_SHAPE.match(text):
        collapsed = re.sub(r"/{2,}", "/", comparable).rstrip("/")
        drive, _, remainder = collapsed.partition(":")
        return "/" + drive + (("/" + remainder.lstrip("/")) if remainder.strip("/") else "")
    collapsed = re.sub(r"/{2,}", "/", comparable).rstrip("/")
    if not collapsed.startswith("/"):
        raise NativeSourceSelectionError(
            f"configured native source {source} is not an absolute path; "
            "original-path restore must be validated, refusing to substitute a subtree",
            "native_subtree_unsupported",
        )
    return collapsed


def _select_native_source_subtree(config: RestoreConfig) -> Optional[tuple[str, str]]:
    """Return (configured_source, snapshot_subtree) when config.target_path exactly
    equals one configured filesystem_paths entry (OS-appropriate case comparison),
    else None. Original-path restore restores only that source's subtree and is a
    native-runtime behavior: a Docker config that happens to carry sources must
    keep the existing full-tree layout."""
    runtime_type = str(getattr(config, "runtime_type", "") or "").strip().lower()
    if runtime_type != "native":
        return None
    configured = tuple(getattr(config, "filesystem_paths", ()) or ())
    if not configured:
        return None
    target_text = str(getattr(config, "target_path", "") or "").strip()
    if not target_text:
        return None
    target_flavor, target_canonical = _canonical_native_path(target_text)
    for source in configured:
        if not isinstance(source, str) or not source.strip():
            continue
        flavor, canonical = _canonical_native_path(source)
        if flavor != target_flavor or canonical != target_canonical:
            continue
        return source.strip(), _native_snapshot_subtree(source)
    return None


_WINDOWS_DRIVE_ROOT_CANONICAL = re.compile(r"^[a-z]:$")


def _is_native_filesystem_root_destination(target_path: str) -> bool:
    """S6: True when the native restore destination is itself a filesystem root:
    the POSIX root, a Windows drive root (with or without a trailing separator,
    or a bare drive form like "L:"), or a UNC share root. Root destinations keep
    the recorded full-tree layout because the recorded path components are the
    destination's own layout there."""
    text = str(target_path or "").strip()
    if not text:
        return False
    flavor, canonical = _canonical_native_path(text)
    if flavor == "posix" and not re.match(r"^[A-Za-z]:$", text):
        # The POSIX root canonicalizes to the empty string; every other POSIX
        # destination keeps at least one path component.
        return canonical == ""
    # Windows drive or UNC flavor (including a bare "L:" drive-relative form).
    if _WINDOWS_DRIVE_ROOT_CANONICAL.fullmatch(canonical):
        return True
    if canonical.startswith("/"):
        # UNC destinations canonicalize to "/server/share[...]"; a share root
        # keeps exactly the server and the share components.
        components = [component for component in canonical.split("/") if component]
        return len(components) == 2
    return False


def _plan_native_direct_restore_specs(
    config: RestoreConfig,
    snapshot: str,
    validate_subtree: Callable[[str], str],
) -> Optional[list[str]]:
    """S6: plan a native restore into a custom destination that is not a
    filesystem root. Returns one `snapshot:subtree` spec per configured source,
    each validated with the bounded `restic ls` preflight BEFORE any mutation,
    so every source's content is extracted directly inside the destination
    instead of recreating the recorded tree (drive-letter components included).
    Returns None when the existing full-tree layout applies: a docker runtime, a
    backup-dir layout, an empty target, or a filesystem-root destination (POSIX
    root, Windows drive root, UNC share root). A native restore without
    configured sources fails closed before any mutation."""
    if str(getattr(config, "runtime_type", "") or "").strip().lower() != "native":
        return None
    if _should_use_backup_dir_layout(config):
        return None
    target_text = str(getattr(config, "target_path", "") or "").strip()
    if not target_text or _is_native_filesystem_root_destination(target_text):
        return None
    configured = [
        source.strip()
        for source in getattr(config, "filesystem_paths", ()) or ()
        if isinstance(source, str) and source.strip()
    ]
    if not configured:
        raise NativeSourceSelectionError(
            "native restore requires configured sources",
            "native_sources_required",
        )
    subtrees = []
    for source in configured:
        subtree = _native_snapshot_subtree(source)
        validate_subtree(subtree)
        subtrees.append(subtree)
    return [f"{snapshot}:{subtree}" for subtree in subtrees]


def _read_only_paths(config: RestoreConfig) -> tuple[str, ...]:
    paths = getattr(config, "read_only_paths", ()) or ()
    return tuple(path for path in paths if isinstance(path, str) and path)


def _read_only_mount_covers_entry(target_entry: Path, read_only_paths: tuple[str, ...]) -> bool:
    return any(
        _path_is_within(target_entry, mount_path) or _path_is_within(mount_path, target_entry)
        for mount_path in read_only_paths
    )


def _ensure_direct_restore_target_is_writable(config: RestoreConfig) -> None:
    if _should_use_backup_dir_layout(config):
        return
    for mount_path in _read_only_paths(config):
        if _path_is_within(config.target_path, mount_path):
            raise ReadOnlyRestoreTargetError(
                f"Direct restore target is read-only: {config.target_path}; refusing to clear or write"
            )

def _restore_backup_dir_layout(
    restore_root: str,
    config: RestoreConfig,
    actions: list[str],
    *,
    preserve_only: bool = False,
    created_paths: Optional[list[Path]] = None,
) -> None:
    if created_paths is None:
        created_paths = []
    target_root = Path(config.target_path)
    target_root_existed = target_root.exists()
    target_root.mkdir(parents=True, exist_ok=True)
    if preserve_only and not target_root_existed:
        created_paths.append(target_root)
    source_root = _resolve_backup_root(restore_root)
    logger.info(f"Resolved backup source root: {source_root}")

    children = list(target_root.iterdir())
    if not children:
        msg = (
            "RESTORE_LAYOUT=backup-dir expects mounted entries inside the target path, "
            "for example /backup/storage_data"
        )
        logger.error(msg)
        raise ValueError(msg)

    actions.append(f"Resolved backup source root: {source_root}")
    logger.info(f"Found {len(children)} target entries to restore: {[c.name for c in children]}")
    restored_entries = []
    missing_entries = []
    read_only_paths = _read_only_paths(config)

    for child in children:
        target_entry = target_root / child.name
        if _read_only_mount_covers_entry(target_entry, read_only_paths):
            message = f"Read-only mount skipped intentionally: {target_entry}"
            actions.append(message)
            logger.warning(message)
            continue

        source_entry = source_root / child.name
        if preserve_only:
            if not source_entry.exists():
                logger.warning(f"No matching source entry for: {child.name}")
                missing_entries.append(child.name)
                continue
            if target_entry.exists() or target_entry.is_symlink():
                if (
                    target_entry.is_dir()
                    and not target_entry.is_symlink()
                    and source_entry.is_dir()
                    and not source_entry.is_symlink()
                ):
                    restored, kept = _restore_missing_only(source_entry, target_entry, actions, created_paths)
                    restored_entries.append(child.name)
                    if restored:
                        actions.append(
                            f"Restored {restored} missing entries within existing mount {target_entry}; "
                            f"kept {kept} existing entries unchanged"
                        )
                else:
                    message = f"Preserved existing target entry: {target_entry}"
                    actions.append(message)
                    logger.info(message)
                continue
            if source_entry.is_dir() and not source_entry.is_symlink():
                logger.info(f"Copying directory {source_entry} -> {target_entry}")
                shutil.copytree(source_entry, target_entry, symlinks=True)
            elif source_entry.is_symlink():
                target_is_directory = stat.S_ISDIR(os.lstat(source_entry).st_mode)
                os.symlink(os.readlink(source_entry), target_entry, target_is_directory=target_is_directory)
            else:
                logger.info(f"Copying file {source_entry} -> {target_entry}")
                shutil.copy2(source_entry, target_entry, follow_symlinks=False)
            created_paths.append(target_entry)
            restored_entries.append(child.name)
            logger.info(f"Restored entry: {child.name}")
            continue

        logger.info(f"Clearing target entry: {target_entry}")
        if target_entry.is_dir() and not target_entry.is_symlink():
            _clear_target_contents(str(target_entry))
        elif target_entry.exists() or target_entry.is_symlink():
            target_entry.unlink()

        if not source_entry.exists():
            logger.warning(f"No matching source entry for: {child.name}")
            missing_entries.append(child.name)
            continue

        if source_entry.is_dir() and not source_entry.is_symlink():
            logger.info(f"Copying directory {source_entry} -> {target_entry}")
            _copy_directory_contents(source_entry, target_entry)
        else:
            logger.info(f"Replacing file {source_entry} -> {target_entry}")
            _replace_path(source_entry, target_entry)
        restored_entries.append(child.name)
        logger.info(f"Restored entry: {child.name}")

    if restored_entries:
        actions.append(f"Restore matching entries under {config.target_path}: {', '.join(sorted(restored_entries))}")
        logger.info(f"Restore complete. Restored {len(restored_entries)} entries: {sorted(restored_entries)}")
    if missing_entries:
        actions.append(
            f"Mounted entries without matching backup content were left empty: {', '.join(sorted(missing_entries))}"
        )
        logger.warning(f"Missing entries (no backup content): {sorted(missing_entries)}")


class ResticBackupStrategy(BackupStrategy, RestoreStrategy):
    def inspect_metadata(self, snapshot: str, config: RestoreConfig, policy: RestoreOwnershipPolicy, scopes=None):
        from src.app.infrastructure.adapters.restic_metadata import ResticMetadataInspectorAdapter

        return ResticMetadataInspectorAdapter().inspect(snapshot, config, scopes, policy)
    @staticmethod
    def _emit_process_line(line: Any, output_stream, callback: Optional[Callable[[str], None]]) -> str:
        text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line or "")
        if callback is not None:
            try:
                callback(text)
            except Exception:
                logger.debug("Restic output callback failed", exc_info=True)
        try:
            output_stream.write(text)
            output_stream.flush()
        except Exception:
            pass
        return text

    def _run_incremental_backup(
        self,
        command: list[str],
        env: dict[str, str],
        output_callback: Optional[Callable[[str], None]] = None,
    ) -> tuple[str, str, int]:
        command = restic_command_with_backend_options(command, env)
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def consume(stream, lines: list[str], output_stream) -> None:
            if stream is None:
                return
            readline = getattr(stream, "readline", None)
            if callable(readline):
                while True:
                    raw_line = readline()
                    if raw_line in ("", b""):
                        break
                    text = self._emit_process_line(raw_line, output_stream, output_callback)
                    lines.append(text)
                return
            else:
                iterator = iter(stream)
            for raw_line in iterator:
                text = self._emit_process_line(raw_line, output_stream, output_callback)
                lines.append(text)

        stdout_thread = threading.Thread(
            target=consume,
            args=(getattr(process, "stdout", None), stdout_lines, sys.stdout),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=consume,
            args=(getattr(process, "stderr", None), stderr_lines, sys.stderr),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        return_code = process.wait()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        if not isinstance(return_code, int):
            return_code = getattr(process, "returncode", 1)
        return "".join(stdout_lines), "".join(stderr_lines), int(return_code or 0)

    def _run_restic_command(
        self, command: list[str], env: dict[str, str], capture_stdout: bool = False
    ) -> tuple[str, str, int]:
        try:
            command = restic_command_with_backend_options(command, env)
            completed = subprocess.run(
                command,
                env=env,
                check=True,
                stdout=subprocess.PIPE if capture_stdout else None,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.CalledProcessError as exc:
            self._echo_stderr(exc.stderr)
            return _text(exc.output), _text(exc.stderr), int(exc.returncode or 1)
        self._echo_stderr(completed.stderr)
        return _text(completed.stdout), _text(completed.stderr), 0

    @classmethod
    def _echo_stderr(cls, text: Any) -> None:
        if isinstance(text, str) and text:
            cls._emit_process_line(text, sys.stderr, None)

    def _run_with_lock_recovery(
        self, restic: str, env: dict[str, str], run: Callable[[], tuple[str, str, int]]
    ) -> tuple[str, str, int]:
        stdout, stderr, return_code = run()
        lock = describe_restic_lock(stderr, return_code)
        if lock is None:
            return stdout, stderr, return_code
        logger.info("Restic repository is locked (%s); running 'restic unlock' to clear stale locks", lock)
        _unlock_stdout, _unlock_stderr, unlock_status = self._run_restic_command([restic, "unlock"], env, capture_stdout=True)
        if unlock_status == 0:
            logger.info("'restic unlock' finished; retrying the locked restic command once")
        else:
            logger.warning("'restic unlock' exited with status %s; retrying the locked restic command once", unlock_status)
        stdout, stderr, return_code = run()
        if describe_restic_lock(stderr, return_code) is not None:
            logger.error("Restic repository is still locked after 'restic unlock' (%s); %s", lock, RESTIC_LOCK_STILL_HELD_HINT)
            stderr = f"{stderr.rstrip()}\n{RESTIC_LOCK_STILL_HELD_HINT}\n"
        return stdout, stderr, return_code

    def perform_backup(
        self,
        config: BackupConfig,
        output_callback: Optional[Callable[[str], None]] = None,
    ) -> BackupResult:
        timestamp = datetime.now()
        env = os.environ.copy()
        if config.restic_repository:
            env["RESTIC_REPOSITORY"] = config.restic_repository
        if config.restic_password:
            env["RESTIC_PASSWORD"] = config.restic_password
            
        # Rclone config support via env vars if needed, usually handled by user volume mount or envs
            
        sources = config.source_paths
        logger.info(f"Running restic backup for {sources}")
        
        try:
            restic = _require_executable("restic") or "restic"
            env = restic_subprocess_environment(env)
            # Check if repo is initialized
            init_check_cmd = [restic, "snapshots", "--json", "--latest", "1"]
            check_stdout, check_stderr, check_status = self._run_with_lock_recovery(
                restic, env, lambda: self._run_restic_command(init_check_cmd, env, capture_stdout=True)
            )
            if check_status != 0:
                if describe_restic_lock(check_stderr, check_status) is not None:
                    raise subprocess.CalledProcessError(check_status, init_check_cmd, output=check_stdout, stderr=check_stderr)
                logger.info("Restic repository not initialized or not accessible. Attempting to initialize...")
                # Try to init
                try:
                    init_cmd = restic_command_with_backend_options([restic, "init"], env)
                    subprocess.run(init_cmd, env=env, check=True)
                    logger.info("Restic repository initialized successfully.")
                except subprocess.CalledProcessError as e:
                    # If init fails, maybe it was a connection error or something else, but we can't proceed
                    logger.error(f"Failed to initialize restic repository: {e}")
                    raise e

            cmd = [restic, "backup", "--json"] + sources
            stdout, stderr, return_code = self._run_with_lock_recovery(
                restic, env, lambda: self._run_incremental_backup(cmd, env, output_callback)
            )
            if return_code != 0:
                raise subprocess.CalledProcessError(return_code, cmd, output=stdout, stderr=stderr)

            # Parse streamed JSON output for the final summary.
            # Restic outputs multiple JSON objects, one per line.
            # The last one usually has summary.
            lines = stdout.strip().split("\n")
            summary = {}
            for line in lines:
                try:
                    data = json.loads(line)
                    if data.get("message_type") == "summary":
                        summary = data
                except (TypeError, ValueError):
                    pass
            
            size = summary.get("data_added", 0)
            
            prune_cmd = build_restic_forget_command(
                keep_last=config.restic_keep_last,
                keep_hourly=config.restic_keep_hourly,
                keep_daily=config.restic_keep_daily,
                keep_weekly=config.restic_keep_weekly,
                keep_monthly=config.restic_keep_monthly,
                keep_yearly=config.restic_keep_yearly,
                prune=config.restic_prune,
            )
            prune_cmd[0] = restic
            logger.info(
                "Pruning old snapshots..."
                if config.restic_prune
                else "Applying Restic retention rules without pruning..."
            )
            _prune_stdout, prune_stderr, prune_status = self._run_with_lock_recovery(
                restic, env, lambda: self._run_restic_command(prune_cmd, env)
            )
            if prune_status != 0:
                logger.error("Restic retention command failed with exit status %s", prune_status)
                lock = describe_restic_lock(prune_stderr, prune_status)
                if lock is not None:
                    raise RuntimeError(
                        f"Restic retention command failed: repository is locked ({lock}); {RESTIC_LOCK_STILL_HELD_HINT}"
                    )
                raise RuntimeError("Restic retention command failed")
            logger.info(
                "Pruning finished successfully."
                if config.restic_prune
                else "Restic retention rules finished successfully without pruning."
            )
            
            return BackupResult(
                timestamp=timestamp,
                duration=summary.get("total_duration", 0),
                size=size,
                success=True,
                artifact_path=None
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Restic backup failed: {e.stderr or e.output or e}")
            return BackupResult(
                timestamp=timestamp,
                duration=0,
                size=0,
                success=False,
                error=f"Restic failed: {e.stderr or e.output or e}"
            )
        except Exception as e:
            logger.error(f"Restic backup error: {e}")
            return BackupResult(
                timestamp=timestamp,
                duration=0,
                size=0,
                success=False,
                error=str(e)
            )

    def restore(self, source_path: str, config: RestoreConfig) -> RestoreResult:
        timestamp = datetime.now()
        preserve_only = not bool(config.force_overwrite)
        actions = (
            ["Preserve existing target contents; restore only missing snapshot items"]
            if preserve_only
            else ["Replace target contents before restic restore"]
        )
        env = os.environ.copy()
        temp_dir = None
        policy = RestoreOwnershipPolicy()
        normalization = {"state": "not_started", "unsupported_metadata": list(UNSUPPORTED_METADATA)}
        destructive_state = "none"
        created_paths: list[Path] = []
        repository = config.restic_repository or os.environ.get("RESTIC_REPOSITORY")
        password = config.restic_password or os.environ.get("RESTIC_PASSWORD")
        if repository:
            env["RESTIC_REPOSITORY"] = repository
        if password:
            env["RESTIC_PASSWORD"] = password

        try:
            _ensure_direct_restore_target_is_writable(config)
            if preserve_only:
                _ensure_safe_preserve_target(Path(config.target_path))
            restic = _require_executable("restic") or "restic"
            env = restic_subprocess_environment(env)
            policy = _ownership_policy(config)
            snapshot = source_path or config.source or "latest"
            restore_specs = [snapshot]
            selection = _select_native_source_subtree(config)
            if selection is not None:
                selected_source, subtree = selection
                self._inspect_native_subtree(restic, env, snapshot, subtree)
                restore_specs = [f"{snapshot}:{subtree}"]
                message = f"Original-path restore selected configured source subtree: {subtree}"
                actions.append(message)
                logger.info(message)
                sys.stderr.flush()
            else:
                direct_specs = _plan_native_direct_restore_specs(
                    config,
                    snapshot,
                    lambda subtree: self._inspect_native_subtree(restic, env, snapshot, subtree),
                )
                if direct_specs is not None:
                    restore_specs = direct_specs
                    subtrees = ", ".join(spec.split(":", 1)[1] for spec in direct_specs)
                    message = (
                        f"Custom native destination: restoring configured source subtrees "
                        f"directly into {config.target_path}: {subtrees}"
                    )
                    actions.append(message)
                    logger.info(message)
                    sys.stderr.flush()

            if _should_use_backup_dir_layout(config):
                # Only the original-path selection (or the bare snapshot) reaches
                # this layout; S6 direct placement applies to the direct layout.
                restore_spec = restore_specs[0]
                temp_dir = tempfile.mkdtemp(prefix="restore-restic-")
                logger.info(f"Restoring restic snapshot {restore_spec} into temp dir: {temp_dir}")
                sys.stderr.flush()
                command = restic_command_with_backend_options([restic, "restore", restore_spec, "--target", temp_dir], env)
                subprocess.run(command, env=env, check=True)
                actions.append(f"Restore restic snapshot into temporary workspace: {restore_spec}")
                logger.info("Restic restore complete, copying entries to target volumes")
                sys.stderr.flush()
                if preserve_only:
                    _restore_backup_dir_layout(temp_dir, config, actions, preserve_only=True, created_paths=created_paths)
                else:
                    if Path(config.target_path).exists() and any(Path(config.target_path).iterdir()):
                        destructive_state = "partial"
                    _restore_backup_dir_layout(temp_dir, config, actions)
            elif preserve_only:
                temp_dir = tempfile.mkdtemp(prefix="restore-restic-")
                restored_total = 0
                kept_total = 0
                for index, restore_spec in enumerate(restore_specs):
                    if len(restore_specs) == 1:
                        staging_dir = Path(temp_dir)
                    else:
                        # One staging directory per source subtree keeps each
                        # staged restoration independent from the others.
                        staging_dir = Path(temp_dir) / f"snapshot-{index + 1}"
                    logger.info(f"Staging restic snapshot {restore_spec} into temp dir: {staging_dir}")
                    sys.stderr.flush()
                    command = restic_command_with_backend_options([restic, "restore", restore_spec, "--target", str(staging_dir)], env)
                    subprocess.run(command, env=env, check=True)
                    if len(restore_specs) == 1:
                        actions.append(f"Staged restic snapshot content in a temporary workspace: {restore_spec}")
                    else:
                        actions.append(f"Staged restic snapshot subtree content in a temporary workspace: {restore_spec}")
                    restored, kept = _restore_missing_only(staging_dir, Path(config.target_path), actions, created_paths)
                    restored_total += restored
                    kept_total += kept
                actions.append(
                    f"Restored {restored_total} missing snapshot entries under {config.target_path}; "
                    f"kept {kept_total} existing entries unchanged"
                )
            else:
                logger.info(f"Clearing target contents at {config.target_path}")
                destructive_state = "partial"
                _clear_target_contents(config.target_path)
                for restore_spec in restore_specs:
                    logger.info(f"Restoring restic snapshot {restore_spec} directly to {config.target_path}")
                    sys.stderr.flush()
                    command = restic_command_with_backend_options([restic, "restore", restore_spec, "--target", config.target_path], env)
                    subprocess.run(command, env=env, check=True)
                    actions.append(f"Restore restic snapshot: {restore_spec}")

            normalization = normalize_ownership(
                config, policy, created_paths=created_paths if preserve_only else None
            )
            if normalization["state"] == "preserved":
                actions.append("Preserved snapshot ownership where supported by restic extraction")
            else:
                actions.append(f"Normalized ownership without following symlinks: {policy.to_dict()}")

            warnings = _sqlite_warnings(config.target_path)
            for warning in warnings:
                logger.warning(warning)

            if not preserve_only:
                destructive_state = "complete"
            return _attach_restore_evidence(RestoreResult(
                timestamp=timestamp,
                duration=0,
                success=True,
                target_path=config.target_path,
                dry_run=False,
                force_overwrite=config.force_overwrite,
                planned_actions=actions,
                permission_warnings=warnings
            ), policy, normalization, destructive_state, "ok")
        except OwnershipNormalizationError as e:
            normalization = e.report
            logger.error(f"Restic restore ownership normalization failed: {e}")
            return _attach_restore_evidence(RestoreResult(
                timestamp=timestamp, duration=0, success=False, target_path=config.target_path,
                dry_run=False, force_overwrite=config.force_overwrite, planned_actions=actions, error=str(e)
            ), policy, normalization, destructive_state or "unknown", e.category)
        except Exception as e:
            logger.error(f"Restic restore failed: {e}")
            return _attach_restore_evidence(RestoreResult(
                timestamp=timestamp,
                duration=0,
                success=False,
                target_path=config.target_path,
                dry_run=False,
                force_overwrite=config.force_overwrite,
                planned_actions=actions,
                error=str(e)
            ), policy, normalization, destructive_state, getattr(e, "category", "restore_failed"))
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _inspect_native_subtree(self, restic: str, env: dict[str, str], snapshot: str, subtree: str) -> str:
        """Validate the selected snapshot subtree with `restic ls --json` BEFORE any
        destructive action and classify it. The listing is streamed with a bounded
        byte budget (existing restic metadata helpers) and a node is only accepted
        when its recorded path exactly matches the selected subtree (Windows paths
        compared case-insensitively, mirroring filesystem semantics); a child line
        alone is never accepted. A file source is rejected, a missing subtree fails
        closed, and malformed or over-budget output fails closed — both without
        touching the restore target."""
        from src.app.infrastructure.adapters.restic_metadata import (
            MAX_STDERR_TAIL_BYTES,
            _drain_stderr,
            _terminate_and_reap,
        )

        command = restic_command_with_backend_options(
            [restic, "ls", "--json", snapshot, subtree], env
        )
        process = None
        stderr_thread = None
        stderr_tail = bytearray()
        stderr_errors: list[Exception] = []
        matched = None
        failure = None
        finished = False
        budget = self._LS_PREFLIGHT_BUDGET_BYTES
        windows_subtree = bool(re.match(r"^/[A-Za-z]/", subtree))
        try:
            try:
                process = subprocess.Popen(
                    command,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                raise self._subtree_rejection(
                    f"restic ls could not run to verify snapshot {snapshot} at {subtree}: {exc}",
                    "native_subtree_missing",
                )
            stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(getattr(process, "stderr", None), stderr_tail, MAX_STDERR_TAIL_BYTES, stderr_errors),
            )
            stderr_thread.daemon = True
            stderr_thread.start()
            stdout = getattr(process, "stdout", None)
            if stdout is None:
                failure = self._subtree_rejection(
                    f"restic ls output for snapshot {snapshot} at {subtree} is incomplete",
                    "native_subtree_unverified",
                )
            else:
                while True:
                    raw_line = stdout.readline(self._LS_MAX_LINE_BYTES)
                    if raw_line in (b"", ""):
                        finished = True
                        break
                    if isinstance(raw_line, str):
                        raw_line = raw_line.encode("utf-8", errors="replace")
                    budget -= len(raw_line)
                    if budget < 0:
                        failure = self._subtree_rejection(
                            f"restic ls output for snapshot {snapshot} at {subtree} exceeded the bounded "
                            "verification budget; refusing to restore an unverified subtree",
                            "native_subtree_unverified",
                        )
                        break
                    text = raw_line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        payload = json.loads(text)
                    except ValueError:
                        failure = self._subtree_rejection(
                            f"restic ls produced non-JSON output while verifying snapshot {snapshot} "
                            f"at {subtree}; refusing to restore an unverified subtree",
                            "native_subtree_unverified",
                        )
                        break
                    if payload.get("struct_type") != "node":
                        continue
                    path = payload.get("path")
                    if not isinstance(path, str):
                        continue
                    if path == subtree or (
                        windows_subtree and path.casefold() == subtree.casefold()
                    ):
                        matched = payload
                        # Stop early: the answer is known; the listing is read-only.
                        break
        finally:
            if process is None:
                pass
            elif finished and failure is None:
                try:
                    process.wait(timeout=self._LS_WAIT_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    _terminate_and_reap(process)
            else:
                _terminate_and_reap(process)
            if stderr_thread is not None:
                stderr_thread.join(timeout=5)
            for name in ("stdout", "stderr"):
                stream = getattr(process, name, None) if process is not None else None
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
        if failure is not None:
            raise failure
        if matched is not None:
            # The early exit terminated the read-only listing; the recorded exit
            # status no longer reflects the command's own outcome.
            status = 0
        else:
            status = getattr(process, "returncode", None) if process is not None else None
            if not isinstance(status, int):
                status = 1
        if status != 0:
            from src.app.infrastructure.adapters.restic_metadata import _STDERR_SECRET

            detail = _STDERR_SECRET.sub(r"\1=<redacted>", stderr_tail.decode("utf-8", errors="replace"))
            detail = " ".join(detail.split())[:300]
            raise self._subtree_rejection(
                f"restic could not verify snapshot {snapshot} against the configured source at {subtree} "
                f"(exit status {status}{': ' + detail if detail else ''})",
                "native_subtree_missing",
            )
        if matched is None:
            raise self._subtree_rejection(
                f"restic snapshot {snapshot} does not contain the configured source at {subtree}; "
                "refusing to restore an unverified subtree",
                "native_subtree_missing",
            )
        if matched.get("type") != "dir":
            name = matched.get("name") or subtree.rsplit("/", 1)[-1]
            raise self._subtree_rejection(
                f"configured native source {subtree} is a {matched.get('type') or 'file'} named {name} "
                f"in snapshot {snapshot}; original-path restore requires a directory source",
                "native_source_file_rejected",
            )
        return "dir"

    @staticmethod
    def _subtree_rejection(message: str, category: str) -> NativeSourceSelectionError:
        return NativeSourceSelectionError(message, category)

    _LS_PREFLIGHT_BUDGET_BYTES = 64 * 1024 * 1024
    _LS_MAX_LINE_BYTES = 1024 * 1024
    _LS_WAIT_TIMEOUT_SECONDS = 120
