"""Standalone tests for the execution-log writer component (S5, S6).

Isolation slice: imports only
``src.worker_agent.infrastructure.security.execution_log_writer`` (and the
committed redaction module it depends on); never the capture module, so the
secure bounded log-file writer can be reviewed and committed independently
of the capture foundation. Covers exactly the writer rules of
odd/tasks/persistent-worker-execution-logs.md: exclusive owner-only creation,
Windows ACL-before-secret-data hardening, redaction before anything reaches
the host file (secrets and repository URLs, also split across chunks), the
bounded size with recorded truncation, periodic fsync and the crash
behaviors (unavailable location fails closed, unusable hardened file leaves
nothing behind).

Temporary state lives in a fresh system temp directory that every test
deletes in teardown.
"""

import contextlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.worker_agent.infrastructure.security.execution_log_redaction import (
    StreamRedactor,
)
from src.worker_agent.infrastructure.security.execution_log_writer import (
    MAX_LOG_BYTES,
    ExecutionLogCaptureUnavailable,
    ExecutionLogWriter,
    restrict_windows_acl,
)

_POSIX = os.name != "nt"
_WRITER_MODULE = "src.worker_agent.infrastructure.security.execution_log_writer"


def _payload(content: str) -> str:
    """Log content without the timestamped event marker lines."""
    return "".join(line + "\n" for line in content.splitlines() if not line.startswith("[2"))


class _TemporaryLogCase(unittest.TestCase):
    def setUp(self):
        self._temporary = Path(tempfile.mkdtemp(prefix="test-execution-log-writer-"))

    def tearDown(self):
        shutil.rmtree(self._temporary, ignore_errors=True)

    @contextlib.contextmanager
    def _writing(self, path, **writer_options):
        """Open a writer and close it on the way out, whether or not the
        body raises (the same close contract as a manual try/finally)."""
        writer = ExecutionLogWriter(path, **writer_options)
        try:
            yield writer
        finally:
            writer.close()

    def _text(self, path) -> str:
        return path.read_text(encoding="utf-8")


class ExecutionLogWriterCreationTests(_TemporaryLogCase):
    """Exclusive owner-only creation and its fail-closed failure modes."""

    @unittest.skipUnless(_POSIX, "file mode bits are POSIX specific")
    def test_log_file_and_directories_are_owner_only(self):
        log_path = self._temporary / "deep" / "job.log"
        with self._writing(log_path, secrets={"s3cret"}) as writer:
            writer.write_chunk(b"still alive\n")
        self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log_path.parent.stat().st_mode & 0o777, 0o700)

    def test_log_records_the_started_and_completed_events(self):
        log_path = self._temporary / "events.log"
        with self._writing(log_path) as writer:
            writer.write_chunk(b"payload\n")
        content = self._text(log_path)
        self.assertIn("[started]", content)
        self.assertIn("[completed]", content)
        self.assertEqual("payload\n", _payload(content))

    def test_collision_raises_file_exists_and_leaves_preexisting_untouched(self):
        log_path = self._temporary / "collision.log"
        log_path.write_bytes(b"pre-existing content")
        with self.assertRaises(FileExistsError):
            ExecutionLogWriter(log_path)
        self.assertEqual(b"pre-existing content", log_path.read_bytes())

    def test_unavailable_log_directory_fails_closed_without_leaving_a_file(self):
        blocker = self._temporary / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(ExecutionLogCaptureUnavailable):
            ExecutionLogWriter(blocker / "job.log")
        self.assertTrue(blocker.is_file())
        self.assertFalse((blocker / "job.log").exists())

    def test_writer_options_are_validated_before_any_file_is_created(self):
        invalid = {
            "maximum_bytes": (
                True,
                False,
                1.5,
                0,
                -64,
                MAX_LOG_BYTES + 1,
                ExecutionLogWriter.MINIMUM_BYTES - 1,
            ),
            "fsync_interval_bytes": (True, False, -1),
        }
        for option, values in invalid.items():
            log_path = self._temporary / f"never-{option}.log"
            for value in values:
                with self.subTest(option=option, value=value):
                    with self.assertRaises(ValueError):
                        ExecutionLogWriter(log_path, **{option: value})
            self.assertFalse(log_path.exists())


class ExecutionLogWriterPersistenceTests(_TemporaryLogCase):
    """Raw unredacted runtime output never reaches the host file (S5)."""

    def test_writer_redacts_secrets_before_anything_reaches_the_host_file(self):
        log_path = self._temporary / "secret.log"
        with self._writing(log_path, secrets={"s3cret", "hunter2"}) as writer:
            writer.write_chunk(b"step 1 token=s3cret ok\n")
            writer.write_event("progress", "detail hunter2 done")
        content = self._text(log_path)
        self.assertNotIn("s3cret", content)
        self.assertNotIn("hunter2", content)
        self.assertIn("step 1 token=<redacted> ok\n", content)
        self.assertIn("detail <redacted> done", content)
        self.assertIn("[completed]", content)

    def test_writer_redacts_repository_urls_without_an_explicit_secret(self):
        log_path = self._temporary / "repo.log"
        with self._writing(log_path) as writer:
            writer.write_chunk(b"restic at https://user:pw@example.com/repo\n")
        content = self._text(log_path)
        self.assertNotIn("user:pw@example.com", content)
        self.assertIn("<redacted-repository>", content)

    def test_writer_redacts_utf8_secrets_split_across_byte_boundaries(self):
        raw = "s3cret-Ñoñ".encode("utf-8")
        log_path = self._temporary / "utf8-split.log"
        with self._writing(log_path, secrets={"s3cret-Ñoñ"}) as writer:
            writer.write_chunk(b"token=" + raw[:-1])
            writer.write_chunk(raw[-1:] + b" end\n")
        content = self._text(log_path)
        self.assertNotIn("s3cret", content)
        self.assertNotIn("\ufffd", content)
        self.assertIn("token=<redacted> end\n", content)

    def test_regression_chunks_split_at_every_point_stay_redacted(self):
        cases = (
            ("url", {}, b"backup -r https://user:pw@example.com/repo done\n",
             "user:pw@example.com", "backup -r <redacted-repository> done\n"),
            ("secret", {"secrets": {"S3cretPASS"}}, b"launch token=S3cretPASS ok\n",
             "S3cretPASS", None),
        )
        for kind, options, text, forbidden, expected_payload in cases:
            for split in range(1, len(text)):
                with self.subTest(kind=kind, split=split):
                    log_path = self._temporary / f"{kind}-split-{split}.log"
                    with self._writing(log_path, **options) as writer:
                        writer.write_chunk(text[:split])
                        writer.write_chunk(text[split:])
                    content = self._text(log_path)
                    self.assertNotIn(forbidden, content, split)
                    if expected_payload is not None:
                        self.assertEqual(expected_payload, _payload(content), split)

    def test_writer_event_lines_are_redacted_too(self):
        log_path = self._temporary / "event-redaction.log"
        with self._writing(log_path, secrets={"s3cret"}) as writer:
            writer.write_event("progress", "url https://user:pw@example.com/repo in detail")
        content = self._text(log_path)
        self.assertNotIn("user:pw@example.com", content)
        self.assertIn("<redacted-repository>", content)

    def test_regression_event_detail_is_redacted_before_the_detail_length_cut(self):
        """Exact verifier scenario: a detail longer than 512 characters whose
        secret crosses the recordable-detail cut must never leak the secret
        or a partial secret; the marker may be cut, the secret may not."""
        log_path = self._temporary / "detail-cut.log"
        with self._writing(log_path, secrets={"topsecret"}) as writer:
            writer.write_event("progress", "a" * 505 + "topsecret" + " tail")
        content = self._text(log_path)
        self.assertNotIn("topsecret", content)
        # The old code sliced the detail to 512 characters before redaction,
        # persisting the secret's first characters ("tops").
        self.assertNotIn("tops", content)
        self.assertIn("redact", content)

    def test_regression_event_detail_cut_keeps_encoding_whole_after_redaction(self):
        log_path = self._temporary / "detail-utf8-cut.log"
        with self._writing(log_path, secrets={"s3cret-Ñoñ"}) as writer:
            writer.write_event("progress", "b" * 505 + "s3cret-Ñoñ")
        # Reading as UTF-8 doubles as the encoding check: no replacement
        # character may appear and no partial multi-byte secret may survive.
        content = self._text(log_path)
        self.assertNotIn("s3cret", content)
        self.assertNotIn("\ufffd", content)
        self.assertIn("redact", content)

    def test_event_url_wholly_within_the_detail_is_redacted_from_line_start(self):
        log_path = self._temporary / "detail-url.log"
        # Space-adjacent scheme: the committed redaction pattern requires a
        # word boundary before it (word-adjacent URLs are a redaction-module
        # finding, out of these edit surfaces).
        with self._writing(log_path) as writer:
            writer.write_event("progress", "z " * 245 + "https://user:pw@example.com/repo more")
        content = self._text(log_path)
        self.assertNotIn("user:pw@example.com", content)
        self.assertIn("<redacted-repository>", content)

    def test_disallowed_event_kinds_fall_back_to_the_plain_event_marker(self):
        log_path = self._temporary / "event-kind.log"
        with self._writing(log_path) as writer:
            writer.write_event("Not A Kind!", "still recorded")
        content = self._text(log_path)
        self.assertIn("[event] still recorded", content)
        self.assertNotIn("Not A Kind!", content)


class ExecutionLogWriterBoundedTests(_TemporaryLogCase):
    """The file never exceeds maximum_bytes and records the truncation (S5)."""

    def test_writer_stays_bounded_and_records_truncation(self):
        log_path = self._temporary / "bounded.log"
        with self._writing(log_path, maximum_bytes=1500) as writer:
            writer.write_chunk(b"x" * 5000)
            writer.write_chunk(b"this must not be stored\n")
        self.assertLessEqual(log_path.stat().st_size, 1500)
        self.assertTrue(writer.truncated)
        content = self._text(log_path)
        self.assertIn("[truncated]", content)
        self.assertNotIn("this must not be stored", content)

    def test_truncated_writer_suppresses_every_later_write(self):
        log_path = self._temporary / "post-truncation.log"
        with self._writing(log_path, maximum_bytes=1500) as writer:
            writer.write_chunk(b"y" * 5000)
            writer.write_chunk(b"suppressed after truncation\n")
            writer.write_event("progress", "also suppressed")
        content = self._text(log_path)
        self.assertNotIn("suppressed after truncation", content)
        # Event lines may still use the headroom after truncation (the
        # truncation marker itself must be recordable); only chunk payload is
        # suppressed.
        self.assertIn("[progress] also suppressed", content)

    def test_writer_reports_interrupted_and_skips_writes_after_close(self):
        log_path = self._temporary / "interrupted.log"
        writer = ExecutionLogWriter(log_path)
        writer.write_chunk(b"partial evidence\n")
        writer.mark_interrupted("worker died")
        writer.close()
        writer.write_chunk(b"after close\n")
        writer.close()
        content = self._text(log_path)
        self.assertIn("partial evidence\n", content)
        self.assertIn("[interrupted] worker died", content)
        self.assertNotIn("after close", content)

    def test_max_pending_chars_alias_pins_the_redactor_memory_bound(self):
        self.assertEqual(ExecutionLogWriter.MAX_PENDING_CHARS, StreamRedactor.MAX_PENDING_CHARS)
        self.assertGreater(ExecutionLogWriter.MAX_PENDING_CHARS, 0)


class ExecutionLogWriterFsyncTests(_TemporaryLogCase):
    """Persistence is fsynced periodically and again before the final close."""

    def _counted_fsync(self):
        real = os.fsync

        def counted(descriptor):
            self._fsync_calls += 1
            return real(descriptor)

        return counted

    def setUp(self):
        super().setUp()
        self._fsync_calls = 0

    def test_periodic_fsync_runs_per_interval_and_at_close(self):
        def run(interval, chunks):
            self._fsync_calls = 0
            log_path = self._temporary / f"fsync-{interval}.log"
            with mock.patch(f"{_WRITER_MODULE}.os.fsync", self._counted_fsync()):
                with self._writing(
                    log_path, fsync_interval_bytes=interval, maximum_bytes=4096
                ) as writer:
                    for chunk in chunks:
                        writer.write_chunk(chunk)
        with self.subTest(interval=1):
            run(1, [b"first\n", b"second\n"])
            self.assertGreaterEqual(self._fsync_calls, 3)
        with self.subTest(interval=0):
            run(0, [b"only one chunk\n"])
            # Disabled periodic fsync still fsyncs exactly once, at close.
            self.assertEqual(1, self._fsync_calls)

    def test_fsync_failure_at_close_still_closes_the_log(self):
        def failing_fsync(descriptor):
            raise OSError("simulated fsync failure")
        with mock.patch(f"{_WRITER_MODULE}.os.fsync", failing_fsync):
            with self._writing(self._temporary / "fsync-failure.log", fsync_interval_bytes=1):
                pass
        # The descriptor was released and the evidence stayed on disk.
        self.assertIn(
            "[completed]",
            self._text(self._temporary / "fsync-failure.log"),
        )


@unittest.skipUnless(os.name == "nt", "Windows ACL hardening")
class ExecutionLogWriterWindowsAclTests(_TemporaryLogCase):
    """S6: the log file's DACL is hardened before any content is written."""

    def test_log_file_acl_is_secured_before_content_is_written(self):
        log_path = self._temporary / "acl-file.log"
        with self._writing(log_path, secrets={"s3cret"}) as writer:
            writer.write_chunk(b"token=s3cret\n")
        listing = subprocess.run(
            ["icacls", str(log_path)], check=True, capture_output=True, text=True
        ).stdout
        self.assertNotIn("(I)", listing)
        expected_account = subprocess.run(
            ["whoami"], check=True, capture_output=True, text=True
        ).stdout.strip().casefold()
        self.assertIn(expected_account, listing.casefold())
        self.assertIn("SYSTEM", listing)

    def test_acl_hardening_precedes_any_secret_byte(self):
        log_path = self._temporary / "acl-order.log"
        observations = []

        def observed_acl(path):
            size = os.stat(path).st_size
            try:
                content = Path(path).read_bytes()
            except OSError:
                content = b"<unreadable>"
            observations.append((size, content))
            restrict_windows_acl(path)

        with mock.patch(f"{_WRITER_MODULE}.restrict_windows_acl", observed_acl):
            with self._writing(log_path, secrets={"s3cret"}) as writer:
                writer.write_chunk(b"token=s3cret\n")
        self.assertEqual([(0, b"")], observations)
        self.assertNotIn("s3cret", self._text(log_path))

    def test_acl_hardening_failure_discards_the_created_log_file(self):
        log_path = self._temporary / "acl-failure.log"

        def failing_acl(path):
            raise ExecutionLogCaptureUnavailable(f"simulated hardening failure: {path}")

        with mock.patch(f"{_WRITER_MODULE}.restrict_windows_acl", failing_acl):
            with self.assertRaises(ExecutionLogCaptureUnavailable):
                ExecutionLogWriter(log_path, secrets={"s3cret"})
        self.assertFalse(log_path.exists())


if __name__ == "__main__":
    unittest.main()