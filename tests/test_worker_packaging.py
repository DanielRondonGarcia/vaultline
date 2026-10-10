import ast
import re
import unittest
from pathlib import Path


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

        self.assertIn("name='vaultline-worker'", spec)
        self.assertIn("console=True", spec)
        self.assertIn("src/worker_agent/cli.py", spec.replace("\\\\", "/"))
        self.assertIn("src.app.main", spec)
        self.assertIn("src.worker_agent.cli", spec)
        self.assertNotIn(".worker_credentials", spec)
        self.assertNotIn("WORKER_SECRET", spec)
        self.assertNotIn("restic", spec.lower())
        self.assertNotIn("tar.exe", spec.lower())

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
