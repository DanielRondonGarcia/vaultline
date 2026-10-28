import ast
import hashlib
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import download_pinned_rclone
from scripts import download_pinned_restic


def _string_constants(node):
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _is_win32_platform_check(test):
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Attribute)
        and isinstance(test.left.value, ast.Name)
        and test.left.value.id == "sys"
        and test.left.attr == "platform"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "win32"
    )


def _win32_blocks(tree):
    return [node for node in tree.body if isinstance(node, ast.If) and _is_win32_platform_check(node.test)]


class WorkerPackagingTests(unittest.TestCase):
    def test_vaultline_worker_pyinstaller_spec_declares_one_file_worker_cli(self):
        spec_path = Path("build/vaultline-worker.spec")

        self.assertTrue(spec_path.exists(), "vaultline-worker PyInstaller spec is required")
        spec = spec_path.read_text(encoding="utf-8")

        self.assertIn("name='vaultline-worker-runtime'", spec)
        self.assertIn("console=True", spec)
        self.assertIn("src/worker_agent/cli.py", spec.replace("\\\\", "/"))
        self.assertIn("src.app.main", spec)
        self.assertIn("src.worker_agent.cli", spec)
        self.assertNotIn(".worker_credentials", spec)
        self.assertNotIn("WORKER_SECRET", spec)
        self.assertNotIn("rclone.conf", spec)
        self.assertNotIn("RCLONE_CONF_CONTENT", spec)
        self.assertIn("VAULTLINE_RESTIC_BINARY", spec)
        self.assertIn("VAULTLINE_RCLONE_BINARY", spec)
        self.assertIn('binaries=[(str(RESTIC_BINARY), "."), (str(RCLONE_BINARY), ".")]', spec)
        self.assertIn('datas=[(str(RESTIC_LICENSE), "licenses/restic"), (str(RCLONE_LICENSE), "licenses/rclone")]', spec)
        self.assertIn("restic-v0.19.1-LICENSE", spec)
        self.assertIn("rclone-v1.75.2-COPYING", spec)
        self.assertNotIn("tar.exe", spec.lower())

    def test_stable_launcher_and_replaceable_runtime_have_separate_msi_roles(self):
        launcher_spec_path = Path("build/vaultline-worker-launcher.spec")
        runtime_spec = Path("build/vaultline-worker.spec").read_text(encoding="utf-8")
        wix_source = Path("packaging/windows/vaultline-worker.wxs").read_text(encoding="utf-8")

        self.assertTrue(launcher_spec_path.is_file(), "stable launcher PyInstaller spec is required")
        launcher_spec = launcher_spec_path.read_text(encoding="utf-8")
        self.assertIn("src/worker_agent/launcher.py", launcher_spec.replace("\\\\", "/"))
        self.assertIn('name="vaultline-worker"', launcher_spec)
        self.assertIn("name='vaultline-worker-runtime'", runtime_spec)
        self.assertIn('Name="vaultline-worker.exe"', wix_source)
        self.assertIn('Name="vaultline-worker-runtime.exe"', wix_source)
        self.assertIn('Version="4.0.0.0"', wix_source)
        self.assertIn('ServiceInstall', wix_source)
        self.assertIn('Arguments="service run"', wix_source)
        self.assertIn('Schedule="afterInstallExecute"', wix_source)

    def test_pinned_restic_archives_use_the_supplied_sha256_digests(self):
        self.assertEqual(download_pinned_restic.RELEASE_TAG, "v0.19.1")
        self.assertEqual(
            {key: asset.sha256 for key, asset in download_pinned_restic.ASSETS.items()},
            {
                ("windows", "amd64"): "da948ad707ed690426473aaba2046cd61f8f90f6f0e7dab6be0d5796531de67d",
                ("linux", "amd64"): "f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c",
                ("linux", "arm64"): "a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465",
            },
        )

    def test_restic_archive_digest_is_checked_before_extraction(self):
        asset = download_pinned_restic.ResticAsset("restic.zip", "0" * 64, "zip", "restic.exe", "restic.exe")
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / asset.filename
            archive.write_bytes(b"unverified archive")
            with patch.object(download_pinned_restic, "_extract_archive") as extract:
                with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                    download_pinned_restic._extract_verified_archive(archive, asset, Path(directory) / "out")
            extract.assert_not_called()
            self.assertFalse((Path(directory) / "out" / "restic.exe").exists())
            self.assertEqual(
                hashlib.sha256(archive.read_bytes()).hexdigest(),
                download_pinned_restic._sha256(archive),
            )

    def test_pinned_rclone_archives_use_the_supplied_sha256_digests(self):
        self.assertEqual(download_pinned_rclone.RELEASE_TAG, "v1.75.2")
        self.assertEqual(
            {key: asset.sha256 for key, asset in download_pinned_rclone.ASSETS.items()},
            {
                ("windows", "amd64"): "8a9db2bad1dc79a65b3348fd857069598e2aab7cf4d118b77b1df23055aff818",
                ("linux", "amd64"): "349ac8fba6ff65d6247043f1750cdcb518ec5d500ef91463a10d37c0ccdf3702",
                ("linux", "arm64"): "7e1e8d69654941b7b7df84ee74c5f7fb09cce7d5496947fb7bf16b55ff427d10",
            },
        )
        self.assertEqual(
            {key: asset.filename for key, asset in download_pinned_rclone.ASSETS.items()},
            {
                ("windows", "amd64"): "rclone-v1.75.2-windows-amd64.zip",
                ("linux", "amd64"): "rclone-v1.75.2-linux-amd64.zip",
                ("linux", "arm64"): "rclone-v1.75.2-linux-arm64.zip",
            },
        )

    def test_rclone_archive_digest_is_checked_before_extraction(self):
        asset = download_pinned_rclone.RcloneAsset("rclone.zip", "0" * 64, "rclone")
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / asset.filename
            archive.write_bytes(b"unverified archive")
            with patch.object(download_pinned_rclone, "_extract_archive") as extract:
                with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                    download_pinned_rclone._extract_verified_archive(archive, asset, Path(directory) / "out")
            extract.assert_not_called()
            self.assertFalse((Path(directory) / "out" / "rclone").exists())

    def test_verified_rclone_archive_extracts_the_linux_executable_with_execute_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "rclone.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("rclone-v1.75.2-linux-amd64/rclone", b"synthetic rclone executable")
            asset = download_pinned_rclone.RcloneAsset(
                archive_path.name,
                download_pinned_rclone._sha256(archive_path),
                "rclone",
                make_executable=True,
            )

            with patch.object(download_pinned_rclone.os, "chmod") as chmod:
                executable = download_pinned_rclone._extract_verified_archive(
                    archive_path, asset, Path(directory) / "out"
                )

            self.assertEqual(executable.read_bytes(), b"synthetic rclone executable")
            chmod.assert_called_once()
            self.assertEqual(Path(chmod.call_args.args[0]).parent, executable.parent)
            self.assertEqual(chmod.call_args.args[1], 0o755)

    def test_pinned_restic_license_is_kept_for_binary_redistribution(self):
        license_path = Path("packaging/licenses/restic-v0.19.1-LICENSE")
        self.assertTrue(license_path.is_file())
        license_text = license_path.read_text(encoding="utf-8")
        self.assertIn("BSD 2-Clause License", license_text)
        self.assertIn("Copyright (c) 2014, the restic developers", license_text)
        self.assertIn("2. Redistributions in binary form must reproduce", license_text)

        deb_builder = Path("scripts/build_worker_deb.sh").read_text(encoding="utf-8")
        wix_source = Path("packaging/windows/vaultline-worker.wxs").read_text(encoding="utf-8")
        self.assertIn("usr/share/doc/vaultline-worker/restic-v0.19.1-LICENSE", deb_builder)
        self.assertIn('Name="restic-v0.19.1-LICENSE"', wix_source)
        self.assertIn('ComponentRef Id="ResticLicense"', wix_source)

    def test_pinned_rclone_license_is_kept_for_binary_redistribution(self):
        license_path = Path("packaging/licenses/rclone-v1.75.2-COPYING")
        self.assertTrue(license_path.is_file())
        license_text = license_path.read_text(encoding="utf-8")
        self.assertIn("Copyright (C) 2012 by Nick Craig-Wood", license_text)
        self.assertIn("Permission is hereby granted, free of charge", license_text)
        self.assertIn("THE SOFTWARE IS PROVIDED \"AS IS\"", license_text)

        spec = Path("build/vaultline-worker.spec").read_text(encoding="utf-8")
        deb_builder = Path("scripts/build_worker_deb.sh").read_text(encoding="utf-8")
        wix_source = Path("packaging/windows/vaultline-worker.wxs").read_text(encoding="utf-8")
        self.assertIn('datas=[(str(RESTIC_LICENSE), "licenses/restic"), (str(RCLONE_LICENSE), "licenses/rclone")]', spec)
        self.assertIn("usr/share/doc/vaultline-worker/rclone-v1.75.2-COPYING", deb_builder)
        self.assertIn('Name="rclone-v1.75.2-COPYING"', wix_source)
        self.assertIn('ComponentRef Id="RcloneLicense"', wix_source)

    def test_backup_and_retention_commands_use_the_resolved_restic_executable(self):
        from src.app.domain.models import BackupConfig
        from src.app.infrastructure.adapters.backup_strategy import ResticBackupStrategy

        strategy = ResticBackupStrategy()
        strategy._run_incremental_backup = Mock(
            return_value=('{"message_type":"summary","data_added":7,"total_duration":1.5}', "", 0)
        )
        executable_name = "restic.exe" if sys.platform == "win32" else "restic"
        rclone_name = "rclone.exe" if sys.platform == "win32" else "rclone"
        with tempfile.TemporaryDirectory() as bundle_dir:
            bundled_file = Path(bundle_dir) / executable_name
            bundled_file.write_text("synthetic bundled executable", encoding="utf-8")
            bundled_file.chmod(0o755)
            bundled_rclone = Path(bundle_dir) / rclone_name
            bundled_rclone.write_text("synthetic bundled executable", encoding="utf-8")
            bundled_rclone.chmod(0o755)
            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "_MEIPASS", bundle_dir, create=True
            ), patch.dict(os.environ, {"PATH": "host-tools"}, clear=True), patch(
                "src.app.infrastructure.adapters.backup_strategy.subprocess.run"
            ) as run:
                result = strategy.perform_backup(BackupConfig(source_paths=["/backup"], restic_keep_daily=1))

        self.assertTrue(result.success, result.error)
        self.assertEqual(run.call_args_list[0].args[0][0], str(bundled_file))
        self.assertEqual(strategy._run_incremental_backup.call_args.args[0][0], str(bundled_file))
        self.assertEqual(run.call_args_list[-1].args[0][0], str(bundled_file))
        expected_path = os.pathsep.join((bundle_dir, "host-tools"))
        self.assertEqual(run.call_args_list[0].kwargs["env"]["PATH"], expected_path)
        self.assertEqual(strategy._run_incremental_backup.call_args.args[1]["PATH"], expected_path)

    def test_restic_snapshot_listing_uses_the_resolved_restic_executable(self):
        from src.app.infrastructure.adapters.storage.multi_storage_adapter import MultiStorageAdapter

        runner = Mock(return_value=SimpleNamespace(stdout="[]"))
        config = SimpleNamespace(restic_password=None)
        with patch(
            "src.app.infrastructure.adapters.storage.multi_storage_adapter.resolve_restic_executable",
            return_value="/bundle/restic",
        ), patch(
            "src.app.infrastructure.adapters.storage.multi_storage_adapter.subprocess.run", runner
        ):
            self.assertEqual(MultiStorageAdapter()._list_restic_candidates(config, "local:/repo"), [])

        self.assertEqual(runner.call_args.args[0], ["/bundle/restic", "snapshots", "--json"])

    def test_direct_rclone_storage_calls_use_the_resolved_executable(self):
        from src.app.infrastructure.adapters.storage.multi_storage_adapter import MultiStorageAdapter

        adapter = MultiStorageAdapter()
        runner = Mock(return_value=SimpleNamespace(stdout="[]"))
        with patch(
            "src.app.infrastructure.adapters.storage.multi_storage_adapter.resolve_rclone_executable",
            return_value="/bundle/rclone",
        ), patch("src.app.infrastructure.adapters.storage.multi_storage_adapter.subprocess.run", runner), patch.object(
            adapter, "_download_to_temp", return_value="/tmp/restored-archive"
        ) as download:
            adapter._upload_rclone("archive.tar", "remote:/backups")
            adapter._list_rclone_candidates("remote:/backups", "tar")
            candidate = SimpleNamespace(source="rclone://remote:/backups/archive.tar")
            self.assertEqual(adapter.download_restore_candidate(candidate, None), "/tmp/restored-archive")

        self.assertEqual(runner.call_args_list[0].args[0], ["/bundle/rclone", "copy", "archive.tar", "remote:/backups"])
        self.assertEqual(runner.call_args_list[1].args[0], ["/bundle/rclone", "lsjson", "remote:/backups"])
        download.assert_called_once_with(["/bundle/rclone", "copyto", "remote:/backups/archive.tar"])

    def test_vaultline_worker_spec_adds_backports_hiddenimports_only_on_windows(self):
        spec_path = Path("build/vaultline-worker.spec")
        spec = spec_path.read_text(encoding="utf-8")
        tree = ast.parse(spec)
        backports_imports = {"backports", "backports.tarfile"}

        win32_blocks = _win32_blocks(tree)
        self.assertEqual(1, len(win32_blocks), 'spec must gate backports imports with sys.platform == "win32"')
        self.assertTrue(backports_imports.issubset(_string_constants(win32_blocks[0])))

        for node in tree.body:
            if node is win32_blocks[0]:
                continue
            self.assertTrue(
                backports_imports.isdisjoint(_string_constants(node)),
                "backports hidden imports must not be declared outside the Windows-only block",
            )

    def test_vaultline_worker_spec_adds_pywin32_hiddenimports_only_on_windows(self):
        spec = Path("build/vaultline-worker.spec").read_text(encoding="utf-8")
        tree = ast.parse(spec)
        pywin32_imports = {
            "win32serviceutil",
            "win32service",
            "win32event",
            "servicemanager",
            "win32api",
            "win32timezone",
            "pywintypes",
            "win32con",
            "win32net",
            "win32netcon",
        }

        win32_blocks = _win32_blocks(tree)
        self.assertEqual(1, len(win32_blocks), "pywin32 hidden imports must reuse the single Windows-only block")
        self.assertTrue(pywin32_imports.issubset(_string_constants(win32_blocks[0])))
        self.assertIn("src.worker_agent.windows_service", spec)

        for node in tree.body:
            if node is win32_blocks[0]:
                continue
            self.assertTrue(
                pywin32_imports.isdisjoint(_string_constants(node)),
                "pywin32 hidden imports must not be declared outside the Windows-only block",
            )

    def test_runtime_requirements_declare_pywin32_for_windows_only(self):
        requirements = Path("requirements.txt").read_text(encoding="utf-8")

        match = re.search(r"^pywin32\s*([^;]*);\s*(.+)$", requirements, re.MULTILINE)
        self.assertIsNotNone(match, "pywin32 must be declared with an environment marker")
        self.assertRegex(match.group(2).strip(), r'^sys_platform\s*==\s*"win32"$')
        self.assertRegex(match.group(1).strip(), r"^>=\s*306")

    def test_vaultline_worker_spec_resolves_cli_from_pyinstaller_spec_dir(self):
        spec_path = Path("build/vaultline-worker.spec")
        spec = spec_path.read_text(encoding="utf-8")
        root_match = re.search(r"^ROOT\s*=\s*(.+)$", spec, re.MULTILINE)
        self.assertIsNotNone(root_match, "spec must define ROOT from PyInstaller SPECPATH")

        namespace = {"Path": Path, "SPECPATH": str(spec_path.parent.resolve())}
        exec(f"ROOT = {root_match.group(1)}", namespace)

        root = namespace["ROOT"]
        self.assertEqual(Path.cwd().resolve(), root)
        self.assertTrue((root / "src/worker_agent/cli.py").exists())

    def test_build_requirements_pin_pyinstaller_for_ci_python_version(self):
        requirements_path = Path("requirements-build.txt")

        self.assertTrue(requirements_path.exists(), "build-only requirements file is required")
        requirements = requirements_path.read_text(encoding="utf-8")

        match = re.search(r"^pyinstaller==([0-9]+)\.([0-9]+)\.([0-9]+)$", requirements, re.MULTILINE | re.IGNORECASE)
        self.assertIsNotNone(match, "PyInstaller must be exactly pinned")
        major, minor, patch = map(int, match.groups())
        self.assertGreaterEqual((major, minor, patch), (6, 15, 0))
        self.assertRegex(requirements, r"(?im)^-r\s+requirements\.txt$")


if __name__ == "__main__":
    unittest.main()
