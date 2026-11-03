"""Execution-log warnings must not be joined to executor output."""

import time
import unittest
from unittest.mock import Mock

from src.worker_agent.application.services.worker_agent_service import _JobProgressReporter
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter


class ExecutionLogAlertDeliveryTests(unittest.TestCase):
    def test_callback_exception_is_not_logged(self):
        secret = 'synthetic-secret-from-callback'
        for adapter_class in (DockerRuntimeAdapter, NativeRuntimeAdapter):
            with self.subTest(adapter=adapter_class.__name__):
                adapter = adapter_class.__new__(adapter_class)

                def fail(_):
                    raise RuntimeError(secret)

                with self.assertLogs(adapter_class.__module__, level='DEBUG') as recorded:
                    adapter._execution_log_alert({'job_id': 'job-1'}, 'write failed', fail)
                self.assertNotIn(secret, '\n'.join(recorded.output))

    def test_alert_is_a_complete_separate_progress_line(self):
        for adapter_class in (DockerRuntimeAdapter, NativeRuntimeAdapter):
            with self.subTest(adapter=adapter_class.__name__):
                reporter = _JobProgressReporter(Mock(), 'worker-1', {'id': 'job-1'})
                reporter._last_sent = time.monotonic()
                reporter.emit('unfinished executor output')
                adapter = adapter_class.__new__(adapter_class)
                adapter._execution_log_alert({'job_id': 'job-1'}, 'write failed', reporter.emit)
                reporter.emit('next executor output\n')
                self.assertEqual(3, len(reporter._pending_lines))
                self.assertEqual('unfinished executor output', reporter._pending_lines[0])
                self.assertTrue(reporter._pending_lines[1].startswith('[persistent-execution-log]'))
                self.assertIn('write failed', reporter._pending_lines[1])
                self.assertEqual('next executor output', reporter._pending_lines[2])


if __name__ == '__main__':
    unittest.main()
