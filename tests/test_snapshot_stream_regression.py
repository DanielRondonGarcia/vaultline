"""Reproducible regression tests for the runtime log stream finish/join path.

Production symptom: ``snapshots.list`` returned exit 0 (``status_code == 0``)
with ``success == False`` and the exact error ``failed to parse restic
snapshots JSON``. The snapshot catalog JSON arrived truncated, so the parse of
the runtime's captured ``logs`` failed after a zero exit status.

Proven mechanism (deterministic, isolated fakes): the runtime streams container
logs off-thread and later joins that thread for at most two seconds
(``_finish_log_stream``). When the stream itself does not reach EOF within that
window -- e.g. the daemon's log tail is still in flight after the container has
already exited -- the runtime force-closes it. A Docker-style stream ends its
iteration *silently* when the underlying response is closed, so the consumer
finishes without any error marker and the truncated log content is treated as
the complete output of a successful exit code. The snapshot JSON parser then
fails on a half-delivered JSON line while the exit status stays 0.
"""

import threading
import unittest
from unittest.mock import Mock, patch

from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter

_SNAPSHOT_ID = "abcdef1234567890"


# Truncated snapshots JSON: a valid JSON line that was cut in half. It starts
# with "[" so it is picked up as a JSON candidate by the parser and then fails
# to parse -- exactly the observed production failure.
_PARTIAL_JSON = b'[{"id": "%s", "short_id": "a' % _SNAPSHOT_ID.encode()
_COMPLETE_JSON_LINE = '[{"id": "%s", "short_id": "%s"}]\n' % (_SNAPSHOT_ID, _SNAPSHOT_ID)


class _HangingSnapshotStream:
    """Docker-like log stream that delivers one partial JSON chunk and then
    never reaches EOF on its own. Closing it (as ``_finish_log_stream`` does)
    ends iteration silently with no exception -- the way a closed HTTP
    response behaves for docker-py -- instead of raising."""

    def __init__(self):
        self._first = True
        self._close_requested = threading.Event()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._first:
            self._first = False
            return _PARTIAL_JSON
        # The daemon never reaches EOF for this stream tail; only an explicit
        # close() (the runtime's bounded join -> close) unblocks the reader.
        self._close_requested.wait(timeout=60)
        raise StopIteration

    def close(self):
        self.closed = True
        self._close_requested.set()


class _RaisingSnapshotStream:
    """Docker-like log stream whose transport breaks mid-read; the consumer
    raises, which must route the job to the complete non-streamed logs."""

    def __init__(self, full_logs: bytes):
        self._first = True
        self._full_logs = full_logs

    def __iter__(self):
        return self

    def __next__(self):
        if self._first:
            self._first = False
            return self._full_logs[:8]
        raise RuntimeError("simulated stream transport failure")

    def close(self):
        pass


class SnapshotStreamRegressionTests(unittest.TestCase):
    def setUp(self):
        # The execution-log capture integration is out of scope for these
        # tests; keep it inert exactly like the focused runtime tests do.
        capture = patch.object(DockerRuntimeAdapter, "_start_execution_log_capture", return_value=None)
        capture.start()
        self.addCleanup(capture.stop)

    @staticmethod
    def runtime(container):
        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.client.containers.run.return_value = container
        runtime.timeout_seconds = 30.0
        runtime.no_lock = False
        runtime.cache_dir = None
        return runtime

    @staticmethod
    def stream_container(stream, fallback_logs):
        container = Mock()
        container.wait.return_value = {"StatusCode": 0}
        # First logs() call opens the daemon stream; a later non-streamed
        # logs() call is the complete bounded fallback the runtime re-reads.
        container.logs.side_effect = [stream, fallback_logs]
        return container

    def test_stream_that_never_finishes_before_close_yields_complete_snapshots(self):
        """A stream that must be force-closed when the container has already
        exited must never let truncated output masquerade as a complete exit-0
        result: the parse failure "failed to parse restic snapshots JSON" with
        ``status_code == 0`` is the production bug and must not happen."""
        stream = _HangingSnapshotStream()
        complete = _COMPLETE_JSON_LINE.encode("utf-8")
        container = self.stream_container(stream, complete)
        runtime = self.runtime(container)

        result = runtime.list_restic_snapshots("runtime", {"command": "restic snapshots --json"})

        self.assertNotEqual(
            result.get("error"),
            "failed to parse restic snapshots JSON",
            result,
        )
        self.assertNotEqual(
            result.get("error"),
            "runtime logs exceeded the permitted limit",
            result,
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(result["status_code"], 0)
        # The complete fallback (not the truncated stream tail) feeds parsing.
        self.assertEqual(
            result.get("snapshots"),
            [{"id": _SNAPSHOT_ID, "short_id": _SNAPSHOT_ID}],
        )
        self.assertEqual(result.get("logs"), _COMPLETE_JSON_LINE)

    def test_complete_stream_still_parses_snapshots(self):
        """Preserved shared behavior: a stream that finishes on its own keeps
        the streamed result, with timestamps metadata and no forced close."""
        container = Mock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.return_value = iter(
            [b'2024-05-05T10:00:00.123456789Z ' + _COMPLETE_JSON_LINE.encode("utf-8")]
        )
        runtime = self.runtime(container)

        result = runtime.list_restic_snapshots("runtime", {"command": "restic snapshots --json"})

        self.assertTrue(result["success"], result)
        self.assertEqual(
            result.get("snapshots"),
            [{"id": _SNAPSHOT_ID, "short_id": _SNAPSHOT_ID}],
        )
        self.assertEqual(result.get("log_timestamps"), ["2024-05-05T10:00:00.123456789Z"])
        self.assertEqual(result.get("logs"), _COMPLETE_JSON_LINE)

    def test_broken_stream_still_falls_back_to_complete_logs(self):
        """Preserved shared behavior: a stream that raises mid-read (existing
        error-marker path) routes the job to the complete non-streamed logs;
        the forced-close marker must ride the same path, not invent its own."""
        complete = _COMPLETE_JSON_LINE.encode("utf-8")
        stream = _RaisingSnapshotStream(complete)
        container = self.stream_container(stream, complete)
        runtime = self.runtime(container)

        result = runtime.list_restic_snapshots("runtime", {"command": "restic snapshots --json"})

        self.assertTrue(result["success"], result)
        self.assertEqual(
            result.get("snapshots"),
            [{"id": _SNAPSHOT_ID, "short_id": _SNAPSHOT_ID}],
        )
        self.assertEqual(result.get("logs"), _COMPLETE_JSON_LINE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
