import base64
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.worker_agent.update import (
    UpdateError,
    build_msi_handoff_script,
    download_verified_installer,
    is_newer_version,
    launch_msi_update,
    parse_latest_release,
    run_worker_update,
    verify_checksum,
)


class WorkerUpdateParsingTests(unittest.TestCase):
    def setUp(self):
        self.version = "3.15.16"
        self.installer_name = "vaultline-worker_3.15.16_windows-amd64.msi"
        self.release_url = "https://github.com/DanielRondonGarcia/vaultline/releases/download/3.15.16"
        self.payload = {
            "tag_name": self.version,
            "prerelease": False,
            "assets": [
                {
                    "name": self.installer_name,
                    "browser_download_url": f"{self.release_url}/{self.installer_name}",
                },
                {
                    "name": "vaultline-worker_3.15.16_SHA256SUMS",
                    "browser_download_url": f"{self.release_url}/vaultline-worker_3.15.16_SHA256SUMS",
                },
            ],
        }

    def test_parse_latest_release_selects_windows_msi_and_checksum_manifest(self):
        release = parse_latest_release(self.payload)

        self.assertEqual(release.version, self.version)
        self.assertEqual(release.installer_name, self.installer_name)
        self.assertEqual(release.installer_url, f"{self.release_url}/{self.installer_name}")
        self.assertEqual(
            release.checksums_url,
            f"{self.release_url}/vaultline-worker_3.15.16_SHA256SUMS",
        )

    def test_parse_latest_release_rejects_prereleases_and_untrusted_asset_urls(self):
        prerelease = dict(self.payload, prerelease=True)
        with self.assertRaisesRegex(UpdateError, "stable release"):
            parse_latest_release(prerelease)

        untrusted = {
            **self.payload,
            "assets": [
                {"name": self.installer_name, "browser_download_url": "https://attacker.invalid/worker.msi"},
                self.payload["assets"][1],
            ],
        }
        with self.assertRaisesRegex(UpdateError, "trusted GitHub release"):
            parse_latest_release(untrusted)

    def test_verify_checksum_accepts_matching_sha256_entry(self):
        digest = "a" * 64
        verify_checksum(f"{digest}  {self.installer_name}\n", self.installer_name, digest)

    def test_verify_checksum_rejects_mismatch_or_missing_asset_entry(self):
        with self.assertRaisesRegex(UpdateError, "checksum mismatch"):
            verify_checksum(f"{'a' * 64}  {self.installer_name}\n", self.installer_name, "b" * 64)

        with self.assertRaisesRegex(UpdateError, "checksum entry"):
            verify_checksum(f"{'a' * 64}  another-file.msi\n", self.installer_name, "a" * 64)

    def test_version_comparison_never_updates_to_same_or_older_release(self):
        self.assertTrue(is_newer_version("3.15.15", "3.15.16"))
        self.assertFalse(is_newer_version("3.15.16", "3.15.16"))
        self.assertFalse(is_newer_version("3.16.0", "3.15.99"))

    def test_download_verified_installer_returns_only_a_checksum_verified_msi(self):
        release = parse_latest_release(self.payload)
        installer_bytes = b"verified installer bytes"
        digest = hashlib.sha256(installer_bytes).hexdigest()
        manifest = f"{digest}  {self.installer_name}\n".encode("ascii")

        def write_installer(_url, destination, *, limit):
            self.assertGreaterEqual(limit, len(installer_bytes))
            destination.write_bytes(installer_bytes)

        with patch("src.worker_agent.update._read_limited", return_value=manifest), patch(
            "src.worker_agent.update._download_asset", side_effect=write_installer
        ):
            installer, log = download_verified_installer(release)

        self.assertEqual(installer.read_bytes(), installer_bytes)
        self.assertEqual(log.parent, installer.parent)
        self.assertEqual(installer.name, self.installer_name)

    def test_checksum_mismatch_removes_staged_msi(self):
        release = parse_latest_release(self.payload)
        installer_bytes = b"tampered installer"
        manifest = f"{'0' * 64}  {self.installer_name}\n".encode("ascii")
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary) / "staging"
            staging.mkdir()

            def write_installer(_url, destination, *, limit):
                destination.write_bytes(installer_bytes)

            with patch("src.worker_agent.update.tempfile.mkdtemp", return_value=str(staging)), patch(
                "src.worker_agent.update._read_limited", return_value=manifest
            ), patch("src.worker_agent.update._download_asset", side_effect=write_installer):
                with self.assertRaisesRegex(UpdateError, "checksum mismatch"):
                    download_verified_installer(release)

            self.assertFalse(staging.exists())

    def test_update_command_launches_newer_release_only_after_verified_download(self):
        release = parse_latest_release(self.payload)
        with patch("src.worker_agent.update.sys.platform", "win32"), patch.object(
            __import__("src.worker_agent.update", fromlist=["sys"]).sys, "frozen", True, create=True
        ), patch("src.worker_agent.update._is_msi_installation", return_value=True), patch(
            "src.worker_agent.update._is_user_admin", return_value=True
        ), patch("src.worker_agent.update.BUILD_VERSION", "3.15.15"), patch(
            "src.worker_agent.update.fetch_latest_release", return_value=release
        ), patch(
            "src.worker_agent.update.download_verified_installer",
            return_value=(Path("C:/Temp/worker.msi"), Path("C:/Temp/msi-update.log")),
        ) as download, patch("src.worker_agent.update.launch_msi_update") as launch:
            message = run_worker_update()

        self.assertEqual(message, "Update to 3.15.16 started; MSI log: C:\\Temp\\msi-update.log")
        download.assert_called_once_with(release)
        launch.assert_called_once_with(Path("C:/Temp/worker.msi"), Path("C:/Temp/msi-update.log"))

    def test_update_command_does_not_download_or_install_when_already_current(self):
        release = parse_latest_release(self.payload)
        with patch("src.worker_agent.update.sys.platform", "win32"), patch.object(
            __import__("src.worker_agent.update", fromlist=["sys"]).sys, "frozen", True, create=True
        ), patch("src.worker_agent.update._is_msi_installation", return_value=True), patch(
            "src.worker_agent.update._is_user_admin", return_value=True
        ), patch("src.worker_agent.update.BUILD_VERSION", "3.15.16"), patch(
            "src.worker_agent.update.fetch_latest_release", return_value=release
        ), patch("src.worker_agent.update.download_verified_installer") as download, patch(
            "src.worker_agent.update.launch_msi_update"
        ) as launch:
            message = run_worker_update()

        self.assertEqual(message, "Vaultline Worker is already up to date (3.15.16).")
        download.assert_not_called()
        launch.assert_not_called()

    def test_update_command_rejects_non_admin_before_network_access(self):
        with patch("src.worker_agent.update.sys.platform", "win32"), patch.object(
            __import__("src.worker_agent.update", fromlist=["sys"]).sys, "frozen", True, create=True
        ), patch("src.worker_agent.update._is_msi_installation", return_value=True), patch(
            "src.worker_agent.update._is_user_admin", return_value=False
        ), patch("src.worker_agent.update.fetch_latest_release") as fetch:
            with self.assertRaisesRegex(UpdateError, "Administrator"):
                run_worker_update()

        fetch.assert_not_called()

    def test_checksum_failure_prevents_the_msi_handoff(self):
        release = parse_latest_release(self.payload)
        with patch("src.worker_agent.update.sys.platform", "win32"), patch.object(
            sys, "frozen", True, create=True
        ), patch("src.worker_agent.update._is_msi_installation", return_value=True), patch(
            "src.worker_agent.update._is_user_admin", return_value=True
        ), patch("src.worker_agent.update.BUILD_VERSION", "3.15.15"), patch(
            "src.worker_agent.update.fetch_latest_release", return_value=release
        ), patch(
            "src.worker_agent.update.download_verified_installer",
            side_effect=UpdateError("SHA256 checksum mismatch"),
        ), patch("src.worker_agent.update.launch_msi_update") as launch:
            with self.assertRaisesRegex(UpdateError, "checksum mismatch"):
                run_worker_update()

        launch.assert_not_called()

    def test_update_is_rejected_outside_windows_before_network_access(self):
        with patch("src.worker_agent.update.sys.platform", "linux"), patch(
            "src.worker_agent.update.fetch_latest_release"
        ) as fetch:
            with self.assertRaisesRegex(UpdateError, "only for the Windows MSI"):
                run_worker_update()

        fetch.assert_not_called()

    def test_msi_handoff_waits_for_cli_exit_and_quotes_paths(self):
        script = build_msi_handoff_script(
            1234,
            r"C:\Users\O'Neil\AppData\Local\Temp\vaultline-worker_3.15.16.msi",
            r"C:\Users\O'Neil\AppData\Local\Temp\vaultline-update.log",
        )

        self.assertLess(script.index("Wait-Process"), script.index("msiexec.exe"))
        self.assertIn("O''Neil", script)
        self.assertIn("/passive", script)
        self.assertIn("/norestart", script)

    def test_launcher_uses_detached_powershell_with_encoded_handoff(self):
        installer = Path("C:/Temp/worker.msi")
        log = Path("C:/Temp/update.log")
        with patch.dict(os.environ, {"SystemRoot": "C:/Windows"}), patch.object(
            Path, "is_file", return_value=True
        ), patch.object(subprocess, "DETACHED_PROCESS", 0x00000008, create=True), patch.object(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, create=True
        ), patch("src.worker_agent.update.subprocess.Popen") as popen:
            launch_msi_update(installer, log)

        args, kwargs = popen.call_args
        expected_powershell = str(Path("C:/Windows") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
        self.assertEqual(args[0][0], expected_powershell)
        script = base64.b64decode(args[0][-1]).decode("utf-16le")
        self.assertLess(script.index("Wait-Process"), script.index("msiexec.exe"))
        self.assertEqual(kwargs["creationflags"], 0x208)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
