import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from src.control_plane.domain.models import JobStatus
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
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


class DockerLogTimestampCaptureTests(unittest.TestCase):
    """S2/T2: Docker streaming lines expose real source timestamps as parallel
    metadata; prefixes are stripped from the text contract and never fabricated."""

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
