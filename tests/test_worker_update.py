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
    stop_worker_service,
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
        ), patch("src.worker_agent.update.fetch_latest_release", return_value=release), patch(
            "src.worker_agent.update.download_verified_installer",
            return_value=(Path("C:/Temp/worker.msi"), Path("C:/Temp/msi-update.log")),
        ) as download, patch("src.worker_agent.update.launch_msi_update", return_value=0) as launch:
            message = run_worker_update(current_version="3.15.15")

        self.assertEqual(
            message,
            "Vaultline Worker update to 3.15.16 completed successfully. MSI log: C:\\Temp\\msi-update.log",
        )
        download.assert_called_once_with(release)
        launch.assert_called_once_with(Path("C:/Temp/worker.msi"), Path("C:/Temp/msi-update.log"))

    def test_update_command_does_not_download_or_install_when_already_current(self):
        release = parse_latest_release(self.payload)
        with patch("src.worker_agent.update.sys.platform", "win32"), patch.object(
            __import__("src.worker_agent.update", fromlist=["sys"]).sys, "frozen", True, create=True
        ), patch("src.worker_agent.update._is_msi_installation", return_value=True), patch(
            "src.worker_agent.update._is_user_admin", return_value=True
        ), patch("src.worker_agent.update.fetch_latest_release", return_value=release), patch(
            "src.worker_agent.update.download_verified_installer"
        ) as download, patch("src.worker_agent.update.launch_msi_update") as launch:
            message = run_worker_update(current_version="3.15.16")

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
                run_worker_update(current_version="3.15.15")

        fetch.assert_not_called()

    def test_checksum_failure_prevents_the_msi_handoff(self):
        release = parse_latest_release(self.payload)
        with patch("src.worker_agent.update.sys.platform", "win32"), patch.object(
            sys, "frozen", True, create=True
        ), patch("src.worker_agent.update._is_msi_installation", return_value=True), patch(
            "src.worker_agent.update._is_user_admin", return_value=True
        ), patch("src.worker_agent.update.fetch_latest_release", return_value=release), patch(
            "src.worker_agent.update.download_verified_installer",
            side_effect=UpdateError("SHA256 checksum mismatch"),
        ), patch("src.worker_agent.update.launch_msi_update") as launch:
            with self.assertRaisesRegex(UpdateError, "checksum mismatch"):
                run_worker_update(current_version="3.15.15")

        launch.assert_not_called()

    def test_update_is_rejected_outside_windows_before_network_access(self):
        with patch("src.worker_agent.update.sys.platform", "linux"), patch(
            "src.worker_agent.update.fetch_latest_release"
        ) as fetch:
            with self.assertRaisesRegex(UpdateError, "only for the Windows MSI"):
                run_worker_update(current_version="3.15.15")

        fetch.assert_not_called()

    def test_service_stop_waits_for_the_service_to_be_stopped(self):
        with patch.dict(os.environ, {"SystemRoot": "C:/Windows"}), patch.object(
            Path, "is_file", return_value=True
        ), patch(
            "src.worker_agent.update.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            stop_worker_service()

        command = run.call_args.args[0]
        self.assertIn("-Command", command)
        script = command[-1]
        self.assertIn("Stop-Service -Name 'VaultlineWorker'", script)
        self.assertIn("WaitForStatus", script)
        self.assertNotIn("Wait-Process", script)

    def test_msi_update_runs_foreground_and_streams_log_until_completion(self):
        class FakeProcess:
            returncode = 0

            def __init__(self):
                self.polls = iter([None, 0])

            def poll(self):
                return next(self.polls)

        installer = Path("C:/Temp/worker.msi")
        log = Path("C:/Temp/update.log")
        with patch.dict(os.environ, {"SystemRoot": "C:/Windows"}), patch.object(
            Path, "is_file", return_value=True
        ), patch("src.worker_agent.update.stop_worker_service") as stop, patch(
            "src.worker_agent.update.subprocess.Popen", return_value=FakeProcess()
        ) as popen, patch("src.worker_agent.update._stream_msi_log", side_effect=[4, 8]) as stream, patch(
            "src.worker_agent.update.time.sleep"
        ), patch("builtins.print"):
            result = launch_msi_update(installer, log)

        self.assertEqual(result, 0)
        stop.assert_called_once_with()
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], str(Path("C:/Windows") / "System32" / "msiexec.exe"))
        self.assertIn("/i", args[0])
        self.assertIn("/qn", args[0])
        self.assertIn("/L*V", args[0])
        self.assertNotIn("creationflags", kwargs)
        self.assertEqual(stream.call_count, 2)


if __name__ == "__main__":
    unittest.main()
