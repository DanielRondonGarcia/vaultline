"""Behavior tests for the one-shot redaction-recipe component.

T1 S6 (``odd/tasks/persistent-worker-execution-logs.md``): the recipe file is
the only channel that lets the detached capture child redact without the
worker being alive, so these tests pin the security invariants: owner-only
creation before any secret byte (Windows ACL on the EMPTY file, POSIX
``O_EXCL`` ``0o600``), fail-closed deletion on any write failure, untouched
colliding destinations, bounded recipes, and read-then-delete loading whose
corrupt, missing or oversized input fails closed.
"""

import errno
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.worker_agent.infrastructure.security.execution_log_recipe import (
    MAX_RECIPE_BYTES,
    MAX_RECIPE_ITEMS,
    ExecutionLogCaptureUnavailable,
    load_redaction_recipe,
    write_redaction_recipe,
)

_POSIX = os.name != "nt"


class ExecutionLogRecipeTests(unittest.TestCase):
    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-recipe-"))

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)

    def test_write_and_load_round_trip_deduplicates_and_deletes_the_recipe(self):
        recipe = self._temporary / "nested" / "recipe.json"
        returned = write_redaction_recipe(recipe, ["s3cret", "", 5, "hunter2", "s3cret"])
        self.assertEqual(str(recipe), returned)
        self.assertIn(b'"s3cret"', recipe.read_bytes())
        if _POSIX:
            self.assertEqual(0o600, recipe.stat().st_mode & 0o777)
            self.assertEqual(0o700, recipe.parent.stat().st_mode & 0o777)
        self.assertEqual(["s3cret", "hunter2"], load_redaction_recipe(recipe))
        self.assertFalse(recipe.exists())

    def _write(self, values, name="recipe.json"):
        recipe = self._temporary / name
        write_redaction_recipe(recipe, values)
        return recipe

    @unittest.skipUnless(_POSIX and hasattr(os, "mkfifo"), "POSIX FIFO required")
    def test_load_rejects_fifo_without_waiting_for_a_writer(self):
        recipe = self._temporary / "recipe-fifo"
        try:
            os.mkfifo(recipe)
        except OSError as exc:
            if exc.errno in (errno.EOPNOTSUPP, errno.ENOSYS):
                self.skipTest("filesystem does not support FIFOs")
            raise
        script = (
            "import sys\n"
            "from src.worker_agent.infrastructure.security.execution_log_recipe "
            "import load_redaction_recipe, ExecutionLogCaptureUnavailable\n"
            "try:\n"
            "    load_redaction_recipe(sys.argv[1])\n"
            "except ExecutionLogCaptureUnavailable:\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        try:
            result = subprocess.run(
                [os.sys.executable, "-c", script, str(recipe)],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except subprocess.TimeoutExpired:
            self.fail("recipe reader blocked while opening a FIFO without a writer")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)
        self.assertFalse(recipe.exists())

    @unittest.skipUnless(_POSIX and hasattr(os, "O_NONBLOCK"), "POSIX nonblocking open required")
    def test_fifo_descriptor_is_rejected_before_reading(self):
        recipe = self._temporary / "recipe-fifo"
        with mock.patch("os.open", return_value=123) as opened, \
             mock.patch("os.fstat", return_value=mock.Mock(st_mode=stat.S_IFIFO)), \
             mock.patch("os.read", side_effect=[b"[]", b""]) as read, \
             mock.patch("os.close") as close, \
             mock.patch("os.unlink"):
            with self.assertRaises(ExecutionLogCaptureUnavailable):
                load_redaction_recipe(recipe)
        self.assertTrue(opened.call_args.args[1] & os.O_NONBLOCK)
        read.assert_not_called()
        close.assert_called_once_with(123)

    def test_empty_recipe_is_valid(self):
        recipe = self._write([])
        self.assertEqual(b"[]", recipe.read_bytes())
        self.assertEqual([], load_redaction_recipe(recipe))
        self.assertFalse(recipe.exists())

    def test_load_of_a_missing_recipe_fails_closed(self):
        with self.assertRaises(ExecutionLogCaptureUnavailable):
            load_redaction_recipe(self._temporary / "absent.json")

    def test_load_of_a_corrupt_recipe_fails_closed_and_deletes_it(self):
        recipe = self._temporary / "corrupt.json"
        recipe.write_text("not json", encoding="utf-8")
        with self.assertRaises(ExecutionLogCaptureUnavailable):
            load_redaction_recipe(recipe)
        self.assertFalse(recipe.exists())

    def test_load_rejects_non_list_and_over_long_recipes_and_deletes_them(self):
        for position, payload in enumerate(['{"secrets": ["a"]}', json.dumps(["x"] * (MAX_RECIPE_ITEMS + 1))]):
            with self.subTest(payload=position):
                recipe = self._temporary / f"invalid-{position}.json"
                recipe.write_text(payload, encoding="utf-8")
                with self.assertRaises(ExecutionLogCaptureUnavailable):
                    load_redaction_recipe(recipe)
                self.assertFalse(recipe.exists())
        # The deletion above means the same destination is writable again.
        recipe = self._write(["public"], name="invalid-0.json")
        self.assertEqual(b'["public"]', recipe.read_bytes())
        # An exactly-full recipe stays valid.
        recipe = self._write([f"s{index}" for index in range(MAX_RECIPE_ITEMS)])
        self.assertEqual(MAX_RECIPE_ITEMS, len(load_redaction_recipe(recipe)))

    def test_load_caps_the_recipe_at_the_bound_so_oversized_input_fails_closed(self):
        recipe = self._temporary / "oversized.json"
        recipe.write_text("[" + "p" * (MAX_RECIPE_BYTES + 1) + "]", encoding="utf-8")
        with self.assertRaises(ExecutionLogCaptureUnavailable):
            load_redaction_recipe(recipe)
        self.assertFalse(recipe.exists())

    def test_load_rejects_a_file_over_the_bound_despite_a_valid_json_prefix(self):
        recipe = self._temporary / "oversized-prefix.json"
        payload = json.dumps(["s3cret"]) + " " * MAX_RECIPE_BYTES
        self.assertGreater(len(payload), MAX_RECIPE_BYTES)
        recipe.write_text(payload, encoding="utf-8")
        self.assertEqual(MAX_RECIPE_BYTES + len(json.dumps(["s3cret"])), recipe.stat().st_size)
        with self.assertRaises(ExecutionLogCaptureUnavailable):
            load_redaction_recipe(recipe)
        self.assertFalse(recipe.exists())

    def test_loaded_read_stays_within_the_bounded_byte_window(self):
        recipe = self._write(["s3cret"])
        observations = []
        real_read = os.read

        def recorded_read(descriptor, size):
            observations.append(size)
            return real_read(descriptor, size)

        with mock.patch("os.read", side_effect=recorded_read):
            self.assertEqual(["s3cret"], load_redaction_recipe(recipe))
        self.assertTrue(observations)
        for size in observations:
            with self.subTest(requested=size):
                self.assertLessEqual(size, MAX_RECIPE_BYTES + 1)

    def test_load_accepts_and_reads_a_recipe_at_the_exact_size_bound(self):
        recipe = self._temporary / "exact-bound.json"
        write_redaction_recipe(recipe, ["x" * (MAX_RECIPE_BYTES - 4)])
        self.assertEqual(MAX_RECIPE_BYTES, recipe.stat().st_size)
        self.assertEqual(["x" * (MAX_RECIPE_BYTES - 4)], load_redaction_recipe(recipe))
        self.assertFalse(recipe.exists())

    def test_write_bounds_reject_overlong_recipes_without_touching_disk(self):
        with self.assertRaises(ValueError):
            write_redaction_recipe(
                self._temporary / "too-many.json", [f"s{index}" for index in range(MAX_RECIPE_ITEMS + 1)]
            )
        with self.assertRaises(ValueError):
            write_redaction_recipe(self._temporary / "too-big.json", ["x" * (MAX_RECIPE_BYTES + 1)])
        # Neither rejection may leave a file, and the boundaries are permitted.
        self.assertFalse((self._temporary / "too-many.json").exists())
        self.assertFalse((self._temporary / "too-big.json").exists())
        self.assertTrue(write_redaction_recipe(self._temporary / "edge.json", []).endswith("edge.json"))
        recipe = self._temporary / "full.json"
        write_redaction_recipe(recipe, [f"s{index}" for index in range(MAX_RECIPE_ITEMS)])
        self.assertEqual(MAX_RECIPE_ITEMS, len(load_redaction_recipe(recipe)))

    def test_write_rejects_a_colliding_destination_untouched(self):
        recipe = self._temporary / "collision.json"
        recipe.write_bytes(b"pre-existing content")
        with self.assertRaises(FileExistsError):
            write_redaction_recipe(recipe, ["s3cret"])
        self.assertEqual(b"pre-existing content", recipe.read_bytes())

    def test_write_failure_on_an_unusable_directory_fails_closed(self):
        blocker = self._temporary / "blocker.json"
        blocker.write_bytes(b"")
        with self.assertRaises(ExecutionLogCaptureUnavailable):
            write_redaction_recipe(blocker / "recipe.json", ["s3cret"])

    @unittest.skipUnless(os.name == "nt", "Windows ACL hardening")
    def test_acl_hook_is_applied_to_the_empty_file_before_any_secret_byte(self):
        recipe = self._temporary / "recipe-order.json"
        observations = []

        def observed_acl(path):
            observations.append((path.stat().st_size, path.read_bytes()))
            raise ExecutionLogCaptureUnavailable("hook failure for the fail-closed probe")

        with self.assertRaises(ExecutionLogCaptureUnavailable):
            write_redaction_recipe(recipe, ["s3cret"], restrict_acl=observed_acl)
        self.assertEqual([(0, b"")], observations)
        self.assertFalse(recipe.exists())  # hook failure deletes the empty file
        # ... and a succeeding hook writes the recipe normally.
        write_redaction_recipe(recipe, ["s3cret"], restrict_acl=lambda path: observations.append(1))
        self.assertEqual(b'["s3cret"]', recipe.read_bytes())
        self.assertEqual(2, len(observations))

    @unittest.skipUnless(_POSIX, "umask behavior is POSIX specific")
    def test_recipe_created_owner_only_even_with_a_permissive_umask(self):
        recipe = self._temporary / "recipe-umask.json"
        previous = os.umask(0)
        try:
            write_redaction_recipe(recipe, ["s3cret"])
        finally:
            os.umask(previous)
        self.assertEqual(0o600, recipe.stat().st_mode & 0o777)

    @unittest.skipUnless(os.name == "nt", "Windows ACL hardening")
    def test_windows_recipe_acl_is_explicit_and_not_inherited(self):
        recipe = self._write(["s3cret"], name="recipe-acl.json")
        listing = subprocess.run(
            ["icacls", str(recipe)], check=True, capture_output=True, text=True
        ).stdout
        self.assertNotIn("(I)", listing)


if __name__ == "__main__":
    unittest.main()
