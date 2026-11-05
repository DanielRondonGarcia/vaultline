"""Run unittest in a hidden console on Windows, preserving terminal output.

Usage: python scripts/run_tests_silent.py [unittest arguments]
Without arguments, discover tests under tests/.
"""

import os
import subprocess
import sys


def _windows_startup():
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return startup, subprocess.CREATE_NEW_CONSOLE


def main(args=None, *, windows=None):
    args = list(sys.argv[1:] if args is None else args)
    command = [sys.executable, "-m", "unittest", *(args or ["discover", "tests"])]
    options = {}
    if windows if windows is not None else os.name == "nt":
        startup, flags = _windows_startup()
        options = {"startupinfo": startup, "creationflags": flags, "stdout": sys.stdout, "stderr": sys.stderr}
    return subprocess.run(command, **options).returncode


if __name__ == "__main__":
    sys.exit(main())
