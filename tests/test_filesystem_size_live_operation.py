import unittest
from unittest.mock import Mock, patch

from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter


class FilesystemSizeLiveOperationTests(unittest.TestCase):
    def make_service(self, runtime):
        service = object.__new__(WorkerAgentService)
        service.runtime = runtime
        service.control_plane_client = Mock()
        return service

    @patch("src.worker_agent.application.services.worker_agent_service.estimate_filesystem_size_bounded")
    def test_native_worker_returns_bounded_scan_result_without_backup_runtime_call(self, estimate):
        service = self.make_service(NativeRuntimeAdapter())
        estimate.return_value = {"complete": False, "reason": "scan_timeout", "logical_bytes": 4,
                                 "file_count": 1, "directory_count": 0, "symlinks_skipped": 0, "roots": []}
        service._process_live_request("worker-1", {
            "operation_id": "operation-1", "operation": "filesystem.size_estimate", "worker_id": "worker-1",
            "filesystem_paths": ["C:\\selected"],
        })
        result = service.control_plane_client.send_live_response.call_args.args[2]
        self.assertEqual(result, {"status": 200, **estimate.return_value})
        estimate.assert_called_once_with(["C:\\selected"])
        self.assertFalse(hasattr(service.runtime, "backup"))

    def test_non_native_or_invalid_request_is_rejected(self):
        for runtime, path in ((object(), "/tmp"), (NativeRuntimeAdapter(), "relative")):
            with self.subTest(path=path):
                service = self.make_service(runtime)
                service._process_live_request("worker-1", {
                    "operation_id": "operation-1", "operation": "filesystem.size_estimate", "worker_id": "worker-1",
                    "filesystem_paths": [path],
                })
                result = service.control_plane_client.send_live_response.call_args.args[2]
                self.assertEqual(result["status"], 400)
                self.assertNotIn("logical_bytes", result)


if __name__ == "__main__":
    unittest.main()
