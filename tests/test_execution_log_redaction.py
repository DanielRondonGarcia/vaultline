"""Standalone tests for the execution-log redaction component (S6).

Isolation slice: imports only
``src.worker_agent.infrastructure.security.execution_log_redaction`` (never
the capture module), so the security-sensitive redactor can be reviewed and
committed independently. Covers exactly the split-content rules of
odd/tasks/persistent-worker-execution-logs.md: secrets and repository URLs
split across chunk boundaries stay redacted, overflow at the bounded-memory
limit fails closed, and ordinary text is preserved verbatim.

Runs in memory only; no temporary state or files on disk. The ``write()``
composition mirrors ``ExecutionLogWriter`` (``feed`` holds boundary tails,
then one-shot ``redact_text`` applies to every emitted chunk and ``flush``
releases the final tail), which is the observable product pipeline.
"""

import unittest

from src.worker_agent.infrastructure.security.execution_log_redaction import (
    StreamRedactor,
)

_REDACTED = "<redacted>"
_REDACTED_REPOSITORY = "<redacted-repository>"


class RedactingPipeline:
    """Observable composition of the redactor as used by ``ExecutionLogWriter``."""

    def __init__(self, secrets):
        self._redactor = StreamRedactor(secrets)
        self._parts = []
        self.overflow_events = 0

    def write(self, raw):
        emitted, overflowed = self._redactor.feed(raw)
        self._parts.append(
            StreamRedactor.redact_text(emitted, self._redactor.secrets)
        )
        if overflowed:
            self.overflow_events += 1
        return emitted, overflowed

    @property
    def output(self):
        released = self._redactor.flush()
        return "".join(self._parts) + StreamRedactor.redact_text(
            released, self._redactor.secrets
        )


class StreamRedactorSplitContentTests(unittest.TestCase):
    """Split secrets and split repository URLs never persist unredacted."""

    def test_secret_split_at_every_chunk_boundary_stays_redacted(self):
        line = "token=s3cret ok\n"
        for index in range(len(line) + 1):
            with self.subTest(split=index):
                pipeline = RedactingPipeline(["s3cret"])
                pipeline.write(line[:index].encode("utf-8"))
                pipeline.write(line[index:].encode("utf-8"))
                self.assertEqual(pipeline.output, "token=<redacted> ok\n")

    def test_multibyte_secret_split_at_every_byte_boundary_stays_redacted(self):
        line = "ok sécret end\n"
        raw = line.encode("utf-8")
        for index in range(len(raw) + 1):
            with self.subTest(split=index):
                pipeline = RedactingPipeline(["sécret"])
                pipeline.write(raw[:index])  # incremental utf-8 decode
                pipeline.write(raw[index:])
                self.assertEqual(pipeline.output, "ok <redacted> end\n")

    def test_repository_url_split_at_every_chunk_boundary_stays_redacted(self):
        cases = (
            (
                "backup -r https://backup.example.com/repo#key=x done\n",
                "backup -r <redacted-repository> done\n",
            ),
            (
                "job -r s3://backup-bucket/config end\n",
                "job -r <redacted-repository> end\n",
            ),
            (
                "rclone:remote:bucket/path done\n",
                "<redacted-repository> done\n",
            ),
        )
        for line, expected in cases:
            for index in range(len(line) + 1):
                with self.subTest(line=line, split=index):
                    pipeline = RedactingPipeline([])
                    pipeline.write(line[:index])
                    pipeline.write(line[index:])
                    self.assertEqual(pipeline.output, expected)


class StreamRedactorOrdinaryTextTests(unittest.TestCase):
    """Redaction changes nothing that is neither a secret nor a repository URL."""

    def test_ordinary_text_survives_every_chunk_boundary_untouched(self):
        text = "plain output starts\nwith detail lines\nand no tokens\n"
        for split in range(len(text) + 1):
            for secrets in ([], ["s3cret"]):
                with self.subTest(split=split, secrets=secrets):
                    pipeline = RedactingPipeline(secrets)
                    pipeline.write(text[:split])
                    pipeline.write(text[split:])
                    self.assertEqual(pipeline.output, text)

    def test_whole_lines_redact_secrets_and_urls_in_one_complete_output(self):
        line = "job -r https://x.example.com/repo secret=s3cret done\n"
        pipeline = RedactingPipeline(["s3cret"])
        pipeline.write(line)
        self.assertEqual(
            pipeline.output,
            "job -r <redacted-repository> secret=<redacted> done\n",
        )


class StreamRedactorOverflowFailClosedTests(unittest.TestCase):
    """The bounded-memory limit drops unterminated tokens, never persists them."""

    def test_overflow_at_the_exact_limit_fails_closed(self):
        limit = StreamRedactor.MAX_PENDING_CHARS
        self.assertEqual(limit, 1 << 16)
        pipeline = RedactingPipeline([])
        # Exactly the limit: an unterminated URL head is held, not dropped.
        self.assertEqual(
            pipeline.write(b"https://" + b"t" * (limit - 8)), ("", False)
        )
        # One char over the limit: fail closed, drop the whole token.
        self.assertEqual(pipeline.write(b"T"), ("", True))
        # Suppression holds later chunks until the token's terminator arrives.
        self.assertEqual(
            pipeline.write(b"credential@example.com/x"), ("", False)
        )
        self.assertEqual(
            pipeline.write(b" after-term\n"), (" after-term\n", False)
        )
        self.assertEqual(
            pipeline.write(b"captured after overflow\n"),
            ("captured after overflow\n", False),
        )
        output = pipeline.output
        self.assertNotIn("credential@example.com", output)
        self.assertNotIn("t" * (limit - 8), output)
        self.assertEqual(output, " after-term\ncaptured after overflow\n")

    def test_overflow_events_stay_capped_at_the_limit(self):
        limit = StreamRedactor.MAX_PENDING_CHARS
        cap = StreamRedactor.MAX_PENDING_OVERFLOW_EVENTS
        self.assertEqual(cap, 16)
        pipeline = RedactingPipeline([])
        results = []
        for _ in range(cap + 4):
            results.append(
                pipeline.write(b"https://" + b"t" * limit)[1]
            )  # token = limit + 8 chars
            self.assertEqual(pipeline.write(b"\n")[1], False)  # resume capture
        self.assertEqual(results, [True] * cap + [False] * 4)
        self.assertEqual(pipeline.overflow_events, cap)


if __name__ == "__main__":
    unittest.main()