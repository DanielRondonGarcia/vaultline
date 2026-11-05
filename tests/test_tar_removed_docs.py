"""T7 (S1, S3): active docs and manual compose examples must no longer
advertise the removed TAR strategy or the obsolete BACKUP_FILENAME /
GPG_PASSPHRASE configuration that only fed it. Safe working Restic examples
may remain; scenarios that cannot work anymore must be marked UNSUPPORTED so
they cannot silently be mistaken for a successful backup.
T7-correction: the manual restore scenarios for local restore, restore
permissions and SFTPGo/SQLite keep their original intent (dry-run/failure
profiles, force-overwrite verification, preserved vs. mapped ownership,
non-root SQLite readonly failure) converted to the Restic pattern that the
tracked test/restore-restic/docker-compose.yml fixture demonstrates."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "doc" / "control-plane-spec.md"
QUICKSTART = ROOT / "doc" / "control-plane-quickstart.md"
UI = ROOT / "src" / "control_plane" / "ui" / "index.html"

COMPOSE_FILES = (
    ROOT / "test" / "kitchen-sink" / "docker-compose.yml",
    ROOT / "test" / "backing-up-to-s3" / "docker-compose.yml",
    ROOT / "test" / "backing-up-to-glacier" / "docker-compose.yml",
    ROOT / "test" / "backing-up-with-encryptation" / "docker-compose.yml",
    ROOT / "test" / "restore-local" / "docker-compose.yml",
    ROOT / "test" / "restore-permissions" / "docker-compose.yml",
    ROOT / "test" / "restore-sftpgo-sqlite" / "docker-compose.yml",
)

UNSUPPORTED_COMPOSE_FILES = (
    ROOT / "test" / "backing-up-to-glacier" / "docker-compose.yml",
)

WORKING_RESTIC_COMPOSE_FILES = (
    ROOT / "test" / "backing-up-to-s3" / "docker-compose.yml",
    ROOT / "test" / "backing-up-with-encryptation" / "docker-compose.yml",
)

RESTIC_RESTORE_COMPOSE_FILES = (
    ROOT / "test" / "restore-local" / "docker-compose.yml",
    ROOT / "test" / "restore-permissions" / "docker-compose.yml",
    ROOT / "test" / "restore-sftpgo-sqlite" / "docker-compose.yml",
)

# Fixtures with services that build the image from the repository root; each
# built service must name the backup-runtime Dockerfile target explicitly so
# the final `worker` stage is never used.
BUILD_TARGET_FIXTURES = (
    ROOT / "test" / "kitchen-sink" / "docker-compose.yml",
    ROOT / "test" / "backing-up-to-s3" / "docker-compose.yml",
    ROOT / "test" / "backing-up-with-encryptation" / "docker-compose.yml",
    ROOT / "test" / "restore-restic" / "docker-compose.yml",
) + RESTIC_RESTORE_COMPOSE_FILES


class TarRemovedDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = SPEC.read_text(encoding="utf-8")
        cls.quickstart = QUICKSTART.read_text(encoding="utf-8")
        cls.ui = UI.read_text(encoding="utf-8")
        cls.compose = {path: path.read_text(encoding="utf-8") for path in COMPOSE_FILES}

    def test_spec_no_longer_describes_tar_strategy_or_self_check_tar_gpg(self):
        self.assertNotIn("`restic|tar`", self.spec)
        self.assertNotIn("Estrategias `tar`", self.spec)
        self.assertNotIn("`tar`", self.spec)
        self.assertNotIn("`gpg`", self.spec)

    def test_quickstart_does_not_list_removed_tar_gpg_external_tools(self):
        self.assertNotIn("`tar`", self.quickstart)
        self.assertNotIn("`gpg`", self.quickstart)

    def test_native_worker_info_panel_does_not_list_removed_tar_gpg_tools(self):
        self.assertNotIn("tar, gpg", self.ui)
        self.assertNotIn("(tar, gpg, aws, ssh y scp)", self.ui)

    def test_compose_examples_stop_advertising_removed_tar_gpg_configuration(self):
        for path, source in self.compose.items():
            with self.subTest(compose=path):
                for name in ("BACKUP_FILENAME:", "GPG_PASSPHRASE:", "GPG_"):
                    self.assertNotIn(name, source)

    def test_unsupported_scenarios_are_marked_and_fail_loudly(self):
        for path, source in self.compose.items():
            if path in UNSUPPORTED_COMPOSE_FILES:
                with self.subTest(unsupported=str(path.relative_to(ROOT))):
                    self.assertIn("UNSUPPORTED", source)
            else:
                with self.subTest(supported=str(path.relative_to(ROOT))):
                    self.assertNotIn("UNSUPPORTED", source)

    def test_working_rewritten_examples_keep_sane_restic_configuration(self):
        for path in WORKING_RESTIC_COMPOSE_FILES:
            source = self.compose[path]
            with self.subTest(compose=str(path.relative_to(ROOT))):
                self.assertIn("BACKUP_STRATEGY: restic", source)
                self.assertIn("RESTIC_REPOSITORY", source)
                self.assertIn("RESTIC_PASSWORD", source)

    def test_manual_restore_fixtures_are_rewritten_as_restic(self):
        """The three rewritten restore scenarios must be Restic-based in the
        shape demonstrated by test/restore-restic/docker-compose.yml: a seed
        service initializes and fills a restic repository, restore services
        run RESTORE_MODE with the same repository credentials, and the
        original verification profiles are preserved."""
        shared_markers = (
            "RESTIC_REPOSITORY: /restic-repo",
            "RESTIC_PASSWORD: restic-test-password",
            "RESTORE_MODE: \"true\"",
            "restic init || true",
            "restic backup /source",
        )
        scenario_markers = {
            ROOT
            / "test"
            / "restore-local"
            / "docker-compose.yml": (
                "restore-dry-run-latest",
                "restore-missing-target",
                "restore-no-force",
                "restore-force-overwrite",
                "verify-force-overwrite",
                "RESTORE_FORCE_OVERWRITE: \"false\"",
                "RESTORE_FORCE_OVERWRITE: \"true\"",
                "'latest backup'",
                "test ! -e /restore-target/stale.txt",
            ),
            ROOT
            / "test"
            / "restore-permissions"
            / "docker-compose.yml": (
                "restore-preserve-archive-ownership",
                "verify-preserved-root-owner",
                "restore-chown",
                "verify-chown-after-replacement",
                "RESTORE_CHOWN: \"1000:1000\"",
                "'0:0'",
                "'1000:1000'",
            ),
            ROOT
            / "test"
            / "restore-sftpgo-sqlite"
            / "docker-compose.yml": (
                "seed-sftpgo-backup",
                "restore-root-owned",
                "verify-readonly-database-fails",
                "restore-chown",
                "verify-sftpgo-user-can-write",
                "RESTORE_CHOWN: \"1000:1000\"",
                "'readonly' in message",
                "image: python:3.11.17-alpine",
            ),
        }
        for path in RESTIC_RESTORE_COMPOSE_FILES:
            source = self.compose[path]
            for marker in shared_markers + scenario_markers[path]:
                with self.subTest(compose=str(path.relative_to(ROOT)), marker=marker):
                    self.assertIn(marker, source)

    def test_every_built_service_selects_backup_runtime_target(self):
        """The Dockerfile's final stage is `worker`; without an explicit
        `build.target` Compose runs the wrong stage and backup/restore
        commands fail with "RuntimeError: worker is not enrolled"."""
        for path in BUILD_TARGET_FIXTURES:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            services = (document or {}).get("services") or {}
            build_names = [name for name, config in services.items() if config.get("build")]
            with self.subTest(compose=str(path.relative_to(ROOT)), built=build_names):
                self.assertTrue(build_names, "fixture should define at least one built service")
            for name in build_names:
                build = services[name]["build"]
                with self.subTest(compose=str(path.relative_to(ROOT)), service=name):
                    self.assertIsInstance(build, dict)
                    self.assertEqual(build.get("target"), "backup-runtime")


if __name__ == "__main__":
    unittest.main()
