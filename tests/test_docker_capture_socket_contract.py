"""Contract tests for the Docker SDK capture stdin handoff."""

import unittest

from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter


class _PartialStream:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        count = min(3, len(data))
        self.data.extend(data[:count])
        return count

    def close(self):
        self.closed = True


class _DockerSdkContainer:
    def __init__(self):
        self.stream = _PartialStream()
        self.params = None

    def attach_socket(self, params=None):
        self.params = params
        return self.stream

    def attach(self, stdout=True, stderr=True, stream=False, logs=False, demux=False):
        raise AssertionError("attach is not the Docker SDK stdin socket API")


class DockerCaptureSocketContractTests(unittest.TestCase):
    def test_recipe_is_fully_transferred_over_sdk_stdin_socket(self):
        container = _DockerSdkContainer()
        frame = b"synthetic-frame"
        DockerRuntimeAdapter._stream_recipe_frame_to_capture_container(container, frame)
        self.assertEqual({"stdin": 1, "stream": 1}, container.params)
        self.assertEqual(frame, bytes(container.stream.data))
        self.assertTrue(container.stream.closed)

    def test_socket_with_sendall_receives_complete_frame(self):
        class SendStream:
            def __init__(self):
                self.data = b""
                self.closed = False

            def sendall(self, data):
                self.data = data

            def close(self):
                self.closed = True

        container = _DockerSdkContainer()
        container.stream = SendStream()
        DockerRuntimeAdapter._stream_recipe_frame_to_capture_container(container, b"frame")
        self.assertEqual(b"frame", container.stream.data)
        self.assertTrue(container.stream.closed)

    def test_zero_byte_write_fails_and_closes_stream(self):
        container = _DockerSdkContainer()
        container.stream.write = lambda data: 0
        with self.assertRaisesRegex(OSError, "did not accept"):
            DockerRuntimeAdapter._stream_recipe_frame_to_capture_container(container, b"frame")
        self.assertTrue(container.stream.closed)
