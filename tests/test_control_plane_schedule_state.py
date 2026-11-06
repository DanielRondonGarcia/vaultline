import http.client
import json
import threading
import time
import unittest
import uuid
from datetime import datetime
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

from src.control_plane.application.services.control_plane_service import (
    ControlPlaneService,
    MAX_OFFLINE_RESULT_BYTES,
)
from src.control_plane.domain.models import (
    BackupTargetRecord,
    JobRecord,
    WorkerRecord,
    WorkerScheduleClaimRecord,
    utcnow,
)
from src.control_plane.infrastructure.repositories.in_memory import (
    InMemoryInventoryRepository,
    InMemoryJobRepository,
    InMemoryRetentionPolicyRepository,
    InMemorySecretRepository,
    InMemorySettingsRepository,
    InMemorySnapshotRepository,
    InMemoryStorageProfileRepository,
    InMemoryTargetRepository,
    InMemoryTargetStatsRepository,
    InMemoryWorkerRepository,
    InMemoryWorkerScheduleClaimRepository,
)
from src.control_plane.main import ControlPlaneRequestHandler
from src.security.hmac_protocol import digest_secret, sign_request


def _record(**overrides):
    record = {
        "id": "offline:target-a:2025-05-05T03:15:00+00:00",
        "worker_id": "worker-a",
        "target_id": "target-a",
        "trigger": "offline_schedule",
        "cron_expression": "15 3 * * *",
        "executed_at": "2025-05-05T03:15:05+00:00",
        "status": "succeeded",
        "result_summary": {"status_code": 0, "target_id": "target-a"},
        "log_lines": ["(offline backup log line)"],
    }
    record.update(overrides)
    return record


class ScheduleStateServiceTests(unittest.TestCase):
    """Control-plane support for worker autonomous scheduling: S1-S3."""

    def make_service(self, claims_repository=None, job_repository=None):
        workers = InMemoryWorkerRepository()
        workers.save(WorkerRecord(name="worker-a", host_name="test", id="worker-a", status="online", last_seen_at=utcnow()))
        workers.save(WorkerRecord(name="worker-b", host_name="test", id="worker-b", status="online", last_seen_at=utcnow()))
        targets = InMemoryTargetRepository()
        targets.save(
            BackupTargetRecord(
                name="target-a",
                worker_id="worker-a",
                id="target-a",
                cron_expression="15 3 * * *",
                runtime_environment={"RESTIC_REPOSITORY": "local:/repo-a"},
            )
        )
        targets.save(
            BackupTargetRecord(
                name="target-b",
                worker_id="worker-b",
                id="target-b",
                cron_expression="30 4 * * *",
                runtime_environment={"RESTIC_REPOSITORY": "local:/repo-b"},
            )
        )
        return ControlPlaneService(
            worker_repository=workers,
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=targets,
            job_repository=job_repository or InMemoryJobRepository(),
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=object(),
            settings_repository=InMemorySettingsRepository(),
            worker_schedule_claim_repository=claims_repository or InMemoryWorkerScheduleClaimRepository(),
        )

    # ------------------------------------------------------- schedule state

    def test_entries_beyond_worker_limit_remain_scheduled_by_control_plane(self):
        service = self.make_service()
        for index in range(33):
            service.target_repository.save(BackupTargetRecord(
                id=f"extra-{index:02d}", name=f"extra-{index:02d}", worker_id="worker-a",
                cron_expression="* * * * *", runtime_environment={"RESTIC_REPOSITORY": "local:/repo"},
            ))
        state = service.get_worker_schedule_state("worker-a")
        owned = set(service.schedule_ownership_status("worker-a")["target_ids"])
        received = {entry["target_id"] for entry in state["entries"]}
        self.assertEqual(owned, received)

    def test_worker_does_not_claim_target_with_active_control_plane_backup(self):
        service = self.make_service()
        service.dispatch_backup_for_target("target-a", trigger="schedule")
        state = service.get_worker_schedule_state("worker-a")
        self.assertNotIn("target-a", {entry["target_id"] for entry in state["entries"]})
        self.assertNotIn("target-a", service.schedule_ownership_status("worker-a")["target_ids"])

    def test_unknown_outcome_blocks_worker_handover_until_resolved(self):
        service = self.make_service()
        job = service.dispatch_backup_for_target("target-a", trigger="schedule")
        job.status = "unconfirmed"
        service.job_repository.save(job)
        state = service.get_worker_schedule_state("worker-a")
        self.assertNotIn("target-a", {entry["target_id"] for entry in state["entries"]})
        self.assertNotIn("target-a", service.schedule_ownership_status("worker-a")["target_ids"])

    def test_reassigned_target_keeps_old_owner_until_safe_handover(self):
        service = self.make_service()
        service.get_worker_schedule_state("worker-a")
        target = service.target_repository.get("target-a")
        target.worker_id = "worker-b"
        service.target_repository.save(target)
        service.get_worker_schedule_state("worker-a")
        state = service.get_worker_schedule_state("worker-b")
        self.assertNotIn("target-a", {entry["target_id"] for entry in state["entries"]})
        with self.assertRaises(ValueError):
            service.dispatch_backup_for_target("target-a", trigger="schedule")

    def test_stale_schedule_ack_does_not_release_previous_claim(self):
        service = self.make_service()
        old_state = service.get_worker_schedule_state("worker-a")
        target = service.target_repository.get("target-a")
        target.worker_id = "worker-b"
        service.target_repository.save(target)
        service.get_worker_schedule_state("worker-a")
        with self.assertRaises(ValueError):
            service.acknowledge_worker_schedule_state("worker-a", old_state["revision"])
        self.assertIn("target-a", service.schedule_ownership_status("worker-a")["target_ids"])

    def test_reassigned_target_moves_only_after_old_worker_acknowledges(self):
        service = self.make_service()
        service.get_worker_schedule_state("worker-a")
        target = service.target_repository.get("target-a")
        target.worker_id = "worker-b"
        service.target_repository.save(target)
        new_state = service.get_worker_schedule_state("worker-a")
        self.assertNotIn("target-a", {entry["target_id"] for entry in service.get_worker_schedule_state("worker-b")["entries"]})
        service.acknowledge_worker_schedule_state("worker-a", new_state["revision"])
        self.assertIn("target-a", {entry["target_id"] for entry in service.get_worker_schedule_state("worker-b")["entries"]})

    def test_schedule_state_returns_version_revision_resolved_payloads_and_timezone(self):
        with patch.dict("os.environ", {"CONTROL_PLANE_TIMEZONE": "UTC"}):
            service = self.make_service()
            state = service.get_worker_schedule_state("worker-a")

            self.assertEqual(state["version"], 1)
            self.assertEqual(state["timezone"], "UTC")
            self.assertIn("revision", state)
            self.assertIn("generated_at", state)
            [entry] = state["entries"]
            self.assertEqual(entry["target_id"], "target-a")
            self.assertEqual(entry["cron_expression"], "15 3 * * *")
            self.assertTrue(entry["enabled"])
            with self.subTest("payload is resolved for offline execution"):
                self.assertEqual(entry["payload"]["target_id"], "target-a")
                self.assertEqual(entry["payload"]["environment"]["RESTIC_REPOSITORY"], "local:/repo-a")
                self.assertIn("BACKUP_STOP_CONTAINERS", entry["payload"]["environment"])

            again = service.get_worker_schedule_state("worker-a")
            self.assertEqual(again["revision"], state["revision"])

    def test_schedule_state_revision_changes_when_schedule_changes(self):
        service = self.make_service()
        first = service.get_worker_schedule_state("worker-a")

        target = service.target_repository.get("target-a")
        target.cron_expression = "15 4 * * *"
        service.target_repository.save(target)
        second = service.get_worker_schedule_state("worker-a")
        self.assertNotEqual(first["revision"], second["revision"])

        target.runtime_environment = {"RESTIC_REPOSITORY": "local:/repo-changed"}
        service.target_repository.save(target)
        third = service.get_worker_schedule_state("worker-a")
        self.assertNotEqual(second["revision"], third["revision"])

    def test_schedule_state_excludes_global_cron_targets_disabled_and_foreign_targets(self):
        service = self.make_service()
        with self.subTest("global cron targets participate (same worker only)"):
            service.update_settings(global_cron_expression="0 5 * * *")
            target_c = BackupTargetRecord(
                name="target-c", worker_id="worker-a", id="target-c",
                runtime_environment={"RESTIC_REPOSITORY": "local:/repo-c"},
            )
            service.target_repository.save(target_c)
            state = service.get_worker_schedule_state("worker-a")
            self.assertEqual(
                [entry["target_id"] for entry in state["entries"]],
                ["target-a", "target-c"],
            )

        with self.subTest("disabled targets are excluded"):
            target = service.target_repository.get("target-a")
            target.enabled = False
            service.target_repository.save(target)
            state = service.get_worker_schedule_state("worker-a")
            self.assertEqual([entry["target_id"] for entry in state["entries"]], ["target-c"])

        with self.subTest("other workers' targets are never included"):
            state_b = service.get_worker_schedule_state("worker-b")
            self.assertEqual([entry["target_id"] for entry in state_b["entries"]], ["target-b"])

    def test_schedule_claim_suppresses_scheduled_dispatch_and_keeps_manual_dispatch(self):
        service = self.make_service()
        with self.subTest("behavior is preserved before any schedule delivery"):
            preserved = service.dispatch_backup_for_target("target-a", requested_by="scheduler", trigger="schedule")
            self.assertEqual(preserved.trigger, "schedule")
            self.assertEqual(preserved.worker_id, "worker-a")
            # Ownership is transferred only once the earlier backup is terminal.
            preserved.status = "succeeded"
            service.job_repository.save(preserved)

        service.get_worker_schedule_state("worker-a")
        with self.subTest("scheduled dispatch is suppressed with a visible reason"):
            with self.assertRaises(ValueError) as ctx:
                service.dispatch_backup_for_target("target-a", requested_by="scheduler", trigger="schedule")
            self.assertIn("owned by worker 'worker-a'", str(ctx.exception))

        with self.subTest("manual dispatch keeps working for the owning worker"):
            job = service.dispatch_backup_for_target("target-a", requested_by="api", trigger="manual")
            self.assertEqual(job.trigger, "manual")

        with self.subTest("targets the worker does not claim keep CP scheduling"):
            job = service.dispatch_backup_for_target("target-b", requested_by="scheduler", trigger="schedule")
            self.assertEqual(job.trigger, "schedule")
            self.assertEqual(job.worker_id, "worker-b")

        scheduled = [
            job.id for job in service.job_repository.list()
            if job.trigger == "schedule" and job.target_id == "target-a"
        ]
        self.assertEqual(scheduled, [preserved.id])

    def test_schedule_ownership_status_is_failure_visible(self):
        service = self.make_service()
        before = service.schedule_ownership_status("worker-a")
        self.assertFalse(before["claims_schedule"])
        service.get_worker_schedule_state("worker-a")
        status = service.schedule_ownership_status("worker-a")
        self.assertTrue(status["claims_schedule"])
        self.assertEqual(status["target_ids"], ["target-a"])
        self.assertEqual(status["worker_id"], "worker-a")
        self.assertIn("revision", status)

    def test_payload_build_failure_is_recorded_and_remains_control_plane_visible(self):
        service = self.make_service()
        target = service.target_repository.get("target-a")
        target.runtime_environment = {"RESTIC_REPOSITORY": "local:/repo-a", "BACKUP_STRATEGY": "tar"}
        target.backup_strategy = None
        service.target_repository.save(target)

        state = service.get_worker_schedule_state("worker-a")
        self.assertEqual(state["entries"], [])
        status = service.schedule_ownership_status("worker-a")
        self.assertFalse(status["claims_schedule"])
        self.assertIn("target-a", status["errors"])
        self.assertIn("BACKUP_STRATEGY", status["errors"]["target-a"])

        # CP scheduling for the failed target is not suppressed: the failure
        # stays a loud control-plane dispatch failure instead of silence.
        with self.assertRaises(ValueError) as ctx:
            service.dispatch_backup_for_target("target-a", requested_by="scheduler", trigger="schedule")
        self.assertNotIn("owned by worker", str(ctx.exception))

    def test_admin_release_requires_revocation_exact_target_and_confirmation(self):
        service = self.make_service()
        service.get_worker_schedule_state("worker-a")
        with self.assertRaises(ValueError):
            service.release_worker_schedule_claim("worker-a", "target-a", "WORKER_STOPPED")
        service.revoke_worker("worker-a")
        for target_id, confirmation in (("target-b", "WORKER_STOPPED"), ("target-a", "yes")):
            with self.assertRaises(ValueError):
                service.release_worker_schedule_claim("worker-a", target_id, confirmation)
        self.assertIn("target-a", service.schedule_ownership_status("worker-a")["target_ids"])
        service.release_worker_schedule_claim("worker-a", "target-a", "WORKER_STOPPED")
        self.assertNotIn("target-a", service.schedule_ownership_status("worker-a")["target_ids"])

    def test_worker_deletion_requires_acknowledged_handover(self):
        service = self.make_service()
        service.get_worker_schedule_state("worker-a")
        with self.assertRaisesRegex(Exception, "schedule"):
            service.delete_worker("worker-a")
        self.assertIsNotNone(service.worker_repository.get("worker-a"))
        target = service.target_repository.get("target-a")
        target.enabled = False
        service.target_repository.save(target)
        state = service.get_worker_schedule_state("worker-a")
        service.acknowledge_worker_schedule_state("worker-a", state["revision"])
        self.assertTrue(service.delete_worker("worker-a"))

    # ------------------------------------------------------- offline results

    def test_offline_result_identity_must_match_its_target(self):
        service = self.make_service()
        record = _record(id="offline:target-b:2025-05-05T03:15:00+00:00")
        with self.assertRaises(ValueError):
            service.record_offline_result("worker-a", record)
        self.assertIsNone(service.job_repository.get(record["id"]))

    def test_worker_cannot_report_results_for_another_workers_target(self):
        service = self.make_service()
        with self.assertRaises(ValueError):
            service.record_offline_result("worker-b", _record(worker_id="worker-b"))
        self.assertIsNone(service.job_repository.get(_record()["id"]))

    def test_offline_result_is_stored_once_and_visible_in_job_history(self):
        job_repository = InMemoryJobRepository()
        service = self.make_service(job_repository=job_repository)
        record_id = _record()["id"]

        outcome = service.record_offline_result("worker-a", _record())
        self.assertEqual(outcome["acknowledged"], True)
        self.assertFalse(outcome["duplicate"])
        self.assertEqual(outcome["job_id"], record_id)

        [job] = job_repository.list()
        self.assertEqual(job.id, record_id)
        self.assertEqual(job.worker_id, "worker-a")
        self.assertEqual(job.command, "backup.run")
        self.assertEqual(job.trigger, "offline_schedule")
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.result_summary["status_code"], 0)
        self.assertEqual(job.log_lines, ["(offline backup log line)"])
        self.assertEqual(job.submitted_at, datetime(2025, 5, 5, 3, 15, 5))
        self.assertEqual(job.started_at, job.submitted_at)
        self.assertEqual(job.finished_at, job.started_at)

        listed, total = job_repository.list_for_listing(filters={"trigger": "offline_schedule"})
        self.assertEqual(total, 1)
        self.assertEqual(listed[0].id, record_id)

        replay = service.record_offline_result("worker-a", _record())
        self.assertTrue(replay["acknowledged"])
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["job_id"], record_id)
        self.assertEqual(len(job_repository.list()), 1)

    def test_failed_offline_result_is_visible_and_triggers_failure_notifications(self):
        job_repository = InMemoryJobRepository()
        service = self.make_service(job_repository=job_repository)
        with patch.object(service, "_schedule_failed_backup_notifications") as notify:
            outcome = service.record_offline_result("worker-a", _record(status="failed"))
        self.assertEqual(outcome["acknowledged"], True)
        [job] = job_repository.list()
        self.assertEqual(job.status, "failed")
        self.assertEqual(notify.call_count, 1)

    def test_offline_result_submission_bounded_and_idempotent_against_history(self):
        job_repository = InMemoryJobRepository()
        service = self.make_service(job_repository=job_repository)
        with self.subTest("log volume is bounded like every other job result"):
            service.record_offline_result("worker-a", _record(log_lines=["line-%d" % index for index in range(1500)]))
            [job] = job_repository.list()
            self.assertEqual(len(job.log_lines), service.MAX_JOB_LOG_LINES)

        with self.subTest("a record id that already exists in history is a duplicate ack"):
            job_repository.save(JobRecord(worker_id="worker-a", command="backup.run", id="offline:target-a:t1"))
            outcome = service.record_offline_result("worker-a", _record(id="offline:target-a:t1", status="failed"))
            self.assertTrue(outcome["duplicate"])
            self.assertEqual(outcome["job_id"], "offline:target-a:t1")
            [kept] = [job for job in job_repository.list() if job.id == "offline:target-a:t1"]
            # First write wins: the re-delivered content never overwrites history.
            self.assertEqual(kept.status, "pending")

    def test_offline_result_validation_rejects_input_without_storing(self):
        cases = {
            "missing id": _record(id=None),
            "unknown trigger": _record(trigger="manual"),
            "unknown status": _record(status="in_progress"),
            "identity mismatch": _record(worker_id="worker-b"),
            "missing executed_at": _record(executed_at=None),
            "invalid executed_at": _record(executed_at="not-a-timestamp"),
            "missing target": _record(target_id=None),
        }
        for name, record in cases.items():
            with self.subTest(name):
                job_repository = InMemoryJobRepository()
                service = self.make_service(job_repository=job_repository)
                with self.assertRaises(ValueError):
                    service.record_offline_result("worker-a", record)
                self.assertEqual(job_repository.list(), [])

        with self.subTest("oversized records are rejected"):
            job_repository = InMemoryJobRepository()
            service = self.make_service(job_repository=job_repository)
            oversized = _record(log_lines=["x" * 4096] * (MAX_OFFLINE_RESULT_BYTES // 4096 + 8))
            with self.assertRaises(ValueError):
                service.record_offline_result("worker-a", oversized)
            self.assertEqual(job_repository.list(), [])

        with self.subTest("non-object payloads are rejected"):
            service = self.make_service()
            with self.assertRaises(ValueError):
                service.record_offline_result("worker-a", ["not", "an", "object"])


class WorkerScheduleRouteTests(unittest.TestCase):
    """HMAC-authenticated worker routes for schedule state and offline results."""

    WORKER_SECRET = "w" * 32

    def setUp(self):
        workers = InMemoryWorkerRepository()
        workers.save(WorkerRecord(name="worker-a", host_name="test", id="worker-a", status="online", last_seen_at=utcnow()))
        targets = InMemoryTargetRepository()
        targets.save(
            BackupTargetRecord(
                name="target-a",
                worker_id="worker-a",
                id="target-a",
                cron_expression="15 3 * * *",
                runtime_environment={"RESTIC_REPOSITORY": "local:/repo-a"},
            )
        )
        self.claims = InMemoryWorkerScheduleClaimRepository()
        self.service = ControlPlaneService(
            worker_repository=workers,
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=targets,
            job_repository=InMemoryJobRepository(),
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=object(),
            settings_repository=InMemorySettingsRepository(),
            worker_schedule_claim_repository=self.claims,
        )
        from src.control_plane.infrastructure.security.worker_auth import WorkerAuthState
        self.auth = WorkerAuthState()
        self.auth.create_enrollment("worker-a", "host", {}, self.WORKER_SECRET, worker_id="worker-a")
        self.auth.complete(self.WORKER_SECRET)
        self.service.worker_auth = self.auth

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ControlPlaneRequestHandler)
        self.server.application = SimpleNamespace(
            auth_service=SimpleNamespace(issue_session_token=lambda *args: ""),
            control_plane_service=self.service,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def signed_request(self, path, body, signature="good"):
        encoded = json.dumps(body).encode("utf-8")
        timestamp = str(int(time.time()))
        nonce = f"nonce-{uuid.uuid4().hex}"
        if signature == "good":
            signature = sign_request(
                digest_secret(self.WORKER_SECRET), "POST", path, encoded, timestamp, nonce, "worker-a", "1"
            )
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        connection.request(
            "POST",
            path,
            encoded,
            headers={
                "Content-Type": "application/json",
                "X-Worker-ID": "worker-a",
                "X-Worker-Credential-Version": "1",
                "X-Worker-Timestamp": timestamp,
                "X-Worker-Nonce": nonce,
                "X-Worker-Signature": signature,
            },
        )
        response = connection.getresponse()
        raw = response.read()
        result = response.status, json.loads(raw) if raw else None
        connection.close()
        return result

    def test_schedule_state_route_requires_signed_worker_identity(self):
        status, body = self.signed_request("/api/v1/workers/worker-a/schedule-state", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["entries"][0]["target_id"], "target-a")
        # The delivery itself transferred schedule ownership (suppression active).
        self.assertTrue(self.service.schedule_ownership_status("worker-a")["claims_schedule"])

    def test_schedule_state_route_rejects_bad_worker_signature(self):
        status, body = self.signed_request(
            "/api/v1/workers/worker-a/schedule-state", {}, signature="bad"
        )
        self.assertEqual(status, 401)
        self.assertEqual(body.get("code"), "worker_auth_failed")
        self.assertFalse(self.service.schedule_ownership_status("worker-a")["claims_schedule"])

    def test_offline_results_route_stores_and_acknowledges_each_record(self):
        status, body = self.signed_request("/api/v1/workers/worker-a/offline-results", {"result": _record()})
        self.assertEqual(status, 200)
        self.assertTrue(body["acknowledged"])
        self.assertFalse(body["duplicate"])
        self.assertEqual(body["job_id"], _record()["id"])

        status, body = self.signed_request("/api/v1/workers/worker-a/offline-results", {"result": _record()})
        self.assertEqual(status, 200)
        self.assertTrue(body["duplicate"])

        [job] = self.service.job_repository.list()
        self.assertEqual(job.trigger, "offline_schedule")

        status, body = self.signed_request(
            "/api/v1/workers/worker-a/offline-results", {"result": _record(status="bogus")}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", body)


class WorkerScheduleClaimRepositoryTests(unittest.TestCase):
    """The claim storage mirrors the in-memory semantics in SQLite."""

    def test_sqlite_claim_repository_roundtrip(self):
        import os
        import tempfile

        from src.control_plane.infrastructure.repositories.sqlite import (
            SQLiteWorkerScheduleClaimRepository,
        )

        descriptor, database_path = tempfile.mkstemp(prefix="cp_claims_", suffix=".db")
        os.close(descriptor)
        try:
            repository = SQLiteWorkerScheduleClaimRepository(database_path)
            self.assertIsNone(repository.get_by_worker("worker-a"))
            claim = WorkerScheduleClaimRecord(
                worker_id="worker-a",
                revision="rev-1",
                target_ids=["target-a", "target-b"],
                errors={"target-c": "schedule payload build failed: missing secret"},
            )
            repository.save(claim)
            stored = repository.get_by_worker("worker-a")
            self.assertEqual(stored.revision, "rev-1")
            self.assertEqual(stored.target_ids, ["target-a", "target-b"])
            self.assertEqual(stored.errors, {"target-c": "schedule payload build failed: missing secret"})
            self.assertEqual([item.worker_id for item in repository.list()], ["worker-a"])
            self.assertTrue(repository.delete_by_worker("worker-a"))
            self.assertFalse(repository.delete_by_worker("worker-a"))
            self.assertIsNone(repository.get_by_worker("worker-a"))
        finally:
            if os.path.exists(database_path):
                os.unlink(database_path)


if __name__ == "__main__":
    unittest.main()
