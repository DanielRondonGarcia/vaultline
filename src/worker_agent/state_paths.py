"""Resolve the native worker state directory and the files that live inside it.

Resolution order for the state directory:

1. ``WORKER_STATE_DIR`` environment variable.
2. When running as a frozen (PyInstaller) executable, the platform default:
   ``%ProgramData%\\Vaultline\\worker`` on Windows, ``/var/lib/docker-volume-backup`` elsewhere.
3. The current working directory (developer mode).

``WORKER_CREDENTIAL_FILE`` keeps precedence over the derived credential location.
In developer mode without ``WORKER_STATE_DIR`` the legacy hidden
``.worker_credentials.json`` in the current directory is preserved so existing
checkouts, docs and tests keep working unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

LINUX_DEFAULT_STATE_DIR = "/var/lib/docker-volume-backup"
WINDOWS_PROGRAM_DATA_FALLBACK = r"C:\ProgramData"
CREDENTIAL_FILE_NAME = "worker_credentials.json"
DEVELOPER_CREDENTIAL_FILE_NAME = ".worker_credentials.json"
ENROLLMENT_CONFIG_FILE_NAME = "worker.json"
ENROLLMENT_CONFIG_KEYS = ("control_plane_url", "control_plane_ca_file", "worker_name")

# Well-known SIDs. icacls accepts "*<SID>" so the grants do not depend on the OS
# locale; the same SIDs identify trusted owners of the enrollment files.
WINDOWS_SYSTEM_SID = "S-1-5-18"
WINDOWS_ADMINISTRATORS_SID = "S-1-5-32-544"
TRUSTED_WINDOWS_OWNER_SIDS = frozenset({WINDOWS_SYSTEM_SID, WINDOWS_ADMINISTRATORS_SID})
STATE_DIR_POSIX_MODE = 0o700

# Owner identities are plain strings so a resolver can be injected in tests:
# "S-1-5-..." on Windows, "uid:<n>" on POSIX, None when the owner is unknown.
OwnerResolver = Callable[[Path], Optional[str]]

_owner_check_warned = False


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _platform_is_windows() -> bool:
    return os.name == "nt"


def resolve_state_dir() -> Path:
    configured = _env("WORKER_STATE_DIR")
    if configured:
        return Path(configured)
    if _is_frozen():
        if _platform_is_windows():
            program_data = _env("ProgramData") or WINDOWS_PROGRAM_DATA_FALLBACK
            return Path(program_data) / "Vaultline" / "worker"
        return Path(LINUX_DEFAULT_STATE_DIR)
    return Path.cwd()


def state_dir_is_managed() -> bool:
    """True when the state directory is dedicated to the worker.

    Only then may its permissions be rewritten. In developer mode the state
    directory is the current working directory (a repository checkout, for
    instance) and must never be re-ACLed.
    """

    return _env("WORKER_STATE_DIR") is not None or _is_frozen()


def secure_state_dir(path: Path | str) -> None:
    """Restrict an existing state directory to the service identity and administrators.

    Windows: inheritance is removed and only SYSTEM and Administrators keep
    full control, so a standard user cannot plant ``worker_credentials.json``
    or ``worker.json`` for the LocalSystem service to adopt. POSIX: ``0700``.
    Raises ``PermissionError`` when the ACL cannot be applied and
    ``FileNotFoundError`` when the directory does not exist.
    """

    target = Path(path)
    if not target.is_dir():
        raise FileNotFoundError(f"worker state directory does not exist: {target}")
    if _platform_is_windows():
        args = [
            "icacls",
            str(target),
            "/inheritance:r",
            "/grant:r",
            f"*{WINDOWS_SYSTEM_SID}:(OI)(CI)F",
            "/grant:r",
            f"*{WINDOWS_ADMINISTRATORS_SID}:(OI)(CI)F",
        ]
        try:
            subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            error = PermissionError(f"could not restrict permissions on {target} with icacls")
            error.icacls_error_code = getattr(exc, "winerror", None) or getattr(exc, "returncode", None)
            raise error from exc
        return
    try:
        os.chmod(target, STATE_DIR_POSIX_MODE)
    except OSError as exc:
        raise PermissionError(f"could not restrict permissions on {target}") from exc


def _current_uid() -> int | None:
    geteuid = getattr(os, "geteuid", None)
    return geteuid() if geteuid is not None else None


def _warn_owner_check_unavailable_once() -> None:
    global _owner_check_warned
    if _owner_check_warned:
        return
    _owner_check_warned = True
    logger.warning("Cannot verify the owner of the enrollment files; refusing to adopt them")


def _reset_owner_check_warning() -> None:
    global _owner_check_warned
    _owner_check_warned = False


def _windows_file_owner(path: Path) -> str | None:
    try:
        import win32security
    except ImportError:
        return None
    descriptor = win32security.GetFileSecurity(str(path), win32security.OWNER_SECURITY_INFORMATION)
    return str(win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner()))


def _windows_owner_is_local_administrator(owner_sid: str) -> bool | None:
    """Return whether a Windows account owner belongs to local Administrators.

    ``None`` means the account or membership could not be verified. Keep the
    imports lazy because developer and POSIX installs do not require pywin32.
    """

    try:
        import win32net
        import win32netcon
        import win32security

        owner_account, owner_domain, _ = win32security.LookupAccountSid(
            None, win32security.ConvertStringSidToSid(owner_sid)
        )
        group_sid = win32security.ConvertStringSidToSid(WINDOWS_ADMINISTRATORS_SID)
        group_name, _group_domain, _ = win32security.LookupAccountSid(None, group_sid)
        qualified_owner = f"{owner_domain}\\{owner_account}" if owner_domain else owner_account
        # pywin32 exposes the level-0 NetUserGetLocalGroups result directly as
        # group names; its wrapper takes (serverName, userName, flags), with
        # level 0 implicit. LG_INCLUDE_INDIRECT includes nested membership.
        groups = win32net.NetUserGetLocalGroups(
            None, qualified_owner, win32netcon.LG_INCLUDE_INDIRECT
        )
        return any(str(name).casefold() == str(group_name).casefold() for name in groups)
    except Exception as exc:
        logger.error("Could not verify Windows enrollment file owner membership (%s)", exc.__class__.__name__)
        return None


def _posix_file_owner(path: Path) -> str:
    return f"uid:{os.stat(path).st_uid}"


def _default_owner_resolver() -> OwnerResolver:
    return _windows_file_owner if _platform_is_windows() else _posix_file_owner


def _trusted_owners() -> frozenset[str]:
    if _platform_is_windows():
        trusted = set(TRUSTED_WINDOWS_OWNER_SIDS)
    else:
        trusted = {"uid:0"}
        uid = _current_uid()
        if uid is not None:
            trusted.add(f"uid:{uid}")
    return frozenset(trusted)


def enrollment_files_trusted(
    paths: Iterable[Path | str],
    owner_resolver: OwnerResolver | None = None,
    membership_resolver: Callable[[str], bool | None] | None = None,
) -> bool:
    """Return True when every existing file in ``paths`` is owned by a trusted identity.

    Trusted owners are SYSTEM or Administrators on Windows and root or the
    current user on POSIX. Missing files are ignored. An owner that cannot be
    determined (no pywin32) is skipped with a single warning; an error while
    reading ownership counts as untrusted.
    """

    resolve = owner_resolver or _default_owner_resolver()
    trusted = _trusted_owners()
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            continue
        try:
            owner = resolve(path)
        except Exception as exc:
            logger.error("Could not read the owner of %s (%s)", path.name, exc.__class__.__name__)
            return False
        if owner is None:
            _warn_owner_check_unavailable_once()
            return False
        if owner not in trusted:
            if _platform_is_windows() and owner.startswith("S-"):
                try:
                    verify_membership = membership_resolver or _windows_owner_is_local_administrator
                    is_administrator = verify_membership(owner)
                except Exception as exc:
                    logger.error("Could not verify Windows enrollment file owner membership (%s)", exc.__class__.__name__)
                    _warn_owner_check_unavailable_once()
                    return False
                if is_administrator is None:
                    _warn_owner_check_unavailable_once()
                    return False
                if is_administrator:
                    continue
            return False
    return True


def resolve_credential_file() -> Path:
    configured = _env("WORKER_CREDENTIAL_FILE")
    if configured:
        return Path(configured)
    if not _is_frozen() and _env("WORKER_STATE_DIR") is None:
        return Path(DEVELOPER_CREDENTIAL_FILE_NAME)
    return resolve_state_dir() / CREDENTIAL_FILE_NAME


def resolve_enrollment_config_file() -> Path:
    """Locate ``worker.json``.

    It lives in the state directory. When the operator relocated the credential
    file explicitly (``WORKER_CREDENTIAL_FILE``) without naming a state directory,
    the enrollment config is kept beside the credential so all worker state stays
    together instead of landing in an arbitrary working directory.
    """

    if _env("WORKER_STATE_DIR") is None:
        configured_credential = _env("WORKER_CREDENTIAL_FILE")
        if configured_credential:
            return Path(configured_credential).with_name(ENROLLMENT_CONFIG_FILE_NAME)
    return resolve_state_dir() / ENROLLMENT_CONFIG_FILE_NAME


def load_enrollment_config(path: Path | str | None = None) -> dict[str, str]:
    """Read ``worker.json`` written by ``enroll``; a missing or invalid file yields ``{}``."""

    target = Path(path) if path is not None else resolve_enrollment_config_file()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("Could not read worker enrollment config %s (%s)", target.name, exc.__class__.__name__)
        return {}
    try:
        data: Any = json.loads(raw)
    except ValueError:
        logger.warning("Ignoring invalid worker enrollment config %s: not valid JSON", target.name)
        return {}
    if not isinstance(data, dict):
        logger.warning("Ignoring invalid worker enrollment config %s: expected a JSON object", target.name)
        return {}
    config: dict[str, str] = {}
    for key in ENROLLMENT_CONFIG_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            config[key] = value.strip()
    return config
