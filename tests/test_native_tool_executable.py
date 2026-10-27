import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from src.native_tool_executable import frozen_tool_directories, installed_tool_directory, resolve_native_tool
from src.rclone_executable import resolve_rclone_executable
from src.restic_executable import resolve_restic_executable, restic_subprocess_environment


RESTIC = "restic.exe" if sys.platform == "win32" else "restic"
RCLONE = "rclone.exe" if sys.platform == "win32" else "rclone"


def _write_tool(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text(f"synthetic {name}", encoding="utf-8")
    tool.chmod(0o755)
    return tool


class FrozenLayout:
    """Simulate a frozen worker: ``sys.executable`` at the installed location plus an optional bundle."""

    def __init__(self, root: Path, bundle: bool = True):
        self.root = root
        if sys.platform == "win32":
            self.install_dir = root / "Program Files" / "Vaultline"
            self.executable = self.install_dir / "vaultline-worker.exe"
        else:
            self.install_dir = root / "usr" / "lib" / "vaultline"
            self.executable = root / "usr" / "bin" / "vaultline-worker"
        self.executable.parent.mkdir(parents=True, exist_ok=True)
        self.executable.write_text("synthetic worker", encoding="utf-8")
        self.bundle_dir = root / "_MEI12345" if bundle else None
        if self.bundle_dir:
            self.bundle_dir.mkdir()

    def __enter__(self):
        self._stack = ExitStack()
        self._stack.enter_context(patch.object(sys, "frozen", True, create=True))
        self._stack.enter_context(patch.object(sys, "executable", str(self.executable)))
        if self.bundle_dir:
            self._stack.enter_context(patch.object(sys, "_MEIPASS", str(self.bundle_dir), create=True))
        elif hasattr(sys, "_MEIPASS"):
            self._stack.enter_context(patch.object(sys, "_MEIPASS", None))
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False


class NativeToolExecutableTests(unittest.TestCase):
    def test_source_mode_keeps_host_path_lookup(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(resolve_native_tool("restic", "Restic"), "restic")
            self.assertEqual(resolve_restic_executable(), "restic")
            self.assertEqual(resolve_rclone_executable(), "rclone")

    def test_installed_directory_is_beside_the_worker_on_windows_and_lib_vaultline_on_linux(self):
        with tempfile.TemporaryDirectory() as directory, FrozenLayout(Path(directory)) as layout:
            self.assertEqual(installed_tool_directory(), layout.install_dir.resolve())
            self.assertEqual(frozen_tool_directories(), [layout.install_dir.resolve(), layout.bundle_dir])
            expected = "Program Files/Vaultline" if sys.platform == "win32" else "usr/lib/vaultline"
            self.assertEqual(installed_tool_directory().relative_to(Path(directory).resolve()).as_posix(), expected)

    def test_installed_copy_wins_over_the_pyinstaller_bundle(self):
        with tempfile.TemporaryDirectory() as directory, FrozenLayout(Path(directory)) as layout:
            installed_restic = _write_tool(layout.install_dir, RESTIC)
            installed_rclone = _write_tool(layout.install_dir, RCLONE)
            _write_tool(layout.bundle_dir, RESTIC)
            _write_tool(layout.bundle_dir, RCLONE)

            self.assertEqual(resolve_restic_executable(), str(installed_restic.resolve()))
            self.assertEqual(resolve_rclone_executable(), str(installed_rclone.resolve()))

    def test_bundle_is_used_only_when_the_installed_copy_is_absent(self):
        with tempfile.TemporaryDirectory() as directory, FrozenLayout(Path(directory)) as layout:
            bundled_restic = _write_tool(layout.bundle_dir, RESTIC)
            bundled_rclone = _write_tool(layout.bundle_dir, RCLONE)

            self.assertEqual(resolve_restic_executable(), str(bundled_restic))
            self.assertEqual(resolve_rclone_executable(), str(bundled_rclone))

    def test_each_tool_is_resolved_independently(self):
        with tempfile.TemporaryDirectory() as directory, FrozenLayout(Path(directory)) as layout:
            installed_restic = _write_tool(layout.install_dir, RESTIC)
            bundled_rclone = _write_tool(layout.bundle_dir, RCLONE)

            self.assertEqual(resolve_restic_executable(), str(installed_restic.resolve()))
            self.assertEqual(resolve_rclone_executable(), str(bundled_rclone))

    def test_frozen_worker_fails_closed_when_neither_location_has_the_tool(self):
        with tempfile.TemporaryDirectory() as directory, FrozenLayout(Path(directory)) as layout:
            with self.assertRaisesRegex(FileNotFoundError, "bundled Restic executable") as restic_error:
                resolve_restic_executable()
            with self.assertRaisesRegex(FileNotFoundError, "bundled rclone executable") as rclone_error:
                resolve_rclone_executable()
        for error, name in ((restic_error, RESTIC), (rclone_error, RCLONE)):
            self.assertIn(str(layout.install_dir.resolve() / name), str(error.exception))
            self.assertIn(str(layout.bundle_dir / name), str(error.exception))

    def test_frozen_worker_without_bundle_searches_only_the_installed_directory(self):
        with tempfile.TemporaryDirectory() as directory, FrozenLayout(Path(directory), bundle=False) as layout:
            self.assertEqual(frozen_tool_directories(), [layout.install_dir.resolve()])
            installed = _write_tool(layout.install_dir, RCLONE)
            self.assertEqual(resolve_rclone_executable(), str(installed.resolve()))
            with self.assertRaisesRegex(FileNotFoundError, "bundled Restic executable"):
                resolve_restic_executable()

    @unittest.skipIf(sys.platform == "win32", "execute bits are a POSIX concept")
    def test_non_executable_installed_tool_is_rejected_instead_of_silently_skipped(self):
        with tempfile.TemporaryDirectory() as directory, FrozenLayout(Path(directory)) as layout:
            _write_tool(layout.install_dir, RCLONE).chmod(0o644)
            _write_tool(layout.bundle_dir, RCLONE)
            with self.assertRaisesRegex(PermissionError, "rclone executable is not executable"):
                resolve_rclone_executable()

    def test_restic_environment_prepends_the_directory_of_the_chosen_rclone(self):
        with tempfile.TemporaryDirectory() as directory, FrozenLayout(Path(directory)) as layout:
            _write_tool(layout.install_dir, RESTIC)
            _write_tool(layout.bundle_dir, RESTIC)
            _write_tool(layout.bundle_dir, RCLONE)
            bundled_env = restic_subprocess_environment({"PATH": "host-tools"})

            _write_tool(layout.install_dir, RCLONE)
            installed_env = restic_subprocess_environment({"PATH": "host-tools"})

        self.assertEqual(bundled_env["PATH"], os.pathsep.join((str(layout.bundle_dir), "host-tools")))
        self.assertEqual(installed_env["PATH"], os.pathsep.join((str(layout.install_dir.resolve()), "host-tools")))

    def test_frozen_rclone_repository_uses_absolute_rclone_program_option(self):
        from src import restic_executable

        with tempfile.TemporaryDirectory() as directory, FrozenLayout(Path(directory)) as layout:
            _write_tool(layout.install_dir, RESTIC)
            rclone = _write_tool(layout.install_dir, RCLONE)
            command = [RESTIC, "init"]

            prepared = restic_executable.restic_command_with_backend_options(
                command, {"RESTIC_REPOSITORY": "rclone:remote:/backup"}
            )

        self.assertEqual(prepared, [RESTIC, "-o", f"rclone.program={rclone.resolve()}", "init"])
        self.assertEqual(command, [RESTIC, "init"])

    def test_restic_command_without_frozen_rclone_repository_is_unchanged(self):
        from src import restic_executable

        command = [RESTIC, "snapshots", "--json"]
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(
                restic_executable.restic_command_with_backend_options(
                    command, {"RESTIC_REPOSITORY": "rclone:remote:/backup"}
                ),
                command,
            )

        with tempfile.TemporaryDirectory() as directory, FrozenLayout(Path(directory)):
            self.assertEqual(
                restic_executable.restic_command_with_backend_options(
                    command, {"RESTIC_REPOSITORY": "s3:https://storage.example/backup"}
                ),
                command,
            )

    def test_restic_environment_fails_closed_when_rclone_is_missing_everywhere(self):
        with tempfile.TemporaryDirectory() as directory, FrozenLayout(Path(directory)) as layout:
            _write_tool(layout.install_dir, RESTIC)
            with self.assertRaisesRegex(FileNotFoundError, "bundled rclone executable"):
                restic_subprocess_environment({"PATH": "host-tools"})


if __name__ == "__main__":
    unittest.main()
