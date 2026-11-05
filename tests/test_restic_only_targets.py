import unittest

from cryptography.fernet import Fernet

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.domain.models import WorkerRecord, utcnow
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
from src.control_plane.infrastructure.security.secret_codec import SecretCodec


class ResticOnlyTargetTests(unittest.TestCase):
    """T5 (S1, S2, S3): the Control Plane target strategy is Restic-only.

    Creating or updating a target must reject any non-Restic strategy value
    (the TAR strategy was removed) without mutating stored targets, while the
    existing Restic create/update path and rclone settings keep working.
    """

    def make_service(self):
        workers = InMemoryWorkerRepository()
        workers.save(WorkerRecord(name="worker-a", host_name="test", id="worker-a", status="online", last_seen_at=utcnow()))
        return ControlPlaneService(
            worker_repository=workers,
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

    def test_register_target_rejects_non_restic_strategy_without_storing_a_target(self):
        service = self.make_service()
        stored_before = [
            target.id
            for target in service.list_targets()
        ]

        for strategy in ("tar", "TAR", "legacy-tarball"):
            with self.subTest(strategy=strategy):
                with self.assertRaisesRegex(ValueError, "restic"):
                    service.register_target(
                        name=f"target-{strategy}",
                        worker_id="worker-a",
                        backup_strategy=strategy,
                    )
                self.assertIsNone(service.target_repository.get(f"target-{strategy}"))

        self.assertEqual([target.id for target in service.list_targets()], stored_before)

    def test_register_target_defaults_to_and_keeps_accepting_restic(self):
        # Regression: the existing Restic-first creation path is untouched.
        service = self.make_service()

        default_target = service.register_target("target-default", "worker-a")
        self.assertEqual(default_target.backup_strategy, "restic")

        explicit_target = service.register_target(
            "target-explicit", "worker-a", backup_strategy="restic"
        )
        self.assertEqual(explicit_target.backup_strategy, "restic")

        normalized_target = service.register_target(
            "target-normalized", "worker-a", backup_strategy="  Restic "
        )
        self.assertEqual(normalized_target.backup_strategy, "restic")

    def test_update_target_rejects_non_restic_strategy_without_mutating_the_stored_target(self):
        service = self.make_service()
        target = service.register_target("target-edit", "worker-a")
        self.assertEqual(target.backup_strategy, "restic")
        original_name = target.name

        for strategy in ("tar", "TAR", "legacy-tarball"):
            with self.subTest(strategy=strategy):
                with self.assertRaisesRegex(ValueError, "restic"):
                    service.update_target(
                        target.id,
                        name=f"renamed-{strategy}",
                        backup_strategy=strategy,
                    )

                stored = service.target_repository.get(target.id)
                self.assertEqual(stored.name, original_name)
                self.assertEqual(stored.backup_strategy, "restic")

    def test_update_target_keeps_restic_strategy_when_field_is_omitted_or_restic(self):
        # Regression: unchanged name-only updates and explicit restic updates
        # must keep the Restic strategy functional.
        service = self.make_service()
        target = service.register_target("target-edit", "worker-a")

        renamed = service.update_target(target.id, name="renamed")
        self.assertEqual(renamed.backup_strategy, "restic")

        reconfirmed = service.update_target(
            target.id, backup_strategy="restic", backup_mode="cold"
        )
        self.assertEqual(reconfirmed.backup_strategy, "restic")
        self.assertEqual(reconfirmed.backup_mode, "cold")

        unchanged = service.update_target(target.id, backup_strategy=None)
        self.assertEqual(unchanged.backup_strategy, "restic")

    def test_padded_restic_update_input_still_validates_path_storage_conflict(self):
        # T5 follow-up bug 1: the effective strategy is normalized before the
        # path-storage/environment check, so padded restic input must not
        # bypass validation, and a rejected update must leave the stored
        # target unchanged (no partial mutation).
        service = self.make_service()
        target = service.register_target(
            "target-storage", "worker-a",
            runtime_environment={"RESTIC_REPOSITORY": "local:/repo"},
        )
        original = (target.name, target.path_storage, target.backup_strategy)

        with self.assertRaisesRegex(ValueError, "path_storage cannot be combined"):
            service.update_target(
                target.id,
                name="renamed-bypass",
                path_storage="tenant/custom",
                backup_strategy=" Restic ",
            )

        stored = service.target_repository.get(target.id)
        self.assertEqual(
            (stored.name, stored.path_storage, stored.backup_strategy), original
        )

    def test_blank_strategy_update_is_ignored_and_keeps_restic_stored(self):
        # T5 follow-up bug 2: a blank strategy input means "field omitted";
        # it must never be assigned as a non-Restic stored value, and it must
        # keep the stored Restic strategy.
        service = self.make_service()
        target = service.register_target(
            "target-blank", "worker-a",
            runtime_environment={"RESTIC_REPOSITORY": "local:/repo"},
        )

        result = service.update_target(target.id, backup_strategy="   ")
        self.assertEqual(result.backup_strategy, "restic")
        stored = service.target_repository.get(target.id)
        self.assertEqual(stored.backup_strategy, "restic")

        # A blank update must still validate like an unchanged Restic target:
        # the conflicting path_storage/environment combination is rejected and
        # leaves the stored target untouched.
        original = (stored.name, stored.path_storage, stored.backup_strategy)
        with self.assertRaisesRegex(ValueError, "path_storage cannot be combined"):
            service.update_target(
                target.id,
                name="renamed-blank",
                path_storage="tenant/custom",
                backup_strategy="",
            )
        stored = service.target_repository.get(target.id)
        self.assertEqual(
            (stored.name, stored.path_storage, stored.backup_strategy), original
        )


if __name__ == "__main__":
    unittest.main()
