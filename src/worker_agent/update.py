"""On-demand self-update for the packaged Windows worker MSI."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from src.worker_agent._build_info import VERSION as BUILD_VERSION


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


def _powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_msi_handoff_script(parent_pid: int, installer_path: str, log_path: str) -> str:
    """Build a PowerShell helper that waits until this CLI exits before starting MSI."""
    installer = _powershell_literal(installer_path)
    log = _powershell_literal(log_path)
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$parentPid = {int(parent_pid)}\n"
        f"$installer = {installer}\n"
        f"$log = {log}\n"
        "try {\n"
        "  Wait-Process -Id $parentPid -ErrorAction SilentlyContinue\n"
        "  $msiexec = Join-Path $env:SystemRoot 'System32\\msiexec.exe'\n"
        "  & $msiexec /i $installer /passive /norestart /L*v $log\n"
        "  $result = $LASTEXITCODE\n"
        "  if ($result -eq 0) { Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue }\n"
        "  exit $result\n"
        "} catch {\n"
        "  $_ | Out-File -FilePath $log -Append -Encoding utf8\n"
        "  exit 1\n"
        "}\n"
    )


def launch_msi_update(installer_path: Path, log_path: Path) -> None:
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise UpdateError("Windows SystemRoot is not available")
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        raise UpdateError("Windows PowerShell could not be found to start the MSI update")
    script = build_msi_handoff_script(os.getpid(), str(installer_path), str(log_path))
    encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    creation_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            [str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise UpdateError(f"could not start the Windows MSI update helper: {exc}") from exc


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
    from src.worker_agent.cli import is_user_admin

    return is_user_admin()


def run_worker_update() -> str:
    """Check the latest stable GitHub release and launch its verified Windows MSI."""
    if sys.platform != "win32":
        raise UpdateError("self-update is currently available only for the Windows MSI installation")
    if not getattr(sys, "frozen", False) or not _is_msi_installation():
        raise UpdateError("self-update requires the installed MSI executable in Program Files\\Vaultline")
    if not _is_user_admin():
        raise UpdateError("run PowerShell as Administrator and retry 'vaultline-worker update'")

    release = fetch_latest_release()
    if not is_newer_version(BUILD_VERSION, release.version):
        return f"Vaultline Worker is already up to date ({BUILD_VERSION})."

    installer_path, log_path = download_verified_installer(release)
    try:
        launch_msi_update(installer_path, log_path)
    except Exception:
        shutil.rmtree(installer_path.parent, ignore_errors=True)
        raise
    return f"Update to {release.version} started; MSI log: {log_path}"
