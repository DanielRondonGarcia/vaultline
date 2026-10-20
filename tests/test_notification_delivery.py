import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
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

    def test_failure_email_previews_head_and_tail_and_attaches_the_redacted_log(self):
        smtp_channel = self.service.create_notification_channel(
            name="Mail", channel_type="smtp",
            config={"host": "mail.internal", "from": "backup@example.test", "to": ["oncall@example.test"]},
        )
        self.job.payload = {"env": {"RESTIC_PASSWORD": "known-secret"}}
        logs = [f"INFO: step {index}" for index in range(60)]
        logs += ["ERROR: <disk full> known-secret token=private-token lease-token"]
        self.jobs.save(self.job)
        channel_before = self.service.notification_channel_public_view(smtp_channel)
        message, webhook, result = self._complete_with_mocked_delivery(logs)

        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(self.jobs.get(self.job.id).status, JobStatus.FAILED)
        self.assertEqual(self.service.notification_channel_public_view(smtp_channel), channel_before)
        for subtype in ("plain", "html"):
            body = message.get_body(preferencelist=(subtype,)).get_content()
            self.assertIn("INFO: step 0", body)
            self.assertIn("INFO: step 59", body)
            self.assertNotIn("INFO: step 30", body)
            self.assertIn("Contenido omitido", body)
            self.assertIn("archivo adjunto", body)
            self.assertIn("Registro de error", body)
        html = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("ERROR: &lt;disk full&gt;", html)
        self.assertNotIn("<disk full>", html)
        self.assertGreater(html.index("Registro de error"), html.index("Ver trabajos"))
        attachments = list(message.iter_attachments())
        self.assertEqual(len(attachments), 1)
        attachment = attachments[0]
        self.assertEqual(attachment.get_filename(), "backup-job-1.log.txt")
        self.assertEqual(attachment.get_content_type(), "text/plain")
        self.assertEqual(attachment.get_content_charset(), "utf-8")
        expected = "\n".join(logs[:-1] + ["ERROR: <disk full> <redacted> token=<redacted> <redacted>"]) + "\n"
        self.assertEqual(attachment.get_content(), expected)
        for secret in ("known-secret", "private-token", "lease-token", "smtp-password"):
            for part in message.walk():
                if not part.is_multipart():
                    self.assertNotIn(secret, part.get_content())
        self.assertNotIn("log_lines", webhook)
        self.assertNotIn("INFO: step", json.dumps(webhook))
        self.assertLessEqual(len(json.dumps(webhook, ensure_ascii=False).encode("utf-8")), 8192)
        self.assertEqual(webhook["summary"]["status_code"], 1)

    def test_long_unicode_logs_still_send_with_bounded_head_tail_preview(self):
        self.service.create_notification_channel(
            name="Mail", channel_type="smtp",
            config={"host": "mail.internal", "from": "backup@example.test", "to": ["oncall@example.test"]},
        )
        logs = ["START: " + "é" * 15000, "界" * 15000, "ERROR: final backup failure"]
        message, webhook, result = self._complete_with_mocked_delivery(logs)
        self.assertEqual(result.status, JobStatus.FAILED)
        body = message.get_body(preferencelist=("plain",)).get_content()
        preview = body.split("Registro de error", 1)[1]
        self.assertIn("START:", preview)
        self.assertIn("ERROR: final backup failure", preview)
        self.assertIn("Contenido omitido", preview)
        self.assertLessEqual(len(preview.encode("utf-8")), 4200)
        self.assertNotIn("\ufffd", preview)
        attachment, = message.iter_attachments()
        self.assertEqual(attachment.get_content(), "\n".join(logs) + "\n")
        self.assertLessEqual(len(json.dumps(webhook).encode("utf-8")), 8192)

    def test_empty_failure_log_sends_without_an_attachment(self):
        self.service.create_notification_channel(
            name="Mail", channel_type="smtp",
            config={"host": "mail.internal", "from": "backup@example.test", "to": ["oncall@example.test"]},
        )
        message, webhook, result = self._complete_with_mocked_delivery([])
        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(list(message.iter_attachments()), [])
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        self.assertNotIn("Registro de error", message.get_body(preferencelist=("plain",)).get_content())
        self.assertNotIn("log_lines", webhook)

    def _complete_with_mocked_delivery(self, logs):
        self.service.update_settings(control_plane_public_url="https://backups.example.test/")
        client = Mock()
        manager = Mock()
        manager.__enter__ = Mock(return_value=client)
        manager.__exit__ = Mock(return_value=False)
        response = Mock(status=202)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response

        def execute(**kwargs):
            thread = Mock()
            thread.start.side_effect = lambda: kwargs["target"](*kwargs["args"])
            return thread

        with patch("threading.Thread", side_effect=execute), patch("smtplib.SMTP", return_value=manager), \
                patch("urllib.request.build_opener", return_value=opener):
            result = self.service.update_job_status(
                "worker-a", self.job.id, JobStatus.FAILED, lease_token="lease-token",
                result_summary={"status_code": 1}, log_lines=logs,
            )
            with self.assertRaises(ValueError) as repeated:
                self.service.update_job_status("worker-a", self.job.id, JobStatus.FAILED, lease_token="lease-token")
            self.assertEqual(str(repeated.exception), "job 'job-1' is not in progress (current status: failed)")
        client.send_message.assert_called_once()
        opener.open.assert_called_once()
        request, = opener.open.call_args.args
        return client.send_message.call_args.args[0], json.loads(request.data), result

    def test_notification_payload_is_safe_bounded_and_excludes_logs(self):
        self.job.status = JobStatus.FAILED
        self.job.result_summary = {
            "error": "backup failed; token=private-token; " + "é" * 12000,
            "status_code": 12,
            "password": "must-not-be-included",
            "arbitrary_payload": {"private": "must-not-be-included"},
        }
        # Log line matches the regex pattern: token=secret
        self.job.log_lines = ["raw log with token=private-token and password=secret123 here"]

        payload = self.service._build_failure_notification_payload(self.job)
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["event"], "backup.failed")
        self.assertEqual(payload["job_id"], self.job.id)
        # The shared webhook payload stays a safe bounded summary.
        self.assertNotIn("private-token", serialized)
        self.assertNotIn("raw worker", serialized)
        self.assertNotIn("must-not-be-included", serialized)
        self.assertNotIn("secret123", serialized)
        self.assertIn("<redacted>", serialized)
        self.assertLessEqual(len(serialized.encode("utf-8")), 8192)
        self.assertLessEqual(len(payload["summary"]["error"]), 2048)
        self.assertNotIn("log_lines", payload)
        self.assertNotIn("raw log", serialized)

    def test_attempt_claim_is_atomic_and_survives_repository_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "notifications.db")
            repositories = [SQLiteNotificationChannelRepository(database_path) for _ in range(8)]

            with ThreadPoolExecutor(max_workers=len(repositories)) as executor:
                claims = list(
                    executor.map(
                        lambda repository: repository.claim_delivery_attempt("job-1", "channel-1"),
                        repositories,
                    )
                )

            self.assertEqual(sum(claims), 1)
            reopened = SQLiteNotificationChannelRepository(database_path)
            self.assertFalse(reopened.claim_delivery_attempt("job-1", "channel-1"))
            self.assertTrue(reopened.claim_delivery_attempt("job-1", "channel-2"))
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
        self.assertEqual(message["Subject"], "Backup fallido: job-1")
        # Multipart: extract plain text and HTML parts
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        parts = message.get_payload()
        plain_parts = [p for p in parts if p.get_content_type() == "text/plain"]
        html_parts = [p for p in parts if p.get_content_type() == "text/html"]
        self.assertEqual(len(plain_parts), 1)
        self.assertEqual(len(html_parts), 1)
        # Plain text part: readable summary, no JSON
        plain_text = plain_parts[0].get_content()
        self.assertIn("Backup fallido: job-1", plain_text)
        self.assertIn("safe failure", plain_text)
        self.assertNotIn("log_lines", plain_text)
        self.assertNotIn("private", plain_text.lower())
        self.assertNotIn("event", plain_text)
        # HTML part
        html_body = html_parts[0].get_content()
        self.assertIn("Backup fallido", html_body)
        self.assertIn("safe failure", html_body)
        self.assertIn("job-1", html_body)


    def test_test_email_sends_to_configured_recipients_without_mutating_channel(self):
        channel = self.service.create_notification_channel(
            name="On-call email",
            channel_type="smtp",
            config={
                "host": "mail.internal",
                "port": 587,
                "from": "backup@example.test",
                "to": ["oncall@example.test", "admin@example.test"],
                "security": "starttls",
                "username": "smtp-user",
                "password": "smtp-password",
            },
        )
        before = self.service.notification_channel_public_view(channel)
        client = Mock()
        manager = Mock()
        manager.__enter__ = Mock(return_value=client)
        manager.__exit__ = Mock(return_value=False)

        with patch("smtplib.SMTP", return_value=manager) as smtp:
            result = self.service.send_test_notification_email(channel.id)

        self.assertEqual(result, {"recipients": ["oncall@example.test", "admin@example.test"]})
        smtp.assert_called_once_with("mail.internal", 587, timeout=5)
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("smtp-user", "smtp-password")
        message = client.send_message.call_args.args[0]
        self.assertEqual(message["Subject"], "Vaultline SMTP test email")
        self.assertEqual(message["To"], "oncall@example.test, admin@example.test")
        payload = json.loads(message.get_content())
        self.assertEqual(payload["event"], "notification.test")
        self.assertEqual(payload["channel_name"], "On-call email")
        self.assertNotIn("smtp-password", message.get_content())
        after = next(
            self.service.notification_channel_public_view(item)
            for item in self.service.list_notification_channels()
            if item.id == channel.id
        )
        self.assertEqual(after, before)
        with self.assertRaisesRegex(ValueError, "SMTP"):
            self.service.send_test_notification_email(self.channel.id)

    def test_html_email_includes_control_plane_link_when_configured(self):
        """Verify that a configured control_plane_public_url appears in the HTML body link."""
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
        # Save a settings record with a public URL
        settings_repo = InMemorySettingsRepository()
        from src.control_plane.domain.models import SettingsRecord
        settings_repo.save(SettingsRecord(control_plane_public_url="https://backups.miempresa.com"))
        service_with_settings = ControlPlaneService(
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
            settings_repository=settings_repo,
            cache_repository=InMemoryCacheRepository(),
            index_repository=InMemoryIndexRepository(),
            notification_channel_repository=self.channels,
        )
        client = Mock()
        manager = Mock()
        manager.__enter__ = Mock(return_value=client)
        manager.__exit__ = Mock(return_value=False)
        payload = {"event": "backup.failed", "job_id": "job-abc123", "target_id": "target-a", "started_at": "2026-10-20T08:14:14", "finished_at": "2026-10-20T08:39:39", "summary": {"error": "docker socket timeout"}}

        with patch("smtplib.SMTP", return_value=manager) as smtp:
            service_with_settings._send_notification_channel(channel, payload)

        message = client.send_message.call_args.args[0]
        parts = message.get_payload()
        html_parts = [part for part in parts if part.get_content_type() == "text/html"]
        self.assertEqual(len(html_parts), 1)
        html_body = html_parts[0].get_content()
        self.assertIn("https://backups.miempresa.com/#/jobs", html_body)
        self.assertIn("docker socket timeout", html_body)
        self.assertIn("job-abc123", html_body)
        self.assertIn("Ver trabajos en el sistema de backups", html_body)


if __name__ == "__main__":
    unittest.main()
