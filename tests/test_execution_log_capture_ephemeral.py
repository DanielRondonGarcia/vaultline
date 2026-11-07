"""Fail-closed ephemeral recipe transfer for detached capture."""

import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.security import execution_log_capture
from src.worker_agent.infrastructure.security.execution_log_capture import ExecutionLogCapture
from src.worker_agent.infrastructure.security.execution_log_recipe_stream import encode_recipe_frame


class EphemeralCaptureTests(unittest.TestCase):
    def test_short_output_is_persisted_before_stdin_closes(self):
        with tempfile.TemporaryDirectory(prefix='ephemeral-capture-') as directory:
            log = Path(directory) / 'execution.log'
            command = [sys.executable, execution_log_capture.__file__, '--run-capture',
                       '--capture', str(log), '--recipe-stream', '1']
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                process.stdin.write(encode_recipe_frame(['synthetic-secret']) + b'visible synthetic-secret\n')
                process.stdin.flush()
                deadline = time.monotonic() + 10
                content = b''
                while time.monotonic() < deadline:
                    if log.exists():
                        content = log.read_bytes()
                        if b'visible' in content:
                            break
                    time.sleep(0.05)
                self.assertIn(b'visible', content)
                self.assertNotIn(b'synthetic-secret', content)
                self.assertIsNone(process.poll())
            finally:
                process.stdin.close()
                process.wait(timeout=15)
                process.stdout.close()
                process.stderr.close()

    def test_empty_recipe_option_cannot_be_combined_with_stream(self):
        with tempfile.TemporaryDirectory(prefix='ephemeral-capture-') as directory:
            log = Path(directory) / 'execution.log'
            command = [sys.executable, execution_log_capture.__file__, '--run-capture',
                       '--capture', str(log), '--recipe', '', '--recipe-stream', '1']
            result = subprocess.run(command, input=encode_recipe_frame([]), capture_output=True, timeout=30)
            self.assertEqual(execution_log_capture.CAPTURE_CHILD_EXIT_RECIPE_FAILURE, result.returncode)
            self.assertFalse(log.exists())

    def test_stdin_frame_redacts_following_output_without_recipe_file(self):
        with tempfile.TemporaryDirectory(prefix='ephemeral-capture-') as directory:
            root = Path(directory)
            log = root / 'execution.log'
            command = [sys.executable, execution_log_capture.__file__, '--run-capture',
                       '--capture', str(log), '--recipe-stream', '1']
            result = subprocess.run(command, input=encode_recipe_frame(['synthetic-secret']) +
                                    b'output synthetic-secret\n', capture_output=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            content = log.read_bytes()
            self.assertIn(b'output', content)
            self.assertNotIn(b'synthetic-secret', content)
            self.assertEqual([log.name], [path.name for path in root.iterdir()])

    def test_truncated_frame_fails_without_creating_a_log(self):
        with tempfile.TemporaryDirectory(prefix='ephemeral-capture-') as directory:
            log = Path(directory) / 'execution.log'
            command = [sys.executable, execution_log_capture.__file__, '--run-capture',
                       '--capture', str(log), '--recipe-stream', '1']
            result = subprocess.run(command, input=b'\x00\x00\x00\x10[]', capture_output=True, timeout=30)
            self.assertEqual(execution_log_capture.CAPTURE_CHILD_EXIT_RECIPE_FAILURE, result.returncode)
            self.assertFalse(log.exists())


class EphemeralRecipeCrashSafetyTests(unittest.TestCase):
    """Real-process crash-safety evidence for the ephemeral framed-recipe
    handoff shared by both runtime adapters (S5): the recipe exists only as
    an in-memory stdin frame from its writer to the capture child, so no
    crash can ever leave plaintext secrets on the persistent log root.

    Every test here runs real relay children (the executor-owning capture
    relay entrypoint) and real executor processes; no runtime is mocked in
    these scenarios. The Docker adapter's two bounded frame-write paths are
    driven against a real, never-consuming child process with a short
    patched transfer timeout.
    """

    SECRET = "synthetic-secret"
    COMPLETION_TIMEOUT = 60.0
    CONTAINER_ID = "a1b2c3d4e5f60718293a4b5c"

    EXECUTOR_COMPLETE = """
    import sys
    print("executor stdout synthetic-secret", flush=True)
    sys.stderr.write("executor stderr synthetic-secret\\n")
    sys.stderr.flush()
    sys.exit(7)
    """

    EXECUTOR_TICKING = """
    import sys, time
    for index in range(60):
        print(f"tick-{index} synthetic-secret", flush=True)
        time.sleep(0.05)
    print("executor done", flush=True)
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ephemeral-crash-safe-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.saved = {}
        for name in ("WORKER_EXECUTION_LOG_ROOT", "HOSTNAME", "DOCKER_HOST", "WORKER_ID"):
            self.saved[name] = os.environ.get(name)

        def restore_env():
            for name, value in self.saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        os.environ["WORKER_EXECUTION_LOG_ROOT"] = self.root
        for name in ("HOSTNAME", "DOCKER_HOST", "WORKER_ID"):
            os.environ.pop(name, None)
        self.addCleanup(restore_env)

    # -- helpers ---------------------------------------------------------

    def relay_command(self, log_path):
        """The real executor-owning relay child command for one job."""

    def spawn_relay(self, log_path, executor_command=None):
        script = textwrap.dedent(executor_command or self.EXECUTOR_COMPLETE)
        relay_command = ExecutionLogCapture.capture_relay_command(
            log_path, None, [sys.executable, "-c", script], recipe_stream=True
        )
        return subprocess.Popen(relay_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def drain(self, process):
        """Keep the relay's own stdout/stderr pipes drained so the executor's
        live output can never stall on a full pipe."""
        parts = []

        def reader(stream):
            while True:
                line = stream.readline()
                if line in (b"", ""):
                    break
                parts.append(line)

        threads = [
            threading.Thread(target=reader, args=(process.stdout,), daemon=True),
            threading.Thread(target=reader, args=(process.stderr,), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=self.COMPLETION_TIMEOUT)
        return parts

    def write_frame_and_close(self, relay, secrets=None):
        """Deliver the bounded framed recipe on the relay's real stdin pipe."""
        data = encode_recipe_frame(secrets or [self.SECRET])
        relay.stdin.write(data)
        relay.stdin.flush()
        relay.stdin.close()

    def wait_for(self, predicate, timeout=COMPLETION_TIMEOUT, interval=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return predicate()

    def log_paths(self):
        return list(Path(self.root).rglob("*.log"))

    @staticmethod
    def log_content(path):
        return path.read_text(encoding="utf-8", errors="replace")

    def assert_no_recipe_files(self):
        self.assertEqual(
            [],
            [str(path) for path in Path(self.root).rglob("*.recipe.json")],
            "a plaintext secret recipe survived on the persistent root",
        )

    @staticmethod
    def stop(process):
        """Best-effort teardown that cannot leak a sleeper between tests."""
        inner = getattr(process, "inner", process)
        try:
            inner.kill()
        except Exception:
            pass
        stream = getattr(process, "stream", None)
        if stream is not None:
            # Closing a stdin whose blocked writer may still hold the stream
            # must never stall the test process: close asynchronously like
            # the relay capture sink does.
            threading.Thread(target=lambda: _quiet_close(stream), daemon=True).start()
        try:
            inner.wait(timeout=15)
        except Exception:
            pass

    def sleeper_child(self):
        """A real relay-style child that never consumes its stdin and stays
        alive; its binary stdin is the blocked write's real pipe."""
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self.stop, _AttachingChildContainer(process))
        return process

    # -- tests -----------------------------------------------------------

    def test_no_recipe_file_on_disk_after_successful_capture(self):
        """S5: a real relay child runs the real executor and hands its recipe
        to a real detached capture child over stdin; after completion the
        persistent root holds redacted evidence and no plaintext recipe."""
        log_path = execution_log_capture.ExecutionLogCapture.job_log_path(
            self.root, "worker-eph", "native", "job-eph-1"
        )
        relay = self.spawn_relay(log_path)
        try:
            self.write_frame_and_close(relay)
            self.assertEqual(7, relay.wait(timeout=self.COMPLETION_TIMEOUT), "relay must return the executor exit code")
        finally:
            output_parts = self.drain(relay)
            self.assertIn(b"executor stdout", b"".join(output_parts))


        populated = self.wait_for(lambda: bool(self.log_paths()))
        self.assertTrue(populated, "detached capture child did not create the log")
        logs = self.log_paths()
        self.assertEqual(len(logs), 1, logs)
        finalized = self.wait_for(lambda: any("[completed]" in self.log_content(path) for path in logs))
        self.assertTrue(finalized, "detached capture child did not finalize the log")
        content = "".join(self.log_content(path) for path in logs)
        self.assertIn("executor stdout", content)
        self.assertIn("<redacted>", content)
        self.assertNotIn(self.SECRET, content)
        self.assert_no_recipe_files()

    def test_no_recipe_file_on_disk_after_relay_crash(self):
        """S5 crash-resistance: the relay is killed mid-job without any
        graceful shutdown. The detached capture child the relay spawned
        still finalizes the redacted evidence it received around the
        crash, and no plaintext recipe survives the abrupt death (the
        executor's own pipes break with the relay, so its post-death lines
        cannot be forwarded by the dead process).
        """
        log_path = execution_log_capture.ExecutionLogCapture.job_log_path(
            self.root, "worker-eph", "native", "job-eph-crash-1"
        )
        relay = self.spawn_relay(log_path, executor_command=self.EXECUTOR_TICKING)
        seen_output = threading.Event()
        parts = []

        def watch():
            while True:
                line = relay.stdout.readline()
                if line in (b"", ""):
                    break
                parts.append(line)
                if b"tick-0" in line:
                    seen_output.set()

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        self.write_frame_and_close(relay)
        self.assertTrue(seen_output.wait(self.COMPLETION_TIMEOUT), "executor output never reached the relay")
        relay.kill()
        relay.wait(timeout=self.COMPLETION_TIMEOUT)
        relay_err = relay.stderr.read()

        populated = self.wait_for(lambda: bool(self.log_paths()))
        self.assertTrue(populated, "surviving detached capture child did not create the log after the relay crash")
        logs = self.log_paths()
        self.assertEqual(len(logs), 1, logs)
        finalized = self.wait_for(lambda: any("[completed]" in self.log_content(path) for path in logs))
        self.assertTrue(finalized, "surviving detached capture child did not finalize after the relay crash")
        content = "".join(self.log_content(path) for path in logs)
        self.assertIn("tick-", content)
        self.assertNotIn(self.SECRET, content)
        self.assertIn("<redacted>", content)
        self.assert_no_recipe_files()
        self.assertNotIn(self.SECRET.encode(), relay_err)

    def test_corrupt_frame_skips_capture_but_job_continues(self):
        """An invalid, non-length-prefixed frame must fail open in the real
        relay: capture is skipped, the executor still runs and its exit
        code is preserved, and nothing is persisted."""
        log_path = execution_log_capture.ExecutionLogCapture.job_log_path(
            self.root, "worker-eph", "native", "job-eph-corrupt-1"
        )
        relay = self.spawn_relay(log_path)
        try:
            relay.stdin.write(b"not-a-valid-frame")
            relay.stdin.flush()
            relay.stdin.close()
            self.assertEqual(7, relay.wait(timeout=self.COMPLETION_TIMEOUT), "relay must skip capture and run the executor")
        finally:
            output_parts = self.drain(relay)

        self.assertIn(b"executor stdout", b"".join(output_parts))
        # Skipped capture means the capture layer redacted nothing: the
        # relay's verbatim live forwarding is the executor's raw stream,
        # so only persistence assertions apply here.
        self.assertEqual([], self.log_paths(), "a rejected frame must persist nothing")
        self.assert_no_recipe_files()

    def test_frame_transfer_timeout_does_not_block_job(self):
        """S4 bounded transfer (native-parity): with a real relay-style child
        that never reads its stdin and a frame larger than the pipe buffer,
        the Docker adapter's daemon-attach handoff must return within its
        timeout (fail-open) so the job proceeds, and the never-consuming
        child must remain alive."""
        sleeper = self.sleeper_child()
        frame = encode_recipe_frame(["0123456789" * 100_000])
        self.assertGreater(len(frame), 65_536, "premise: the frame must exceed the real pipe buffer")
        attach_fake = _AttachingChildContainer(sleeper)
        started = time.monotonic()
        with patch.object(DockerRuntimeAdapter, "RECIPE_FRAME_TRANSFER_TIMEOUT", 0.5):
            DockerRuntimeAdapter._stream_recipe_frame_to_capture_container(attach_fake, frame)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10.0, "bounded transfer returned after its timeout, not before")
        self.assertGreaterEqual(attach_fake.attached, 1)
        self.assertIsNone(sleeper.poll(), "the never-consuming child (and with it the job flow) must keep going")
        self.assert_no_recipe_files()

    def test_frame_process_mode_timeout_does_not_block_job(self):
        """S4 bounded transfer, detached host child path: the raw os.write
        loop runs on a daemon thread against a real pipe whose duplicate
        read end is inherited by a real never-consuming child; the handoff
        must surrender at the transfer timeout and the job must proceed."""
        captured_process = {}

        def fake_spawn(*args, **kwargs):
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                stdin=kwargs.get("stdin"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            captured_process["sleeper"] = process
            self.addCleanup(self.stop, _AttachingChildContainer(process))
            return process

        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.worker_id = "worker-eph"
        payload = {"_job_id": "job-eph-proc"}
        big_secrets = ["0123456789" * 100_000]
        started = time.monotonic()
        with patch.object(DockerRuntimeAdapter, "_execution_log_capture_mode", return_value={"mode": "process"}):
            with patch.object(execution_log_capture.ExecutionLogCapture, "prune", return_value={"deleted": 0, "failed": 0, "errors": []}):
                with patch.object(execution_log_capture.ExecutionLogCapture, "spawn_detached_docker_capture", side_effect=fake_spawn):
                    with patch.object(DockerRuntimeAdapter, "RECIPE_FRAME_TRANSFER_TIMEOUT", 0.5):
                        capture = runtime._start_execution_log_capture(
                            _AttachingChildContainer(None), payload, big_secrets, None
                        )
        elapsed = time.monotonic() - started
        self.assertIsNotNone(capture, capture)
        self.assertEqual("process", capture["mode"])
        self.assertEqual(captured_process["sleeper"], capture["process"])
        self.assertLess(elapsed, 10.0, "bounded os.write handoff returned after its timeout, not before")
        self.assertGreater(len(capture["recipe_frame"]), 65_536)
        self.assertIsNone(captured_process["sleeper"].poll(), "the never-consuming child must survive the bounded handoff")
        self.assert_no_recipe_files()


class _AttachingChildContainer:
    """Minimal real-child container fake for the Docker adapter seams: the
    attach_socket() seam returns the child's true binary stdin so the bounded
    write exercises a real, never-drained OS pipe."""

    def __init__(self, process, *, identifier="capcontainerid0123456789"):
        self.inner = process
        self.id = identifier
        self.attached = 0

    def attach_socket(self, params=None):
        self.attached += 1
        if self.inner is None:
            return Mock()
        return self.inner.stdin

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _quiet_close(stream):
    try:
        stream.close()
    except Exception:
        pass


if __name__ == '__main__':
    unittest.main()
