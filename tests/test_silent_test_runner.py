import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts import run_tests_silent


class SilentTestRunnerTests(unittest.TestCase):
    def test_windows_uses_hidden_console_and_preserves_arguments_and_exit_code(self):
        with patch.object(run_tests_silent, "_windows_startup", return_value=("startup", 16)), patch.object(run_tests_silent.subprocess, "run", return_value=subprocess.CompletedProcess([], 7)) as run:
            result = run_tests_silent.main(["tests.test_ui_states", "-q"], windows=True)
        self.assertEqual(result, 7)
        self.assertEqual(run.call_args.args[0][1:], ["-m", "unittest", "tests.test_ui_states", "-q"])
        self.assertEqual(run.call_args.kwargs, {"startupinfo": "startup", "creationflags": 16, "stdout": sys.stdout, "stderr": sys.stderr})

    def test_non_windows_preserves_default_discovery(self):
        with patch.object(run_tests_silent.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(run_tests_silent.main([], windows=False), 0)
        self.assertEqual(run.call_args.args[0][1:], ["-m", "unittest", "discover", "tests"])
        self.assertEqual(run.call_args.kwargs, {})
