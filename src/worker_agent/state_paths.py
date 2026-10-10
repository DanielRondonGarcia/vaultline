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
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LINUX_DEFAULT_STATE_DIR = "/var/lib/docker-volume-backup"
WINDOWS_PROGRAM_DATA_FALLBACK = r"C:\ProgramData"
CREDENTIAL_FILE_NAME = "worker_credentials.json"
DEVELOPER_CREDENTIAL_FILE_NAME = ".worker_credentials.json"
ENROLLMENT_CONFIG_FILE_NAME = "worker.json"
ENROLLMENT_CONFIG_KEYS = ("control_plane_url", "control_plane_ca_file", "worker_name")


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
