"""Native file regex filter (opt-in, native runtime only).

A native filesystem target may configure ``BACKUP_NATIVE_FILE_REGEX`` in its
runtime environment. When present, only immediate regular files whose basename
fully matches the regex are backed up (deterministic listing, one scan per
run, no directory traversal, no symlink/junction follow, no shell), the
snapshot is tagged with a reserved, target-derived stable tag so dynamic file
names still share one retention group, and retention/forget commands are
scoped to that tag. Invalid regex and zero matches fail closed before any
repository contact.
"""

import hashlib
import json
import os
import re
import tempfile

NATIVE_FILE_REGEX_ENV = "BACKUP_NATIVE_FILE_REGEX"
NATIVE_FILE_FILTER_TAG_ENV = "BACKUP_FILE_FILTER_TAG"
NATIVE_FILE_FILTER_COMMAND_MARKER = "vaultline-native-file-filter"
NATIVE_FILE_FILTER_TAG_PREFIX = "vaultline-file-filter"
NATIVE_FILE_FORGET_GROUP_BY = "host,tags"


def filter_scope_tag(target_id) -> str:
    """Stable, safe retention tag derived from the payload target ID.

    The tag uses a reserved fixed prefix the UI cannot configure and a short
    digest of the target ID, so every filtered snapshot of the same target
    shares one retention group even when the matched file names change."""
    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError("native file filter requires a scoped target ID")
    digest = hashlib.sha256(target_id.strip().encode("utf-8")).hexdigest()[:16]
    return f"{NATIVE_FILE_FILTER_TAG_PREFIX}-{digest}"


def compile_native_filter(regex_value) -> re.Pattern:
    """Compile the configured native file regex, failing closed with a
    stable diagnostic before any repository interaction."""
    if not isinstance(regex_value, str) or not regex_value.strip():
        raise ValueError("Native file regex must be a non-empty string")
    try:
        return re.compile(regex_value)
    except re.error as exc:
        raise ValueError(f"Invalid native file regex: {exc}") from exc


def select_native_matches(source_paths, regex_value) -> list[str]:
    """Return the absolute paths of immediate regular files whose basename
    fully matches ``regex_value`` under the configured source directories.

    One deterministic scan per run: entries are sorted by name; only regular
    files are selected (directories and symlinks/junctions are never taken,
    and subdirectory contents are never traversed). Raises when no file
    matched so the caller never touches the repository with an empty plan."""
    pattern = compile_native_filter(regex_value)
    matches: list[str] = []
    for source in source_paths or ():
        if not isinstance(source, str) or not source:
            raise ValueError("native file filter requires explicit filesystem paths")
        try:
            with os.scandir(source) as directory:
                entries = sorted(directory, key=lambda entry: entry.name)
        except OSError as exc:
            raise ValueError(
                f"Native file regex source is not a readable directory: {json.dumps(source, ensure_ascii=True)}"
            ) from exc
        for entry in entries:
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                if pattern.fullmatch(entry.name):
                    matches.append(os.path.join(source, entry.name))
            except OSError:
                continue
    if not matches:
        raise ValueError("Native file regex matched no regular files")
    return matches


def log_match_line(path) -> str:
    """JSON-escaped path for logs so no filesystem name can inject newlines
    or control characters into the job log stream."""
    return json.dumps(str(path), ensure_ascii=True)


def write_nul_separated_manifest(matches) -> str:
    """Write a NUL-separated ``restic --files-from-raw`` manifest (``os.fsencode``
    paths) into a transient temporary file; the caller removes it with
    ``remove_nul_separated_manifest``."""
    descriptor, path = tempfile.mkstemp(prefix="vaultline-file-filter-")
    with os.fdopen(descriptor, "wb") as handle:
        for match in matches:
            handle.write(os.fsencode(match) + b"\0")
        handle.flush()
        os.fsync(handle.fileno())
    return path


def remove_nul_separated_manifest(path) -> None:
    """Best-effort removal of the transient manifest file."""
    try:
        os.remove(path)
    except OSError:
        pass


def scoped_forget_argv(argv, tag) -> list[str]:
    """Append the fixed safe scoping flags to a validated restic forget
    argv: ``--tag <scope> --group-by host,tags``. The flags are appended only
    after the command already passed validation, so no expanded allowlist is
    needed."""
    if not isinstance(tag, str) or not tag:
        raise ValueError("forget scoping requires the native file filter tag")
    if len(argv) < 2 or argv[0] != "restic" or argv[1] != "forget":
        raise ValueError("forget scoping requires a validated restic forget command")
    return ["restic", "forget", "--tag", tag, "--group-by", NATIVE_FILE_FORGET_GROUP_BY, *argv[2:]]
