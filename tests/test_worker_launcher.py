import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from src.worker_agent import launcher


class WorkerLauncherTests(unittest.TestCase):
    def test_update_is_handled_by_stable_launcher_without_spawning_runtime(self):
        with patch.object(launcher, "_installed_runtime_version", return_value="3.15.17"), patch.object(
            launcher, "run_worker_update", return_value="Vaultline Worker is already up to date (3.15.17)."
        ) as update, patch("builtins.print") as print_line, patch.object(launcher.subprocess, "run") as run:
            result = launcher.main(["update"])

        self.assertEqual(result, 0)
        update.assert_called_once_with(current_version="3.15.17")
        print_line.assert_any_call("Checking the latest stable Vaultline Worker release...")
        print_line.assert_any_call("Vaultline Worker is already up to date (3.15.17).")
        run.assert_not_called()

    def test_other_commands_delegate_to_replaceable_runtime_with_inherited_console(self):
        runtime = Path("C:/Program Files/Vaultline/vaultline-worker-runtime.exe")
        completed = subprocess.CompletedProcess([str(runtime), "--version"], 0)
        with patch.object(launcher, "_runtime_path", return_value=runtime), patch.object(
            Path, "is_file", return_value=True
        ), patch.object(launcher.subprocess, "run", return_value=completed) as run:
            result = launcher.main(["--version"])

        self.assertEqual(result, 0)
        run.assert_called_once_with([str(runtime), "--version"], check=False)

    def test_missing_runtime_is_reported_without_attempting_to_start_it(self):
        runtime = Path("C:/Program Files/Vaultline/vaultline-worker-runtime.exe")
        with patch.object(launcher, "_runtime_path", return_value=runtime), patch.object(
            Path, "is_file", return_value=False
        ), patch.object(launcher.subprocess, "run") as run, patch("builtins.print") as print_line:
            result = launcher.main(["service", "status"])

        self.assertEqual(result, 1)
        print_line.assert_called_once()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
