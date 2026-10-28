"""On-demand self-update for the packaged Windows worker MSI."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

REPOSITORY = "DanielRondonGarcia/vaultline"
LATEST_RELEASE_API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
GITHUB_ASSET_HOSTS = frozenset(
    {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
)
MAX_RELEASE_METADATA_BYTES = 2 * 1024 * 1024
MAX_CHECKSUMS_BYTES = 1024 * 1024
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class UpdateError(RuntimeError):
    """A recoverable failure while checking or applying a worker update."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    installer_name: str
    installer_url: str
    checksums_url: str


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(value.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_newer_version(current_version: str, candidate_version: str) -> bool:
    """Return whether a stable candidate is newer; dev/local builds may update."""
    candidate = _version_tuple(candidate_version)
    if candidate is None:
        raise UpdateError("GitHub release tag must be a stable semantic version")
    current = _version_tuple(current_version)
    return current is None or candidate > current


def _trusted_release_asset_url(url: object, tag: str, filename: str) -> str:
    if not isinstance(url, str):
        raise UpdateError("GitHub release asset is missing its download URL")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise UpdateError("GitHub release asset URL is not from the trusted GitHub release") from exc
    expected_path = f"/{REPOSITORY}/releases/download/{tag}/{filename}"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise UpdateError("GitHub release asset URL is not from the trusted GitHub release")
    return url


def parse_latest_release(payload: object) -> ReleaseInfo:
    """Select the version-matched Windows MSI and checksums from GitHub metadata."""
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        raise UpdateError("GitHub did not return a stable release")

    tag = payload.get("tag_name")
    if not isinstance(tag, str):
        raise UpdateError("GitHub release is missing its version tag")
    version_match = _VERSION_RE.fullmatch(tag)
    if version_match is None:
        raise UpdateError("GitHub release tag must be a stable semantic version")
    version = ".".join(version_match.groups())
    installer_name = f"vaultline-worker_{version}_windows-amd64.msi"
    checksums_name = f"vaultline-worker_{version}_SHA256SUMS"

    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("GitHub release is missing its assets")
    selected: dict[str, object] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if name in (installer_name, checksums_name):
            if name in selected:
                raise UpdateError(f"GitHub release contains duplicate asset {name}")
            selected[name] = asset.get("browser_download_url")
    if installer_name not in selected:
        raise UpdateError(f"GitHub release is missing {installer_name}")
    if checksums_name not in selected:
        raise UpdateError(f"GitHub release is missing {checksums_name}")

    return ReleaseInfo(
        version=version,
        tag=tag,
        installer_name=installer_name,
        installer_url=_trusted_release_asset_url(selected[installer_name], tag, installer_name),
        checksums_url=_trusted_release_asset_url(selected[checksums_name], tag, checksums_name),
    )


def verify_checksum(manifest: str | bytes, asset_name: str, actual_digest: str) -> None:
    """Require one SHA256SUMS row for the named asset and compare it to its bytes."""
    if isinstance(manifest, bytes):
        try:
            manifest = manifest.decode("ascii")
        except UnicodeDecodeError as exc:
            raise UpdateError("SHA256SUMS is not valid ASCII") from exc
    if not _SHA256_RE.fullmatch(actual_digest):
        raise UpdateError("downloaded MSI produced an invalid SHA-256 digest")

    expected: list[str] = []
    for line in manifest.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+?)\s*", line)
        if match and match.group(2) == asset_name:
            expected.append(match.group(1).lower())
    if len(expected) != 1:
        raise UpdateError(f"SHA256SUMS checksum entry for {asset_name} is missing or ambiguous")
    if expected[0] != actual_digest.lower():
        raise UpdateError(f"SHA256 checksum mismatch for {asset_name}")


def _open_url(url: str, *, accept: str = "application/octet-stream"):
    request = Request(url, headers={"Accept": accept, "User-Agent": "vaultline-worker-updater"})
    return urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS)


def _validate_final_url(response: object, allowed_hosts: frozenset[str]) -> None:
    geturl = getattr(response, "geturl", None)
    final_url = geturl() if callable(geturl) else ""
    parsed = urlsplit(final_url)
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
        raise UpdateError("download redirected outside the trusted HTTPS hosts")


def _read_limited(url: str, *, limit: int, allowed_hosts: frozenset[str], accept: str) -> bytes:
    try:
        with _open_url(url, accept=accept) as response:
            _validate_final_url(response, allowed_hosts)
            content = response.read(limit + 1)
    except UpdateError:
        raise
    except Exception as exc:
        raise UpdateError(f"could not download update metadata: {exc}") from exc
    if len(content) > limit:
        raise UpdateError("update metadata exceeded the permitted size")
    return content


def fetch_latest_release() -> ReleaseInfo:
    payload_bytes = _read_limited(
        LATEST_RELEASE_API_URL,
        limit=MAX_RELEASE_METADATA_BYTES,
        allowed_hosts=frozenset({"api.github.com"}),
        accept="application/vnd.github+json",
    )
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("GitHub returned invalid release metadata") from exc
    return parse_latest_release(payload)


def _download_asset(url: str, destination: Path, *, limit: int) -> None:
    try:
        with _open_url(url) as response:
            _validate_final_url(response, GITHUB_ASSET_HOSTS)
            total = 0
            with destination.open("xb") as output:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise UpdateError("downloaded update asset exceeded the permitted size")
                    output.write(chunk)
            if total == 0:
                raise UpdateError("downloaded update asset was empty")
    except UpdateError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise UpdateError(f"could not download the Windows MSI: {exc}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified_installer(release: ReleaseInfo) -> tuple[Path, Path]:
    """Download the MSI, validate its same-release SHA256SUMS entry, and return MSI/log paths."""
    try:
        staging_dir = Path(tempfile.mkdtemp(prefix="vaultline-worker-update-"))
    except OSError as exc:
        raise UpdateError(f"could not create a temporary update directory: {exc}") from exc
    installer_path = staging_dir / release.installer_name
    log_path = staging_dir / "msi-update.log"
    try:
        manifest = _read_limited(
            release.checksums_url,
            limit=MAX_CHECKSUMS_BYTES,
            allowed_hosts=GITHUB_ASSET_HOSTS,
            accept="application/octet-stream",
        )
        _download_asset(release.installer_url, installer_path, limit=MAX_INSTALLER_BYTES)
        digest = _sha256_file(installer_path)
        verify_checksum(manifest, release.installer_name, digest)
    except UpdateError:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise UpdateError(f"could not stage or verify the Windows MSI: {exc}") from exc
    return installer_path, log_path


def stop_worker_service() -> None:
    """Stop and wait for the installed service before MSI replaces its runtime."""
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise UpdateError("Windows SystemRoot is not available")
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        raise UpdateError("Windows PowerShell could not be found to stop the worker service")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$service = Get-Service -Name 'VaultlineWorker' -ErrorAction SilentlyContinue; "
        "if ($service -and $service.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) { "
        "if ($service.Status -ne [System.ServiceProcess.ServiceControllerStatus]::StopPending) { "
        "Stop-Service -Name 'VaultlineWorker' -Force -ErrorAction Stop }; "
        "$service.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Stopped, "
        "[TimeSpan]::FromSeconds(60)) }; "
        "Write-Output 'VaultlineWorker service is stopped.'"
    )
    try:
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
        )
    except OSError as exc:
        raise UpdateError(f"could not stop the VaultlineWorker service: {exc}") from exc
    if completed.returncode != 0:
        raise UpdateError(f"could not stop the VaultlineWorker service (PowerShell exit {completed.returncode})")


def _stream_msi_log(log_path: Path, offset: int) -> int:
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as log:
            log.seek(offset)
            for line in log:
                print(line, end="", flush=True)
            return log.tell()
    except FileNotFoundError:
        return offset
    except OSError as exc:
        print(f"warning: could not read the live MSI log: {exc}", file=sys.stderr, flush=True)
        return offset


def launch_msi_update(installer_path: Path, log_path: Path) -> int:
    """Run MSI in the foreground and stream its verbose log to the invoking console."""
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise UpdateError("Windows SystemRoot is not available")
    msiexec = Path(system_root) / "System32" / "msiexec.exe"
    if not msiexec.is_file():
        raise UpdateError("Windows Installer (msiexec.exe) could not be found")

    print("Stopping VaultlineWorker service...", flush=True)
    stop_worker_service()
    print(f"Installing verified MSI; live installer log: {log_path}", flush=True)
    try:
        process = subprocess.Popen(
            [str(msiexec), "/i", str(installer_path), "/qn", "/norestart", "/L*V", str(log_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise UpdateError(f"could not start Windows Installer: {exc}; MSI log: {log_path}") from exc

    offset = 0
    while process.poll() is None:
        offset = _stream_msi_log(log_path, offset)
        time.sleep(0.2)
    offset = _stream_msi_log(log_path, offset)
    if process.returncode not in (0, 3010):
        raise UpdateError(f"Windows Installer failed with exit code {process.returncode}; MSI log: {log_path}")
    installer_path.unlink(missing_ok=True)
    return process.returncode


def _is_msi_installation() -> bool:
    program_files = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles")
    if not program_files:
        return False
    expected = Path(program_files) / "Vaultline" / "vaultline-worker.exe"
    try:
        return os.path.normcase(str(Path(sys.executable).resolve())) == os.path.normcase(str(expected.resolve()))
    except OSError:
        return False


def _is_user_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def run_worker_update(current_version: str) -> str:
    """Check the latest stable GitHub release and synchronously install its verified MSI."""
    if sys.platform != "win32":
        raise UpdateError("self-update is currently available only for the Windows MSI installation")
    if not getattr(sys, "frozen", False) or not _is_msi_installation():
        raise UpdateError("self-update requires the installed MSI executable in Program Files\\Vaultline")
    if not _is_user_admin():
        raise UpdateError("run PowerShell as Administrator and retry 'vaultline-worker update'")

    release = fetch_latest_release()
    if not is_newer_version(current_version, release.version):
        return f"Vaultline Worker is already up to date ({current_version})."

    installer_path, log_path = download_verified_installer(release)
    result = launch_msi_update(installer_path, log_path)
    if result == 3010:
        return f"Vaultline Worker update to {release.version} completed; a Windows restart is required. MSI log: {log_path}"
    return f"Vaultline Worker update to {release.version} completed successfully. MSI log: {log_path}"
