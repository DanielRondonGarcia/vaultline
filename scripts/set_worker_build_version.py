"""Stamp a validated release version into the frozen worker build."""

import argparse
import re
from pathlib import Path


VERSION_PATTERN = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "src" / "worker_agent" / "_build_info.py"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not VERSION_PATTERN.fullmatch(args.version):
        parser.error("version must be a plain semantic version (for example, 3.8.0)")
    args.output.write_text(f'VERSION = "{args.version}"\n', encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
