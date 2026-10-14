import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import quote

import test_live_file_browser as browser_tests
from src.app.domain.models import BackupConfig
from src.app.infrastructure.adapters.backup_strategy import ResticBackupStrategy
from src.control_plane.main import ControlPlaneRequestHandler
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.live_file_runtime import LiveFileRuntime
from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter
from src.worker_agent import live_file_helper


class NativeLiveFilesTests(unittest.TestCase):
    def make_worker(self):
        docker = Mock()
        docker.containers.run.side_effect = AssertionError("native browsing must not use Docker")
        runtime = NativeRuntimeAdapter()
        runtime.client = docker
        client = SimpleNamespace(
            credential_store=SimpleNamespace(load=lambda: SimpleNamespace(secret="s" * 32)),
            send_live_response=Mock(), send_live_chunk=Mock(), send_live_change=Mock(return_value=True),
        )
        worker = WorkerAgentService(WorkerAgentConfig("http://unused", "worker", "host"), client, runtime)
        self.addCleanup(worker.live_sessions.close)
        return worker, client, docker

    @staticmethod
    def request(worker, client, source, **overrides):
        worker._process_live_request("worker", {
            "operation_id": "operation", "operation": "entries", "worker_id": "worker",
            "target_id": "target", "config_revision": "revision", "mount_source": source,
            "path": "/", **overrides,
        })
        return client.send_live_response.call_args.args[2]

    def test_native_worker_lists_nested_unicode_and_downloads_binary_without_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "carpeta").mkdir()
            data = bytes(range(256)) * 512
            (root / "carpeta" / "informe-Íñigo.bin").write_bytes(data)
            worker, client, docker = self.make_worker()
            result = self.request(worker, client, str(root))
            self.assertEqual(result["status"], 200, result)
            self.assertEqual([entry["name"] for entry in result["entries"]], ["carpeta"])
            result = self.request(worker, client, str(root), path="/carpeta")
            self.assertEqual(result["entries"][0]["path"], "/carpeta/informe-Íñigo.bin")
            result = self.request(worker, client, str(root), operation="file", path="/carpeta/informe-Íñigo.bin")
            self.assertEqual(result["status"], 200, result)
            chunks = [call.args[2] for call in client.send_live_chunk.call_args_list]
            self.assertEqual(b"".join(chunks), data)
            self.assertTrue(all(len(chunk) <= 64 * 1024 for chunk in chunks))
            self.assertTrue(client.send_live_chunk.call_args.kwargs["final"])
            denied = self.request(worker, client, str(root), operation="file", path="/carpeta/informe-Íñigo.bin", max_bytes=8)
            self.assertNotEqual(denied["status"], 200)
            docker.containers.run.assert_not_called()
            docker.volumes.get.assert_not_called()

    def test_native_multiple_roots_virtual_folders_pagination_and_watch_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory, name) for name in ("first", "second")]
            for root in roots:
                root.mkdir()
                (root / "á.bin").write_bytes(b"\x00\xff\x8d")
            worker, client, docker = self.make_worker()
            descriptors = [{"source": str(root), "folder": root.name} for root in roots]
            result = self.request(worker, client, None, mount_sources=descriptors, limit=1)
            self.assertEqual(result["status"], 200, result)
            self.assertEqual(result["entries"][0]["path"], "/first")
            result = self.request(worker, client, None, mount_sources=descriptors, cursor=result["next_cursor"], limit=1)
            self.assertEqual(result["entries"][0]["path"], "/second")
            result = self.request(worker, client, None, mount_sources=descriptors, path="/second")
            self.assertEqual(result["entries"][0]["path"], "/second/á.bin")
            self.request(worker, client, None, mount_sources=descriptors, operation="file", path="/first/á.bin")
            self.assertEqual(b"".join(call.args[2] for call in client.send_live_chunk.call_args_list), b"\x00\xff\x8d")
            runtime = LiveFileRuntime(worker._resolve_live_sources(descriptors), "s" * 32, max_watch_entries=3)
            snapshot, complete = runtime._watch_snapshot()
            self.assertLessEqual(len(snapshot), 3)
            self.assertFalse(complete)
            self.assertIn("/first/á.bin", snapshot)
            runtime.cancel()
            result = self.request(worker, client, None, mount_sources=descriptors, operation="watch")
            self.assertEqual(result["status"], 200, result)
            changed = threading.Event()
            client.send_live_change.side_effect = lambda **_kwargs: changed.set() or True
            (roots[1] / "á.bin").write_bytes(b"updated")
            self.assertTrue(changed.wait(2), "native watcher did not publish a change")
            self.assertEqual(client.send_live_change.call_args.kwargs["path"], "/second/á.bin")
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                worker._resolve_live_sources([descriptors[0], {**descriptors[0], "folder": "other"}])
            docker.containers.run.assert_not_called()
            docker.volumes.get.assert_not_called()

    def test_native_worker_rejects_invalid_sources_and_virtual_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, client, docker = self.make_worker()
            for source in ("volume-a", "relative/path", "C:relative", "\\\\?\\C:\\data", "//?/C:/data", "/proc", str(Path(directory, "..", "escape"))):
                with self.subTest(source=source):
                    result = self.request(worker, client, source)
                    self.assertEqual((result["status"], result["reason"]), (400, "invalid_source"))
            result = self.request(worker, client, str(Path(directory, "missing")))
            self.assertEqual((result["status"], result["reason"]), (503, "source_unavailable"))
            paths = ["/../outside", "/nested/../../outside", "/bad\\path", "//outside", "/file\x00", "/file\n"]
            if os.name == "nt":
                paths.append("/C:/Windows")
            for path in paths:
                with self.subTest(path=path):
                    result = self.request(worker, client, directory, path=path)
                    self.assertEqual(result["status"], 400, result)
            docker.containers.run.assert_not_called()
            docker.volumes.get.assert_not_called()

    def test_native_permission_denials_and_wrong_worker_requests_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            worker, client, _docker = self.make_worker()
            with patch("src.worker_agent.infrastructure.adapters.live_file_runtime.os.scandir", side_effect=PermissionError("private-host-path")):
                result = self.request(worker, client, directory)
            self.assertEqual(result, {"status": 403, "code": "live_access_denied", "reason": "protected_volume"})
            result = self.request(worker, client, directory, worker_id="other-worker")
            self.assertEqual((result["status"], result["reason"]), (400, "invalid_request"))
            result = self.request(worker, client, directory, config_revision="")
            self.assertEqual((result["status"], result["reason"]), (400, "invalid_request"))

    def test_native_junction_escape_is_not_listed_read_or_watched(self):
        if os.name != "nt":
            self.skipTest("Windows junction regression")
        with tempfile.TemporaryDirectory() as directory:
            root, outside = Path(directory, "root"), Path(directory, "outside")
            root.mkdir(); outside.mkdir()
            (outside / "private.bin").write_bytes(b"outside")
            junction = root / "escape"
            subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
            try:
                worker, client, docker = self.make_worker()
                result = self.request(worker, client, str(root))
                self.assertEqual((result["status"], result.get("entries")), (200, []))
                for operation, path in (("entries", "/escape"), ("file", "/escape/private.bin")):
                    self.assertEqual(self.request(worker, client, str(root), operation=operation, path=path)["status"], 400)
                with self.assertRaises(ValueError):
                    worker._resolve_live_source(str(junction))
                runtime = LiveFileRuntime(worker._resolve_live_source(str(root)), "s" * 32)
                self.assertEqual(runtime._watch_snapshot(), ({}, True))
                runtime.cancel()
                docker.containers.run.assert_not_called()
            finally:
                junction.rmdir()

    def _assert_check_to_open_junction_race_rejected(self, replace_root):
        if os.name != "nt":
            self.skipTest("Windows handle confinement regression")
        with tempfile.TemporaryDirectory() as directory:
            root, outside = Path(directory, "root"), Path(directory, "root-outside")
            nested = root / "nested"
            nested.mkdir(parents=True)
            outside.mkdir()
            filename = "informe-Íñigo-漢.bin"
            (nested / filename).write_bytes(b"inside")
            outside_file = outside / "nested" / filename if replace_root else outside / filename
            outside_file.parent.mkdir(exist_ok=True)
            outside_file.write_bytes(b"outside-synthetic\x00\xff\x1a\r\n")
            worker, _client, docker = self.make_worker()
            runtime = LiveFileRuntime(worker._resolve_live_source(str(root)), "s" * 32)
            replacement = root if replace_root else nested
            moved = replacement.with_name(replacement.name + "-original")
            original_open, original_read = os.open, os.read
            original_confined_path = live_file_helper.confined_path
            opened = []
            swapped = False

            def install_junction():
                nonlocal swapped
                replacement.rename(moved)
                subprocess.run(["cmd", "/c", "mklink", "/J", str(replacement), str(outside)], check=True, capture_output=True)
                swapped = True

            def swap_after_validation(root_arg, path_arg):
                candidate = original_confined_path(root_arg, path_arg)
                install_junction()
                return candidate

            def record_open(path, flags, *args, **kwargs):
                fd = original_open(path, flags, *args, **kwargs)
                opened.append(fd)
                return fd

            try:
                with patch("src.worker_agent.live_file_helper.confined_path", side_effect=swap_after_validation), patch(
                    "src.worker_agent.live_file_helper.os.open", side_effect=record_open
                ), patch(
                    "src.worker_agent.live_file_helper.os.read", wraps=original_read
                ) as read:
                    try:
                        returned = b"".join(runtime.read_file("/nested/" + filename))
                    except ValueError as error:
                        self.assertIn("escapes target root", str(error))
                    else:
                        self.fail(f"check-to-open race returned outside-root bytes: {returned!r}")
                self.assertTrue(swapped)
                self.assertEqual(len(opened), 1)
                read.assert_not_called()
                with self.assertRaises(OSError):
                    os.fstat(opened[0])
                docker.containers.run.assert_not_called()
            finally:
                runtime.cancel()
                if swapped:
                    replacement.rmdir()

    def test_native_open_handle_rejects_parent_junction_swapped_after_validation(self):
        self._assert_check_to_open_junction_race_rejected(replace_root=False)

    def test_native_open_handle_rejects_root_junction_swapped_after_validation(self):
        self._assert_check_to_open_junction_race_rejected(replace_root=True)

    def test_native_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root, outside = Path(directory, "root"), Path(directory, "outside")
            root.mkdir(); outside.mkdir()
            (outside / "private.bin").write_bytes(b"outside")
            try:
                (root / "escape").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable to this user")
            worker, client, _docker = self.make_worker()
            self.assertEqual(self.request(worker, client, str(root))["entries"], [])
            self.assertEqual(self.request(worker, client, str(root), operation="file", path="/escape/private.bin")["status"], 400)

    def test_native_control_plane_descriptors_and_revision_use_configured_paths_only(self):
        service = browser_tests.LiveTargetFoundationTests.make_service()
        target = service.register_target("native", "worker-a")
        target.runtime_type = "native"
        target.filesystem_paths = [r"C:\Work\AFINIA", r"D:\Other\Data"]
        target.runtime_volumes = {"unrelated-volume": {"bind": "/data"}}
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.server = SimpleNamespace(application=SimpleNamespace(control_plane_service=service))
        self.assertEqual(handler._live_mount_source(target.id), [
            {"source": r"C:\Work\AFINIA", "folder": "C-Work-AFINIA"},
            {"source": r"D:\Other\Data", "folder": "D-Other-Data"},
        ])
        revision = service.get_live_target_revision(target.id)
        target.filesystem_paths = [r"C:\Work\Updated"]
        self.assertNotEqual(revision, service.get_live_target_revision(target.id))

    def test_native_authenticated_http_listing_download_and_revision_cursor(self):
        harness = browser_tests._LiveHttpHarness()
        try:
            harness.worker.stop()
            harness.target.runtime_type = "native"
            harness.target.filesystem_paths = [harness.root]
            harness.target.runtime_volumes = {}
            harness.target.volume_targets = []
            worker, _client, docker = self.make_worker()
            worker.control_plane_client = harness.worker.control_plane_client
            harness.worker._handle = lambda command: worker._process_live_request("worker-a", command)
            harness.worker.stop_event.clear()
            harness.worker.thread = threading.Thread(target=harness.worker._run, daemon=True)
            harness.worker.start()
            status, _, page = harness.json_request(harness.live_url("entries", "?limit=1"))
            self.assertEqual(status, 200, page)
            folder = page["entries"][0]["path"]
            status, _, page = harness.json_request(harness.live_url("entries", "?path=" + quote(folder) + "&limit=1"))
            self.assertEqual(status, 200, page)
            cursor = page["next_cursor"]
            status, _, data = harness.request(harness.live_url("file", "?path=" + quote(folder + "/binary.bin")))
            self.assertEqual((status, data), (200, b"\x00\xffraw\x01"))
            self.assertEqual(harness.request(harness.live_url("entries"), token=None)[0], 401)
            harness.target.filesystem_paths = [str(Path(harness.root, "different"))]
            status, _, _ = harness.request(harness.live_url("entries", "?path=" + quote(folder) + "&cursor=" + quote(cursor)))
            self.assertNotEqual(status, 200)
            docker.containers.run.assert_not_called()
            self.assertEqual(harness.service.job_repository.list(), [])
        finally:
            harness.close()


def byte_process(stdout, stderr, returncode, kwargs):
    process = Mock(returncode=returncode)
    process.wait.return_value = returncode
    for name, raw in (("stdout", stdout), ("stderr", stderr)):
        stream = io.BytesIO(raw)
        if kwargs.get("text"):
            stream = io.TextIOWrapper(stream, encoding=kwargs.get("encoding") or "cp1252", errors=kwargs.get("errors") or "strict")
        setattr(process, name, stream)
    return process


class NativeUtf8StreamingTests(unittest.TestCase):
    def test_real_native_pipes_drain_malformed_bytes_under_cp1252_without_deadlock(self):
        adapter = NativeRuntimeAdapter(timeout_seconds=5)
        callbacks = []
        script = (
            "import sys; "
            "sys.stdout.buffer.write(('informe-\\u00cd\\u00f1igo.txt\\n'.encode('utf-8') + b'invalid:\\x8d\\n') * 4096 + b'summary\\n'); "
            "sys.stderr.buffer.write(b'warning:\\x8d\\n' * 4096); sys.exit(3)"
        )
        with patch("subprocess._text_encoding", return_value="cp1252"):
            result = adapter._run_process([sys.executable, "-c", script], os.environ.copy(), {}, None, callbacks.append)
        self.assertEqual((result["success"], result["status_code"]), (False, 3))
        self.assertIn("informe-Íñigo.txt", "".join(callbacks))
        self.assertIn("invalid:\ufffd", "".join(callbacks))
        self.assertIn("summary", "".join(callbacks))
        self.assertNotIn("timed out", result.get("error", ""))

    def test_native_backup_engine_child_emits_utf8_even_with_cp1252_environment(self):
        adapter = NativeRuntimeAdapter()
        _command, environment = adapter._command_and_environment({
            "runtime_type": "native", "filesystem_paths": ["synthetic"], "command": "/root/backup.sh",
            "environment": {"PYTHONIOENCODING": "cp1252"},
        })
        script = "print('informe-\\u00cd\\u00f1igo-\\u6f22.txt')"
        result = adapter._run_process([sys.executable, "-c", script], environment, {}, None, None)
        self.assertTrue(result["success"], result)
        self.assertIn("informe-Íñigo-漢.txt", result["logs"])

    def test_restic_cp1252_default_preserves_unicode_malformed_output_summary_and_exit(self):
        status = json.dumps({"message_type": "status", "current_files": ["informe-Íñigo.txt"]}, ensure_ascii=False).encode("utf-8") + b"\n"
        summary = b'{"message_type":"summary","data_added":123,"total_duration":4.5}\n'
        for returncode in (0, 3):
            with self.subTest(returncode=returncode):
                callbacks = []
                processes = []
                def popen(_command, **kwargs):
                    process = byte_process(status + b"invalid:\x8d\n" + summary, b"warning:\x8d\n", returncode, kwargs)
                    processes.append(process)
                    return process
                with patch("src.app.infrastructure.adapters.backup_strategy._require_executable"), patch(
                    "src.app.infrastructure.adapters.backup_strategy.subprocess.Popen", side_effect=popen
                ), patch("src.app.infrastructure.adapters.backup_strategy.subprocess.run") as run:
                    result = ResticBackupStrategy().perform_backup(BackupConfig(source_paths=["synthetic"], restic_repository="synthetic", restic_password="synthetic"), output_callback=callbacks.append)
                self.assertIn("informe-Íñigo.txt", "".join(callbacks))
                self.assertIn("invalid:\ufffd", "".join(callbacks))
                self.assertEqual(result.success, returncode == 0)
                if returncode == 0:
                    self.assertEqual((result.size, result.duration), (123, 4.5))
                    self.assertEqual(run.call_args_list[-1].args[0][1], "forget")
                else:
                    self.assertEqual(run.call_count, 1)
                self.assertTrue(processes[0].stdout.buffer.tell() > 0)

    def test_native_cp1252_default_preserves_unicode_redaction_and_failure_status(self):
        callbacks = []
        secret = "synthetic-secret"
        payload = {"runtime_type": "native", "filesystem_paths": ["synthetic"], "command": ["restic", "snapshots", "--json"], "environment": {"RESTIC_PASSWORD": secret}}
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="restic"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen",
            side_effect=lambda _command, **kwargs: byte_process("informe-Íñigo.txt\n".encode("utf-8") + b"invalid:\x8d\nsummary\n", f"failure {secret}\n".encode() + b"invalid:\x8d\n", 3, kwargs),
        ):
            result = NativeRuntimeAdapter().run_runtime_job("unused", payload, output_callback=callbacks.append)
        self.assertEqual((result["success"], result["status_code"]), (False, 3))
        self.assertIn("informe-Íñigo.txt", result["logs"])
        self.assertIn("summary", result["logs"])
        self.assertIn("invalid:\ufffd", result["logs"])
        self.assertNotIn(secret, repr(result) + "".join(callbacks))
        self.assertIn("<redacted>", result["stderr"])

    def test_native_binary_snapshot_pipe_is_never_decoded(self):
        data = bytes(range(256)) + b"\r\n\x8d\xff"
        payload = {"runtime_type": "native", "filesystem_paths": ["synthetic"], "command": ["restic", "dump", "a" * 64, "/binary.bin"]}
        with patch("src.worker_agent.infrastructure.adapters.native_runtime.shutil.which", return_value="restic"), patch(
            "src.worker_agent.infrastructure.adapters.native_runtime.subprocess.Popen",
            side_effect=lambda _command, **kwargs: byte_process(data, b"warning:\x8d", 0, kwargs),
        ):
            result = NativeRuntimeAdapter().run_runtime_job_binary("unused", payload)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["stdout_bytes"], data)
