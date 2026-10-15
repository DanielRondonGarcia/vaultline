import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
QUICKSTART = ROOT / "doc" / "control-plane-quickstart.md"
# Historical release evidence intentionally remains outside this active-doc audit.
DOCS_TO_AUDIT = (
    ROOT / "README.md",
    ROOT / "doc" / "control-plane-quickstart.md",
    ROOT / "doc" / "control-plane-spec.md",
    ROOT / "deploy" / "control-plane" / "README.md",
    ROOT / "deploy" / "worker" / "README.md",
    ROOT / "examples" / "restic-rclone" / "docker-compose.yml",
)


class NativeWorkerDocsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = README.read_text(encoding="utf-8")
        cls.quickstart = QUICKSTART.read_text(encoding="utf-8")

    def test_quickstart_documents_secure_native_worker_installation_flow(self):
        for marker in (
            "V2 seguro",
            "V1 legacy",
            "vaultline-worker enroll --control-plane-url",
            "prompt oculto",
            "dvb-worker",
            "/etc/docker-volume-backup/worker.env",
            "/var/lib/docker-volume-backup",
            "docker-volume-backup-worker.service",
            "VaultlineWorker",
            "C:\\ProgramData\\Vaultline\\worker",
            "glibc >= 2.17",
            "SmartScreen",
            "Restic v0.19.1 and rclone v1.75.2 executables are bundled with native worker packages",
            "Keep `rclone.conf` and repository credentials in their separate secrets",
            "Install the remaining external tools (`tar`, `gpg`, `aws`, `ssh`, and `scp`) separately",
            "In source/development mode, executables (`restic`,",
            "espera el enrolamiento",
        ):
            self.assertIn(marker, self.quickstart)
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN=", self.quickstart)

    def test_quickstart_native_sections_teach_install_then_enroll_with_running_service(self):
        start = self.quickstart.index("### Worker nativo empaquetado (V2 seguro)")
        end = self.quickstart.index("## 3. Autenticación y RBAC")
        native = self.quickstart[start:end]

        # Headings: MSI service is the Windows path; the portable exe is an alternative.
        self.assertIn("#### Runner Linux con systemd", native)
        self.assertIn("#### Runner Windows como servicio (MSI)", native)
        self.assertIn("##### Alternativa: ejecutable portable", native)
        self.assertIn("Restic v0.19.1 and rclone v1.75.2 executables are bundled with native worker packages", native)
        self.assertNotIn("Task Scheduler", native)
        self.assertNotIn("ScheduledTask", native)
        self.assertNotIn("No usa Windows SCM", native)
        self.assertNotIn("no arranca ni enrola automaticamente", native)

        # Windows: install -> enroll (elevated, hidden prompt) -> status; service already running.
        windows = native[native.index("#### Runner Windows como servicio (MSI)"):]
        self.assertIn("msiexec /i vaultline-worker_<version>_windows-amd64.msi /qn /norestart", windows)
        self.assertIn("vaultline-worker enroll --control-plane-url https://backups.example.com", windows)
        self.assertIn("vaultline-worker service status", windows)
        self.assertIn("C:\\ProgramData\\Vaultline\\worker\\logs\\worker.log", windows)
        self.assertIn("LocalSystem", windows)
        self.assertLess(windows.index("msiexec /i"), windows.index("enroll --control-plane-url"))
        self.assertLess(windows.index("enroll --control-plane-url"), windows.index("service status"))
        self.assertIn("msiexec /x", windows)
        self.assertIn("vaultline-worker.exe daemon", windows)
        self.assertIn("service install", windows)

        # Linux: dpkg -i (unit enabled and started) -> enroll -> status; worker.env optional.
        linux = native[native.index("#### Runner Linux con systemd"):native.index("#### Runner Windows como servicio (MSI)")]
        self.assertIn("sudo dpkg -i vaultline-worker_<version>_amd64.deb", linux)
        self.assertIn("sudo vaultline-worker enroll --control-plane-url https://backups.example.com", linux)
        self.assertIn("systemctl status docker-volume-backup-worker.service", linux)
        self.assertIn("journalctl -u docker-volume-backup-worker.service -f", linux)
        self.assertIn("`/etc/docker-volume-backup/worker.env` es **opcional**", linux)
        self.assertIn("worker.env.example", linux)
        self.assertLess(linux.index("dpkg -i"), linux.index("enroll --control-plane-url"))
        self.assertLess(linux.index("enroll --control-plane-url"), linux.index("systemctl status"))

        # Lifecycle: upgrades keep state, uninstall keeps state, no re-enroll.
        self.assertIn("no hace falta re-enrolar", native)
        self.assertIn("sudo apt remove vaultline-worker", native)
        self.assertIn("sin reiniciar", native)

        # Developer checkout flow is preserved.
        self.assertIn("python -m src.worker_agent.cli daemon", native)

    def test_readme_links_to_native_worker_package_guidance_concisely(self):
        self.assertIn("packaged native worker guide", self.readme)
        self.assertIn("doc/control-plane-quickstart.md#worker-nativo-empaquetado-v2-seguro", self.readme)
        self.assertIn("vaultline-worker enroll --control-plane-url <url>", self.readme)
        self.assertIn("VaultlineWorker (Windows MSI) / docker-volume-backup-worker.service (Linux .deb)", self.readme)
        self.assertIn("Native packages include Restic v0.19.1 and rclone v1.75.2 executables", self.readme)
        self.assertIn("keep `rclone.conf` and repository credentials in separate secrets", self.readme)
        self.assertNotIn("Task Scheduler", self.readme)

    def test_public_docs_use_canonical_vaultline_repository_and_images(self):
        docs_by_path = {path: path.read_text(encoding="utf-8") for path in DOCS_TO_AUDIT}
        combined_docs = "\n".join(docs_by_path.values())

        self.assertIn("https://github.com/DanielRondonGarcia/vaultline/releases", combined_docs)
        for image in (
            "ghcr.io/danielrondongarcia/vaultline",
            "ghcr.io/danielrondongarcia/vaultline-control-plane",
            "ghcr.io/danielrondongarcia/vaultline-worker",
        ):
            self.assertIn(image, combined_docs)

        self.assertIn(
            "image: ghcr.io/danielrondongarcia/vaultline",
            docs_by_path[ROOT / "examples" / "restic-rclone" / "docker-compose.yml"],
        )
        for legacy_reference in (
            "github.com/DanielRondonGarcia/docker-volume-backup",
            "ghcr.io/danielrondongarcia/docker-volume-backup",
            "danielrondongarcia/docker-volume-backup",
        ):
            with self.subTest(legacy_reference=legacy_reference):
                self.assertNotIn(legacy_reference, combined_docs)


if __name__ == "__main__":
    unittest.main()
