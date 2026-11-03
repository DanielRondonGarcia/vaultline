"""Binary executor output must never be copied into diagnostic logs."""

import io
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.worker_agent.infrastructure.security import execution_log_capture
from src.worker_agent.infrastructure.security.execution_log_capture import DockerContainerLogSource, ExecutionLogCapture
from src.worker_agent.infrastructure.security.execution_log_recipe import write_redaction_recipe


class DockerDiagnosticSourceTests(unittest.TestCase):
    def test_stderr_only_excludes_stdout_from_docker_log_request(self):
        container = Mock()
        container.logs.return_value = iter([b'diagnostic'])
        client = Mock()
        client.containers.get.return_value = container
        source = DockerContainerLogSource('container-1', client_factory=lambda _: client, stderr_only=True)
        self.assertEqual([b'diagnostic'], list(source.open_stream()))
        container.logs.assert_called_once_with(stdout=False, stderr=True, stream=True, follow=True)

    def test_default_preserves_stdout_and_stderr(self):
        container = Mock()
        container.logs.return_value = iter([])
        client = Mock()
        client.containers.get.return_value = container
        source = DockerContainerLogSource('container-1', client_factory=lambda _: client)
        list(source.open_stream())
        container.logs.assert_called_once_with(stdout=True, stderr=True, stream=True, follow=True)


class RelayDiagnosticTests(unittest.TestCase):
    def test_relay_bounds_reads_for_binary_without_newlines(self):
        class Tracked(io.BytesIO):
            def __init__(self, data):
                super().__init__(data)
                self.limits = []

            def readline(self, size=-1):
                self.limits.append(size)
                return super().readline(size)

        payload = b'X' * (execution_log_capture.READ_CHUNK_BYTES * 3)
        source = Tracked(payload)
        live = io.BytesIO()
        execution_log_capture._relay_forward_source(source, live, [True], None)
        self.assertEqual(payload, live.getvalue())
        self.assertTrue(all(limit == execution_log_capture.READ_CHUNK_BYTES for limit in source.limits))

    def test_unlabelled_stdin_rejects_stderr_only_before_loading_recipe(self):
        with unittest.mock.patch.object(execution_log_capture, '_load_child_recipe') as load:
            result = execution_log_capture.run_capture_child(
                ['--capture', 'log', '--recipe', 'recipe', '--stderr-only', '1']
            )
        self.assertEqual(execution_log_capture.CAPTURE_CHILD_EXIT_RECIPE_FAILURE, result)
        load.assert_not_called()

    def test_relay_command_selects_stderr_without_changing_executor_arguments(self):
        command = ExecutionLogCapture.capture_relay_command('log', 'recipe', ['executor', '--flag'], stderr_only=True)
        self.assertEqual(['--stderr-only', '1', '--', 'executor', '--flag'], command[-5:])

    def test_binary_stdout_is_forwarded_but_never_persisted(self):
        with tempfile.TemporaryDirectory(prefix='relay-stderr-only-') as directory:
            root = Path(directory)
            recipe = root / 'recipe.json'
            log = root / 'execution.log'
            write_redaction_recipe(recipe, ['synthetic-secret'])
            command = [sys.executable, execution_log_capture.__file__, '--run-relay',
                       '--capture', str(log), '--recipe', str(recipe), '--stderr-only', '1', '--',
                       sys.executable, '-c',
                       "import sys;sys.stdout.buffer.write(b'PRIVATE-BINARY-\\xff');"
                       "sys.stderr.write('diagnostic synthetic-secret\\n')"]
            result = subprocess.run(command, capture_output=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(b'PRIVATE-BINARY-\xff', result.stdout)
            deadline = time.monotonic() + 10
            content = b''
            while time.monotonic() < deadline:
                if log.exists():
                    content = log.read_bytes()
                    if b'completed' in content:
                        break
                time.sleep(0.05)
            self.assertIn(b'completed', content)
            self.assertNotIn(b'PRIVATE-BINARY', content)
            self.assertIn(b'diagnostic', content)
            self.assertNotIn(b'synthetic-secret', content)


if __name__ == '__main__':
    unittest.main()
