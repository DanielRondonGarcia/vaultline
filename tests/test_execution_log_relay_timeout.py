"""A stalled capture pipe must not stall the executor-owning relay."""

import subprocess
import sys
import unittest


class RelayTimeoutTests(unittest.TestCase):
    def test_finalize_does_not_wait_for_a_locked_stream_close(self):
        script = r'''
import threading
from src.worker_agent.infrastructure.security.execution_log_capture import _RelayCaptureSink
class Blocked:
    def __init__(self):
        self.lock = threading.Lock()
        self.entered = threading.Event()
    def write(self, data):
        with self.lock:
            self.entered.set()
            threading.Event().wait()
    def flush(self): pass
    def close(self):
        with self.lock: pass
stream = Blocked()
sink = _RelayCaptureSink(stream)
sink._DRAIN_SECONDS = 0.1
sink.write(b'data')
assert stream.entered.wait(2)
sink.finalize()
print('returned', flush=True)
'''
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn(b'returned', result.stdout)


if __name__ == '__main__':
    unittest.main()
