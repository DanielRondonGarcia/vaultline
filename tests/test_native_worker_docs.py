import re
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
            "vaultline-worker enroll --control-plane-url <url> --bootstrap-token <token>",
            "VAULTLINE_BOOTSTRAP_TOKEN",
            "prompt oculto",
            "vaultline-worker enroll --reset-pending",
            "code=enrollment_expired",
            "dvb-worker",
            "/etc/docker-volume-backup/worker.env",
            "/var/lib/docker-volume-backup",
            "docker-volume-backup-worker.service",
            "VaultlineWorker",
            "C:\\ProgramData\\Vaultline\\worker",
            "glibc >= 2.17",
            "SmartScreen",
            "Restic v0.19.1 and rclone v1.75.2 executables are bundled with native worker packages",
            "The MSI installs them in `C:\\Program Files\\Vaultline` and the `.deb` in `/usr/lib/vaultline`",
            "Keep `rclone.conf` and repository credentials in their separate secrets",
            "Install the remaining external tools (`tar`, `gpg`, `aws`, `ssh`, and `scp`) separately",
            "In source/development mode, executables (`restic`,",
            "espera el enrolamiento",
        ):
            self.assertIn(marker, self.quickstart)
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN=", self.quickstart)
        # The token is embedded in the enroll command on purpose; the old blanket ban is gone.
        self.assertNotIn("No pongas el token en argumentos", self.quickstart)
        self.assertIn("Pegarlo en el comando `enroll` es aceptable", self.quickstart)

    @staticmethod
    def _fenced_blocks(markdown):
        return re.findall(r"```(?:bash|powershell)\n(.*?)```", markdown, re.S)

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

        # Windows: download -> install (wait) -> reload PATH -> enroll with token -> status; service already running.
        windows = native[native.index("#### Runner Windows como servicio (MSI)"):]
        windows_block = self._fenced_blocks(windows)[0]
        self.assertIn("Invoke-WebRequest -Uri https://github.com/DanielRondonGarcia/vaultline/releases/download/<version>/vaultline-worker_<version>_windows-amd64.msi -OutFile $msi", windows_block)
        self.assertIn("Start-Process msiexec.exe -ArgumentList ('/i \"{0}\" /qn /norestart' -f $msi) -Wait", windows_block)
        self.assertIn('$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine")', windows_block)
        self.assertIn("vaultline-worker enroll --control-plane-url https://backups.example.com --bootstrap-token <token>", windows_block)
        self.assertIn("vaultline-worker service status", windows_block)
        self.assertIn("C:\\ProgramData\\Vaultline\\worker\\logs\\worker.log", windows)
        self.assertIn("LocalSystem", windows)
        self.assertIn("`restic.exe` y `rclone.exe`", windows)
        self.assertLess(windows_block.index("Invoke-WebRequest"), windows_block.index("msiexec.exe"))
        self.assertLess(windows_block.index("msiexec.exe"), windows_block.index("$env:Path ="))
        self.assertLess(windows_block.index("$env:Path ="), windows_block.index("enroll --control-plane-url"))
        self.assertLess(windows_block.index("enroll --control-plane-url"), windows_block.index("service status"))
        self.assertIn("msiexec /x", windows)
        self.assertIn("vaultline-worker.exe daemon", windows)
        self.assertIn("service install", windows)

        # Linux: download -> dpkg -i (unit enabled and started) -> enroll with token -> status; worker.env optional.
        linux = native[native.index("#### Runner Linux con systemd"):native.index("#### Runner Windows como servicio (MSI)")]
        linux_block = self._fenced_blocks(linux)[0]
        self.assertIn("curl -fsSL -o vaultline-worker_<version>_amd64.deb https://github.com/DanielRondonGarcia/vaultline/releases/download/<version>/vaultline-worker_<version>_amd64.deb", linux_block)
        self.assertIn("sudo dpkg -i vaultline-worker_<version>_amd64.deb", linux_block)
        self.assertIn("sudo vaultline-worker enroll --control-plane-url https://backups.example.com --bootstrap-token <token>", linux_block)
        self.assertIn("sudo systemctl status docker-volume-backup-worker.service --no-pager", linux_block)
        self.assertIn("journalctl -u docker-volume-backup-worker.service -f", linux)
        self.assertIn("`restic` y `rclone`\nen `/usr/lib/vaultline`", linux)
        self.assertIn("`/etc/docker-volume-backup/worker.env` es **opcional**", linux)
        self.assertIn("worker.env.example", linux)
        self.assertLess(linux_block.index("curl -fsSL"), linux_block.index("dpkg -i"))
        self.assertLess(linux_block.index("dpkg -i"), linux_block.index("enroll --control-plane-url"))
        self.assertLess(linux_block.index("enroll --control-plane-url"), linux_block.index("systemctl status"))

        # The UI-mirrored blocks are copy-paste ready: no comment lines, no blank lines.
        for name, block in (("windows", windows_block), ("linux", linux_block)):
            with self.subTest(block=name):
                lines = block.strip("\n").split("\n")
                self.assertTrue(all(line.strip() for line in lines), lines)
                self.assertFalse(any(line.lstrip().startswith("#") for line in lines), lines)

        # Lifecycle: upgrades keep state, uninstall keeps state, no re-enroll.
        self.assertIn("no hace falta re-enrolar", native)
        self.assertIn("sudo apt remove vaultline-worker", native)
        self.assertIn("sin reiniciar", native)

        # Developer checkout flow is preserved.
        self.assertIn("python -m src.worker_agent.cli daemon", native)

    def test_readme_links_to_native_worker_package_guidance_concisely(self):
        self.assertIn("packaged native worker guide", self.readme)
        self.assertIn("doc/control-plane-quickstart.md#worker-nativo-empaquetado-v2-seguro", self.readme)
        self.assertIn("vaultline-worker enroll --control-plane-url <url> --bootstrap-token <token>", self.readme)
        self.assertIn("ready to paste", self.readme)
        self.assertNotIn("enter the secret at the hidden prompt", self.readme)
        self.assertIn("VaultlineWorker (Windows MSI) / docker-volume-backup-worker.service (Linux .deb)", self.readme)
        self.assertIn("Native packages include Restic v0.19.1 and rclone v1.75.2 executables", self.readme)
        self.assertIn("(`C:\\Program Files\\Vaultline` or `/usr/lib/vaultline`)", self.readme)
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
