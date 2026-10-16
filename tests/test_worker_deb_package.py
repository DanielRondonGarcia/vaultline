import re
import unittest
from pathlib import Path


PACKAGING_ROOT = Path("packaging/debian")
CONTROL = PACKAGING_ROOT / "control.in"
POSTINST = PACKAGING_ROOT / "postinst"
PRERM = PACKAGING_ROOT / "prerm"
UNIT_NAME = "docker-volume-backup-worker.service"
SERVICE = PACKAGING_ROOT / "lib/systemd/system/docker-volume-backup-worker.service"
ENV_EXAMPLE = PACKAGING_ROOT / "usr/share/doc/vaultline-worker/worker.env.example"
BUILDER = Path("scripts/build_worker_deb.sh")


class WorkerDebPackageTests(unittest.TestCase):
    def read_text(self, path: Path) -> str:
        self.assertTrue(path.exists(), f"{path} is required")
        return path.read_text(encoding="utf-8")

    def test_control_metadata_uses_package_arch_and_runtime_dependencies(self):
        control = self.read_text(CONTROL)

        self.assertRegex(control, r"(?m)^Package: vaultline-worker$")
        self.assertRegex(control, r"(?m)^Version: @VERSION@$")
        self.assertRegex(control, r"(?m)^Architecture: @ARCH@$")
        self.assertRegex(control, r"(?m)^Depends: .*libc6 \(>= 2\.17\)")
        self.assertRegex(control, r"(?m)^Depends: .*adduser")
        self.assertRegex(control, r"(?m)^Depends: .*systemd")
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN", control)
        self.assertNotIn("WORKER_SECRET", control)
        self.assertIn("auto-started systemd unit that waits for enrollment", control)
        self.assertNotIn("inactive systemd unit", control)

    def test_builder_validates_semver_and_arch_and_names_assets_deterministically(self):
        builder = self.read_text(BUILDER)

        self.assertIn("dpkg-deb --build --root-owner-group", builder)
        self.assertRegex(builder, r"\[\[ \$\{VERSION\} =~ \^\[0-9\]\+\\\.\[0-9\]\+\\\.\[0-9\]\+")
        self.assertRegex(builder, r"case \"\$\{ARCH\}\" in\s+amd64\|arm64\)")
        self.assertIn('vaultline-worker_${VERSION}_${ARCH}.deb', builder)
        self.assertIn("install -m 0755", builder)
        self.assertIn("install -m 0644", builder)
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN", builder)
        self.assertNotIn("WORKER_SECRET", builder)
        for tool in ("gpg", "aws", "ssh", "scp"):
            self.assertNotIn(tool, builder.lower())
        self.assertNotIn("/usr/bin/rclone", builder)
        self.assertNotIn("/usr/bin/restic", builder)
        self.assertIn("restic-v0.19.1-LICENSE", builder)
        self.assertIn("rclone-v1.75.2-COPYING", builder)

    def test_builder_installs_pinned_restic_and_rclone_under_usr_lib_vaultline(self):
        # The frozen worker resolves both tools from /usr/lib/vaultline before its
        # PyInstaller bundle, so the package must ship them there as executables
        # and refuse to build without the verified binaries.
        builder = self.read_text(BUILDER)

        self.assertIn('install -d -m 0755 "${PACKAGE_ROOT}/usr/lib/vaultline"', builder)
        self.assertIn('install -m 0755 "${RESTIC_BINARY}" "${PACKAGE_ROOT}/usr/lib/vaultline/restic"', builder)
        self.assertIn('install -m 0755 "${RCLONE_BINARY}" "${PACKAGE_ROOT}/usr/lib/vaultline/rclone"', builder)
        self.assertIn('test -x "${PACKAGE_ROOT}/usr/lib/vaultline/restic"', builder)
        self.assertIn('test -x "${PACKAGE_ROOT}/usr/lib/vaultline/rclone"', builder)
        self.assertIn('RESTIC_BINARY="${VAULTLINE_RESTIC_BINARY:-}"', builder)
        self.assertIn('RCLONE_BINARY="${VAULTLINE_RCLONE_BINARY:-}"', builder)
        self.assertRegex(builder, r'if \[\[ -z "\$\{RESTIC_BINARY\}" \|\| ! -f "\$\{RESTIC_BINARY\}" \|\| ! -x "\$\{RESTIC_BINARY\}" \]\]; then\n  printf .*\n  exit 2')
        self.assertRegex(builder, r'if \[\[ -z "\$\{RCLONE_BINARY\}" \|\| ! -f "\$\{RCLONE_BINARY\}" \|\| ! -x "\$\{RCLONE_BINARY\}" \]\]; then\n  printf .*\n  exit 2')
        self.assertLess(builder.index('exit 2\nfi\n\nif ! command -v dpkg-deb'), builder.index("dpkg-deb --build"))
        self.assertIn("VAULTLINE_RESTIC_BINARY / VAULTLINE_RCLONE_BINARY (env)", builder)

    def test_package_service_preserves_identifiers_paths_and_binary_execstart(self):
        service = self.read_text(SERVICE)

        self.assertIn("User=dvb-worker", service)
        self.assertIn("Group=dvb-worker", service)
        self.assertIn("EnvironmentFile=-/etc/docker-volume-backup/worker.env", service)
        self.assertIn("ReadWritePaths=/var/lib/docker-volume-backup", service)
        self.assertIn("ExecStart=/usr/bin/vaultline-worker daemon", service)
        self.assertIn("ProtectHome=read-only", service)
        self.assertIn("restore", service.lower())
        self.assertNotIn("/opt/docker-volume-backup", service)
        self.assertNotIn("python3 -m", service)

    def test_postinst_prepares_state_safely_before_touching_systemd(self):
        postinst = self.read_text(POSTINST)

        self.assertRegex(postinst, r"addgroup.*dvb-worker")
        self.assertRegex(postinst, r"adduser.*dvb-worker")
        self.assertIn("/etc/docker-volume-backup", postinst)
        self.assertIn("/var/lib/docker-volume-backup", postinst)
        self.assertRegex(postinst, r"install -d -m 0750 .* /etc/docker-volume-backup")
        self.assertRegex(postinst, r"install -d -m 0700 .* /var/lib/docker-volume-backup")
        self.assertNotIn("worker.env", postinst)
        self.assertIn("systemctl daemon-reload", postinst)
        self.assertLess(postinst.index("install -d -m 0700"), postinst.index("systemctl daemon-reload"))
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN", postinst)
        self.assertNotIn("WORKER_SECRET", postinst)

    def test_postinst_enables_and_starts_unit_on_configure_only_when_systemd_runs(self):
        postinst = self.read_text(POSTINST)

        configure = re.search(r"(?ms)^\s*configure\)\n(.*?)^\s*;;", postinst)
        self.assertIsNotNone(configure, "postinst must handle the configure action")
        body = configure.group(1)
        self.assertIn("/run/systemd/system", body)
        enable = re.search(r"systemctl enable --now " + re.escape(UNIT_NAME) + r"\s*\|\|\s*true", body)
        self.assertIsNotNone(enable, "unit must be enabled and started tolerantly")
        self.assertLess(body.index("systemctl daemon-reload"), enable.start())
        # Upgrades restart an already-active unit so the new binary runs.
        self.assertRegex(body, r"systemctl is-active --quiet " + re.escape(UNIT_NAME))
        self.assertRegex(body, r"systemctl restart " + re.escape(UNIT_NAME) + r"\s*\|\|\s*true")
        self.assertLess(body.index("/run/systemd/system"), body.index("systemctl enable --now"))
        mutating = [line for line in postinst.splitlines() if re.search(r"systemctl (enable|start|restart) ", line)]
        self.assertTrue(mutating)
        for line in mutating:
            self.assertRegex(line, r"\|\|\s*true\s*$", f"not tolerant: {line.strip()}")
        self.assertIn("exit 0", postinst)

    def test_prerm_stops_before_remove_and_upgrade_without_disabling_upgrade(self):
        prerm = self.read_text(PRERM)

        self.assertTrue(prerm.startswith("#!/bin/sh"))
        self.assertIn("set -e", prerm)
        lifecycle = re.search(r"(?ms)^\s*remove\|upgrade\)\n(.*?)^\s*;;", prerm)
        self.assertIsNotNone(lifecycle, "prerm must stop the unit on remove and upgrade")
        body = lifecycle.group(1)
        self.assertIn("/run/systemd/system", body)
        self.assertRegex(body, r"(?m)systemctl stop " + re.escape(UNIT_NAME) + r"\s*$")
        self.assertRegex(body, r"systemctl disable " + re.escape(UNIT_NAME) + r"\s*\|\|\s*true")
        self.assertLess(body.index("systemctl stop"), body.index("systemctl disable"))
        self.assertIn('[ "$1" = remove ]', body)
        self.assertNotIn("systemctl stop docker-volume-backup-worker.service || true", body)
        self.assertNotIn("/var/lib/docker-volume-backup", prerm, "state (credentials) must survive removal")
        self.assertNotIn("rm ", prerm)
        self.assertNotIn("deluser", prerm)
        self.assertIn("exit 0", prerm)
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN", prerm)
        self.assertNotIn("WORKER_SECRET", prerm)

    def test_unit_bounds_systemd_shutdown(self):
        service = self.read_text(SERVICE)
        self.assertIn("TimeoutStopSec=120s", service)

    def test_package_contains_only_non_secret_environment_example(self):
        example = self.read_text(ENV_EXAMPLE)

        self.assertIn("CONTROL_PLANE_URL=", example)
        self.assertIn("WORKER_NAME=", example)
        self.assertIn("WORKER_CREDENTIAL_FILE=/var/lib/docker-volume-backup/worker_credentials.json", example)
        self.assertIn("WORKER_RUNTIME=native", example)
        # The file is an optional override set: enroll persists the URL for the service.
        self.assertIn("OPTIONAL", example)
        self.assertIn("vaultline-worker enroll --control-plane-url", example)
        self.assertIn("worker.json", example)
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN", example)
        self.assertNotIn("WORKER_SECRET", example)
        self.assertNotIn("change-me", example.lower())
        self.assertNotIn("password", example.lower())

    def test_builder_stages_expected_debian_tree_without_active_secret_config_or_tools(self):
        builder = self.read_text(BUILDER)

        self.assertIn("DEBIAN/control", builder)
        self.assertIn("DEBIAN/postinst", builder)
        self.assertRegex(builder, r'install -m 0755 "\$\{TEMPLATE_ROOT\}/prerm" "\$\{PACKAGE_ROOT\}/DEBIAN/prerm"')
        self.assertIn('test -x "${PACKAGE_ROOT}/DEBIAN/prerm"', builder)
        self.assertIn("usr/bin/vaultline-worker", builder)
        self.assertIn("usr/lib/vaultline/restic", builder)
        self.assertIn("usr/lib/vaultline/rclone", builder)
        self.assertIn("lib/systemd/system/docker-volume-backup-worker.service", builder)
        self.assertIn("usr/share/doc/vaultline-worker/worker.env.example", builder)
        self.assertIn("usr/share/doc/vaultline-worker/restic-v0.19.1-LICENSE", builder)
        self.assertIn("usr/share/doc/vaultline-worker/rclone-v1.75.2-COPYING", builder)
        self.assertNotIn("etc/docker-volume-backup/worker.env", builder)
        self.assertNotIn("worker.env.example etc/", builder)
        self.assertIn("test -x", builder)
        self.assertIn("test -f", builder)


if __name__ == "__main__":
    unittest.main()
