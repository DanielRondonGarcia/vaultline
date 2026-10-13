import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
WORKFLOW = ROOT / ".github" / "workflows" / "release-dispatch.yml"


class ReleaseAssetWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_workflow_stays_manual_and_splits_prepare_native_image_publish_jobs(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertNotIn("push:\n", self.workflow)
        for job in ("prepare:", "native-linux:", "native-windows:", "image-build:", "publish-release:"):
            self.assertRegex(self.workflow, rf"(?m)^  {re.escape(job)}")
        self.assertRegex(self.workflow, r"publish-release:\n(?:.|\n)*?needs:\n(?:.|\n)*?- prepare\n(?:.|\n)*?- native-linux\n(?:.|\n)*?- native-windows\n(?:.|\n)*?- image-build")

    def test_linux_matrix_uses_native_runners_manylinux_and_python_311(self):
        self.assertIn("ubuntu-24.04", self.workflow)
        self.assertIn("ubuntu-24.04-arm", self.workflow)
        self.assertIn("quay.io/pypa/manylinux2014_x86_64", self.workflow)
        self.assertIn("quay.io/pypa/manylinux2014_aarch64", self.workflow)
        self.assertIn("cpython-3.11.17%2B20261003", self.workflow)
        self.assertNotIn("/opt/python/cp311-cp311/bin/python", self.workflow)
        self.assertIn("requirements-build.txt", self.workflow)
        self.assertIn("PyInstaller build/vaultline-worker.spec", self.workflow)
        self.assertIn("scripts/build_worker_deb.sh", self.workflow)

    def test_linux_debian_builder_is_invoked_with_bash(self):
        self.assertIn(
            'bash scripts/build_worker_deb.sh "${VERSION}" "${{ matrix.arch }}" "dist/vaultline-worker" "dist/packages"',
            self.workflow,
        )

    def test_linux_pyinstaller_uses_shared_python_build_standalone_runtime(self):
        self.assertIn("pbs_url", self.workflow)
        self.assertIn("pbs_sha256", self.workflow)
        self.assertIn("cpython-3.11.17%2B20261003-x86_64-unknown-linux-gnu-install_only.tar.gz", self.workflow)
        self.assertIn("c624af93ad62a596806bbd2404e1fb80744a407ca7279854445ede16d93858b8", self.workflow)
        self.assertIn("cpython-3.11.17%2B20261003-aarch64-unknown-linux-gnu-install_only.tar.gz", self.workflow)
        self.assertIn("2238f0556d3a9777d42261b1e4d7b9834d56f111d3dd879a0647c27c824cc31d", self.workflow)
        self.assertIn("/tmp/pbs/python/bin:$PATH", self.workflow)
        self.assertIn("/tmp/pbs/python/lib:${LD_LIBRARY_PATH:-}", self.workflow)
        self.assertIn("Py_ENABLE_SHARED=1", self.workflow)
        self.assertIn("libpython3.11.so", self.workflow)
        self.assertRegex(self.workflow, r"dist/vaultline-worker --help")

    def test_windows_build_names_smokes_and_uploads_exact_exe(self):
        self.assertIn("runs-on: windows-2022", self.workflow)
        self.assertIn("python-version: '3.11'", self.workflow)
        self.assertIn("vaultline-worker_${{ needs.prepare.outputs.new_version }}_windows-amd64.exe", self.workflow)
        self.assertRegex(self.workflow, r"vaultline-worker(?:\.exe)? --help")
        self.assertIn("actions/upload-artifact@v4", self.workflow)

    def windows_job(self) -> str:
        match = re.search(r"(?ms)^  native-windows:\n(.*?)(?=^  [\w-]+:\n|\Z)", self.workflow)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_windows_builds_msi_with_pinned_wix_from_the_wxs_source(self):
        job = self.windows_job()
        self.assertRegex(job, r"dotnet tool install --global wix --version 5\.\d+\.\d+")
        self.assertRegex(job, r"wix build -arch x64 .*packaging[\\/]windows[\\/]vaultline-worker\.wxs")
        self.assertRegex(job, r"wix build .*-d Version=\$Version")
        self.assertRegex(job, r"wix build .*-d WorkerExe=dist[\\/]vaultline-worker\.exe")
        self.assertIn('vaultline-worker_${Version}_windows-amd64.msi', job)
        # util:ServiceConfig (service recovery) needs the Util extension, pinned
        # to the same version as the wix CLI and added globally so the CLI finds it.
        self.assertIn("wix extension add -g WixToolset.Util.wixext/5.0.2", job)
        self.assertRegex(job, r"dotnet tool install --global wix --version 5\.0\.2")
        self.assertRegex(job, r"wix build .*-ext WixToolset\.Util\.wixext")
        self.assertLess(job.index("wix extension add -g WixToolset.Util.wixext"), job.index("wix build"))
        self.assertIn("vaultline-worker_${{ needs.prepare.outputs.new_version }}_windows-amd64.msi", job)
        self.assertIn("vaultline-worker_${{ needs.prepare.outputs.new_version }}_windows-amd64.exe", job)
        # The exe is moved to its release name before the msi is built, so the
        # msi build must consume the portable exe from its staged location.
        self.assertLess(job.index("wix build"), job.index("Move-Item -Path dist\\vaultline-worker.exe"))

    def test_windows_msi_smoke_installs_checks_service_and_uninstalls_silently(self):
        job = self.windows_job()
        smoke = re.search(r"(?ms)- name: Smoke MSI.*?(?=^      - name: |\Z)", job)
        self.assertIsNotNone(smoke, "an MSI smoke step is required")
        smoke = smoke.group(0)
        self.assertRegex(smoke, r"msiexec.*'/i'.*'/qn'.*'/norestart'")
        self.assertRegex(smoke, r"msiexec.*'/x'.*'/qn'.*'/norestart'")
        self.assertIn("'/l*v'", smoke)
        self.assertIn("ExitCode", smoke)
        self.assertIn("Get-Service", smoke)
        self.assertIn("VaultlineWorker", smoke)
        self.assertIn("'Running'", smoke)
        self.assertIn("C:\\Program Files\\Vaultline\\vaultline-worker.exe", smoke)
        self.assertIn("service status", smoke)
        self.assertIn("C:\\ProgramData\\Vaultline\\worker\\logs\\worker.log", smoke)
        self.assertIn("Waiting for enrollment", smoke)
        self.assertIn("-ErrorAction SilentlyContinue", smoke)
        self.assertNotIn("enroll --control-plane-url", smoke.replace("Waiting for enrollment", ""))
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN", smoke)
        self.assertNotIn("WORKER_SECRET", smoke)

    def test_windows_artifact_uploads_exe_and_msi_together(self):
        job = self.windows_job()
        upload = re.search(r"(?ms)- name: Upload Windows .*?path: \|\n((?:            .*\n)+)", job)
        self.assertIsNotNone(upload)
        paths = upload.group(1)
        self.assertIn("_windows-amd64.exe", paths)
        self.assertIn("_windows-amd64.msi", paths)
        self.assertEqual(job.count("name: vaultline-worker-windows-amd64"), 1)

    def test_publish_validates_assets_generates_checksums_and_uses_release_notes(self):
        required = re.search(r"(?ms)required=\(\n(.*?)\n\s*\)", self.workflow)
        self.assertIsNotNone(required)
        self.assertEqual(
            [line.strip().strip('"') for line in required.group(1).splitlines()],
            [
                "vaultline-worker_${VERSION}_amd64.deb",
                "vaultline-worker_${VERSION}_arm64.deb",
                "vaultline-worker_${VERSION}_windows-amd64.exe",
                "vaultline-worker_${VERSION}_windows-amd64.msi",
            ],
        )
        self.assertIn("vaultline-worker_${VERSION}_SHA256SUMS", self.workflow)
        self.assertIn('sha256sum "${required[@]}" > "vaultline-worker_${VERSION}_SHA256SUMS"', self.workflow)
        self.assertIn("--notes-file release_body.md", self.workflow)
        self.assertIn("--title \"v${VERSION}\"", self.workflow)
        self.assertIn("${{ inputs.prerelease && '--prerelease' || '' }}", self.workflow)
        release_args = re.search(
            r"gh release create \"\$\{VERSION\}\"(?:.|\n)*?\"dist/release-assets/vaultline-worker_\$\{VERSION\}_SHA256SUMS\"",
            self.workflow,
        )
        self.assertIsNotNone(release_args)
        for name in (
            "vaultline-worker_${VERSION}_amd64.deb",
            "vaultline-worker_${VERSION}_arm64.deb",
            "vaultline-worker_${VERSION}_windows-amd64.exe",
            "vaultline-worker_${VERSION}_windows-amd64.msi",
        ):
            self.assertIn(f'"dist/release-assets/{name}"', release_args.group(0))

    def test_tag_and_release_are_only_in_final_publish_job_after_dependencies(self):
        before_publish = self.workflow.split("  publish-release:", 1)[0]
        self.assertNotIn("git tag -a", before_publish)
        self.assertNotIn("gh release create", before_publish)
        self.assertIn("git tag -a \"${VERSION}\"", self.workflow)
        self.assertIn("git push origin \"${VERSION}\"", self.workflow)

    def test_existing_image_tags_platforms_and_worker_options_are_preserved(self):
        self.assertGreaterEqual(self.workflow.count("platforms: linux/amd64,linux/arm64"), 3)
        for target in ("backup-runtime", "control-plane", "worker"):
            self.assertIn(f"target: {target}", self.workflow)
        self.assertIn("ghcr.io/${{ github.repository }}", self.workflow)
        self.assertIn("ghcr.io/${{ github.repository_owner }}/${{ github.event.repository.name }}-control-plane", self.workflow)
        self.assertIn("ghcr.io/${{ github.repository_owner }}/${{ github.event.repository.name }}-worker", self.workflow)
        self.assertIn("type=semver,pattern={{version}},value=${{ needs.prepare.outputs.new_version }}", self.workflow)
        self.assertIn("type=raw,value=latest", self.workflow)
        self.assertIn("INSTALL_DOCKER_CLI=true", self.workflow)

    def test_buildx_uses_ecr_public_buildkit_image(self):
        self.assertRegex(
            self.workflow,
            r"(?m)^      - name: Set up Docker Buildx\n        uses: docker/setup-buildx-action@v3\n        with:\n          driver-opts: image=public\.ecr\.aws/vend/moby/buildkit:buildx-stable-1$",
        )

    def test_release_image_builds_use_ecr_public_python_base_image(self):
        self.assertRegex(
            self.dockerfile,
            r"(?m)^ARG PYTHON_BASE_IMAGE=python:3\.11-slim-bookworm$",
        )
        self.assertRegex(self.dockerfile, r"(?m)^FROM \$\{PYTHON_BASE_IMAGE\} AS app-base$")

        build_steps = dict(
            re.findall(
                r"(?ms)^      - name: Build and push ([^\n]+)\n(.*?)(?=^      - name: |\Z)",
                self.workflow,
            )
        )
        expected_targets = {
            "backup runtime image": "backup-runtime",
            "Control Plane image": "control-plane",
            "Worker image": "worker",
        }
        self.assertEqual(set(build_steps), set(expected_targets))
        for image_name, target in expected_targets.items():
            with self.subTest(target=target):
                self.assertIn(f"target: {target}", build_steps[image_name])
                build_args = re.search(
                    r"(?ms)^          build-args: \|\n((?:(?!^          [\w-]+:).+\n)*)",
                    build_steps[image_name],
                )
                self.assertIsNotNone(build_args)
                self.assertIn(
                    "PYTHON_BASE_IMAGE=public.ecr.aws/docker/library/python:3.11-slim-bookworm",
                    build_args.group(1),
                )
                self.assertIn("APP_VERSION=${{ needs.prepare.outputs.new_version }}", build_args.group(1))
                if target == "worker":
                    self.assertIn("INSTALL_DOCKER_CLI=true", build_args.group(1))


if __name__ == "__main__":
    unittest.main()
