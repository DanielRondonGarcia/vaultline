import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.worker_agent.infrastructure.filesystem_size_estimator import (
    estimate_filesystem_size,
    estimate_filesystem_size_bounded,
)


class FilesystemSizeEstimatorTests(unittest.TestCase):
    def test_counts_logical_sizes_without_reading_contents_and_deduplicates_overlapping_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "folder with spaces"
            root.mkdir()
            (root / "data.bin").write_bytes(b"contents")
            result = estimate_filesystem_size([str(root), str(root / "data.bin")])
            self.assertEqual((result["logical_bytes"], result["file_count"], result["directory_count"]), (8, 1, 1))
            self.assertEqual(result["roots"][1]["file_count"], 0)

    def test_counts_multiple_roots_and_skips_symlink_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one").write_bytes(b"123")
            (root / "two").write_bytes(b"12345")
            try:
                (root / "link").symlink_to(root / "two")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            result = estimate_filesystem_size([str(root / "one"), str(root / "two"), str(root / "link")])
            self.assertEqual(result["logical_bytes"], 8)
            self.assertEqual(result["file_count"], 2)
            self.assertEqual(result["symlinks_skipped"], 1)

    def test_missing_path_and_entry_limit_are_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one").write_bytes(b"x")
            missing = estimate_filesystem_size([str(root / "missing")])
            self.assertFalse(missing["complete"])
            self.assertEqual(missing["roots"][0]["status"], "inaccessible")
            limited = estimate_filesystem_size([str(root)], max_entries=1)
            self.assertFalse(limited["complete"])
            self.assertEqual(limited["reason"], "scan_limit_reached")

    @patch("src.worker_agent.infrastructure.filesystem_size_estimator.subprocess.run")
    def test_bounded_wrapper_passes_paths_over_stdin_and_returns_json_result(self, run):
        expected = {"complete": True, "logical_bytes": 7, "file_count": 1, "directory_count": 0,
                    "symlinks_skipped": 0, "roots": [{"path": "C:\\chosen path", "status": "complete",
                    "logical_bytes": 7, "file_count": 1, "directory_count": 0, "symlinks_skipped": 0}]}
        run.return_value = Mock(returncode=0, stdout=json.dumps(expected), stderr="")

        result = estimate_filesystem_size_bounded(["C:\\chosen path"], timeout_seconds=12)

        self.assertEqual(result, expected)
        args, kwargs = run.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "src.worker_agent.cli", "__filesystem-size-worker"])
        self.assertEqual(kwargs["input"], '{"paths":["C:\\\\chosen path"]}')
        self.assertEqual(kwargs["timeout"], 12)
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertFalse(kwargs["shell"])
        self.assertEqual(Path(kwargs["cwd"]), Path(__file__).resolve().parents[1])
        self.assertNotIn("C:\\chosen path", " ".join(args[0]))

    @patch("src.worker_agent.infrastructure.filesystem_size_estimator.subprocess.run")
    def test_bounded_wrapper_uses_frozen_executable_dispatch(self, run):
        import src.worker_agent.infrastructure.filesystem_size_estimator as estimator

        run.return_value = Mock(returncode=0, stdout='{"complete":true,"logical_bytes":0,"file_count":0,'
                                '"directory_count":0,"symlinks_skipped":0,"roots":[{"path":"C:\\\\x",'
                                '"status":"complete","logical_bytes":0,"file_count":0,'
                                '"directory_count":0,"symlinks_skipped":0}]}', stderr="")
        with patch.object(estimator.sys, "frozen", True, create=True):
            estimate_filesystem_size_bounded(["C:\\x"])
        self.assertEqual(run.call_args.args[0], [sys.executable, "__filesystem-size-worker"])
        self.assertNotIn("cwd", run.call_args.kwargs)

    @patch("src.worker_agent.infrastructure.filesystem_size_estimator.subprocess.run")
    def test_bounded_wrapper_converts_timeout_to_partial_result(self, run):
        import subprocess

        run.side_effect = subprocess.TimeoutExpired(["worker"], 3)

        result = estimate_filesystem_size_bounded(["C:\\slow"], timeout_seconds=3)

        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "scan_timeout")
        self.assertEqual(result["roots"][0]["path"], "C:\\slow")
        self.assertEqual(result["roots"][0]["status"], "not_scanned")

    @patch("src.worker_agent.infrastructure.filesystem_size_estimator.subprocess.run")
    def test_bounded_wrapper_fails_safely_for_nonzero_or_malformed_child(self, run):
        for completed in (Mock(returncode=1, stdout="", stderr="private error"),
                          Mock(returncode=0, stdout="not json", stderr=""),
                          Mock(returncode=0, stdout='{"complete":true}', stderr="")):
            with self.subTest(completed=completed.returncode), patch(
                "src.worker_agent.infrastructure.filesystem_size_estimator.subprocess.run", return_value=completed
            ):
                result = estimate_filesystem_size_bounded(["C:\\safe"])
                self.assertFalse(result["complete"])
                self.assertEqual(result["reason"], "scan_failed")
                self.assertEqual(result["roots"][0]["status"], "not_scanned")


if __name__ == "__main__":
    unittest.main()
