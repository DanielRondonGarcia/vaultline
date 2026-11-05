import unittest

from cryptography.fernet import Fernet

from src.control_plane.application.services.control_plane_service import ControlPlaneService
from src.control_plane.domain.models import BackupTargetRecord, WorkerRecord, utcnow
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

    # --- T5 blocker B2: no environment source may reactivate TAR jobs ---

    def test_register_target_rejects_tar_backup_strategy_in_runtime_environment(self):
        service = self.make_service()
        for value in ("tar", "TAR"):
            with self.subTest(value=value):
                name = f"target-env-{value}"
                with self.assertRaisesRegex(ValueError, "restic"):
                    service.register_target(
                        name,
                        "worker-a",
                        runtime_environment={"BACKUP_STRATEGY": value},
                    )
                self.assertIsNone(service.target_repository.get(name))

        allowed = service.register_target(
            "target-env-restic",
            "worker-a",
            runtime_environment={"BACKUP_STRATEGY": " Restic "},
            backup_strategy="restic",
        )
        self.assertEqual(allowed.runtime_environment["BACKUP_STRATEGY"], " Restic ")

    def test_register_target_rejects_conflicting_storage_profile_environment(self):
        service = self.make_service()
        profile = service.create_storage_profile(
            name="profile-tar",
            backend_type="rclone",
            environment={"BACKUP_STRATEGY": "tar", "RESTIC_REPOSITORY": "rclone:remote:/backups"},
        )
        stored_before = [target.id for target in service.list_targets()]

        with self.assertRaisesRegex(ValueError, "restic"):
            service.register_target(
                "target-profile-tar", "worker-a", storage_profile_id=profile.id
            )
        self.assertEqual([target.id for target in service.list_targets()], stored_before)

        clean_profile = service.create_storage_profile(
            name="profile-restic",
            backend_type="rclone",
            environment={"RESTIC_REPOSITORY": "rclone:remote:/backups"},
        )
        attached = service.register_target(
            "target-profile-clean", "worker-a", storage_profile_id=clean_profile.id
        )
        self.assertEqual(attached.storage_profile_id, clean_profile.id)

    def test_update_target_rejects_tar_environment_and_conflicting_profile_without_mutation(self):
        service = self.make_service()
        conflicting_profile = service.create_storage_profile(
            name="profile-tar",
            backend_type="rclone",
            environment={"BACKUP_STRATEGY": "TAR"},
        )
        target = service.register_target("target-edit", "worker-a")
        original_env = dict(target.runtime_environment)

        with self.assertRaisesRegex(ValueError, "restic"):
            service.update_target(
                target.id,
                name="renamed-tar-env",
                runtime_environment={"BACKUP_STRATEGY": "tar"},
            )
        with self.assertRaisesRegex(ValueError, "restic"):
            service.update_target(
                target.id,
                name="renamed-tar-profile",
                storage_profile_id=conflicting_profile.id,
            )

        stored = service.target_repository.get(target.id)
        self.assertEqual(stored.name, "target-edit")
        self.assertEqual(stored.runtime_environment, original_env)
        self.assertIsNone(stored.storage_profile_id)

        # Preserving valid rclone configuration: attaching a clean profile and
        # providing a restic BACKUP_STRATEGY in the target env keep working.
        clean_profile = service.create_storage_profile(
            name="profile-clean",
            backend_type="rclone",
            environment={"RESTIC_REPOSITORY": "rclone:remote:/backups"},
        )
        updated = service.update_target(
            target.id,
            storage_profile_id=clean_profile.id,
            runtime_environment={"BACKUP_STRATEGY": "restic", "RCLONE_REMOTE": "remote:archive"},
        )
        self.assertEqual(updated.storage_profile_id, clean_profile.id)
        self.assertEqual(updated.runtime_environment["BACKUP_STRATEGY"], "restic")

    def test_dispatch_rejects_tar_strategy_stored_in_target_environment_without_conversion(self):
        service = self.make_service()
        # Pre-existing (legacy) seed: a stored target whose runtime environment
        # still carries BACKUP_STRATEGY=tar. Dispatch must fail loudly instead
        # of emitting an invalid TAR job, without silently converting the target.
        service.target_repository.save(
            BackupTargetRecord(
                name="legacy-env-target",
                worker_id="worker-a",
                id="legacy-env",
                backup_strategy="restic",
                runtime_environment={
                    "BACKUP_STRATEGY": "tar",
                    "RESTIC_REPOSITORY": "local:/repo",
                },
            )
        )
        jobs_before = len(service.job_repository.list())

        with self.assertRaisesRegex(ValueError, "restic"):
            service.dispatch_backup_for_target("legacy-env")

        stored = service.target_repository.get("legacy-env")
        self.assertEqual(stored.runtime_environment["BACKUP_STRATEGY"], "tar")
        self.assertEqual(len(service.job_repository.list()), jobs_before)

    def test_dispatch_rejects_legacy_tar_strategy_record_without_emitting_a_job(self):
        # S3 ("Eliminar TAR por completo"): a legacy stored record with
        # backup_strategy=tar must fail before a backup job is emitted, without
        # creating a inevitably-failing TAR job and without silently converting
        # the stored target.
        service = self.make_service()
        service.target_repository.save(
            BackupTargetRecord(
                name="legacy-strategy-target",
                worker_id="worker-a",
                id="legacy-strategy",
                backup_strategy="tar",
                runtime_environment={"RCLONE_REMOTE": "legacy:archive"},
            )
        )
        jobs_before = len(service.job_repository.list())

        with self.assertRaisesRegex(ValueError, "restic"):
            service.dispatch_backup_for_target("legacy-strategy")

        self.assertEqual(len(service.job_repository.list()), jobs_before)
        stored = service.target_repository.get("legacy-strategy")
        self.assertEqual(stored.backup_strategy, "tar")
        self.assertEqual(stored.runtime_environment["RCLONE_REMOTE"], "legacy:archive")

    def test_restore_dispatch_rejects_legacy_tar_strategy_record_without_emitting_a_job(self):
        # S3 ("Eliminar TAR por completo"): legacy restore dispatch must also
        # fail before a restore job is emitted, without creating an
        # inevitably-failing TAR restore job and without silently converting
        # the stored target.
        service = self.make_service()
        service.target_repository.save(
            BackupTargetRecord(
                name="legacy-restore-target",
                worker_id="worker-a",
                id="legacy-restore",
                backup_strategy="tar",
                runtime_environment={"RCLONE_REMOTE": "legacy:archive"},
                restore_defaults={"source": "legacy-archive"},
            )
        )
        jobs_before = len(service.job_repository.list())

        for kwargs in (dict(), dict(dry_run=False, force_overwrite=True)):
            with self.subTest(dry_run=kwargs.get("dry_run", True)):
                with self.assertRaisesRegex(ValueError, "restic"):
                    service.dispatch_restore_for_target("legacy-restore", **kwargs)
                self.assertEqual(len(service.job_repository.list()), jobs_before)

        stored = service.target_repository.get("legacy-restore")
        self.assertEqual(stored.backup_strategy, "tar")
        self.assertEqual(stored.runtime_environment["RCLONE_REMOTE"], "legacy:archive")

    def test_restore_dispatch_keeps_restic_strategy_for_valid_targets(self):
        # Regression: valid Restic restore dispatch (default dry-run) keeps
        # emitting the Restic strategy for the worker runtime.
        service = self.make_service()
        target = service.register_target("target-restore", "worker-a")
        job = service.dispatch_restore_for_target(target.id)
        self.assertEqual(job.payload["environment"]["RESTORE_BACKUP_STRATEGY"], "restic")

        job = service.dispatch_restore_for_target(
            target.id, dry_run=False, restore_source="abcdef12"
        )
        self.assertEqual(job.payload["environment"]["RESTORE_BACKUP_STRATEGY"], "restic")

    def test_dispatch_treats_blank_env_backup_strategy_as_unset_restic(self):
        # Verifier finding T5: a whitespace-only BACKUP_STRATEGY in the target
        # runtime environment previously survived the dispatch guard and made
        # the runtime fail the job with an unsupported empty strategy.
        # Consistent with the blank-means-unset semantics used at target
        # create/update, dispatch must treat it as unset and emit restic,
        # without rewriting the stored record.
        service = self.make_service()
        service.target_repository.save(
            BackupTargetRecord(
                name="blank-env-target",
                worker_id="worker-a",
                id="blank-env",
                backup_strategy="restic",
                runtime_environment={
                    "BACKUP_STRATEGY": "   ",
                    "RESTIC_REPOSITORY": "local:/repo",
                },
            )
        )

        job = service.dispatch_backup_for_target("blank-env")

        self.assertEqual(job.payload["environment"]["BACKUP_STRATEGY"], "restic")
        stored = service.target_repository.get("blank-env")
        self.assertEqual(stored.runtime_environment["BACKUP_STRATEGY"], "   ")

        # Restore regression: the record strategy overrides any environment
        # RESTORE_BACKUP_STRATEGY value, whitespace-only included.
        job = service.dispatch_restore_for_target("blank-env", restore_source="abcdef12")
        self.assertEqual(job.payload["environment"]["RESTORE_BACKUP_STRATEGY"], "restic")

    def test_dispatch_without_env_strategy_emits_restic_and_unrelated_updates_still_pass(self):
        service = self.make_service()
        target = service.register_target("target-dispatch", "worker-a")
        job = service.dispatch_backup_for_target(target.id)
        self.assertEqual(job.payload["environment"]["BACKUP_STRATEGY"], "restic")

        # Compatibility: an unrelated rename of a legacy seeded tar-env target
        # must keep working (no silent conversion, no spurious rejection).
        service.target_repository.save(
            BackupTargetRecord(
                name="legacy-env-target",
                worker_id="worker-a",
                id="legacy-env",
                backup_strategy="restic",
                runtime_environment={"BACKUP_STRATEGY": "tar"},
            )
        )
        renamed = service.update_target("legacy-env", name="renamed-legacy")
        self.assertEqual(renamed.name, "renamed-legacy")
        self.assertEqual(renamed.runtime_environment["BACKUP_STRATEGY"], "tar")


if __name__ == "__main__":
    unittest.main()
