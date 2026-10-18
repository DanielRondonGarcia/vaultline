import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.domain.models import JobRecord, JobStatus, WorkerRecord, WorkerStatus, utcnow
from src.control_plane.infrastructure.repositories.in_memory import (
    InMemoryCacheRepository,
    InMemoryIndexRepository,
    InMemoryInventoryRepository,
    InMemoryJobRepository,
    InMemoryNotificationChannelRepository,
    InMemoryRetentionPolicyRepository,
    InMemorySecretRepository,
    InMemorySettingsRepository,
    InMemorySnapshotRepository,
    InMemoryStorageProfileRepository,
    InMemoryTargetRepository,
    InMemoryTargetStatsRepository,
    InMemoryWorkerRepository,
)
from src.control_plane.infrastructure.repositories.sqlite import SQLiteNotificationChannelRepository
from src.control_plane.infrastructure.security.secret_codec import SecretCodec


class NotificationDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.codec = SecretCodec(Fernet.generate_key())
        self.workers = InMemoryWorkerRepository()
        self.workers.save(
            WorkerRecord(
                name="worker-a",
                host_name="test",
                id="worker-a",
                status=WorkerStatus.ONLINE,
                last_seen_at=utcnow(),
            )
        )
        self.jobs = InMemoryJobRepository()
        self.channels = InMemoryNotificationChannelRepository()
        self.service = ControlPlaneService(
            worker_repository=self.workers,
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=InMemoryTargetRepository(),
            job_repository=self.jobs,
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=self.codec,
            settings_repository=InMemorySettingsRepository(),
            cache_repository=InMemoryCacheRepository(),
            index_repository=InMemoryIndexRepository(),
            notification_channel_repository=self.channels,
        )
        self.channel = self.service.create_notification_channel(
            name="Operations webhook",
            channel_type="webhook",
            config={"url": "http://hooks.internal/backup?token=private-hook-token"},
        )
        self.job = self._new_job()

    def _new_job(self, command="backup.run", status=JobStatus.IN_PROGRESS, job_id="job-1"):
        job = JobRecord(
            worker_id="worker-a",
            command=command,
            target_id="target-a",
            id=job_id,
            status=status,
            owner_worker_id="worker-a",
            lease_token="lease-token",
            lease_issued_at=utcnow() - timedelta(minutes=1),
        )
        return self.jobs.save(job)

    def test_failed_backup_schedules_delivery_on_a_daemon_thread(self):
        with patch("threading.Thread") as thread_factory:
            result = self.service.update_job_status(
                worker_id="worker-a",
                job_id=self.job.id,
                status=JobStatus.FAILED,
                lease_token="lease-token",
                result_summary={"error": "backup command failed"},
                log_lines=["raw worker log must not be sent"],
            )

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(self.jobs.get(self.job.id).status, JobStatus.FAILED)
        thread_factory.assert_called_once()
        self.assertTrue(thread_factory.call_args.kwargs["daemon"])
        thread_factory.return_value.start.assert_called_once_with()

    def test_expired_lease_failure_also_schedules_notification(self):
        expired = self._new_job(job_id="job-expired")
        expired.lease_expires_at = utcnow() - timedelta(seconds=1)
        self.jobs.save(expired)

        with patch("threading.Thread") as thread_factory:
            reconciled = self.service._reconcile_expired_jobs()

        self.assertEqual(reconciled, 1)
        self.assertEqual(self.jobs.get(expired.id).status, JobStatus.FAILED)
        thread_factory.assert_called_once()
        thread_factory.return_value.start.assert_called_once_with()

    def test_delivery_error_does_not_change_failure_or_log_destination_secret(self):
        with self.assertLogs("src.control_plane.application.services.control_plane_service", level="WARNING") as captured:
            with patch("threading.Thread") as thread_factory:
                def execute_in_test_thread(**kwargs):
                    thread = Mock()
                    thread.start.side_effect = lambda: kwargs["target"](*kwargs["args"])
                    return thread

                thread_factory.side_effect = execute_in_test_thread
                with patch.object(self.service, "_send_notification_channel", side_effect=OSError("private-hook-token")):
                    result = self.service.update_job_status(
                        "worker-a",
                        self.job.id,
                        JobStatus.FAILED,
                        lease_token="lease-token",
                        result_summary={"error": "backup failed"},
                    )

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(self.jobs.get(self.job.id).status, JobStatus.FAILED)
        self.assertNotIn("private-hook-token", "\n".join(captured.output))

    def test_only_failed_backup_run_jobs_are_scheduled(self):
        with patch("threading.Thread") as thread_factory:
            other = self._new_job(command="stats.get", job_id="job-other")
            self.service.update_job_status(
                "worker-a", other.id, JobStatus.FAILED, lease_token="lease-token", result_summary={"error": "failed"}
            )
            success = self._new_job(job_id="job-success")
            self.service.update_job_status(
                "worker-a", success.id, JobStatus.SUCCEEDED, lease_token="lease-token", result_summary={}
            )

        thread_factory.assert_not_called()

    def test_notification_payload_is_safe_bounded_and_excludes_logs(self):
        self.job.status = JobStatus.FAILED
        self.job.result_summary = {
            "error": "backup failed; token=private-token; " + "é" * 12000,
            "status_code": 12,
            "password": "must-not-be-included",
            "arbitrary_payload": {"private": "must-not-be-included"},
        }
        self.job.log_lines = ["raw log private-token"]

        payload = self.service._build_failure_notification_payload(self.job)
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["event"], "backup.failed")
        self.assertEqual(payload["job_id"], self.job.id)
        self.assertNotIn("log", serialized.lower())
        self.assertNotIn("raw worker", serialized)
        self.assertNotIn("private-token", serialized)
        self.assertNotIn("must-not-be-included", serialized)
        self.assertIn("<redacted>", serialized)
        self.assertLessEqual(len(serialized.encode("utf-8")), 8192)
        self.assertLessEqual(len(payload["summary"]["error"]), 2048)

    def test_attempt_claim_is_atomic_and_survives_repository_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "notifications.db")
            repository = SQLiteNotificationChannelRepository(database_path)

            self.assertTrue(repository.claim_delivery_attempt("job-1", "channel-1"))
            self.assertFalse(repository.claim_delivery_attempt("job-1", "channel-1"))
            reopened = SQLiteNotificationChannelRepository(database_path)
            self.assertFalse(reopened.claim_delivery_attempt("job-1", "channel-1"))
            self.assertTrue(reopened.claim_delivery_attempt("job-2", "channel-1"))

    def test_repeated_failed_publications_send_once_per_job_and_channel(self):
        failed_job = self._new_job(status=JobStatus.FAILED)
        with patch.object(self.service, "_send_notification_channel") as send:
            self.service._deliver_failed_backup_notifications(failed_job)
            self.service._deliver_failed_backup_notifications(failed_job)

        send.assert_called_once()
        self.assertEqual(send.call_args.args[0].id, self.channel.id)
        self.assertEqual(send.call_args.args[1]["job_id"], failed_job.id)

    def test_webhook_posts_json_without_following_redirects(self):
        payload = {"event": "backup.failed", "job_id": "job-1"}
        response = Mock(status=202)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response

        with patch("urllib.request.build_opener", return_value=opener) as build_opener:
            self.service._send_notification_channel(self.channel, payload)

        build_opener.assert_called_once()
        request, = opener.open.call_args.args
        self.assertEqual(request.full_url, "http://hooks.internal/backup?token=private-hook-token")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data.decode("utf-8")), payload)
        self.assertLessEqual(opener.open.call_args.kwargs["timeout"], 5)
        self.assertTrue(any("Redirect" in type(handler).__name__ for handler in build_opener.call_args.args))

    def test_smtp_rejects_malformed_sender_before_saving(self):
        with self.assertRaisesRegex(ValueError, "SMTP sender must be a valid email address"):
            self.service.create_notification_channel(
                name="Malformed sender",
                channel_type="smtp",
                config={
                    "host": "mail.internal",
                    "port": 587,
                    "from": "backup@@example.test",
                    "to": ["oncall@example.test"],
                },
            )

        self.assertEqual(len(self.channels.list()), 1)

    def test_smtp_sender_uses_starttls_and_never_includes_logs(self):
        channel = self.service.create_notification_channel(
            name="On-call email",
            channel_type="smtp",
            config={
                "host": "mail.internal",
                "port": 587,
                "from": "backup@example.test",
                "to": ["oncall@example.test"],
                "security": "starttls",
                "username": "smtp-user",
                "password": "smtp-password",
            },
        )
        client = Mock()
        manager = Mock()
        manager.__enter__ = Mock(return_value=client)
        manager.__exit__ = Mock(return_value=False)
        payload = {"event": "backup.failed", "job_id": "job-1", "summary": {"error": "safe failure"}}

        with patch("smtplib.SMTP", return_value=manager) as smtp:
            self.service._send_notification_channel(channel, payload)

        smtp.assert_called_once_with("mail.internal", 587, timeout=5)
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("smtp-user", "smtp-password")
        message = client.send_message.call_args.args[0]
        self.assertEqual(message["To"], "oncall@example.test")
        self.assertEqual(json.loads(message.get_content()), payload)
        self.assertNotIn("log_lines", message.get_content())


if __name__ == "__main__":
    unittest.main()
