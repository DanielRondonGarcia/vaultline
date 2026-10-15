#!/usr/bin/env python3
"""Download and verify the Restic release binary used by native worker builds."""

import argparse
import bz2
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Callable
from urllib.request import Request, urlopen
import zipfile


RELEASE_TAG = "v0.19.1"
RELEASE_URL = f"https://github.com/restic/restic/releases/download/{RELEASE_TAG}"


@dataclass(frozen=True)
class ResticAsset:
    filename: str
    sha256: str
    archive_format: str
    executable_name: str
    member_name: str | None = None


ASSETS = {
    ("windows", "amd64"): ResticAsset(
        "restic_0.19.1_windows_amd64.zip",
        "da948ad707ed690426473aaba2046cd61f8f90f6f0e7dab6be0d5796531de67d",
        "zip",
        "restic.exe",
        "restic_0.19.1_windows_amd64.exe",
    ),
    ("linux", "amd64"): ResticAsset(
        "restic_0.19.1_linux_amd64.bz2",
        "f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c",
        "bz2",
        "restic",
    ),
    ("linux", "arm64"): ResticAsset(
        "restic_0.19.1_linux_arm64.bz2",
        "a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465",
        "bz2",
        "restic",
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_archive(archive_path: Path, asset: ResticAsset, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
        ) as output:
            temporary_path = Path(output.name)
            if asset.archive_format == "zip":
                with zipfile.ZipFile(archive_path) as archive:
                    member = archive.getinfo(asset.member_name or "")
                    member_mode = member.external_attr >> 16
                    if member.is_dir() or stat.S_ISLNK(member_mode):
                        raise ValueError("Restic release archive contains an invalid executable entry")
                    with archive.open(member) as source:
                        shutil.copyfileobj(source, output)
            elif asset.archive_format == "bz2":
                with bz2.open(archive_path, "rb") as source:
                    shutil.copyfileobj(source, output)
            else:
                raise ValueError(f"unsupported Restic archive format: {asset.archive_format}")

        if asset.archive_format == "bz2":
            os.chmod(temporary_path, 0o755)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _extract_verified_archive(archive_path: Path, asset: ResticAsset, output_dir: Path) -> Path:
    actual = _sha256(archive_path)
    if actual != asset.sha256:
        raise ValueError(f"SHA-256 mismatch for {asset.filename}: expected {asset.sha256}, got {actual}")

    executable = output_dir / asset.executable_name
    _extract_archive(archive_path, asset, executable)
    return executable


def _download_asset(asset: ResticAsset, archive_path: Path, opener: Callable | None = None) -> None:
    open_url = opener or urlopen
    request = Request(f"{RELEASE_URL}/{asset.filename}", headers={"User-Agent": "vaultline-worker-build"})
    with open_url(request, timeout=120) as response, archive_path.open("wb") as output:
        shutil.copyfileobj(response, output)


def download_restic(
    platform: str,
    architecture: str,
    output_dir: Path,
    opener: Callable | None = None,
) -> Path:
    try:
        asset = ASSETS[(platform, architecture)]
    except KeyError as exc:
        raise ValueError(f"unsupported Restic platform and architecture: {platform}/{architecture}") from exc

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vaultline-restic-download-") as temporary_dir:
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
        executable = download_restic(args.platform, args.arch, args.output_dir)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
