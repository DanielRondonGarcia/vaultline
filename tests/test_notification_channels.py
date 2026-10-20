import json
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.auth import ROLE_ADMIN
from src.control_plane.infrastructure.repositories.in_memory import (
    InMemoryCacheRepository,
    InMemoryIndexRepository,
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
)
from src.control_plane.infrastructure.repositories.sqlite import SQLiteNotificationChannelRepository
from src.control_plane.infrastructure.security.secret_codec import SecretCodec
from src.control_plane.main import ControlPlaneRequestHandler


class _NotificationRepository:
    def __init__(self):
        self.items = {}

    def save(self, channel):
        self.items[channel.id] = channel
        return channel

    def get(self, channel_id):
        return self.items.get(channel_id)

    def list(self):
        return sorted(self.items.values(), key=lambda item: item.created_at)

    def delete(self, channel_id):
        return self.items.pop(channel_id, None) is not None


class NotificationChannelServiceTests(unittest.TestCase):
    def setUp(self):
        self.codec = SecretCodec(Fernet.generate_key())
        self.repository = _NotificationRepository()
        self.service = ControlPlaneService(
            worker_repository=InMemoryWorkerRepository(),
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=InMemoryTargetRepository(),
            job_repository=InMemoryJobRepository(),
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=self.codec,
            settings_repository=InMemorySettingsRepository(),
            cache_repository=InMemoryCacheRepository(),
            index_repository=InMemoryIndexRepository(),
        )
        self.service.notification_channel_repository = self.repository

    def test_multiple_channel_configs_are_encrypted_and_kept_out_of_listing(self):
        webhook_config = {"url": "http://hooks.internal/backup?key=webhook-secret"}
        smtp_config = {
            "host": "mail.internal",
            "port": 587,
            "from": "backup@example.test",
            "to": ["oncall@example.test"],
            "security": "starttls",
            "username": "smtp-user",
            "password": "smtp-secret",
        }

        webhook = self.service.create_notification_channel(
            name="Operations webhook",
            channel_type="webhook",
            config=webhook_config,
        )
        email = self.service.create_notification_channel(
            name="On-call email",
            channel_type="smtp",
            config=smtp_config,
        )

        listed = self.service.list_notification_channels()
        serialized_records = json.dumps([item.__dict__ for item in listed], default=str)
        public_views = [self.service.notification_channel_public_view(item) for item in listed]
        serialized_views = json.dumps(public_views, default=str)
        self.assertEqual([item.name for item in listed], ["Operations webhook", "On-call email"])
        self.assertEqual(webhook.channel_type, "webhook")
        self.assertEqual(email.channel_type, "smtp")
        self.assertNotEqual(webhook.config_ciphertext, json.dumps(webhook_config, sort_keys=True))
        self.assertNotIn("webhook-secret", serialized_records)
        self.assertNotIn("smtp-secret", serialized_records)
        self.assertNotIn("webhook-secret", serialized_views)
        self.assertNotIn("smtp-secret", serialized_views)
        self.assertNotIn("config_ciphertext", serialized_views)
        self.assertEqual(json.loads(self.codec.decrypt(webhook.config_ciphertext)), webhook_config)
        self.assertEqual(json.loads(self.codec.decrypt(email.config_ciphertext)), smtp_config)

    def test_sqlite_repository_persists_and_deletes_encrypted_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "channels.db")
            self.service.notification_channel_repository = SQLiteNotificationChannelRepository(database_path)
            channel = self.service.create_notification_channel(
                name="Private destination",
                channel_type="webhook",
                config={"url": "https://hooks.internal/events?token=private-token"},
            )

            reloaded_repository = SQLiteNotificationChannelRepository(database_path)
            reloaded = reloaded_repository.get(channel.id)

            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.config_ciphertext, channel.config_ciphertext)
            self.assertEqual(
                json.loads(self.codec.decrypt(reloaded.config_ciphertext)),
                {"url": "https://hooks.internal/events?token=private-token"},
            )
            self.assertTrue(reloaded_repository.delete(channel.id))
            self.assertIsNone(reloaded_repository.get(channel.id))

    def test_unknown_channel_type_is_rejected_without_saving(self):
        with self.assertRaisesRegex(ValueError, "unsupported notification channel type"):
            self.service.create_notification_channel(
                name="invalid",
                channel_type="pager",
                config={"destination": "unused"},
            )

        self.assertEqual(self.service.list_notification_channels(), [])


class NotificationChannelRouteTests(unittest.TestCase):
    @staticmethod
    def _handler(path, service, body=None):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = path
        handler.headers = {}
        handler._read_json_body = Mock(return_value=body or {})
        handler._require_auth = Mock(return_value=True)
        handler._control_plane_service = Mock(return_value=service)
        handler._write_json = Mock()
        return handler

    def test_admin_can_list_create_update_and_delete_channels(self):
        service = Mock()
        channel = object()
        view = {"id": "channel-1", "name": "Operations"}
        service.list_notification_channels.return_value = [channel]
        service.notification_channel_public_view.return_value = view
        service.create_notification_channel.return_value = channel
        service.update_notification_channel.return_value = channel
        service.delete_notification_channel.return_value = True

        get_handler = self._handler("/api/v1/notification-channels", service)
        get_handler._handle_get_request(head_only=False)
        get_handler._require_auth.assert_called_once_with(ROLE_ADMIN, head_only=False, api_mode=True)
        get_handler._write_json.assert_called_once_with(
            200,
            {"items": [view]},
            head_only=False,
            extra_headers={"Cache-Control": "no-store"},
        )

        create_body = {"name": "Operations", "channel_type": "webhook", "config": {"url": "https://hooks.internal"}}
        post_handler = self._handler("/api/v1/notification-channels", service, create_body)
        post_handler.do_POST()
        post_handler._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
        service.create_notification_channel.assert_called_once_with(
            name="Operations",
            channel_type="webhook",
            config={"url": "https://hooks.internal"},
            enabled=True,
        )
        post_handler._write_json.assert_called_once_with(201, view)

        patch_body = {"name": "Operations updated", "enabled": False}
        patch_handler = self._handler("/api/v1/notification-channels/channel-1", service, patch_body)
        patch_handler.do_PATCH()
        patch_handler._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
        service.update_notification_channel.assert_called_once_with(
            channel_id="channel-1",
            name="Operations updated",
            channel_type=None,
            config=None,
            enabled=False,
        )
        patch_handler._write_json.assert_called_once_with(200, view)

        delete_handler = self._handler("/api/v1/notification-channels/channel-1", service)
        delete_handler.do_DELETE()
        delete_handler._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
        service.delete_notification_channel.assert_called_once_with("channel-1")
        delete_handler._write_json.assert_called_once_with(204, {})

    def test_admin_can_send_test_email_and_non_admin_cannot(self):
        recipients = ["oncall@example.test", "admin@example.test"]
        service = Mock()
        service.send_test_notification_email.return_value = {"recipients": recipients}
        handler = self._handler(
            "/api/v1/notification-channels/channel-1/test-email",
            service,
        )

        handler.do_POST()

        handler._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
        service.send_test_notification_email.assert_called_once_with("channel-1")
        handler._write_json.assert_called_once_with(200, {"recipients": recipients})

        denied_service = Mock()
        denied = self._handler(
            "/api/v1/notification-channels/channel-1/test-email",
            denied_service,
        )
        denied._require_auth.return_value = False

        denied.do_POST()

        denied._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
        denied._control_plane_service.assert_not_called()
        denied_service.send_test_notification_email.assert_not_called()

        failed_service = Mock()
        failed_service.send_test_notification_email.side_effect = smtplib.SMTPRecipientsRefused(
            {"oncall@example.test": (550, b"private-smtp-detail")}
        )
        failed = self._handler(
            "/api/v1/notification-channels/channel-1/test-email",
            failed_service,
        )

        failed.do_POST()

        failed._write_json.assert_called_once_with(
            502,
            {"error": "No se pudo enviar el correo de prueba. Verifica la configuración SMTP."},
        )
        self.assertNotIn("private-smtp-detail", str(failed._write_json.call_args))

        invalid_service = Mock()
        invalid_service.send_test_notification_email.side_effect = ValueError("INTERNAL_SMTP_DETAIL_SENTINEL")
        invalid = self._handler(
            "/api/v1/notification-channels/channel-1/test-email",
            invalid_service,
        )

        invalid.do_POST()

        invalid._write_json.assert_called_once_with(
            502,
            {"error": "No se pudo enviar el correo de prueba. Verifica la configuración SMTP."},
        )
        self.assertNotIn("INTERNAL_SMTP_DETAIL_SENTINEL", str(invalid._write_json.call_args))

    def test_test_email_rejects_non_object_request_body_with_400(self):
        service = Mock()
        non_object_bodies = [[1, 2], [], "invalid", None, 7]

        for body in non_object_bodies:
            with self.subTest(body=body):
                handler = self._handler(
                    "/api/v1/notification-channels/channel-1/test-email",
                    service,
                )
                handler._read_json_body = Mock(return_value=body)

                handler.do_POST()

                handler._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
                handler._write_json.assert_called_once_with(400, {"error": "request body must be a JSON object"})
                handler._control_plane_service.assert_not_called()
                service.send_test_notification_email.assert_not_called()

    def test_test_email_empty_request_body_keeps_configured_recipients_contract(self):
        service = Mock()
        service.send_test_notification_email.return_value = {"recipients": ["oncall@example.test"]}
        handler = self._handler(
            "/api/v1/notification-channels/channel-1/test-email",
            service,
        )
        handler._read_json_body = Mock(return_value={})

        handler.do_POST()

        handler._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
        service.send_test_notification_email.assert_called_once_with("channel-1")
        handler._write_json.assert_called_once_with(200, {"recipients": ["oncall@example.test"]})

    def test_test_email_forwards_explicit_recipient_body_to_service(self):
        passed_service = Mock()
        passed_service.send_test_notification_email.return_value = {"recipients": ["admin@example.test"]}
        passed = self._handler(
            "/api/v1/notification-channels/channel-1/test-email",
            passed_service,
            {"recipients": ["admin@example.test"]},
        )

        passed.do_POST()

        passed._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
        passed_service.send_test_notification_email.assert_called_once_with(
            "channel-1", recipients=["admin@example.test"]
        )
        passed._write_json.assert_called_once_with(200, {"recipients": ["admin@example.test"]})

    def test_test_email_rejects_invalid_recipient_bodies_with_400_before_smtp(self):
        service = self._real_service()
        channel = service.create_notification_channel(
            name="On-call",
            channel_type="smtp",
            config={
                "host": "mail.internal",
                "port": 587,
                "from": "backup@example.test",
                "to": ["oncall@example.test"],
                "security": "starttls",
            },
        )
        before = service.notification_channel_public_view(channel)
        invalid_bodies = [
            {"recipients": None},
            {"recipients": []},
            {"recipients": "oncall@example.test"},
            {"recipients": [None]},
            {"recipients": ["oncall@example\r.test"]},
            {"recipients": ["not-an-email"]},
            {"recipients": ["oncall@example.test", "bad@@example.test"]},
        ]

        for body in invalid_bodies:
            with self.subTest(body=body), patch("smtplib.SMTP") as smtp, patch("smtplib.SMTP_SSL") as smtp_ssl:
                handler = self._handler(
                    f"/api/v1/notification-channels/{channel.id}/test-email",
                    service,
                    body,
                )

                handler.do_POST()

                handler._write_json.assert_called_once_with(
                    400, {"error": "test recipients must be a non-empty list of valid email addresses"}
                )
                smtp.assert_not_called()
                smtp_ssl.assert_not_called()

        self.assertEqual(service.notification_channel_public_view(channel), before)

    def test_test_email_with_valid_explicit_recipients_sends_trimmed_list_and_keeps_channel(self):
        service = self._real_service()
        channel = service.create_notification_channel(
            name="On-call",
            channel_type="smtp",
            config={
                "host": "mail.internal",
                "port": 587,
                "from": "backup@example.test",
                "to": ["oncall@example.test"],
                "security": "starttls",
            },
        )
        client = Mock()
        manager = Mock()
        manager.__enter__ = Mock(return_value=client)
        manager.__exit__ = Mock(return_value=False)
        handler = self._handler(
            f"/api/v1/notification-channels/{channel.id}/test-email",
            service,
            {"recipients": [" admin@example.test ", "admin@example.test"]},
        )

        with patch("smtplib.SMTP", return_value=manager) as smtp:
            handler.do_POST()

        handler._write_json.assert_called_once_with(200, {"recipients": ["admin@example.test"]})
        smtp.assert_called_once()
        message = client.send_message.call_args.args[0]
        self.assertEqual(message["To"], "admin@example.test")
        self.assertEqual(client.send_message.call_args.kwargs["to_addrs"], ["admin@example.test"])
        self.assertEqual(
            service.notification_channel_public_view(channel)["config_summary"]["to"],
            ["oncall@example.test"],
        )

    @staticmethod
    def _real_service():
        service = ControlPlaneService(
            worker_repository=InMemoryWorkerRepository(),
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=InMemoryTargetRepository(),
            job_repository=InMemoryJobRepository(),
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=SecretCodec(Fernet.generate_key()),
            settings_repository=InMemorySettingsRepository(),
            cache_repository=InMemoryCacheRepository(),
            index_repository=InMemoryIndexRepository(),
        )
        service.notification_channel_repository = _NotificationRepository()
        return service

    def test_channel_collection_requires_admin_before_loading_configuration(self):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = "/api/v1/notification-channels"
        handler.headers = {}
        handler._require_auth = Mock(return_value=False)
        handler._control_plane_service = Mock()
        handler._write_json = Mock()

        handler._handle_get_request(head_only=False)

        handler._require_auth.assert_called_once_with(ROLE_ADMIN, head_only=False, api_mode=True)
        handler._control_plane_service.assert_not_called()
        handler._write_json.assert_not_called()

    def test_channel_creation_requires_admin(self):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = "/api/v1/notification-channels"
        handler.headers = {}
        handler._read_json_body = Mock(return_value={"name": "Operations", "channel_type": "webhook", "config": {}})
        handler._require_auth = Mock(return_value=False)
        handler._control_plane_service = Mock()
        handler._write_json = Mock()

        handler.do_POST()

        handler._require_auth.assert_called_once_with(ROLE_ADMIN, api_mode=True)
        handler._control_plane_service.assert_not_called()
        handler._write_json.assert_not_called()

    def test_malformed_smtp_security_returns_validation_error_without_saving(self):
        repository = _NotificationRepository()
        service = ControlPlaneService(
            worker_repository=InMemoryWorkerRepository(),
            inventory_repository=InMemoryInventoryRepository(),
            target_repository=InMemoryTargetRepository(),
            job_repository=InMemoryJobRepository(),
            storage_profile_repository=InMemoryStorageProfileRepository(),
            secret_repository=InMemorySecretRepository(),
            snapshot_repository=InMemorySnapshotRepository(),
            retention_policy_repository=InMemoryRetentionPolicyRepository(),
            target_stats_repository=InMemoryTargetStatsRepository(),
            secret_codec=SecretCodec(Fernet.generate_key()),
            settings_repository=InMemorySettingsRepository(),
            cache_repository=InMemoryCacheRepository(),
            index_repository=InMemoryIndexRepository(),
        )
        service.notification_channel_repository = repository
        handler = self._handler(
            "/api/v1/notification-channels",
            service,
            {
                "name": "Invalid SMTP",
                "channel_type": "smtp",
                "config": {
                    "host": "mail.internal",
                    "port": 587,
                    "from": "backup@example.test",
                    "to": ["oncall@example.test"],
                    "security": [],
                },
            },
        )

        handler.do_POST()

        handler._write_json.assert_called_once_with(
            400, {"error": "SMTP security must be starttls, ssl, or none"}
        )
        self.assertEqual(repository.list(), [])


if __name__ == "__main__":
    unittest.main()
