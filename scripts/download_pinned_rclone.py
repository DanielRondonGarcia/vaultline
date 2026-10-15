#!/usr/bin/env python3
"""Download and verify the rclone release binary used by native worker builds."""

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import Callable
from urllib.request import Request, urlopen
import zipfile


RELEASE_TAG = "v1.75.2"
RELEASE_URL = f"https://github.com/rclone/rclone/releases/download/{RELEASE_TAG}"


@dataclass(frozen=True)
class RcloneAsset:
    filename: str
    sha256: str
    executable_name: str
    make_executable: bool = False


ASSETS = {
    ("windows", "amd64"): RcloneAsset(
        "rclone-v1.75.2-windows-amd64.zip",
        "8a9db2bad1dc79a65b3348fd857069598e2aab7cf4d118b77b1df23055aff818",
        "rclone.exe",
    ),
    ("linux", "amd64"): RcloneAsset(
        "rclone-v1.75.2-linux-amd64.zip",
        "349ac8fba6ff65d6247043f1750cdcb518ec5d500ef91463a10d37c0ccdf3702",
        "rclone",
        make_executable=True,
    ),
    ("linux", "arm64"): RcloneAsset(
        "rclone-v1.75.2-linux-arm64.zip",
        "7e1e8d69654941b7b7df84ee74c5f7fb09cce7d5496947fb7bf16b55ff427d10",
        "rclone",
        make_executable=True,
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_archive(archive_path: Path, asset: RcloneAsset, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with zipfile.ZipFile(archive_path) as archive:
            executable_members = [
                member
                for member in archive.infolist()
                if PurePosixPath(member.filename.replace("\\", "/")).name == asset.executable_name
            ]
            if len(executable_members) != 1:
                raise ValueError("rclone release archive must contain exactly one executable")
            member = executable_members[0]
            member_mode = member.external_attr >> 16
            member_type = stat.S_IFMT(member_mode)
            if member.is_dir() or stat.S_ISLNK(member_mode) or member_type not in (0, stat.S_IFREG):
                raise ValueError("rclone release archive contains an invalid executable entry")

            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
            ) as output:
                temporary_path = Path(output.name)
                with archive.open(member) as source:
                    shutil.copyfileobj(source, output)

        if asset.make_executable:
            os.chmod(temporary_path, 0o755)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _extract_verified_archive(archive_path: Path, asset: RcloneAsset, output_dir: Path) -> Path:
    actual = _sha256(archive_path)
    if actual != asset.sha256:
        raise ValueError(f"SHA-256 mismatch for {asset.filename}: expected {asset.sha256}, got {actual}")

    executable = output_dir / asset.executable_name
    _extract_archive(archive_path, asset, executable)
    return executable


def _download_asset(asset: RcloneAsset, archive_path: Path, opener: Callable | None = None) -> None:
    open_url = opener or urlopen
    request = Request(f"{RELEASE_URL}/{asset.filename}", headers={"User-Agent": "vaultline-worker-build"})
    with open_url(request, timeout=120) as response, archive_path.open("wb") as output:
        shutil.copyfileobj(response, output)


def download_rclone(
    platform: str,
    architecture: str,
    output_dir: Path,
    opener: Callable | None = None,
) -> Path:
    try:
        asset = ASSETS[(platform, architecture)]
    except KeyError as exc:
        raise ValueError(f"unsupported rclone platform and architecture: {platform}/{architecture}") from exc

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vaultline-rclone-download-") as temporary_dir:
        archive_path = Path(temporary_dir) / asset.filename
        _download_asset(asset, archive_path, opener)
        return _extract_verified_archive(archive_path, asset, output_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("linux", "windows"), required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    try:
        executable = download_rclone(args.platform, args.arch, args.output_dir)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
