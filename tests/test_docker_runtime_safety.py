import contextlib
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from src.control_plane.domain.models import JobStatus
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.security.execution_log_capture import (
    ExecutionLogCapture,
    ExecutionLogCaptureUnavailable,
)
from src.worker_agent.infrastructure.security.execution_log_recipe_stream import (
    encode_recipe_frame,
)
from src.worker_agent.infrastructure.security.job_recovery_journal import WorkerJobRecoveryJournal


class DockerRuntimeSafetyTests(unittest.TestCase):
    @staticmethod
    def container(logs=b"ok", attach_output=None):
        container = Mock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.return_value = logs
        container.attach.return_value = logs if attach_output is None else attach_output
        return container

    def runtime(self, container, no_lock=False, cache_dir=None):
        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.client.containers.run.return_value = container
        runtime.timeout_seconds = 30.0
        runtime.no_lock = no_lock
        runtime.cache_dir = cache_dir
        return runtime

    def test_shell_metacharacters_and_non_allowlisted_argv_fail_closed(self):
        runtime = self.runtime(self.container())

        shell_result = runtime.run_runtime_job(
            "runtime",
            {"command": ["restic", "snapshots", "--json", "$(touch", "/tmp/pwned)"]},
        )
        executable_result = runtime.run_runtime_job(
            "runtime",
            {"command": ["python", "-c", "dangerous"]},
        )

        self.assertFalse(shell_result["success"])
        self.assertIn("shell metacharacters", shell_result["error"])
        self.assertFalse(executable_result["success"])
        self.assertIn("unsupported", executable_result["error"])
        runtime.client.containers.run.assert_not_called()

    def test_no_lock_is_added_only_to_read_operations(self):
        read_runtime = self.runtime(self.container(logs=b"[]"), no_lock=True)
        result = read_runtime.run_runtime_job(
            "runtime",
            {"command": "restic snapshots --json"},
        )

        self.assertTrue(result["success"])
        read_command = read_runtime.client.containers.run.call_args.kwargs["command"]
        self.assertEqual(read_command, ["restic", "snapshots", "--json", "--no-lock"])

        tree_runtime = self.runtime(self.container(logs=b'{"nodes":[]}'), no_lock=True)
        tree_result = tree_runtime.run_runtime_job(
            "runtime",
            {"command": ["restic", "cat", "tree", "abcdef12:/packages"]},
        )
        self.assertTrue(tree_result["success"])
        self.assertEqual(
            tree_runtime.client.containers.run.call_args.kwargs["command"],
            ["restic", "cat", "tree", "abcdef12:/packages", "--no-lock"],
        )

        for command in ("/root/backup.sh", "restic forget --keep-last 1", "restic prune"):
            write_runtime = self.runtime(self.container(), no_lock=True)
            write_result = write_runtime.run_runtime_job("runtime", {"command": command})
            self.assertTrue(write_result["success"])
            write_command = write_runtime.client.containers.run.call_args.kwargs["command"]
            self.assertNotIn("--no-lock", write_command)

    def test_snapshot_about_allows_only_scoped_restore_size_stats_argv(self):
        admitted = DockerRuntimeAdapter._runtime_command_argv(
            ["restic", "stats", "--mode", "restore-size", "--json", "abcdef12"]
        )
        self.assertEqual(
            admitted,
            ["restic", "stats", "--mode", "restore-size", "--json", "abcdef12"],
        )
        self.assertTrue(
            DockerRuntimeAdapter._is_snapshot_metadata_command(
                ["restic", "stats", "--mode", "restore-size", "--json", "abcdef12", "--no-lock"]
            )
        )

        for variant in (
            ["restic", "stats", "--mode", "raw-data", "--json", "abcdef12"],
            ["restic", "stats", "--mode", "blobs-per-file", "--json", "abcdef12"],
            ["restic", "stats", "--mode", "restore-size", "--json"],
            ["restic", "stats", "--mode", "restore-size", "--json", "abcdef12", "extra"],
            ["restic", "stats", "--json", "--mode", "restore-size", "abcdef12"],
            ["restic", "stats", "--mode", "restore-size", "--json", "not-a-snapshot"],
        ):
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                DockerRuntimeAdapter._runtime_command_argv(variant)

        runtime = self.runtime(
            self.container(
                logs=json.dumps(
                    {"total_size": 4096, "total_file_count": 7, "snapshots_count": 1}
                ).encode("utf-8")
            )
        )
        result = runtime.get_restic_snapshot_stats(
            "runtime",
            {
                "command": admitted,
                "snapshot_id": "abcdef12",
            },
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            result["stats"],
            {"total_size": 4096, "total_file_count": 7, "snapshots_count": 1},
        )
        self.assertEqual(
            runtime.client.containers.run.call_args.kwargs["command"],
            admitted,
        )

    def test_target_stats_runs_both_exact_read_only_modes_and_keeps_legacy_stats(self):
        raw_stats = {
            "total_size": 4096,
            "total_uncompressed_size": 8192,
            "unique_size": 3072,
            "snapshots_count": 2,
        }
        blobs_stats = {
            "total_size": 2048,
            "total_file_count": 7,
            "total_blob_count": 9,
        }
        raw_container = self.container(logs=json.dumps(raw_stats).encode("utf-8"))
        blobs_container = self.container(logs=json.dumps(blobs_stats).encode("utf-8"))
        runtime = self.runtime(raw_container)
        runtime.client.containers.run.side_effect = [raw_container, blobs_container]

        blob_argv = ["restic", "stats", "--mode", "blobs-per-file", "--json"]
        self.assertEqual(DockerRuntimeAdapter._runtime_command_argv(blob_argv), blob_argv)
        self.assertTrue(DockerRuntimeAdapter._is_read_only_argv(blob_argv))

        result = runtime.get_restic_stats(
            "runtime",
            {
                "command": ["restic", "stats", "--mode", "raw-data", "--json"],
                "stats_modes": ["raw-data", "blobs-per-file"],
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["stats"], raw_stats)
        self.assertEqual(result["stats_by_mode"], {"raw-data": raw_stats, "blobs-per-file": blobs_stats})
        self.assertEqual(
            [call.kwargs["command"] for call in runtime.client.containers.run.call_args_list],
            [
                ["restic", "stats", "--mode", "raw-data", "--json"],
                blob_argv,
            ],
        )

    def test_target_stats_rejects_unsupported_modes_and_fails_closed_on_malformed_second_mode(self):
        for mode in ("restore-size", "raw-data --json", "unknown"):
            with self.subTest(mode=mode):
                runtime = self.runtime(self.container())
                result = runtime.get_restic_stats("runtime", {"stats_modes": [mode]})
                self.assertFalse(result["success"])
                self.assertEqual(result["stats"], {})
                self.assertEqual(result["stats_by_mode"], {})
                runtime.client.containers.run.assert_not_called()

        runtime = self.runtime(self.container(logs=json.dumps({"total_size": 1}).encode("utf-8")))
        runtime.client.containers.run.side_effect = [
            self.container(logs=json.dumps({"total_size": 1}).encode("utf-8")),
            self.container(logs=b"not-json"),
        ]
        result = runtime.get_restic_stats(
            "runtime",
            {"stats_modes": ["raw-data", "blobs-per-file"]},
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["stats"], {})
        self.assertEqual(result["stats_by_mode"], {})
        self.assertEqual(len(runtime.client.containers.run.call_args_list), 2)

    def test_snapshot_about_stats_parser_rejects_unbounded_or_malformed_output(self):
        for logs in (
            b"not-json",
            b'{"total_size": 1, "total_file_count": 2}',
            b'{"total_size": -1, "total_file_count": 2, "snapshots_count": 1}',
            b'{"total_size": 1, "total_file_count": "2", "snapshots_count": 1}',
        ):
            with self.subTest(logs=logs):
                runtime = self.runtime(self.container(logs=logs))
                result = runtime.get_restic_snapshot_stats(
                    "runtime",
                    {
                        "command": [
                            "restic",
                            "stats",
                            "--mode",
                            "restore-size",
                            "--json",
                            "abcdef12",
                        ],
                        "snapshot_id": "abcdef12",
                    },
                )
                self.assertFalse(result["success"])
                self.assertEqual(result["stats"], {})

    def test_rclone_about_argv_is_admitted_and_variants_fail_closed(self):
        admitted = DockerRuntimeAdapter._runtime_command_argv(["rclone", "about", "rem:", "--json"])
        self.assertEqual(admitted, ["rclone", "about", "rem:", "--json"])

        variants = (
            ["rclone", "lsjson", "rem:", "--json"],
            ["rclone", "about", "rem:", "--json", "--extra"],
            ["rclone", "about", "rem:"],
            ["rclone", "about", "rem: --json"],
            ["rclone", "about", "rem:/path", "--json"],
            ["rclone", "about", "rem", "--json"],
            ["rclone", "about", "rem:$(touch /tmp/pwned)", "--json"],
            ["rclone", "about", "..", "--json"],
            ["rclone", "about", "not a remote:", "--json"],
        )
        for variant in variants:
            with self.subTest(variant=variant):
                with self.assertRaises(ValueError):
                    DockerRuntimeAdapter._runtime_command_argv(variant)

        invalid_runtime = self.runtime(self.container())
        invalid_result = invalid_runtime.run_runtime_job(
            "runtime",
            {"command": ["rclone", "about", "rem:/path", "--json"]},
        )
        self.assertFalse(invalid_result["success"])
        invalid_runtime.client.containers.run.assert_not_called()

    def test_rclone_about_runs_admitted_argv_without_shell(self):
        runtime = self.runtime(self.container(logs=b'{"total":1,"used":2,"free":3,"trashed":0}'))

        result = runtime.run_runtime_job(
            "runtime",
            {"command": ["rclone", "about", "rem:", "--json"]},
        )

        self.assertTrue(result["success"])
        command = runtime.client.containers.run.call_args.kwargs["command"]
        self.assertEqual(command, ["rclone", "about", "rem:", "--json"])
        self.assertNotIn("/bin/sh", " ".join(command))

    def test_collect_inventory_keeps_container_when_image_is_missing(self):
        class MissingImage:
            @property
            def tags(self):
                raise RuntimeError(
                    '404 Client Error for http+docker://localhost/images/sha256:missing: '
                    'Not Found ("No such image: sha256:missing")'
                )

        container = Mock()
        container.id = "container-1"
        container.name = "compose-web-1"
        container.status = "exited"
        container.labels = {
            "com.docker.compose.project": "compose-project",
            "com.docker.compose.service": "web",
        }
        container.attrs = {"Mounts": []}
        container.image = MissingImage()

        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.client.containers.list.return_value = [container]
        runtime.client.volumes.list.return_value = []
        runtime.client.networks.list.return_value = []
        runtime.client.info.return_value = {}

        inventory = runtime.collect_inventory()

        self.assertEqual(inventory["containers"][0]["image"], [])
        self.assertEqual(inventory["compose_projects"], ["compose-project"])
        self.assertEqual(
            inventory["compose_project_details"][0]["containers"][0]["id"],
            "container-1",
        )

    def test_collect_inventory_keeps_shared_named_sources_separate_and_marks_generated_mounts(self):
        named_data = "discourse_discourse_data"
        named_sidekiq = "discourse_discourse_sidekiq_data"
        generated = "a" * 64

        def make_volume(name, labels):
            volume = Mock()
            volume.name = name
            volume.attrs = {"Labels": labels}
            return volume

        def make_container(container_id, name, service, mounts):
            container = Mock()
            container.id = container_id
            container.name = name
            container.status = "running"
            container.labels = {
                "com.docker.compose.project": "discourse",
                "com.docker.compose.service": service,
            }
            container.attrs = {"Mounts": mounts}
            container.image = Mock(tags=["bitnami/discourse:latest"])
            return container

        web = make_container(
            "container-web",
            "discourse-web-1",
            "web",
            [
                {
                    "Type": "volume",
                    "Name": named_data,
                    "Source": f"/var/lib/docker/volumes/{named_data}/_data",
                    "Destination": "/bitnami/discourse",
                    "RW": True,
                },
                {
                    "Type": "volume",
                    "Name": generated,
                    "Source": f"/var/lib/docker/volumes/{generated}/_data",
                    "Destination": "/docker-entrypoint-initdb.d",
                    "RW": False,
                },
                {
                    "Type": "bind",
                    "Source": "/srv/discourse/config",
                    "Destination": "/bitnami/config",
                    "RW": True,
                },
            ],
        )
        sidekiq = make_container(
            "container-sidekiq",
            "discourse-sidekiq-1",
            "sidekiq",
            [
                {
                    "Type": "volume",
                    "Name": named_sidekiq,
                    "Source": f"/var/lib/docker/volumes/{named_sidekiq}/_data",
                    "Destination": "/bitnami/discourse",
                    "RW": True,
                },
            ],
        )

        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.client.containers.list.return_value = [web, sidekiq]
        runtime.client.volumes.list.return_value = [
            make_volume(
                named_data,
                {
                    "com.docker.compose.project": "discourse",
                    "com.docker.compose.volume": "discourse_data",
                },
            ),
            make_volume(
                named_sidekiq,
                {
                    "com.docker.compose.project": "discourse",
                    "com.docker.compose.volume": "discourse_sidekiq_data",
                },
            ),
            make_volume(generated, {}),
        ]
        runtime.client.networks.list.return_value = []
        runtime.client.info.return_value = {}

        inventory = runtime.collect_inventory()

        project = inventory["compose_project_details"][0]
        candidates = {candidate["source"]: candidate for candidate in project["volume_candidates"]}
        self.assertEqual(
            candidates[named_data]["bind"],
            "/bitnami/discourse",
        )
        self.assertEqual(candidates[named_data]["name"], "discourse_data")
        self.assertEqual(candidates[named_data]["services"], ["web"])
        self.assertEqual(candidates[named_data]["containers"], ["discourse-web-1"])
        self.assertEqual(candidates[named_sidekiq]["name"], "discourse_sidekiq_data")
        self.assertEqual(candidates[named_sidekiq]["services"], ["sidekiq"])
        self.assertFalse(candidates[named_data]["anonymous"])
        self.assertTrue(candidates[generated]["anonymous"])
        self.assertEqual(candidates[generated]["bind"], "/docker-entrypoint-initdb.d")
        self.assertFalse(candidates["/srv/discourse/config"]["anonymous"])
        self.assertEqual(candidates["/srv/discourse/config"]["mount_type"], "bind")

    def test_collect_inventory_propagates_unrelated_image_errors(self):
        class BrokenImage:
            @property
            def tags(self):
                raise RuntimeError("404 Client Error: Not Found")

        container = Mock()
        container.id = "container-1"
        container.name = "compose-web-1"
        container.status = "exited"
        container.labels = {}
        container.attrs = {"Mounts": []}
        container.image = BrokenImage()

        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.client.containers.list.return_value = [container]
        runtime.client.volumes.list.return_value = []
        runtime.client.networks.list.return_value = []
        runtime.client.info.return_value = {}

        with self.assertRaisesRegex(RuntimeError, "404 Client Error"):
            runtime.collect_inventory()

    def test_snapshot_path_id_and_target_scope_are_validated_before_launch(self):
        cases = (
            {"command": "restic ls --json not-a-restic-id /"},
            {"command": "restic ls --json abcdef12 /../secret"},
            {"command": "restic cat tree not-a-restic-id"},
            {"command": "restic cat tree abcdef12:relative"},
            {"command": "restic cat tree abcdef12:/"},
            {"command": ["restic", "cat", "tree", "abcdef12:/safe", "extra"]},
            {
                "command": "restic ls --json abcdef12 /",
                "target_id": "target-a",
                "snapshot_target_id": "target-b",
            },
            {"command": "restic ls --json abcdef12 /", "path": "/safe\x00path"},
        )

        for payload in cases:
            runtime = self.runtime(self.container())
            result = runtime.run_runtime_job("runtime", payload)
            self.assertFalse(result["success"], payload)
            runtime.client.containers.run.assert_not_called()

        with self.assertRaisesRegex(ValueError, "traversal"):
            DockerRuntimeAdapter.normalize_snapshot_path("/safe/../secret")

    def test_cat_tree_allowlist_normalizes_safe_targets_and_rejects_variants(self):
        self.assertEqual(
            DockerRuntimeAdapter._runtime_command_argv(["restic", "cat", "tree", "abcdef12"]),
            ["restic", "cat", "tree", "abcdef12"],
        )
        self.assertEqual(
            DockerRuntimeAdapter._runtime_command_argv(["restic", "cat", "tree", "abcdef12:/packages/"]),
            ["restic", "cat", "tree", "abcdef12:/packages"],
        )
        self.assertTrue(
            DockerRuntimeAdapter._is_snapshot_metadata_command(
                ["restic", "--cache-dir", DockerRuntimeAdapter.CACHE_MOUNT_PATH, "cat", "tree", "abcdef12:/packages", "--no-lock"]
            )
        )
        for command in (
            ["restic", "cat", "tree", "abcdef12:/"],
            ["restic", "cat", "tree", "abcdef12:relative"],
            ["restic", "cat", "tree", "abcdef12:/safe", "--json"],
            ["restic", "cat", "blob", "abcdef12"],
            ["restic", "cat", "tree", "abcdef12:/safe/../secret"],
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                DockerRuntimeAdapter._runtime_command_argv(command)

    @staticmethod
    def clock(start: float, step: float):
        value = start - step

        def read() -> float:
            nonlocal value
            value += step
            return value

        return read

    def test_timeout_stops_then_removes_only_the_request_container(self):
        container = self.container()
        container.wait.side_effect = TimeoutError("container wait timed out")
        runtime = self.runtime(container)
        runtime._monotonic = self.clock(100.0, 0.6)

        result = runtime.run_runtime_job(
            "runtime",
            {"command": "/root/backup.sh", "timeout_seconds": 1},
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 124)
        self.assertEqual(result["error"], "runtime timed out after 1 seconds")
        container.stop.assert_called_once_with(timeout=1)
        container.remove.assert_called_once_with(force=True)

    def test_restore_keeps_its_default_wall_clock_while_backups_have_none(self):
        runtime = self.runtime(self.container())
        runtime.timeout_seconds = None
        restore_timeout = runtime._prepare_runtime(
            {"command": "/root/backup.sh", "environment": {"RESTORE_MODE": "true"}}, binary=False
        )[4]
        backup_timeout = runtime._prepare_runtime({"command": "/root/backup.sh"}, binary=False)[4]

        self.assertEqual(restore_timeout, DockerRuntimeAdapter.DEFAULT_RESTORE_RUNTIME_TIMEOUT_SECONDS)
        self.assertIsNone(backup_timeout)

        with patch.dict(os.environ, {"WORKER_RUNTIME_TIMEOUT_SECONDS": "", "WORKER_RUNTIME_IDLE_TIMEOUT_SECONDS": "0"}), patch(
            "src.worker_agent.infrastructure.adapters.docker_runtime.docker", None
        ):
            adapter = DockerRuntimeAdapter()
        self.assertIsNone(adapter.timeout_seconds)
        self.assertIsNone(adapter.idle_timeout_seconds)
        with patch.dict(os.environ, {"WORKER_RUNTIME_TIMEOUT_SECONDS": "1200", "WORKER_RUNTIME_IDLE_TIMEOUT_SECONDS": "5"}), patch(
            "src.worker_agent.infrastructure.adapters.docker_runtime.docker", None
        ):
            adapter = DockerRuntimeAdapter()
        self.assertEqual(adapter.timeout_seconds, "1200")
        self.assertEqual(adapter._prepare_runtime({"command": "/root/backup.sh"}, binary=False)[4], 1200.0)
        self.assertEqual(adapter.idle_timeout_seconds, DockerRuntimeAdapter.MIN_IDLE_TIMEOUT_SECONDS)

    def test_streaming_container_outlives_the_old_wall_clock_without_an_explicit_timeout(self):
        polls = {"remaining": 10}
        delivered = threading.Event()
        released = threading.Event()

        class Stream:
            def __iter__(self):
                while polls["remaining"] > 0:
                    yield b"status\n"
                    delivered.set()
                    released.wait(5)
                    released.clear()

        container = self.container()
        container.logs.return_value = Stream()

        def wait(timeout=None):
            if polls["remaining"] <= 0:
                return {"StatusCode": 0}
            self.assertTrue(delivered.wait(5))
            delivered.clear()
            polls["remaining"] -= 1
            released.set()
            raise TimeoutError("container wait timed out")

        container.wait.side_effect = wait
        runtime = self.runtime(container)
        runtime.timeout_seconds = None
        runtime._monotonic = self.clock(1000.0, 600.0)

        result = runtime.run_runtime_job("runtime", {"command": "/root/backup.sh"})

        self.assertTrue(result["success"], result)
        container.stop.assert_not_called()
        container.remove.assert_called_once_with(force=True)

    def test_silent_container_is_stopped_by_the_idle_watchdog_with_an_honest_message(self):
        container = self.container()
        container.logs.return_value = iter([])
        container.wait.side_effect = TimeoutError("container wait timed out")
        runtime = self.runtime(container)
        runtime.timeout_seconds = None
        runtime._monotonic = self.clock(1000.0, 600.0)

        result = runtime.run_runtime_job("runtime", {"command": "/root/backup.sh"})

        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 124)
        self.assertEqual(result["error"], "runtime produced no output for 900 seconds")
        container.stop.assert_called_once_with(timeout=1)
        container.remove.assert_called_once_with(force=True)

    def test_binary_jobs_have_no_idle_watchdog_but_honor_explicit_timeouts(self):
        container = self.container(attach_output=(b"payload", b""))
        container.wait.side_effect = TimeoutError("container wait timed out")
        runtime = self.runtime(container)
        runtime.timeout_seconds = None
        runtime._monotonic = self.clock(1000.0, 1800.0)
        polls = iter(range(3))

        def wait(timeout=None):
            if next(polls, None) is None:
                return {"StatusCode": 0}
            raise TimeoutError("container wait timed out")

        container.wait.side_effect = wait
        result = runtime.run_runtime_job_binary("runtime", {"command": "restic dump abcdef12 /file"})
        self.assertTrue(result["success"], result)

        bounded = self.container(attach_output=(b"payload", b""))
        bounded.wait.side_effect = TimeoutError("container wait timed out")
        bounded_runtime = self.runtime(bounded)
        bounded_runtime._monotonic = self.clock(1000.0, 30.0)
        result = bounded_runtime.run_runtime_job_binary("runtime", {"command": "restic dump abcdef12 /file", "timeout_seconds": 20})
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "runtime timed out after 20 seconds")

    def test_restore_timeout_can_be_overridden_but_remains_bounded(self):
        runtime = self.runtime(self.container())
        payload = {
            "command": "/root/backup.sh",
            "environment": {"RESTORE_MODE": "true"},
            "timeout_seconds": 7200,
        }
        result = runtime.run_runtime_job("runtime", payload)

        self.assertTrue(result["success"])
        self.assertEqual(runtime._prepare_runtime(payload, binary=False)[4], 7200.0)

        for invalid in (True, 0, DockerRuntimeAdapter.MAX_RUNTIME_TIMEOUT_SECONDS + 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                runtime._prepare_runtime(
                    {
                        "command": "/root/backup.sh",
                        "environment": {"RESTORE_MODE": "true"},
                        "timeout_seconds": invalid,
                    },
                    binary=False,
                )

    def test_restore_timeout_keeps_cancellation_polling(self):
        container = self.container()
        container.wait.side_effect = TimeoutError("wait timed out")
        runtime = self.runtime(container)
        checks = iter((False, False, True))

        result = runtime.run_runtime_job(
            "runtime",
            {
                "command": "/root/backup.sh",
                "environment": {"RESTORE_MODE": "true"},
            },
            cancel_check=lambda: next(checks),
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["canceled"])
        self.assertEqual(result["status_code"], 130)
        container.stop.assert_called_once_with(timeout=1)
        container.remove.assert_called_once_with(force=True)

    def test_cancellation_stops_container_and_returns_canceled_without_leak(self):
        container = self.container()
        container.wait.side_effect = TimeoutError("wait timed out")
        runtime = self.runtime(container)
        checks = iter((False, False, True))

        result = runtime.run_runtime_job(
            "runtime",
            {"command": "/root/backup.sh", "timeout_seconds": 2},
            cancel_check=lambda: next(checks),
        )

        self.assertFalse(result["success"])
        self.assertTrue(result["canceled"])
        self.assertEqual(result["status_code"], 130)
        container.stop.assert_called_once_with(timeout=1)
        container.remove.assert_called_once_with(force=True)

    def test_repository_and_secret_values_are_redacted_from_logs_and_errors(self):
        repository = "https://user:repo-password@example.invalid/backups"
        secret = "restic-password-value"
        container = self.container(
            f"repository={repository} password={secret}".encode("utf-8")
        )
        runtime = self.runtime(container)
        payload = {
            "command": "restic snapshots --json",
            "environment": {
                "RESTIC_REPOSITORY": repository,
                "RESTIC_PASSWORD": secret,
            },
        }

        result = runtime.run_runtime_job("runtime", payload)

        self.assertTrue(result["success"])
        self.assertNotIn(repository, repr(result))
        self.assertNotIn(secret, repr(result))
        self.assertIn("<redacted", result["logs"])

        failing = self.runtime(self.container())
        failing.client.containers.run.side_effect = RuntimeError(f"cannot open {repository}: {secret}")
        error = failing.run_runtime_job("runtime", payload)
        self.assertNotIn(repository, repr(error))
        self.assertNotIn(secret, repr(error))

    def test_cache_path_isolated_by_target_and_repository_fingerprint(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            runtime = self.runtime(self.container(logs=b"[]"), cache_dir=cache_dir)
            payload = {
                "target_id": "target-a",
                "command": "restic snapshots --json",
                "environment": {"RESTIC_REPOSITORY": "https://example.invalid/repo-a"},
            }
            runtime.run_runtime_job("runtime", payload)
            first = runtime.client.containers.run.call_args.kwargs["volumes"]
            first_source = next(source for source, spec in first.items() if spec["bind"] == runtime.CACHE_MOUNT_PATH)
            first_command = runtime.client.containers.run.call_args.kwargs["command"]

            runtime.client.containers.run.reset_mock()
            runtime.run_runtime_job(
                "runtime",
                {
                    **payload,
                    "target_id": "target-b",
                    "environment": {"RESTIC_REPOSITORY": "https://example.invalid/repo-b"},
                },
            )
            second = runtime.client.containers.run.call_args.kwargs["volumes"]
            second_source = next(source for source, spec in second.items() if spec["bind"] == runtime.CACHE_MOUNT_PATH)

            self.assertNotEqual(first_source, second_source)
            self.assertTrue(first_source.endswith(os.path.join("target-a", DockerRuntimeAdapter.repository_fingerprint("https://example.invalid/repo-a"))))
            self.assertNotIn("https://example.invalid/repo-a", first_source)
            self.assertIn("--cache-dir", first_command)

    def test_orphan_sweep_only_removes_stopped_marked_runtime_containers(self):
        runtime = self.runtime(self.container())

        marked = self.container()
        marked.id = "marked-container"
        marked.status = "exited"
        marked.labels = {
            DockerRuntimeAdapter.RUNTIME_TEMPORARY_LABEL: "true",
            DockerRuntimeAdapter.RUNTIME_JOB_ID_LABEL: "job-1",
        }
        unmarked = self.container()
        unmarked.id = "unmarked-container"
        unmarked.status = "exited"
        unmarked.labels = {"user.container": "true"}
        running = self.container()
        running.id = "running-container"
        running.status = "running"
        running.labels = {
            DockerRuntimeAdapter.RUNTIME_TEMPORARY_LABEL: "true",
            DockerRuntimeAdapter.RUNTIME_JOB_ID_LABEL: "job-2",
        }
        runtime.client.containers.list.return_value = [marked, unmarked, running]

        result = runtime.cleanup_orphaned_runtime_containers()

        runtime.client.containers.list.assert_called_once_with(
            all=True,
            filters={"label": f"{DockerRuntimeAdapter.RUNTIME_TEMPORARY_LABEL}=true"},
        )
        self.assertEqual(result["inspected"], 3)
        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["skipped"], 2)
        marked.stop.assert_not_called()
        marked.remove.assert_called_once_with(force=True)
        unmarked.remove.assert_not_called()
        running.remove.assert_not_called()
        self.assertEqual(result["removed_ids"], ["marked-container"])

    def test_dump_zip_output_is_allowlisted_and_bounded(self):
        small = self.runtime(self.container(attach_output=b"zip-content"))
        result = small.run_runtime_job_binary(
            "runtime",
            {
                "command": "restic dump -a zip abcdef12 /directory",
                "max_output_bytes": 32,
            },
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["stdout_bytes"], b"zip-content")
        small.client.containers.run.return_value.logs.assert_not_called()
        small.client.containers.run.return_value.attach.assert_called_once_with(
            stdout=True,
            stderr=True,
            stream=False,
            logs=True,
            demux=True,
        )

        large = self.runtime(self.container(attach_output=b"0123456789"))
        result = large.run_runtime_job_binary(
            "runtime",
            {
                "command": "restic dump -a zip abcdef12 /directory",
                "max_output_bytes": 4,
            },
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 413)
        self.assertEqual(result["stdout_bytes"], b"")

    def test_binary_dump_preserves_non_text_stdout_and_redacts_demuxed_stderr(self):
        secret = "runtime-secret"
        png = b"\x89PNG\r\n\x1a\n\x00\xff\x80image-data"
        container = self.container(
            attach_output=(png, f"restic failed: {secret}".encode("utf-8"))
        )
        runtime = self.runtime(container)

        result = runtime.run_runtime_job_binary(
            "runtime",
            {
                "command": "restic dump abcdef12 /image.png",
                "environment": {"RESTIC_PASSWORD": secret},
            },
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["stdout_bytes"], png)
        self.assertIn("<redacted>", result["stderr"])
        self.assertNotIn(secret, result["stderr"])
        container.logs.assert_not_called()

    def test_binary_dump_stderr_limit_fails_closed_without_log_fallback(self):
        container = self.container(attach_output=(b"png", b"12345"))
        runtime = self.runtime(container)

        result = runtime.run_runtime_job_binary(
            "runtime",
            {
                "command": "restic dump abcdef12 /image.png",
                "max_log_bytes": 4,
            },
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 413)
        self.assertEqual(result["stdout_bytes"], b"")
        container.logs.assert_not_called()

    def test_binary_dump_attach_failure_does_not_fallback_to_logs(self):
        container = self.container(attach_output=b"should-not-be-used")
        container.attach.side_effect = RuntimeError("raw capture unavailable")
        runtime = self.runtime(container)

        result = runtime.run_runtime_job_binary(
            "runtime",
            {"command": "restic dump abcdef12 /image.png"},
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["stdout_bytes"], b"")
        self.assertIn("raw capture unavailable", result["error"])
        container.logs.assert_not_called()

    def test_snapshot_metadata_log_limit_can_exceed_generic_default_but_stays_bounded(self):
        over_default = b"x" * (DockerRuntimeAdapter.MAX_LOG_BYTES + 1)
        metadata = self.runtime(self.container(logs=over_default))
        result = metadata.run_runtime_job(
            "runtime",
            {
                "command": "restic snapshots --json",
                "max_log_bytes": 8 * 1024 * 1024,
            },
        )
        self.assertTrue(result["success"])
        self.assertEqual(len(result["logs"]), len(over_default))

        generic = self.runtime(self.container(logs=over_default))
        result = generic.run_runtime_job("runtime", {"command": "/root/backup.sh"})
        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 413)

        over_configured = self.runtime(
            self.container(logs=b"x" * (8 * 1024 * 1024 + 1))
        )
        result = over_configured.run_runtime_job(
            "runtime",
            {
                "command": "restic snapshots --json",
                "max_log_bytes": 8 * 1024 * 1024,
            },
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["status_code"], 413)

        rejected = self.runtime(self.container())
        result = rejected.run_runtime_job(
            "runtime",
            {
                "command": "restic snapshots --json",
                "max_log_bytes": DockerRuntimeAdapter.MAX_SNAPSHOT_METADATA_LOG_BYTES + 1,
            },
        )
        self.assertFalse(result["success"])
        self.assertIn("outside the permitted bounds", result["error"])
        rejected.client.containers.run.assert_not_called()

        tree = self.runtime(self.container(logs=b"x" * (DockerRuntimeAdapter.MAX_LOG_BYTES + 1)))
        tree_result = tree.run_runtime_job(
            "runtime",
            {
                "command": ["restic", "cat", "tree", "abcdef12"],
                "max_log_bytes": 8 * 1024 * 1024,
            },
        )
        self.assertTrue(tree_result["success"])
        self.assertEqual(len(tree_result["logs"]), DockerRuntimeAdapter.MAX_LOG_BYTES + 1)


class WorkerReadPathTests(unittest.TestCase):
    def test_snapshot_parser_is_bounded(self):
        entries = "\n".join(
            '{"struct_type":"node","type":"file","path":"/file-%s"}' % index
            for index in range(50)
        )
        parsed = WorkerAgentService._parse_snapshot_ls_entries(entries, max_entries=7)
        self.assertEqual(len(parsed), 7)
        self.assertEqual(parsed[0]["path"], "/file-0")

    def test_snapshot_parser_uses_configured_metadata_log_limit(self):
        entry = '{"struct_type":"node","type":"file","path":"/after-limit"}'
        logs = "x" * (WorkerAgentService.MAX_LOG_CHARS + 16) + "\n" + entry

        parsed = WorkerAgentService._parse_snapshot_ls_entries(
            logs,
            max_log_bytes=8 * 1024 * 1024,
        )

        self.assertEqual(parsed, [{"struct_type": "node", "type": "file", "path": "/after-limit"}])
        with self.assertRaises(ValueError):
            WorkerAgentService._parse_snapshot_ls_entries(
                entry,
                max_log_bytes=WorkerAgentService.MAX_SNAPSHOT_METADATA_LOG_CHARS + 1,
            )

    def test_storage_about_is_an_interactive_command(self):
        self.assertIn("storage.about", WorkerAgentService.INTERACTIVE_COMMANDS)

    def test_storage_about_success_returns_whitelisted_metrics(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": True,
            "status_code": 0,
            "logs": '{"total":100,"used":25,"free":75,"trashed":2}',
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {"command": "storage.about", "payload": {"remote": "rem:"}}
        )

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.result_summary["state"], "available")
        self.assertEqual(
            result.result_summary["metrics"],
            {"total": 100, "used": 25, "free": 75, "trashed": 2},
        )
        self.assertNotIn("error", result.result_summary)
        runtime.run_runtime_job.assert_called_once()
        launched_payload = runtime.run_runtime_job.call_args.kwargs["payload"]
        self.assertEqual(launched_payload["command"], ["rclone", "about", "rem:", "--json"])

    def test_storage_about_rejects_invalid_remote_without_launch(self):
        runtime = Mock()
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        for remote in ("", "rem", "not a remote", "..", "rem:/path", "rem:$(id)"):
            with self.subTest(remote=remote):
                result = service.execute_job(
                    {"command": "storage.about", "payload": {"remote": remote}}
                )
                self.assertEqual(result.status, JobStatus.FAILED)
                self.assertIn("invalid", result.result_summary["error"])
                runtime.run_runtime_job.assert_not_called()

    def test_storage_about_malformed_json_returns_safe_failure_summary(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": True,
            "status_code": 0,
            "logs": "not json at all",
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {"command": "storage.about", "payload": {"remote": "rem:"}}
        )

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.result_summary["state"], "transient-failure")
        self.assertIn("failed to parse", result.result_summary["error"])
        self.assertNotIn("metrics", result.result_summary)

    def test_storage_about_unsupported_remote_is_explicit_not_transient(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": False,
            "status_code": 1,
            "logs": "rclone about: about is not supported by this backend",
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {"command": "storage.about", "payload": {"remote": "rem:"}}
        )

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.result_summary["state"], "about-unsupported")
        self.assertNotIn("metrics", result.result_summary)
        self.assertNotIn("error", result.result_summary)

    def test_storage_about_exact_doesnt_support_contraction_is_about_unsupported(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": False,
            "status_code": 1,
            "logs": "S3 root doesn't support about",
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {"command": "storage.about", "payload": {"remote": "rem:"}}
        )

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertEqual(result.result_summary["state"], "about-unsupported")
        self.assertNotIn("metrics", result.result_summary)
        self.assertNotIn("error", result.result_summary)

    def test_storage_about_transient_failure_is_retryable_and_redacts_secrets(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": False,
            "status_code": 124,
            "logs": "rclone about timed out after about-secret",
            "stderr": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {
                "command": "storage.about",
                "payload": {"remote": "rem:", "environment": {"RCLONE_CONF_CONTENT": "about-secret"}},
            }
        )

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.result_summary["state"], "transient-failure")
        self.assertIn("timed out", result.result_summary["error"])
        self.assertNotIn("about-secret", repr(result))
        self.assertNotIn("about-secret", "\n".join(result.log_lines))

    def test_interactive_poll_falls_back_to_durable_fetch(self):
        class CredentialStore:
            def load(self):
                return object()

        class Client:
            credential_store = CredentialStore()

            def __init__(self):
                self.fetched = 0
                self.updated = []

            def fetch_jobs(self, worker_id):
                self.fetched += 1
                return [{"id": "job-1", "command": "snapshot.ls", "payload": {}, "lease_token": "lease"}]

            def update_job_status(self, **kwargs):
                self.updated.append(kwargs)
                return kwargs

        client = Client()
        runtime = Mock()
        runtime.run_runtime_job.return_value = {"success": True, "logs": '{"nodes":[]}', "stderr": ""}
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            client,
            runtime,
        )

        result = service.poll_interactive_once()

        self.assertEqual(len(result), 1)
        self.assertEqual(client.fetched, 1)
        self.assertEqual(client.updated[0]["status"], JobStatus.SUCCEEDED)

    def test_canceled_job_is_not_left_as_a_running_lease(self):
        class CancelRuntime:
            def run_runtime_job(self, image, payload, cancel_check=None):
                self.observed = bool(cancel_check and cancel_check())
                return {"success": False, "canceled": True, "status_code": 130, "logs": "", "stderr": ""}

        class Client:
            credential_store = None

            def update_job_status(self, **kwargs):
                raise ValueError("job is canceled")

        cancel_runtime = CancelRuntime()
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Client(),
            cancel_runtime,
        )
        job = {
            "id": "job-1",
            "command": "snapshot.ls",
            "payload": {"_cancel_check": lambda: True},
            "lease_token": "lease",
        }

        result = service.execute_job(job)

        self.assertEqual(result.status, JobStatus.CANCELED)
        self.assertTrue(cancel_runtime.observed)

    def test_restore_failure_preserves_redacted_runtime_error_and_stderr(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": False,
            "status_code": 2,
            "error": "restore failed with restore-secret",
            "stderr": "stderr contains restore-secret",
            "logs": "",
        }
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job(
            {
                "command": "restore.run",
                "payload": {
                    "environment": {"RESTIC_PASSWORD": "restore-secret"},
                },
            }
        )

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(result.result_summary["error"], "restore failed with <redacted>")
        self.assertEqual(result.result_summary["stderr"], "stderr contains <redacted>")
        self.assertTrue(result.log_lines)
        self.assertNotIn("restore-secret", "\n".join(result.log_lines))

    def test_restore_failure_without_runtime_output_has_fallback_log(self):
        runtime = Mock()
        runtime.run_runtime_job.return_value = {"success": False, "status_code": 1, "logs": "", "stderr": ""}
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host"),
            Mock(),
            runtime,
        )

        result = service.execute_job({"command": "restore.run", "payload": {}})

        self.assertEqual(result.log_lines, ["Restore runtime failed without logs."])

    def test_backup_result_carries_the_runtime_stop_reason_without_duplicating_stderr(self):
        runtime = Mock()
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            Mock(),
            runtime,
        )
        payload = {"target_id": "target-1", "command": "/root/backup.sh"}

        runtime.run_runtime_job.return_value = {
            "success": False,
            "status_code": 124,
            "logs": '{"message_type":"status","percent_done":0.4}',
            "stderr": "",
            "canceled": False,
            "process_terminated": True,
            "error": "native runtime produced no output for 900 seconds",
        }
        for command in ("backup.run", "retention.run"):
            with self.subTest(command=command):
                result = service.execute_job({"id": "job-1", "command": command, "payload": payload})
                self.assertEqual(result.status, JobStatus.FAILED)
                self.assertEqual(result.result_summary["status_code"], 124)
                self.assertEqual(result.result_summary["error"], "native runtime produced no output for 900 seconds")
                self.assertEqual(result.log_lines[-1], "native runtime produced no output for 900 seconds")

        runtime.run_runtime_job.return_value = {
            "success": False,
            "status_code": 1,
            "logs": "",
            "stderr": "Fatal: repository does not exist",
            "canceled": False,
            "error": "Fatal: repository does not exist",
        }
        result = service.execute_job({"id": "job-2", "command": "backup.run", "payload": payload})
        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertNotIn("error", result.result_summary)
        self.assertEqual(result.log_lines, ["Fatal: repository does not exist"])

    def test_worker_renews_long_running_job_when_client_supports_it(self):
        class Client:
            def __init__(self):
                self.renewals = []

            def renew_job_lease(self, worker_id, job_id, lease_token):
                self.renewals.append((worker_id, job_id, lease_token))
                return {"status": JobStatus.IN_PROGRESS}

            def update_job_status(self, **kwargs):
                return kwargs

        client = Client()
        runtime = Mock()

        def run_runtime_job(**kwargs):
            time.sleep(0.05)
            return {"success": True, "status_code": 0, "logs": "done", "stderr": ""}

        runtime.run_runtime_job.side_effect = run_runtime_job
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            client,
            runtime,
        )
        service.JOB_LEASE_RENEWAL_INTERVAL_SECONDS = 0.01

        service._process_jobs(
            "worker",
            [{"id": "job-1", "command": "backup.run", "payload": {}, "lease_token": "lease-token"}],
        )

        self.assertTrue(client.renewals)
        self.assertEqual(client.renewals[0], ("worker", "job-1", "lease-token"))

    def test_worker_renewal_failure_is_bounded_and_carried_to_terminal_report(self):
        class Client:
            credential_store = None

            def __init__(self):
                self.reports = []
                self.updates = []

            def renew_job_lease(self, worker_id, job_id, lease_token):
                raise TimeoutError("control plane token=private-token https://private.example/retry")

            def report_job_lease_diagnostic(self, **kwargs):
                self.reports.append(kwargs)
                raise ConnectionError("control plane unavailable")

            def update_job_status(self, **kwargs):
                self.updates.append(kwargs)
                return kwargs

        client = Client()
        runtime = Mock()

        def run_runtime_job(**kwargs):
            time.sleep(0.05)
            return {"success": True, "status_code": 0, "logs": "done", "stderr": ""}

        runtime.run_runtime_job.side_effect = run_runtime_job
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            client,
            runtime,
        )
        service.JOB_LEASE_RENEWAL_INTERVAL_SECONDS = 0.01

        service._process_jobs(
            "worker",
            [{"id": "job-1", "command": "backup.run", "payload": {}, "lease_token": "lease-token"}],
        )

        self.assertTrue(client.reports)
        renewal = client.updates[0]["result_summary"]["lease_context"]["last_renewal"]
        self.assertEqual(renewal["outcome"], "failed")
        self.assertEqual(renewal["error_type"], "TimeoutError")
        self.assertEqual(renewal["error_category"], "connectivity")
        rendered = json.dumps(client.updates[0])
        self.assertNotIn("private-token", rendered)
        self.assertNotIn("private.example", rendered)
        self.assertNotIn("control plane token", rendered)

    def test_control_plane_cancellation_probe_is_bounded_and_exception_safe(self):
        class Client:
            credential_store = None

            def __init__(self):
                self.calls = []

            def is_job_cancelled(self, worker_id, job_id):
                self.calls.append((worker_id, job_id))
                return True

        client = Client()
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            client,
            Mock(),
        )
        check = service._cancellation_check({"id": "job-1", "payload": {}})
        try:
            deadline = time.monotonic() + 2
            while not check() and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertTrue(check())
            self.assertTrue(check())
            self.assertEqual(client.calls, [("worker", "job-1")])
        finally:
            check.close()

    def test_control_plane_cancellation_probe_runs_off_the_wait_loop_at_its_own_cadence(self):
        class Client:
            credential_store = None

            def __init__(self):
                self.calls = []
                self.threads = set()

            def is_job_cancelled(self, worker_id, job_id):
                self.calls.append((worker_id, job_id))
                self.threads.add(threading.current_thread())
                time.sleep(0.05)
                return False

        client = Client()
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            client,
            Mock(),
        )
        with patch.dict(os.environ, {"WORKER_CANCEL_PROBE_INTERVAL_SECONDS": "0.05"}, clear=False):
            check = service._cancellation_check({"id": "job-1", "payload": {}})
        try:
            started = time.monotonic()
            wait_loop_calls = 0
            while time.monotonic() - started < 0.4:
                self.assertFalse(check())
                wait_loop_calls += 1
                time.sleep(0.001)
            self.assertGreater(wait_loop_calls, 50)
            self.assertGreaterEqual(len(client.calls), 2)
            self.assertLess(len(client.calls), wait_loop_calls)
            self.assertNotIn(threading.current_thread(), client.threads)
        finally:
            check.close()

    def test_control_plane_cancellation_probe_failures_never_cancel_the_job(self):
        class Client:
            credential_store = None

            def __init__(self):
                self.calls = 0
                self.outcomes = [TimeoutError("control plane unreachable"), ConnectionError("down"), True]

            def is_job_cancelled(self, worker_id, job_id):
                self.calls += 1
                outcome = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        client = Client()
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            client,
            Mock(),
        )
        with patch.dict(os.environ, {"WORKER_CANCEL_PROBE_INTERVAL_SECONDS": "0.02"}, clear=False):
            check = service._cancellation_check({"id": "job-1", "payload": {}})
        try:
            self.assertFalse(check())
            deadline = time.monotonic() + 2
            while client.calls < 2 and time.monotonic() < deadline:
                self.assertFalse(check())
                time.sleep(0.005)
            self.assertFalse(check())
            while not check() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(check())
            self.assertEqual(client.calls, 3)
        finally:
            check.close()

    def test_execute_job_stops_the_cancellation_probe_when_the_job_ends(self):
        class Client:
            credential_store = None

            def __init__(self):
                self.calls = 0

            def is_job_cancelled(self, worker_id, job_id):
                self.calls += 1
                return False

        client = Client()
        runtime = Mock()

        def run_runtime_job(**kwargs):
            deadline = time.monotonic() + 0.1
            while time.monotonic() < deadline:
                kwargs["cancel_check"]()
                time.sleep(0.005)
            return {"success": True, "status_code": 0, "logs": "done", "stderr": ""}

        runtime.run_runtime_job.side_effect = run_runtime_job
        service = WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            client,
            runtime,
        )
        with patch.dict(os.environ, {"WORKER_CANCEL_PROBE_INTERVAL_SECONDS": "0.02"}, clear=False):
            result = service.execute_job({"id": "job-1", "command": "backup.run", "payload": {}, "lease_token": "lease"})

        self.assertEqual(result.status, JobStatus.SUCCEEDED)
        self.assertGreaterEqual(client.calls, 1)
        settled = client.calls
        time.sleep(0.15)
        self.assertEqual(client.calls, settled)


class WorkerMainPollingTests(unittest.TestCase):
    def test_worker_labels_json_remains_available_to_service_configuration(self):
        from src.worker_agent import main as worker_main

        with patch.dict(os.environ, {"WORKER_LABELS": '{"lane":"interactive"}'}, clear=False):
            self.assertEqual(worker_main._labels_from_env(), {"lane": "interactive"})

    def test_build_service_derives_recovery_file_beside_worker_credentials(self):
        from src.worker_agent import main as worker_main

        with tempfile.TemporaryDirectory() as directory:
            credential_file = os.path.join(directory, "worker_credentials.json")
            with patch.object(worker_main, "DockerRuntimeAdapter", return_value=Mock()), patch.object(
                worker_main.RedisSnapshotCache, "from_env", return_value=None
            ), patch.dict(os.environ, {"WORKER_CREDENTIAL_FILE": credential_file}, clear=True):
                service = worker_main.build_service()

            self.assertEqual(
                str(service.recovery_journal.path),
                os.path.join(directory, "worker_job_recovery.json"),
            )

    def test_main_poll_helper_falls_back_when_fast_lane_is_unavailable(self):
        from src.worker_agent import main as worker_main

        service = Mock()
        service.poll_interactive_once.side_effect = NotImplementedError()
        service.poll_once.return_value = ["durable"]

        result = worker_main._poll_worker(service)

        self.assertEqual(result, ["durable"])
        service.poll_once.assert_called_once_with()

    def test_interactive_interval_is_sub_second_by_default(self):
        from src.worker_agent import main as worker_main

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WORKER_INTERACTIVE_POLL_INTERVAL_SECONDS", None)
            self.assertLess(worker_main._bounded_interval("WORKER_INTERACTIVE_POLL_INTERVAL_SECONDS", 0.5, 0.1, 5.0), 1.0)

    def test_runtime_orphan_sweep_interval_uses_default_and_safe_bounds(self):
        from src.worker_agent import main as worker_main

        variable = "WORKER_RUNTIME_ORPHAN_SWEEP_INTERVAL_SECONDS"
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(worker_main._runtime_orphan_sweep_interval(), 15.0)
        with patch.dict(os.environ, {variable: "5"}, clear=True):
            self.assertEqual(worker_main._runtime_orphan_sweep_interval(), 5.0)
        with patch.dict(os.environ, {variable: "3600"}, clear=True):
            self.assertEqual(worker_main._runtime_orphan_sweep_interval(), 3600.0)
        for value in ("4", "3601", "not-a-number"):
            with self.subTest(value=value), patch.dict(os.environ, {variable: value}, clear=True):
                self.assertEqual(worker_main._runtime_orphan_sweep_interval(), 15.0)

    def test_main_runs_runtime_orphan_sweep_once_after_registration(self):
        from src.worker_agent import main as worker_main

        service = Mock()
        service.config = WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker")
        service.ensure_registered.return_value = "worker"
        service.cleanup_orphaned_runtime_containers.return_value = {
            "inspected": 2,
            "removed": 1,
            "failed": 0,
            "skipped": 1,
        }
        service.poll_interactive_once.return_value = []

        with patch.object(worker_main, "build_service", return_value=service), patch.object(worker_main, "start_health_server"), patch.dict(
            os.environ,
            {
                "WORKER_RUN_ONCE": "true",
                "WORKER_HEALTH_PORT": "0",
                "WORKER_RUNTIME_ORPHAN_SWEEP_INTERVAL_SECONDS": "15",
            },
            clear=False,
        ):
            worker_main.main()

        service.cleanup_orphaned_runtime_containers.assert_called_once_with()

    def test_main_repeats_runtime_orphan_sweep_only_after_interval(self):
        from src.worker_agent import main as worker_main

        service = Mock()
        service.config = WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker")
        service.ensure_registered.return_value = "worker"
        sweep_times = []

        def sweep():
            sweep_times.append(clock.value)
            return {"inspected": 0, "removed": 0, "failed": 0, "skipped": 0}

        service.cleanup_orphaned_runtime_containers.side_effect = sweep
        service.poll_interactive_once.return_value = []

        class Clock:
            value = None

            def __init__(self):
                self._values = iter((0.0, 0.0, 5.0, 5.0, 16.0, 16.0))

            def monotonic(self):
                self.value = next(self._values)
                return self.value

        clock = Clock()
        sleep_calls = []

        def controlled_sleep(delay):
            sleep_calls.append(delay)
            if len(sleep_calls) == 3:
                raise StopIteration("stop controlled worker loop")
            return False

        with patch.object(worker_main, "build_service", return_value=service), patch.object(worker_main, "start_health_server"), patch.object(
            worker_main.time, "monotonic", side_effect=clock.monotonic
        ), patch.object(worker_main, "_wait_for_stop", side_effect=controlled_sleep), patch.dict(
            os.environ,
            {
                "WORKER_RUN_ONCE": "false",
                "WORKER_HEALTH_PORT": "0",
                "WORKER_POLL_INTERVAL_SECONDS": "3600",
                "WORKER_INVENTORY_SYNC_INTERVAL_SECONDS": "3600",
                "WORKER_INTERACTIVE_POLL_INTERVAL_SECONDS": "0.1",
                "WORKER_RUNTIME_ORPHAN_SWEEP_INTERVAL_SECONDS": "15",
            },
            clear=True,
        ):
            with self.assertRaises(StopIteration):
                worker_main.main()

        self.assertEqual(sweep_times, [0.0, 16.0])


class WorkerRestartRecoveryTests(unittest.TestCase):
    @staticmethod
    def runtime_for(container):
        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.client.containers.list.return_value = [container]
        return runtime

    @staticmethod
    def container(job_id, status="exited", status_code=0, logs=b""):
        container = Mock()
        container.id = f"runtime-{job_id}"
        container.status = status
        container.labels = {
            DockerRuntimeAdapter.RUNTIME_TEMPORARY_LABEL: "true",
            DockerRuntimeAdapter.RUNTIME_JOB_ID_LABEL: job_id,
        }
        container.wait.return_value = {"StatusCode": status_code}
        container.logs.return_value = logs
        return container

    @staticmethod
    def job(job_id="job-restore", command="restore.run"):
        return {
            "id": job_id,
            "command": command,
            "payload": {"environment": {"RESTIC_PASSWORD": "runtime-secret"}},
            "lease_token": "lease-token-never-logged",
        }

    class RecordingClient:
        def __init__(self, failure=None):
            self.updates = []
            self.failure = failure

        def update_job_status(self, **kwargs):
            self.updates.append(kwargs)
            if self.failure:
                failure = self.failure
                self.failure = None
                raise failure
            return {"status": kwargs["status"]}

    def service(self, client, recovery_file):
        return WorkerAgentService(
            WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
            client,
            self.runtime_for(self.container("unrelated")),
            recovery_file=recovery_file,
        )

    def test_worker_loss_recovers_exited_restore_once_with_redacted_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "worker_job_recovery.json")

            class CrashRuntime:
                def run_runtime_job(self, image, payload, cancel_check=None):
                    raise KeyboardInterrupt()

            first_service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                self.RecordingClient(),
                CrashRuntime(),
                recovery_file=recovery_file,
            )
            with self.assertLogs("src.worker_agent.application.services.worker_agent_service", level="INFO") as captured:
                with self.assertRaises(KeyboardInterrupt):
                    first_service._process_jobs("worker", [self.job()])
            self.assertNotIn("lease-token-never-logged", "\n".join(captured.output))
            with open(recovery_file, encoding="utf-8") as journal_stream:
                self.assertIn("lease-token-never-logged", journal_stream.read())

            container = self.container("job-restore", logs=b"restore completed lease-token-never-logged\n")
            client = self.RecordingClient()
            runtime = self.runtime_for(container)
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                runtime,
                recovery_file=recovery_file,
            )

            result = service.cleanup_orphaned_runtime_containers()

            self.assertEqual(result["removed"], 1)
            self.assertEqual(client.updates[0]["status"], JobStatus.SUCCEEDED)
            self.assertEqual(client.updates[0]["result_summary"]["recovery"], "worker_restart_recovered")
            self.assertIn("restore completed", "\n".join(client.updates[0]["log_lines"]))
            self.assertNotIn("lease-token-never-logged", "\n".join(client.updates[0]["log_lines"]))
            self.assertFalse(os.path.exists(recovery_file))
            container.remove.assert_called_once_with(force=True)

            runtime.client.containers.list.return_value = []
            service.cleanup_orphaned_runtime_containers()
            self.assertEqual(len(client.updates), 1)

    def test_failed_runtime_recovery_is_terminal_and_has_useful_fallback_log(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "recovery.json")
            WorkerJobRecoveryJournal(recovery_file).write(
                "job-restore", "worker", "restore.run", "lease-token"
            )
            container = self.container("job-restore", status_code=7)
            client = self.RecordingClient()
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                self.runtime_for(container),
                recovery_file=recovery_file,
            )

            service.cleanup_orphaned_runtime_containers()

            self.assertEqual(client.updates[0]["status"], JobStatus.FAILED)
            self.assertEqual(client.updates[0]["result_summary"]["status_code"], 7)
            self.assertIn("exited with status code 7", "\n".join(client.updates[0]["log_lines"]))
            self.assertFalse(os.path.exists(recovery_file))

    def test_snapshots_list_recovery_rebuilds_bounded_catalog_result(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "recovery.json")
            WorkerJobRecoveryJournal(recovery_file).write(
                "job-snapshots", "worker", "snapshots.list", "lease-token"
            )
            container = self.container(
                "job-snapshots",
                logs=b'[{"short_id":"abcdef12","time":"2026-08-15T00:00:00"}]',
            )
            client = self.RecordingClient()
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                self.runtime_for(container),
                recovery_file=recovery_file,
            )

            service.cleanup_orphaned_runtime_containers()

            self.assertEqual(client.updates[0]["status"], JobStatus.SUCCEEDED)
            self.assertEqual(client.updates[0]["result_summary"]["snapshots"][0]["short_id"], "abcdef12")

    def test_running_orphan_is_retained_for_a_later_sweep(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "recovery.json")
            WorkerJobRecoveryJournal(recovery_file).write(
                "job-restore", "worker", "restore.run", "lease-token"
            )
            container = self.container("job-restore", status="running")
            client = self.RecordingClient()
            runtime = self.runtime_for(container)
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                runtime,
                recovery_file=recovery_file,
            )

            result = service.cleanup_orphaned_runtime_containers()

            self.assertEqual(result["skipped"], 1)
            self.assertEqual(result["retained"], 0)
            client.update_job_status = Mock()
            container.remove.assert_not_called()
            container.logs.assert_not_called()
            self.assertIsNotNone(WorkerJobRecoveryJournal(recovery_file).load())

    def test_transient_control_plane_failure_retains_container_and_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "recovery.json")
            WorkerJobRecoveryJournal(recovery_file).write(
                "job-restore", "worker", "restore.run", "lease-token"
            )
            container = self.container("job-restore", logs=b"restore completed")
            client = self.RecordingClient(RuntimeError("network unavailable"))
            runtime = self.runtime_for(container)
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                runtime,
                recovery_file=recovery_file,
            )

            first = service.cleanup_orphaned_runtime_containers()
            self.assertEqual(first["retained"], 1)
            container.remove.assert_not_called()
            self.assertIsNotNone(WorkerJobRecoveryJournal(recovery_file).load())

            second = service.cleanup_orphaned_runtime_containers()
            self.assertEqual(second["removed"], 1)
            self.assertEqual(len(client.updates), 2)
            self.assertFalse(os.path.exists(recovery_file))

    def test_mismatched_or_stale_journal_never_reuses_a_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "recovery.json")
            WorkerJobRecoveryJournal(recovery_file).write(
                "different-job", "other-worker", "restore.run", "wrong-lease"
            )
            container = self.container("job-restore", logs=b"restore completed")
            client = self.RecordingClient()
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                self.runtime_for(container),
                recovery_file=recovery_file,
            )

            service.cleanup_orphaned_runtime_containers()

            client_updates = client.updates
            self.assertEqual(client_updates, [])
            container.remove.assert_called_once_with(force=True)

    def test_stale_journal_is_not_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "recovery.json")
            with open(recovery_file, "w", encoding="utf-8") as journal_stream:
                json.dump(
                    {
                        "version": 1,
                        "job_id": "job-restore",
                        "worker_id": "worker",
                        "command": "restore.run",
                        "lease_token": "old-lease",
                        "created_at": "2000-01-01T00:00:00+00:00",
                    },
                    journal_stream,
                )
            container = self.container("job-restore", logs=b"restore completed")
            client = self.RecordingClient()
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                self.runtime_for(container),
                recovery_file=recovery_file,
            )

            service.cleanup_orphaned_runtime_containers()

            self.assertEqual(client.updates, [])
            container.remove.assert_called_once_with(force=True)

    class OutageClient:
        class _CredentialStore:
            def load(self):
                return object()

        credential_store = _CredentialStore()

        def __init__(self, failures):
            self.updates = []
            self.fetches = 0
            self.failures = list(failures)

        def fetch_jobs(self, worker_id):
            self.fetches += 1
            return []

        def update_job_status(self, **kwargs):
            self.updates.append(kwargs)
            if self.failures:
                raise self.failures.pop(0)
            return {"status": kwargs["status"]}

    @staticmethod
    def succeeding_runtime():
        runtime = Mock()
        runtime.run_runtime_job.return_value = {
            "success": True,
            "status_code": 0,
            "logs": "backup done lease-token-never-logged",
            "stderr": "",
        }
        return runtime

    def test_terminal_report_survives_control_plane_outage_and_is_delivered_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "worker_job_recovery.json")
            client = self.OutageClient([ConnectionError("control plane down"), TimeoutError("still down")])
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                self.succeeding_runtime(),
                recovery_file=recovery_file,
            )
            service.TERMINAL_REPORT_RETRY_MIN_SECONDS = 0.01
            service._terminal_retry_delay = 0.01

            processed = service._process_jobs("worker", [self.job()])

            self.assertEqual(processed[0]["status"], JobStatus.SUCCEEDED)
            self.assertEqual(len(client.updates), 1)
            self.assertFalse(os.path.exists(recovery_file))
            self.assertTrue(os.path.exists(service.terminal_report_journal.path))

            self.assertEqual(service.poll_once(), [])
            self.assertEqual(client.fetches, 1)
            self.assertEqual(len(client.updates), 2)
            time.sleep(0.05)
            self.assertEqual(service.poll_once(), [])
            self.assertEqual(len(client.updates), 3)
            self.assertEqual(client.updates[2], client.updates[0])
            self.assertEqual(client.updates[2]["status"], JobStatus.SUCCEEDED)
            self.assertEqual(client.updates[2]["lease_token"], "lease-token-never-logged")
            self.assertIn("backup done", "\n".join(client.updates[2]["log_lines"]))
            self.assertFalse(os.path.exists(service.terminal_report_journal.path))

            service.poll_once()
            self.assertEqual(len(client.updates), 3)

    def test_terminal_report_retry_respects_backoff_between_ticks(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.OutageClient([ConnectionError("down")] * 5)
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                self.succeeding_runtime(),
                recovery_file=os.path.join(directory, "worker_job_recovery.json"),
            )
            service._terminal_retry_delay = 10.0

            service._process_jobs("worker", [self.job()])
            service.poll_once()
            service.poll_once()
            service.poll_once()

            self.assertEqual(len(client.updates), 2)
            self.assertEqual(client.fetches, 3)

    def test_pending_terminal_report_is_reloaded_after_worker_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery_file = os.path.join(directory, "worker_job_recovery.json")
            first_client = self.OutageClient([ConnectionError("down")])
            first = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                first_client,
                self.succeeding_runtime(),
                recovery_file=recovery_file,
            )
            first._process_jobs("worker", [self.job()])

            restarted_client = self.OutageClient([])
            restarted = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                restarted_client,
                self.succeeding_runtime(),
                recovery_file=recovery_file,
            )
            restarted.poll_once()

            self.assertEqual(len(restarted_client.updates), 1)
            self.assertEqual(restarted_client.updates[0]["job_id"], first_client.updates[0]["job_id"])
            self.assertEqual(restarted_client.updates[0]["result_summary"], first_client.updates[0]["result_summary"])
            self.assertEqual(restarted_client.updates[0]["log_lines"], first_client.updates[0]["log_lines"])
            self.assertFalse(os.path.exists(restarted.terminal_report_journal.path))

    def test_definitively_rejected_pending_report_is_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.OutageClient([ConnectionError("down"), ValueError("worker 'worker' does not own job 'job-restore'")])
            service = WorkerAgentService(
                WorkerAgentConfig("http://control-plane", "worker", "host", worker_id="worker"),
                client,
                self.succeeding_runtime(),
                recovery_file=os.path.join(directory, "worker_job_recovery.json"),
            )
            service._terminal_retry_delay = 0.0

            service._process_jobs("worker", [self.job()])
            service.poll_once()
            service.poll_once()

            self.assertEqual(len(client.updates), 2)
            self.assertFalse(os.path.exists(service.terminal_report_journal.path))


class DockerExecutionLogCaptureTests(unittest.TestCase):
    """T2 (S1-S6, persistent-worker-execution-logs.md): the Docker runtime
    spawns an independent, redacted, persistent per-job execution-log capture
    child that continues when the worker dies; any capture failure continues
    and alerts locally and through the job progress callback without failing
    or stopping the backup."""

    CONTAINER_ID = "abcdef0123456789abcdef0123456789abcdef01"
    JOB_ID = "job-42"
    SECRET = "hunter2"
    WARNING_PREFIX = "[persistent-execution-log]"

    class FakeCaptureProcess:
        def __init__(self, exit_code=0, trace=None):
            self.exit_code = exit_code
            self.wait_timeout = None
            self.wait_calls = 0
            self.poll_calls = 0
            self._trace = trace

        def wait(self, timeout=None):
            self.wait_calls += 1
            self.wait_timeout = timeout
            if self._trace is not None:
                self._trace.append("wait")
            return self.exit_code

        def poll(self):
            self.poll_calls += 1
            return self.exit_code

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="docker-exec-log-capture-")
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.worker_id = "worker-a"
        # Environment isolation: the capture root points inside the test temp
        # directory and ambient daemon/retention/identity/state variables are
        # cleared so every test decides its own worker-state configuration.
        saved = {
            name: os.environ.get(name)
            for name in (
                "WORKER_EXECUTION_LOG_ROOT",
                "DOCKER_HOST",
                "WORKER_EXECUTION_LOG_RETENTION_DAYS",
                "WORKER_ID",
                "WORKER_STATE_DIR",
                "WORKER_JOB_RECOVERY_FILE",
                "WORKER_CREDENTIAL_FILE",
            )
        }

        def restore_env():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        for name in saved:
            os.environ.pop(name, None)
        os.environ["WORKER_EXECUTION_LOG_ROOT"] = self.root
        self.addCleanup(restore_env)

    def drop_env(self, *names):
        """Remove environment names for the remainder of this test (restored
        afterwards, including keys changed by the test itself)."""
        saved = {name: os.environ.get(name) for name in names}

        def restore():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        for name in names:
            os.environ.pop(name, None)
        self.addCleanup(restore)

    def payload(self, **extra):
        payload = {
            "command": "/root/backup.sh",
            "environment": {"RESTIC_PASSWORD": self.SECRET},
            "_job_id": self.JOB_ID,
        }
        payload.update(extra)
        return payload

    def container(self, container_id=CONTAINER_ID):
        container = Mock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.return_value = b"ok"
        container.attach.return_value = b"ok"
        container.id = container_id
        return container

    def runtime(self, container, worker_id="worker-a"):
        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.client.containers.run.return_value = container
        runtime.timeout_seconds = 30.0
        runtime.no_lock = False
        runtime.cache_dir = None
        runtime.worker_id = worker_id
        return runtime

    def patch_capture(self, *, spawn=None, spawn_raises=None, recipe_raises=None, real_recipe=False, exit_code=0, trace=None):
        """Isolate the capture seams: the ephemeral recipe-frame encode, the
        detached spawn and the retention sweep never touch a real daemon or
        write outside the test root. ``spawn`` is the process handle to
        return; ``spawn_raises`` makes the spawn call raise. The ``spawn``
        Mock is exposed as seams["spawn"]."""
        seams = {"frames": [], "prunes": []}
        process = self.FakeCaptureProcess(exit_code=exit_code, trace=trace)

        def record_frame(values):
            seams["frames"].append(tuple(values))
            return encode_recipe_frame(values)

        def record_prune(*args, **kwargs):
            seams["prunes"].append((args, kwargs))
            return {"deleted": 0, "failed": 0, "errors": []}

        seams["process"] = process
        recipe_patch = patch(
            "src.worker_agent.infrastructure.adapters.docker_runtime.encode_recipe_frame",
            side_effect=(
                encode_recipe_frame
                if real_recipe
                else (record_frame if recipe_raises is None else recipe_raises)
            ),
        )
        spawn_patch = patch.object(ExecutionLogCapture, "spawn_detached_docker_capture")
        prune_patch = patch.object(ExecutionLogCapture, "prune", side_effect=record_prune)
        recipe_patch.start()
        spawn_patch.start()
        prune_patch.start()
        self.addCleanup(recipe_patch.stop)
        self.addCleanup(spawn_patch.stop)
        self.addCleanup(prune_patch.stop)
        spawn_mock = ExecutionLogCapture.spawn_detached_docker_capture
        if spawn_raises is not None:
            spawn_mock.side_effect = spawn_raises
            seams["process"] = None
        else:
            spawn_mock.return_value = process if spawn is None else spawn
            seams["process"] = spawn_mock.return_value
        seams["spawn"] = spawn_mock
        return contextlib.nullcontext(seams)

    def spawn_kwargs(self):
        call = ExecutionLogCapture.spawn_detached_docker_capture.call_args
        return call.args, call.kwargs

    def managed_log_parts(self, log_path):
        return Path(log_path).relative_to(self.root).parts

    WORKER_IMAGE = "vaultline-worker:test"
    WORKER_HOSTNAME = "workercontainerid0123456789"

    def daemon_mode_env(self):
        """Deployed compose topology: worker_state volume at /data carrying the
        credential file, docker socket bind, HOSTNAME equal to the worker
        container id. The explicit root override is removed so the default
        follows the state volume (worker-path /data)."""
        self.drop_env("WORKER_EXECUTION_LOG_ROOT")
        os.environ["WORKER_CREDENTIAL_FILE"] = "/data/worker_credentials.json"
        os.environ["HOSTNAME"] = self.WORKER_HOSTNAME
        self.addCleanup(os.environ.pop, "HOSTNAME", None)
        self.addCleanup(os.environ.pop, "WORKER_CREDENTIAL_FILE", None)

    def self_container(self, *, env=None, cmd=None, mounts=None):
        """Fake of the worker's own daemon record (read-only self-inspection
        seam): image, python runtime layout, worker_state volume and the
        daemon socket bind copied from the deployed compose."""
        own = Mock()
        own.attrs = {
            "Config": {
                "Image": self.WORKER_IMAGE,
                "Env": ["PYTHONPATH=/app"] if env is None else env,
                "Cmd": ["python", "-m", "src.worker_agent.main"] if cmd is None else cmd,
            },
            "Mounts": [
                {"Type": "volume", "Name": "proj_worker_state", "Destination": "/data", "RW": True, "Mode": "z"},
                {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True},
            ] if mounts is None else mounts,
        }
        own.id = self.WORKER_HOSTNAME + "abcd"
        own.status = "running"
        return own

    def capture_container(self, exit_code=0, trace=None):
        fake = Mock()

        def wait(timeout=None):
            if trace is not None:
                trace.append("capture-wait")
            return {"StatusCode": exit_code}

        def remove_mock(force=None):
            if trace is not None:
                trace.append("capture-removed")

        fake.wait = Mock(side_effect=wait)
        fake.remove = Mock(side_effect=remove_mock)
        fake.id = "capturecontainerid0123456789"
        fake.attrs = {"Config": {"Labels": {}}}
        # Ephemeral framed-stdin handoff: attach returns a fake stream whose
        # write (binary bytes) and close are observable.
        fake.capture_stream = Mock()
        fake.attach = Mock(return_value=fake.capture_stream)
        return fake

    def test_containerized_worker_uses_a_daemon_owned_capture_container(self):
        """S5: when the worker itself is a managed container, the capture
        child must run as its own daemon-owned container (mounted from the
        worker's own state-volume and socket spec) so the user's reported
        worker-container recreation cannot kill it; the in-worker detached
        child dies with the container (reproduced with a real daemon)."""
        self.daemon_mode_env()
        trace = []
        executor = self.container()
        executor.remove.side_effect = lambda **kwargs: trace.append("executor-remove")
        capture = self.capture_container(trace=trace)
        with self.patch_capture(trace=trace) as seams:
            runtime = self.runtime(executor, worker_id="worker-a")
            runtime.client.containers.run.side_effect = [executor, capture]
            runtime.client.containers.get = Mock(return_value=self.self_container())
            result = runtime.run_runtime_job("runtime", self.payload())

        self.assertTrue(result["success"], result)
        # No in-worker child: the detached Popen path is not used at all.
        seams["spawn"].assert_not_called()
        # One read-only self-inspection through the daemon.
        runtime.client.containers.get.assert_called_once_with(self.WORKER_HOSTNAME)
        self.assertEqual(runtime.client.containers.run.call_count, 2)
        _, capture_call = runtime.client.containers.run.call_args_list
        capture_kwargs = capture_call.kwargs
        self.assertEqual(capture_kwargs["image"], self.WORKER_IMAGE)
        command = capture_kwargs["command"]
        self.assertEqual(command[0], "python")
        self.assertTrue(command[1].endswith("execution_log_capture.py"))
        self.assertIn("--run-capture", command)
        capture_log = Path(command[command.index("--capture") + 1])
        self.assertEqual(capture_log.parents[1], Path("/data/execution-logs/worker-a/docker"))
        self.assertTrue(capture_log.name.startswith(f"{self.JOB_ID}-"))
        # Ephemeral recipe transfer: framed stdin, never a plaintext file path.
        self.assertIn("--recipe-stream", command)
        self.assertEqual(command[command.index("--recipe-stream") + 1], "1")
        self.assertNotIn("--recipe", command)
        self.assertTrue(capture_kwargs["stdin_open"])
        self.assertEqual(command[command.index("--container") + 1], self.CONTAINER_ID)
        self.assertEqual(command[command.index("--socket") + 1], "/var/run/docker.sock")
        self.assertNotIn(self.SECRET, repr(command))
        self.assertEqual(capture_kwargs["network_mode"], "none")
        self.assertIs(capture_kwargs["remove"], False)
        volumes = capture_kwargs["volumes"]
        self.assertEqual(volumes["proj_worker_state"]["bind"], "/data")
        self.assertEqual(volumes["/var/run/docker.sock"]["bind"], "/var/run/docker.sock")
        labels = capture_kwargs["labels"]
        self.assertEqual(labels[DockerRuntimeAdapter.RUNTIME_CAPTURE_LABEL], DockerRuntimeAdapter.RUNTIME_CAPTURE_VALUE)
        self.assertEqual(labels[DockerRuntimeAdapter.RUNTIME_JOB_ID_LABEL], self.JOB_ID)
        # Cleanup without data loss: drain, remove the finished capture
        # container, then the executor stream/container.
        self.assertEqual(trace, ["capture-wait", "capture-removed", "executor-remove"])
        self.assertEqual(capture.wait.call_args.kwargs.get("timeout"), DockerRuntimeAdapter.EXECUTION_LOG_DRAIN_SECONDS)
        # The bounded framed recipe was attached and written to the capture
        # container's stdin, then closed: secrets never rest on the volume.
        capture.attach.assert_called_once_with(socket=True, stdin=True, stream=True)
        frame = capture.capture_stream.write.call_args.args[0]
        self.assertIsInstance(frame, bytes)
        self.assertIn(f'"{self.SECRET}"'.encode("utf-8"), frame)
        capture.capture_stream.close.assert_called_once()

    def test_hostname_override_worker_container_degrades_with_one_safe_actionable_warning(self):
        """A containerized worker whose hostname cannot resolve to its container
        id (e.g. a compose ``hostname:`` override) silently fell back to the
        in-worker child, losing capture on worker recreation without any
        warning: it must emit exactly one safe actionable warning locally and
        through the job progress callback, while the job keeps succeeding."""
        os.environ["WORKER_CREDENTIAL_FILE"] = "/data/worker_credentials.json"
        os.environ["HOSTNAME"] = "custom-worker-hostname"
        self.addCleanup(os.environ.pop, "HOSTNAME", None)
        self.addCleanup(os.environ.pop, "WORKER_CREDENTIAL_FILE", None)
        with self.patch_capture() as seams:
            runtime = self.runtime(self.container(), worker_id="worker-a")
            runtime.client.containers.get = Mock(side_effect=RuntimeError("No such container: custom-worker-hostname"))
            with patch.object(DockerRuntimeAdapter, "_runs_inside_a_docker_container", return_value=True):
                lines = []
                with self.assertLogs("src.worker_agent.infrastructure.adapters.docker_runtime", level="WARNING") as logged:
                    result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["logs"], "ok")
        # Fail open: the degraded in-worker child still runs (worker-process
        # death resistance preserved); no daemon capture container is created.
        seams["spawn"].assert_called_once()
        self.assertEqual(runtime.client.containers.run.call_count, 1)
        # Exactly one safe, actionable warning reaches both lanes.
        warnings = [line for line in lines if self.WARNING_PREFIX in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("worker container", warnings[0])
        self.assertIn("degrades", warnings[0])
        self.assertIn("worker container recreation", warnings[0])
        self.assertNotIn(self.SECRET, warnings[0])
        joined_logs = "".join(logged.output)
        self.assertIn("degrades", joined_logs)
        self.assertNotIn(self.SECRET, joined_logs)

    def test_genuinely_host_native_worker_is_not_warned_about_container_degradation(self):
        """A host-native worker (deb/MSI service, no /.dockerenv) whose
        hostname naturally does not resolve through the daemon keeps the
        in-worker detached capture silently: no false degraded warning."""
        with self.patch_capture() as seams:
            runtime = self.runtime(self.container(), worker_id="worker-a")
            runtime.client.containers.get = Mock(side_effect=RuntimeError("No such container: daniel-pc"))
            os.environ["HOSTNAME"] = "daniel-pc"
            self.addCleanup(os.environ.pop, "HOSTNAME", None)
            with patch.object(DockerRuntimeAdapter, "_runs_inside_a_docker_container", return_value=False):
                lines = []
                result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        seams["spawn"].assert_called_once()
        warnings = [line for line in lines if self.WARNING_PREFIX in line]
        self.assertEqual(warnings, [])

    def test_unresolving_hostname_inside_a_container_without_the_daemon_marker_is_silent(self):
        """When the container-vs-host marker is absent (host-native machine
        that merely carries such a hostname), the fallback stays silent."""
        with self.patch_capture() as seams:
            runtime = self.runtime(self.container(), worker_id="worker-a")
            runtime.client.containers.get = Mock(side_effect=RuntimeError("No such container"))
            os.environ["HOSTNAME"] = "custom-worker-hostname"
            self.addCleanup(os.environ.pop, "HOSTNAME", None)
            with patch.object(DockerRuntimeAdapter, "_runs_inside_a_docker_container", return_value=False):
                lines = []
                result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        seams["spawn"].assert_called_once()
        self.assertEqual([line for line in lines if self.WARNING_PREFIX in line], [])

    def test_non_containerized_worker_keeps_in_process_detached_capture(self):
        """Existing process-death behavior is preserved for workers that are
        not managed containers (host/MSI services)."""
        with self.patch_capture() as seams:
            runtime = self.runtime(self.container(), worker_id="worker-a")
            runtime.client.containers.get = Mock(side_effect=RuntimeError("No such container"))
            os.environ["HOSTNAME"] = "native-host-name"
            self.addCleanup(os.environ.pop, "HOSTNAME", None)
            lines = []
            result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        seams["spawn"].assert_called_once()
        args, kwargs = self.spawn_kwargs()
        self.assertEqual(args[2], self.CONTAINER_ID)
        self.assertEqual(runtime.client.containers.run.call_count, 1)
        self.assertFalse([line for line in lines if self.WARNING_PREFIX in line])

    def test_containerized_worker_without_socket_or_state_mounts_degrades_and_alerts(self):
        variants = [
            ("daemon socket mount",
             [{"Type": "volume", "Name": "proj_worker_state", "Destination": "/data", "RW": True}]),
            ("worker-state volume",
             [{"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True}]),
            ("worker runtime layout",
             ["PYTHONPATH_MISSING"]),
        ]
        for expected_fragment, special in variants:
            with self.subTest(expected_fragment=expected_fragment):
                self.daemon_mode_env()
                if expected_fragment == "worker runtime layout":
                    own = self.self_container(env=[])
                else:
                    own = self.self_container(mounts=special)
                with self.patch_capture() as seams:
                    runtime = self.runtime(self.container(), worker_id="worker-a")
                    runtime.client.containers.get = Mock(return_value=own)
                    lines = []
                    result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)
                self.assertTrue(result["success"], result)
                seams["spawn"].assert_called_once()
                warnings = [line for line in lines if self.WARNING_PREFIX in line]
                self.assertEqual(len(warnings), 1)
                self.assertIn(expected_fragment, warnings[0])
                self.assertIn("degrades", warnings[0])
                self.assertNotIn(self.SECRET, warnings[0])

    def test_capture_container_creation_failure_fails_open_without_a_recipe(self):
        self.daemon_mode_env()
        # Real recipe framing must stay observable: the ephemeral frame never
        # touches the test root the way the state volume does in deployment.
        os.environ["WORKER_EXECUTION_LOG_ROOT"] = self.root
        own = self.self_container(
            mounts=[
                {"Type": "volume", "Name": "proj_worker_state", "Destination": self.root, "RW": True},
                {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True},
            ]
        )

        def run_side_effect(image, **kwargs):
            if kwargs.get("network_mode") == "none":
                raise RuntimeError("capture container creation failed")
            return self.container()

        with self.patch_capture(real_recipe=True) as seams:
            runtime = self.runtime(self.container(), worker_id="worker-a")
            runtime.client.containers.run = Mock(side_effect=run_side_effect)
            runtime.client.containers.get = Mock(return_value=own)
            lines = []
            result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["logs"], "ok")
        warnings = [line for line in lines if self.WARNING_PREFIX in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("capture container", warnings[0])
        self.assertNotIn(self.SECRET, warnings[0])
        # No secret recipe is ever left behind: ephemeral transfer writes no
        # recipe file at all.
        self.assertEqual([p.name for p in Path(self.root).rglob("*.recipe.json")], [])

    def test_capture_container_exit_code_alerts_after_drain(self):
        self.daemon_mode_env()
        capture = self.capture_container(exit_code=4)
        with self.patch_capture() as seams:
            runtime = self.runtime(self.container(), worker_id="worker-a")
            runtime.client.containers.run.side_effect = [self.container(), capture]
            runtime.client.containers.get = Mock(return_value=self.self_container())
            lines = []
            result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        warnings = [line for line in lines if self.WARNING_PREFIX in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("docker daemon log stream could not be opened", warnings[0])

    def test_cancellation_leaves_the_daemon_capture_container_to_finish_safely(self):
        """Existing cancellation semantics are preserved; the running capture
        container follows the executor teardown on the daemon (interrupted
        evidence is kept on the volume) and is left for the orphan sweep."""
        self.daemon_mode_env()
        container = self.container()
        container.wait.side_effect = TimeoutError("wait timed out")

        class MustNotWaitOrRemove:
            def wait(self, timeout=None):
                raise AssertionError("canceled jobs must not wait for the capture container")

            def remove(self, force=None):
                raise AssertionError("the running capture container must not be removed by the worker")

        with self.patch_capture(spawn=MustNotWaitOrRemove()):
            runtime = self.runtime(container, worker_id="worker-a")
            runtime.client.containers.run.side_effect = [container, MustNotWaitOrRemove()]
            runtime.client.containers.get = Mock(return_value=self.self_container())
            checks = iter((False, False, True))
            result = runtime.run_runtime_job(
                "runtime",
                {**self.payload(), "timeout_seconds": 2},
                cancel_check=lambda: next(checks),
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["canceled"])
        self.assertEqual(result["status_code"], 130)
        container.stop.assert_called_once_with(timeout=1)
        container.remove.assert_called_once_with(force=True)

    def test_orphan_sweep_removes_exited_daemon_capture_containers(self):
        """Daemon capture containers left after a worker death are cleaned by
        the sweep without consulting job recovery and without touching any
        other container's volume data (logs already live in the state volume)."""
        def capture_record(name, status):
            fake = Mock()
            fake.id = f"capture-{name}-0123456789"
            fake.status = status
            fake.labels = {
                DockerRuntimeAdapter.RUNTIME_TEMPORARY_LABEL: "true",
                DockerRuntimeAdapter.RUNTIME_CAPTURE_LABEL: "true",
                DockerRuntimeAdapter.RUNTIME_JOB_ID_LABEL: "s5-job",
            }
            return fake

        exited = capture_record("exitedcap", "exited")
        running = capture_record("runningcap", "running")
        recover = Mock()

        runtime = self.runtime(None, worker_id="worker-a")
        runtime.client = Mock()
        runtime.client.containers.list.return_value = [running, exited]
        summary = runtime.cleanup_orphaned_runtime_containers(recover_callback=recover)

        exited.remove.assert_called_once_with(force=True)
        running.remove.assert_not_called()
        running.stop.assert_not_called()
        recover.assert_not_called()
        self.assertEqual(summary["removed"], 1)

    def test_successful_job_spawns_independent_persistent_capture_and_drains_before_cleanup(self):
        trace = []
        container = self.container()
        container.remove.side_effect = lambda **kwargs: trace.append("remove")
        with self.patch_capture(trace=trace) as seams:
            runtime = self.runtime(container)
            lines = []
            result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        self.assertIn("ok", result["logs"])
        # One independent detached capture for this job, daemon-streaming mode.
        seams["spawn"].assert_called_once()
        args, kwargs = self.spawn_kwargs()
        log_path, recipe_path, container_id = args
        self.assertEqual(container_id, self.CONTAINER_ID)
        self.assertIsNone(recipe_path)
        self.assertIsNone(kwargs["socket_path"])
        self.assertTrue(kwargs["recipe_stream"])
        self.assertIsInstance(kwargs["stdin"], int)
        # Correct worker/job identity inside the fixed host structure (S1, S2).
        parts = self.managed_log_parts(log_path)
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], self.worker_id)
        self.assertEqual(parts[1], "docker")
        self.assertRegex(parts[2], r"^\d{8}$")
        self.assertTrue(Path(parts[3]).name.startswith(f"{self.JOB_ID}-"))
        self.assertEqual(Path(parts[3]).suffix, ".log")
        # Secrets travel only inside the ephemeral framed recipe; neither
        # argv nor the persistent volume ever carries them.
        self.assertIn(self.SECRET, seams["frames"][0])
        self.assertNotIn(self.SECRET, f"{args!r}{kwargs!r}")
        self.assertEqual([p.name for p in Path(self.root).rglob("*.recipe.json")], [])
        # S3: the retention sweep prunes exactly the root that was written to.
        self.assertEqual(len(seams["prunes"]), 1)
        self.assertEqual(Path(seams["prunes"][0][0][0]), Path(self.root))
        # Existing live progress semantics are untouched.
        self.assertIn("ok", lines)
        self.assertFalse(any(self.WARNING_PREFIX in line for line in lines))
        # The executor container is not destroyed before capture finishes.
        self.assertEqual(trace, ["wait", "remove"])
        self.assertEqual(seams["process"].wait_timeout, DockerRuntimeAdapter.EXECUTION_LOG_DRAIN_SECONDS)

    def test_capture_setup_failure_keeps_the_backup_running_and_alerts_safely(self):
        container = self.container()
        with self.patch_capture(spawn_raises=ExecutionLogCaptureUnavailable("detached execution log capture could not start")):
            runtime = self.runtime(container)
            lines = []
            with self.assertLogs("src.worker_agent.infrastructure.adapters.docker_runtime", level="WARNING") as logged:
                result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["logs"], "ok")
        container.remove.assert_called_once_with(force=True)
        self.assertEqual(ExecutionLogCapture.prune.call_count, 0)
        # Exactly one actionable, safe warning reaches the Control Plane.
        warnings = [line for line in lines if self.WARNING_PREFIX in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn(self.JOB_ID, warnings[0])
        self.assertIn("detached capture process could not start", warnings[0])
        self.assertNotIn(self.SECRET, warnings[0])
        joined_logs = "".join(logged.output)
        self.assertNotIn(self.SECRET, joined_logs)

    def test_failed_spawn_leaves_no_recipe_file(self):
        """A spawn failure after the frame encode leaves no secret recipe
        behind: the ephemeral handoff never creates a one-shot file at all."""
        container = self.container()
        with self.patch_capture(spawn_raises=ExecutionLogCaptureUnavailable("detached execution log capture could not start"), real_recipe=True):
            runtime = self.runtime(container)
            lines = []
            result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        self.assertEqual(
            [path.name for path in Path(self.root).rglob("*.recipe.json")],
            [],
        )
        warnings = [line for line in lines if self.WARNING_PREFIX in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("detached capture process could not start", warnings[0])

    def test_worker_death_before_child_consumes_secrets_leaves_no_plaintext_recipe(self):
        """L63 (persistent-worker-execution-logs.md): a worker/child death
        between recipe preparation and the capture child consuming it used to
        leave the plaintext-secret recipe file on the persistent volume
        indefinitely. Ephemeral framed-stdin transfer never writes a recipe
        file, so even an abrupt creator death leaves nothing on disk."""
        container = self.container()
        # Simulated crash: the frame is really encoded, the spawned child
        # never executes (dies before its frame read) and nothing removes it.
        with self.patch_capture(real_recipe=True):
            runtime = self.runtime(container)
            first = runtime.run_runtime_job("runtime", self.payload())

        self.assertTrue(first["success"], first)
        leftovers = [path for path in Path(self.root).rglob("*.recipe.json")]
        self.assertEqual(leftovers, [])

    def test_recipe_frame_write_failure_never_blocks_capture_or_the_job(self):
        """S4: a failed stdin-write handoff of the framed recipe is a logging
        problem; capture still starts and the backup result is untouched."""
        container = self.container()
        with self.patch_capture() as seams:
            runtime = self.runtime(container)
            with patch("os.write", side_effect=BrokenPipeError("child died")):
                lines = []
                result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["logs"], "ok")
        seams["spawn"].assert_called_once()
        self.assertEqual([line for line in lines if self.WARNING_PREFIX in line], [])

    def test_recipe_failure_skips_capture_and_alerts_without_false_failure(self):
        container = self.container()
        with self.patch_capture(recipe_raises=ExecutionLogCaptureUnavailable("redaction recipe frame could not be encoded")):
            runtime = self.runtime(container)
            lines = []
            result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["logs"], "ok")
        self.assertEqual(ExecutionLogCapture.spawn_detached_docker_capture.call_count, 0)
        warnings = [line for line in lines if self.WARNING_PREFIX in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("redaction recipe frame could not be encoded", warnings[0])

    def test_unsupported_docker_host_skips_capture_and_alerts(self):
        container = self.container()
        host_patch = patch.dict(os.environ, {"DOCKER_HOST": "tcp://127.0.0.1:2375"})
        with self.patch_capture(spawn_raises=AssertionError("unsupported daemon location must never spawn")):
            with host_patch:
                runtime = self.runtime(container)
                lines = []
                result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        self.assertEqual(ExecutionLogCapture.spawn_detached_docker_capture.call_count, 0)
        warnings = [line for line in lines if self.WARNING_PREFIX in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("daemon", warnings[0].lower())

    def test_supported_docker_host_socket_forms_are_passed_to_the_child(self):
        for host, expected_socket in (
            ("unix:///var/run/docker.sock", "/var/run/docker.sock"),
            ("npipe:////./pipe/docker_engine", "//./pipe/docker_engine"),
        ):
            with self.subTest(host=host):
                with self.patch_capture():
                    with patch.dict(os.environ, {"DOCKER_HOST": host}):
                        runtime = self.runtime(self.container())
                        result = runtime.run_runtime_job("runtime", self.payload())
                self.assertTrue(result["success"], result)
                _, kwargs = self.spawn_kwargs()
                self.assertEqual(kwargs["socket_path"], expected_socket)

    def test_unknown_worker_identity_skips_capture_and_alerts(self):
        container = self.container()
        with self.patch_capture(spawn_raises=AssertionError("unregistered workers must not spawn capture")):
            runtime = self.runtime(container, worker_id=None)
            lines = []
            result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        self.assertEqual(ExecutionLogCapture.spawn_detached_docker_capture.call_count, 0)
        warnings = [line for line in lines if self.WARNING_PREFIX in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("worker identity", warnings[0])

    def test_invalid_container_identity_skips_capture_and_alerts(self):
        container = self.container("bad container id")
        with self.patch_capture(spawn_raises=AssertionError("unusable container identities must not spawn capture")):
            runtime = self.runtime(container)
            lines = []
            result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        self.assertEqual(ExecutionLogCapture.spawn_detached_docker_capture.call_count, 0)
        warnings = [line for line in lines if self.WARNING_PREFIX in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("container identity", warnings[0])

    def test_capture_child_rejection_after_drain_alerts_the_control_plane(self):
        container = self.container()
        with self.patch_capture(exit_code=3):
            runtime = self.runtime(container)
            lines = []
            result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)

        self.assertTrue(result["success"], result)
        warnings = [line for line in lines if self.WARNING_PREFIX in line]
        self.assertEqual(len(warnings), 1)
        self.assertIn("log file could not be created", warnings[0])

    def test_still_streaming_capture_after_job_finish_keeps_partial_evidence_without_false_failure(self):
        trace = []
        container = self.container()
        container.remove.side_effect = lambda **kwargs: trace.append("remove")

        class StuckChild:
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="capture", timeout=timeout)

            def poll(self):
                return None

        with self.patch_capture(spawn=StuckChild()):
            runtime = self.runtime(container)
            result = runtime.run_runtime_job("runtime", self.payload())

        self.assertTrue(result["success"], result)
        self.assertIn("remove", trace)

    def test_cancellation_does_not_wait_for_capture_and_keeps_prior_result(self):
        container = self.container()
        container.wait.side_effect = TimeoutError("wait timed out")
        runtime = self.runtime(container)
        checks = iter((False, False, True))

        class MustNotWait:
            def poll(self):
                return None

            def wait(self, timeout=None):
                raise AssertionError("canceled jobs must not wait for the capture child")

        with self.patch_capture(spawn=MustNotWait()):
            result = runtime.run_runtime_job(
                "runtime",
                {**self.payload(), "timeout_seconds": 2},
                cancel_check=lambda: next(checks),
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["canceled"])
        self.assertEqual(result["status_code"], 130)
        container.stop.assert_called_once_with(timeout=1)
        container.remove.assert_called_once_with(force=True)

    def test_default_execution_log_root_follows_the_persistent_worker_state_volume(self):
        """S1: deployed Docker workers mount worker_state at /data only; with
        no explicit WORKER_EXECUTION_LOG_ROOT the fixed log structure must
        default under the persistent worker state/recovery directory
        (existing compose convention: WORKER_CREDENTIAL_FILE=/data/...
        alongside worker_job_recovery.json), never the ephemeral container
        filesystem."""
        self.drop_env("WORKER_EXECUTION_LOG_ROOT")
        os.environ["WORKER_CREDENTIAL_FILE"] = "/data/worker_credentials.json"

        with self.patch_capture() as seams:
            runtime = self.runtime(self.container())
            result = runtime.run_runtime_job("runtime", self.payload())

        self.assertTrue(result["success"], result)
        seams["spawn"].assert_called_once()
        args, kwargs = self.spawn_kwargs()
        log_path, recipe_path, container_id = args
        self.assertEqual(container_id, self.CONTAINER_ID)
        self.assertIsNone(recipe_path)
        log_root = Path(log_path).parents[3]
        self.assertEqual(log_root.name, "execution-logs")
        self.assertEqual(log_root.parent, Path("/data"))
        self.assertEqual(Path(log_path).parents[2].name, self.worker_id)
        self.assertEqual(Path(log_path).parents[1].name, "docker")
        self.assertRegex(Path(log_path).parents[0].name, r"^\d{8}$")
        self.assertTrue(Path(log_path).name.startswith(f"{self.JOB_ID}-"))
        self.assertEqual(Path(log_path).suffix, ".log")
        # The retention sweep must prune the same persistent root it wrote to.
        self.assertEqual(len(seams["prunes"]), 1)
        self.assertEqual(Path(seams["prunes"][0][0][0]), log_root)

    def test_execution_log_root_precedence_state_recovery_then_committed_default(self):
        cases = [
            ({"WORKER_STATE_DIR": "/state"}, "/state/execution-logs"),
            (
                {"WORKER_STATE_DIR": "/state", "WORKER_CREDENTIAL_FILE": "/data/worker_credentials.json"},
                "/state/execution-logs",
            ),
            (
                {"WORKER_JOB_RECOVERY_FILE": "/recover/worker_job_recovery.json"},
                "/recover/execution-logs",
            ),
            (
                {"WORKER_JOB_RECOVERY_FILE": "/recover/worker_job_recovery.json", "WORKER_CREDENTIAL_FILE": "/data/worker_credentials.json"},
                "/recover/execution-logs",
            ),
            (
                {"WORKER_CREDENTIAL_FILE": "/data/worker_credentials.json"},
                "/data/execution-logs",
            ),
            ({}, None),
        ]
        for environment, expected in cases:
            with self.subTest(environment=environment, expected=expected):
                self.drop_env("WORKER_EXECUTION_LOG_ROOT")
                self.drop_env("WORKER_STATE_DIR", "WORKER_JOB_RECOVERY_FILE", "WORKER_CREDENTIAL_FILE")
                os.environ.update(environment)
                if expected is None:
                    expected = str(ExecutionLogCapture.default_root())
                with self.patch_capture() as seams:
                    runtime = self.runtime(self.container())
                    result = runtime.run_runtime_job("runtime", self.payload())
                self.assertTrue(result["success"], result)
                self.assertEqual(len(seams["prunes"]), 1)
                self.assertEqual(Path(seams["prunes"][0][0][0]), Path(expected))

    def test_explicit_work_execution_log_root_keeps_precedence_over_worker_state(self):
        self.drop_env("WORKER_STATE_DIR", "WORKER_JOB_RECOVERY_FILE", "WORKER_CREDENTIAL_FILE")
        os.environ["WORKER_STATE_DIR"] = "/state"
        os.environ["WORKER_CREDENTIAL_FILE"] = "/data/worker_credentials.json"
        with self.patch_capture() as seams:
            runtime = self.runtime(self.container())
            result = runtime.run_runtime_job("runtime", self.payload())
        self.assertTrue(result["success"], result)
        self.assertEqual(Path(seams["prunes"][0][0][0]), Path(self.root))
        _, kwargs = self.spawn_kwargs()
        self.assertIsNone(kwargs["socket_path"])

    def test_same_second_capture_shifts_to_a_free_log_name_without_touching_stored_evidence(self):
        frozen = datetime(2026, 1, 7, 12, 0, 0, tzinfo=timezone.utc)
        calls = []
        real_job_log_path = ExecutionLogCapture.__dict__["job_log_path"].__get__(ExecutionLogCapture)

        def frozen_job_log_path(root, worker_id, runtime_kind, job_id, *, when=None):
            calls.append((root, worker_id, runtime_kind, job_id))
            return real_job_log_path(root, worker_id, runtime_kind, job_id, when=frozen)

        container = self.container()
        with self.patch_capture(), patch.object(ExecutionLogCapture, "job_log_path", side_effect=frozen_job_log_path):
            prior = ExecutionLogCapture.job_log_path(str(Path(self.root)), "worker-a", "docker", self.JOB_ID, when=frozen)
            prior.parent.mkdir(parents=True, exist_ok=True)
            prior.write_text("prior capture evidence", encoding="utf-8")
            calls.clear()  # Drop the test's own probe call; only the adapter's call is asserted.
            runtime = self.runtime(container)
            result = runtime.run_runtime_job("runtime", self.payload())

        self.assertTrue(result["success"], result)
        seams_spawn_args, kwargs = self.spawn_kwargs()
        log_path, recipe_path, container_id = seams_spawn_args
        self.assertEqual(calls, [(self.root, "worker-a", "docker", self.JOB_ID)])
        # Same-second collision resolved by shifting to a free sibling name.
        self.assertEqual(log_path, prior.with_name(f"{prior.stem}-1{prior.suffix}"))
        self.assertIsNone(recipe_path)
        # The stored prior capture was never opened, moved or rewritten.
        self.assertEqual(prior.read_text(encoding="utf-8"), "prior capture evidence")

    def test_job_without_capture_conditions_keeps_the_previous_result_shape(self):
        """PRESERVE: previous behavior of run_runtime_job holds when capture
        cannot run (no worker identity): same success shape, same cleanup."""
        container = self.container()
        runtime = self.runtime(container, worker_id=None)
        lines = []
        result = runtime.run_runtime_job("runtime", self.payload(), output_callback=lines.append)
        self.assertTrue(result["success"])
        self.assertEqual(result["status_code"], 0)
        self.assertEqual(result["logs"], "ok")
        self.assertEqual(result["stderr"], "")
        self.assertIn("ok", lines)
        container.remove.assert_called_once_with(force=True)


class DockerLogTimestampCaptureTests(unittest.TestCase):
    """S2/T2: Docker streaming lines expose real source timestamps as parallel
    metadata; prefixes are stripped from the text contract and never fabricated."""

    def setUp(self):
        capture = patch.object(DockerRuntimeAdapter, "_start_execution_log_capture", return_value=None)
        capture.start()
        self.addCleanup(capture.stop)

    @staticmethod
    def streaming_container(chunks):
        container = Mock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.return_value = iter(chunks)
        return container

    @staticmethod
    def runtime(container):
        runtime = DockerRuntimeAdapter.__new__(DockerRuntimeAdapter)
        runtime.client = Mock()
        runtime.client.containers.run.return_value = container
        runtime.timeout_seconds = 30.0
        runtime.no_lock = False
        runtime.cache_dir = None
        return runtime

    def test_docker_streaming_captures_real_source_timestamps_and_strips_the_prefix(self):
        container = self.streaming_container([
            b"2024-05-05T10:00:00.123456789Z starting backup\n",
            b"2024-05-05T10:00:0",
            b"1.000000000Z finished\n",
        ])
        runtime = self.runtime(container)

        result = runtime.run_runtime_job("runtime", {"command": "/root/backup.sh"})

        self.assertTrue(result["success"], result)
        stream_call = container.logs.call_args
        self.assertTrue(stream_call.kwargs["timestamps"])
        self.assertTrue(stream_call.kwargs["stream"])
        self.assertEqual(result["logs"], "starting backup\nfinished\n")
        self.assertEqual(
            result["log_timestamps"],
            ["2024-05-05T10:00:00.123456789Z", "2024-05-05T10:00:01.000000000Z"],
        )

    def test_non_streamed_output_never_carries_fabricated_timestamps(self):
        container = Mock()
        container.wait.return_value = {"StatusCode": 0}
        container.logs.side_effect = [RuntimeError("stream refused"), b"plain output\n"]
        runtime = self.runtime(container)

        result = runtime.run_runtime_job("runtime", {"command": "/root/backup.sh"})

        self.assertTrue(result["success"], result)
        fallback_call = container.logs.call_args_list[-1]
        self.assertIn("timestamps", fallback_call.kwargs)
        self.assertFalse(fallback_call.kwargs["timestamps"])
        self.assertNotIn("log_timestamps", result)
        self.assertEqual(result["logs"], "plain output\n")

    def test_captured_timestamps_survive_redaction_and_never_leak_secrets(self):
        secret = "restic-password-value"
        container = self.streaming_container(
            [f"2024-05-05T10:00:00Z token={secret}\n".encode("utf-8")]
        )
        runtime = self.runtime(container)

        result = runtime.run_runtime_job(
            "runtime",
            {"command": "/root/backup.sh", "environment": {"RESTIC_PASSWORD": secret}},
        )

        self.assertTrue(result["success"], result)
        self.assertNotIn(secret, repr(result))
        self.assertEqual(result["logs"], "token=<redacted>\n")
        self.assertEqual(result["log_timestamps"], ["2024-05-05T10:00:00Z"])

    def test_timestamp_capture_keeps_restic_json_parsing_working(self):
        container = self.streaming_container(
            [b'2024-05-05T10:00:00Z [{"id": "abcdef1234567890", "short_id": "abcdef1234567890"}]\n']
        )
        runtime = self.runtime(container)

        result = runtime.list_restic_snapshots("runtime", {"command": "restic snapshots --json"})

        self.assertTrue(result["success"], result)
        self.assertEqual(
            result["snapshots"],
            [{"id": "abcdef1234567890", "short_id": "abcdef1234567890"}],
        )

    def test_docker_streaming_emits_line_aligned_callback_metadata(self):
        container = self.streaming_container([
            b"2024-05-05T10:00:00.123456789Z starting backup\n",
            b"2024-05-05T10:00:0",
            b"1.000000000Z finished\n",
        ])
        runtime = self.runtime(container)
        collected = []

        def collect(text, timestamps=None):
            collected.append((text, timestamps))

        result = runtime.run_runtime_job(
            "runtime", {"command": "/root/backup.sh"}, output_callback=collect
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(
            [(text, stamps) for text, stamps in collected if text.strip()],
            [
                ("starting backup\n", ["2024-05-05T10:00:00.123456789Z"]),
                ("finished\n", ["2024-05-05T10:00:01.000000000Z"]),
            ],
        )

    def test_timestamp_capture_preserves_stream_chunk_emission_granularity(self):
        first_ts = "2024-05-05T10:00:00Z"
        second_ts = "2024-05-05T10:00:01Z"
        container = self.streaming_container([
            f"{first_ts} first line\n".encode(),
            f"{second_ts} partial ".encode(),
            b"continuation\n",
        ])
        runtime = self.runtime(container)
        collected = []

        def collect(text, timestamps=None):
            collected.append((text, timestamps))

        result = runtime.run_runtime_job(
            "runtime", {"command": "/root/backup.sh"}, output_callback=collect
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["logs"], "first line\npartial continuation\n")
        self.assertEqual(result["log_timestamps"], [first_ts, second_ts])
        self.assertEqual(
            [text for text, _ in collected if text],
            ["first line\n", "partial ", "continuation\n"],
        )
        self.assertEqual(collected[1][1], [second_ts])

    def test_multiline_secret_is_redacted_atomically_and_timestamps_are_withheld(self):
        secret = "line-one\nline-two\nline-three"
        container = self.streaming_container([
            b"2024-05-05T10:00:00Z line-one\n",
            b"line-two\n",
            b"line-three next\n",
        ])
        runtime = self.runtime(container)

        result = runtime.run_runtime_job(
            "runtime",
            {
                "command": "/root/backup.sh",
                "environment": {"MULTILINE_SECRET": secret},
            },
        )

        self.assertTrue(result["success"], result)
        self.assertNotIn("line-one", repr(result))
        self.assertEqual(result["logs"], "<redacted> next\n")
        self.assertNotIn("log_timestamps", result)


if __name__ == "__main__":
    unittest.main()
