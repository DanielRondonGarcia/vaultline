"""Independent security verification for the ephemeral framed-stdin recipe
handoff (odd/tasks/persistent-worker-execution-logs.md, commit ec9802a).

Probes (parent-authorized):
P1 no plaintext *.recipe.json survives any crash scenario,
P2 read_recipe_frame leaves following executor output untouched,
P3 a corrupt frame fails open (executor exit code preserved, not exit 2),
P4 the bounded frame transfer cannot block the job indefinitely,
P5 no secret value ever appears in a spawned command argv.

Every process spawned here is terminated by the test (or is a real crash
scenario); all roots are temporary directories. The large-frame transfer
probe runs in a throwaway child process so a hang can never wedge the
suite: the child is killed tree-wide and the failure is reported.
"""

import os
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter
from src.worker_agent.infrastructure.security import execution_log_capture
from src.worker_agent.infrastructure.security.execution_log_capture import ExecutionLogCapture
from src.worker_agent.infrastructure.security.execution_log_recipe_stream import (
    MAX_RECIPE_BYTES,
    RecipeFrameError,
    encode_recipe_frame,
    read_recipe_frame,
)

SECRET = "synthetic-verifier-secret"

EXECUTOR_EXIT_42 = "import sys\nsys.exit(42)\n"

EXECUTOR_TICKING = (
    "import sys, time\n"
    "for index in range(60):\n"
    "    print(f'tick-{index} " + SECRET + "', flush=True)\n"
    "    time.sleep(0.05)\n"
)


class _RecipeRootTest(unittest.TestCase):
    """Common temporary log root and secret-file assertions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="execlog-sec-verify-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def log_path(self, worker="worker-verify", job="job-verify"):
        return ExecutionLogCapture.job_log_path(self.root, worker, "native", job)

    def assert_no_recipe_files(self):
        found = [str(path) for path in self.root.rglob("*.recipe.json")]
        self.assertEqual(
            [],
            found,
            "a plaintext secret recipe survived on the persistent log root",
        )

    def assert_no_secret_on_disk(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                with open(path, "rb") as handle:
                    body = handle.read()
                if body:
                    self.assertNotIn(
                        SECRET.encode("utf-8"), body, f"plaintext secret survived in {path}"
                    )

    def spawn_relay(self, log_path, executor_script=EXECUTOR_EXIT_42):
        relay_command = ExecutionLogCapture.capture_relay_command(
            log_path, None, [sys.executable, "-c", executor_script], recipe_stream=True
        )
        return subprocess.Popen(
            relay_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

    def drain(self, process, timeout=60.0):
        parts = []

        def reader(stream):
            while True:
                try:
                    line = stream.readline()
                except (OSError, ValueError):
                    break
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
            thread.join(timeout)
        return parts

    def close_quietly(self, stream):
        try:
            stream.close()
        except Exception:
            pass

    def write_frame_chunks(self, stream, frame, chunk=8192, interval=0.01, stop=None):
        """Write a frame in small chunks with sleeps so the transfer spans
        real time (crash injection); never raises into the caller."""
        offset = 0
        while offset < len(frame):
            if stop is not None and stop.is_set():
                return
            try:
                stream.write(frame[offset : offset + chunk])
                stream.flush()
            except (OSError, ValueError):
                return
            offset += chunk
            time.sleep(interval)

    def wait_for(self, predicate, timeout=60.0, interval=0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return predicate()


class WorkerDeathBeforeRelaySpawnTest(_RecipeRootTest):
    def test_worker_death_before_relay_spawn_leaves_nothing_on_disk(self):
        """The worker encodes the frame in memory and dies (os._exit) before
        the relay is spawned: no recipe file can exist."""
        repo_root = str(Path(__file__).resolve().parents[1])
        script = textwrap.dedent(
            f"""
            import os, sys
            os.environ["WORKER_EXECUTION_LOG_ROOT"] = r"{self.root}"
            os.environ["WORKER_ID"] = "worker-death-a"
            sys.path.insert(0, r"{repo_root}")
            from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter
            runtime = NativeRuntimeAdapter.__new__(NativeRuntimeAdapter)
            runtime.worker_id = "worker-death-a"
            capture = runtime._start_execution_log_capture(
                {{"_job_id": "job-death-a"}}, {{"{SECRET}"}}, None
            )
            assert capture is not None and capture["recipe_frame"]
            os._exit(9)
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        try:
            _, stderr = process.communicate(timeout=60)
            self.assertEqual(9, process.returncode, stderr)
        finally:
            if process.poll() is None:
                process.kill()
        # Nothing at all may exist under the log root yet (no relay, no
        # capture child): no recipe file, no log, no secret.
        self.assertEqual([], list(self.root.rglob("*")), "worker death created files before the relay spawn")
        self.assert_no_recipe_files()
        self.assert_no_secret_on_disk()


class RelayDeathAfterExecutorSpawnTest(_RecipeRootTest):
    def test_relay_killed_mid_job_leaves_no_recipe_file(self):
        log_path = self.log_path(worker="worker-verify-b", job="job-verify-b")
        relay = self.spawn_relay(log_path, executor_script=EXECUTOR_TICKING)
        seen = threading.Event()

        def watch():
            while True:
                try:
                    line = relay.stdout.readline()
                except (OSError, ValueError):
                    break
                if line in (b"", ""):
                    break
                if b"tick-0" in line:
                    seen.set()

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        frame = encode_recipe_frame([SECRET])
        relay.stdin.write(frame)
        relay.stdin.flush()
        relay.stdin.close()
        self.assertTrue(seen.wait(60), "executor output never reached the relay stdout")
        relay.kill()
        relay.wait(timeout=60)
        self.drain(relay)
        relay.stdout.close()
        relay.stderr.close()
        # The detached capture child survives and finalizes redacted partial
        # evidence; no plaintext recipe may survive the abrupt relay death.
        populated = self.wait_for(lambda: any(self.root.rglob("*.log")))
        self.assertTrue(populated, "surviving detached capture child did not create the log")
        finalized = self.wait_for(
            lambda: any(
                b"[completed]" in path.read_bytes() or b"[interrupted]" in path.read_bytes()
                for path in self.root.rglob("*.log")
            ),
            timeout=60,
        )
        self.assertTrue(finalized, "surviving detached capture child did not finalize the log")
        self.assert_no_recipe_files()
        self.assert_no_secret_on_disk()
        content = b"".join(path.read_bytes() for path in self.root.rglob("*.log"))
        self.assertIn(b"<redacted>", content)
        self.assertNotIn(SECRET.encode("utf-8"), content)


class RelayDeathDuringFrameWriteTest(_RecipeRootTest):
    def test_relay_killed_during_frame_write_leaves_nothing_on_disk(self):
        """The relay is killed while the parent is still pumping the frame:
        the frame was never complete, so the relay could not have spawned a
        capture child or an executor."""
        log_path = self.log_path(worker="worker-verify-c", job="job-verify-c")
        frame = encode_recipe_frame(["0123456789" * 100_000])  # 1 MB frame
        self.assertGreater(len(frame), 1_000_000)
        relay = self.spawn_relay(log_path)
        writer_done = threading.Event()

        def pump():
            self.write_frame_chunks(relay.stdin, frame)
            self.close_quietly(relay.stdin)
            writer_done.set()

        writer = threading.Thread(target=pump)
        writer.start()
        # Kill the relay well before the 1 MB frame transfer completes
        # (about 1.3 s of chunked writes), while the relay is still blocked
        # inside read_recipe_frame.
        time.sleep(0.3)
        relay.kill()
        try:
            relay.wait(timeout=60)
        finally:
            writer.join(15)
            self.drain(relay)
            relay.stdout.close()
            relay.stderr.close()
        # Give any unexpected straggler child a bounded window, then assert
        # nothing was ever created under the root.
        time.sleep(1.0)
        self.assertEqual([], list(self.root.rglob("*")), "relay death during frame write persisted files")
        self.assert_no_recipe_files()
        self.assert_no_secret_on_disk()


class CaptureChildDeathDuringFrameReadTest(_RecipeRootTest):
    def test_capture_child_killed_during_frame_read_leaves_nothing_on_disk(self):
        """A real detached capture child is killed while reading the frame:
        the writer step (and therefore any log file) is never reached."""
        log_path = self.log_path(worker="worker-verify-d", job="job-verify-d")
        command = ExecutionLogCapture.capture_child_command(log_path, None, recipe_stream=True)
        child = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        frame = encode_recipe_frame(["0123456789" * 100_000])
        self.assertGreater(len(frame), 1_000_000)
        writer = threading.Thread(target=self.write_frame_chunks, args=(child.stdin, frame))
        writer.start()
        time.sleep(0.3)
        child.kill()
        try:
            child.wait(timeout=60)
        finally:
            writer.join(15)
            self.close_quietly(child.stdin)
        self.assertNotEqual(
            0, child.returncode, "the capture child exited normally, it should have been killed"
        )
        # The frame read never completed, so the writer step never opened a
        # log file: nothing at all may exist under the root.
        time.sleep(1.0)
        self.assertEqual([], list(self.root.rglob("*")), "capture child death during frame read persisted files")
        self.assert_no_recipe_files()
        self.assert_no_secret_on_disk()


class FrameProtocolPreservesFollowingOutputTest(_RecipeRootTest):
    def test_executor_output_after_frame_is_intact_in_real_capture_child(self):
        """P2: executor output following the frame on the same stdin stream
        reaches the log byte-intact (in order), with the secret redacted."""
        log_path = self.log_path(worker="worker-verify-e", job="job-verify-e")
        command = ExecutionLogCapture.capture_child_command(log_path, None, recipe_stream=True)
        result = subprocess.run(
            command,
            input=(
                encode_recipe_frame([SECRET])
                + b"alpha first executor line "
                + SECRET.encode("utf-8")
                + b"\nsecond executor line\nthird executor line\n"
            ),
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(log_path.exists(), "the capture child did not persist the log")
        content = log_path.read_bytes()
        self.assertIn(b"alpha first executor line", content)
        self.assertIn(b"second executor line", content)
        self.assertIn(b"third executor line", content)
        first = content.find(b"alpha first executor line")
        second = content.find(b"second executor line")
        self.assertLess(first, second, "executor output order was corrupted by the frame reader")
        self.assertIn(b"<redacted>", content)
        self.assertNotIn(SECRET.encode("utf-8"), content)

    def test_frame_reader_leaves_tail_bytes_untouched_in_process(self):
        tail = b"executor output after the frame"
        stream = _OneByteReader(encode_recipe_frame([SECRET]) + tail)
        self.assertEqual([SECRET], read_recipe_frame(stream))
        self.assertEqual(tail, stream.read_rest())

    def test_oversize_declared_header_leaves_tail_and_fails(self):
        tail = b"kept tail"
        stream = _OneByteReader(struct.pack("!I", MAX_RECIPE_BYTES + 1) + tail)
        with self.assertRaises(RecipeFrameError):
            read_recipe_frame(stream)
        self.assertEqual(tail, stream.read_rest())


class _OneByteReader:
    """A stream that returns at most one byte per read call."""

    def __init__(self, data):
        self._buffer = data
        self._offset = 0

    def read(self, count):
        chunk = self._buffer[self._offset : self._offset + min(count, 1)]
        self._offset += len(chunk)
        return chunk

    def read_rest(self):
        chunk = self._buffer[self._offset :]
        self._offset = len(self._buffer)
        return chunk


class CorruptFrameFailOpenTest(_RecipeRootTest):
    def test_corrupt_frame_relay_exit_code_is_the_executor_exit_code(self):
        """P3: a non-length-prefixed frame fails open - the executor runs
        and the relay's exit code is the executor's exit code (not 2)."""
        log_path = self.log_path(worker="worker-verify-f", job="job-verify-f")
        relay = self.spawn_relay(log_path)
        try:
            relay.stdin.write(b"garbage-not-a-frame")
            relay.stdin.flush()
            relay.stdin.close()
            self.assertEqual(
                42,
                relay.wait(timeout=60),
                "the relay must preserve the executor's exit code (fail-open), not exit 2",
            )
        finally:
            self.drain(relay)
            relay.stdout.close()
            relay.stderr.close()
        self.assertEqual([], list(self.root.rglob("*")), "a rejected frame must persist nothing")
        self.assert_no_recipe_files()
        self.assert_no_secret_on_disk()


class BoundedTransferTimeoutTest(_RecipeRootTest):
    def test_never_reading_relay_small_frame_job_proceeds_within_timeout(self):
        """P4, mandated form: a real relay-style child that never reads its
        stdin and sleeps; with a frame that fits the pipe buffer the job
        must proceed within the (patched) transfer timeout."""
        runtime = NativeRuntimeAdapter.__new__(NativeRuntimeAdapter)
        runtime.timeout_seconds = None
        runtime.idle_timeout_seconds = None
        frame = encode_recipe_frame([SECRET])
        started = time.monotonic()
        with unittest.mock.patch.object(NativeRuntimeAdapter, "RECIPE_FRAME_TRANSFER_TIMEOUT", 0.5):
            result = runtime._run_process(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                {},
                {"timeout_seconds": 2, "job_id": "job-verify-timeout-small"},
                None,
                None,
                capture={
                    "recipe_frame": frame,
                    "spawn_options": {
                        "stdin": subprocess.PIPE,
                        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    },
                },
            )
        elapsed = time.monotonic() - started
        self.assertEqual(124, result.get("status_code"), result)
        self.assertLess(elapsed, 15.0, "the bounded transfer did not bound the job")
        self.assert_no_recipe_files()

    def test_never_reading_relay_large_frame_does_not_hang_the_job(self):
        """P4, large frame: the mandated bound must hold when the frame
        exceeds the pipe buffer too. Runs in a throwaway child so a defect
        that hangs _run_process cannot wedge the suite; the child must exit
        with RESULT 124 well inside the 30 s budget."""
        repo_root = str(Path(__file__).resolve().parents[1])
        child_root = Path(self.tmp.name) / "child-root"
        script = textwrap.dedent(
            f"""
            import os, subprocess, sys, time
            sys.path.insert(0, r"{repo_root}")
            os.environ["WORKER_EXECUTION_LOG_ROOT"] = r"{child_root}"
            from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter
            NativeRuntimeAdapter.RECIPE_FRAME_TRANSFER_TIMEOUT = 0.5
            runtime = NativeRuntimeAdapter.__new__(NativeRuntimeAdapter)
            runtime.timeout_seconds = None
            runtime.idle_timeout_seconds = None
            frame = ("0123456789" * 100_000).encode("utf-8")
            payload = {{"timeout_seconds": 3, "job_id": "job-verify-timeout-big"}}
            capture = {{"recipe_frame": frame, "spawn_options": {{"stdin": subprocess.PIPE, "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}}}}
            started = time.monotonic()
            result = runtime._run_process(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                {{}},
                payload,
                None,
                None,
                capture=capture,
            )
            print("RESULT", result.get("status_code"), "ELAPSED", round(time.monotonic() - started, 2))
            sys.stdout.flush()
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            except Exception:
                process.kill()
            self.fail(
                "RED: _run_process did not return within 30 s with a large frame and a "
                "never-reading relay child; the Event.wait() bound cannot unblock the "
                "synchronous process.stdin.close() against a writer thread still "
                "blocked inside stdin_binary.write(). Suggested fix: close the stdin "
                "asynchronously (daemon thread) when transfer_done is not set, "
                "mirroring _RelayCaptureSink._close_stream_async and "
                "DockerRuntimeAdapter._release_os_handle_pair."
            )
        self.assertIn(b"RESULT 124", stdout, (stdout, stderr))
        self.assert_no_recipe_files()


class NoSecretInArgvTest(_RecipeRootTest):
    def test_secrets_never_appear_in_spawned_command_argv(self):
        """P5: the relay and capture child command builders carry paths and
        fixed options only - no secret value, and in stream mode no recipe
        file name at all."""
        log_path = self.log_path(worker="worker-verify-g", job="job-verify-g")
        executor = ["restic", "backup", "/data"]
        commands = {
            "relay-stream": ExecutionLogCapture.capture_relay_command(
                log_path, None, executor, recipe_stream=True
            ),
            "child-stream": ExecutionLogCapture.capture_child_command(
                log_path, None, recipe_stream=True
            ),
            "child-recipe": ExecutionLogCapture.capture_child_command(
                log_path, "worker-state/job.log.recipe.json"
            ),
        }
        for name, command in commands.items():
            for element in command:
                self.assertNotIn(
                    SECRET, element, f"secret value leaked into the {name} argv element {element!r}"
                )
        for name in ("relay-stream", "child-stream"):
            command = commands[name]
            self.assertIn("--recipe-stream", command, f"{name} must select stream mode")
            self.assertIn("1", command)
            self.assertNotIn("--recipe", command, f"{name} must carry no recipe file name")
        # The daemon-mode container command assembles the same fixed options
        # with a container identifier and socket path (reviewed inline in
        # DockerRuntimeAdapter._start_execution_log_capture, daemon branch:
        # it embeds only log_path, "1", container_id and spec["socket"]).


if __name__ == "__main__":
    unittest.main()
