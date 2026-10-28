import re
import tempfile
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
        linux_job = self.workflow.split("  native-linux:", 1)[1].split("  native-windows:", 1)[0]
        self.assertIn("scripts/download_pinned_restic.py --platform linux --arch", linux_job)
        self.assertIn("scripts/download_pinned_rclone.py --platform linux --arch", linux_job)
        self.assertIn("export VAULTLINE_RESTIC_BINARY=/tmp/vaultline-restic/restic", linux_job)
        self.assertIn("export VAULTLINE_RCLONE_BINARY=/tmp/vaultline-rclone/rclone", linux_job)
        self.assertLess(linux_job.index("download_pinned_restic.py"), linux_job.index("python -m PyInstaller"))
        self.assertLess(linux_job.index("download_pinned_rclone.py"), linux_job.index("python -m PyInstaller"))
        self.assertIn("scripts/build_worker_deb.sh", self.workflow)

    def test_linux_debian_builder_is_invoked_with_bash(self):
        self.assertIn(
            'bash scripts/build_worker_deb.sh "${VERSION}" "${{ matrix.arch }}" "dist/vaultline-worker" "dist/packages"',
            self.workflow,
        )

    def test_linux_debian_builder_receives_the_verified_restic_and_rclone_binaries(self):
        # The download happens inside the manylinux container (/tmp is not shared),
        # so the verified tools must be staged into the workspace before the
        # native-host .deb step installs them under /usr/lib/vaultline.
        linux_job = self.workflow.split("  native-linux:", 1)[1].split("  native-windows:", 1)[0]
        self.assertIn('install -m 0755 "$VAULTLINE_RESTIC_BINARY" /workspace/dist/restic', linux_job)
        self.assertIn('install -m 0755 "$VAULTLINE_RCLONE_BINARY" /workspace/dist/rclone', linux_job)
        self.assertIn("export VAULTLINE_RESTIC_BINARY=dist/restic", linux_job)
        self.assertIn("export VAULTLINE_RCLONE_BINARY=dist/rclone", linux_job)
        self.assertLess(linux_job.index("python -m PyInstaller"), linux_job.index("/workspace/dist/restic"))
        self.assertLess(linux_job.index("/workspace/dist/rclone"), linux_job.index("export VAULTLINE_RESTIC_BINARY=dist/restic"))
        self.assertLess(linux_job.index("export VAULTLINE_RCLONE_BINARY=dist/rclone"), linux_job.index("bash scripts/build_worker_deb.sh"))

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
        linux_job = self.workflow.split("  native-linux:", 1)[1].split("  native-windows:", 1)[0]
        self.assertLess(linux_job.index("scripts/set_worker_build_version.py"), linux_job.index("python -m PyInstaller"))
        self.assertRegex(self.workflow, r"dist/vaultline-worker --help")

    def test_version_stamps_bracket_lower_and_current_runtime_builds(self):
        job = self.windows_job()
        launcher_build = job.index("PyInstaller build/vaultline-worker-launcher.spec")
        lower_stamp = job.index("scripts/set_worker_build_version.py $LowerVersion")
        lower_runtime_build = job.index("PyInstaller build/vaultline-worker.spec")
        current_stamp = job.index('scripts/set_worker_build_version.py "${{ needs.prepare.outputs.new_version }}"')
        current_runtime_build = job.rindex("PyInstaller build/vaultline-worker.spec")
        self.assertLess(launcher_build, lower_stamp)
        self.assertLess(lower_stamp, lower_runtime_build)
        self.assertLess(lower_runtime_build, current_stamp)
        self.assertLess(current_stamp, current_runtime_build)
        self.assertIn("scripts/download_pinned_restic.py --platform windows --arch amd64", job)
        self.assertIn("scripts/download_pinned_rclone.py --platform windows --arch amd64", job)
        self.assertIn("$env:VAULTLINE_RESTIC_BINARY", job)
        self.assertIn("$env:VAULTLINE_RCLONE_BINARY", job)
        self.assertLess(job.index("download_pinned_restic.py"), job.index("python -m PyInstaller"))
        self.assertLess(job.index("download_pinned_rclone.py"), job.index("python -m PyInstaller"))
        from src.worker_agent._build_info import VERSION
        self.assertEqual(VERSION, "dev")

    def test_version_stamp_script_accepts_only_semver(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "_build_info.py"
            script = ROOT / "scripts" / "set_worker_build_version.py"
            result = subprocess.run([sys.executable, str(script), "3.8.1", "--output", str(target)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), 'VERSION = "3.8.1"\n')
            original = target.read_text(encoding="utf-8")
            invalid = subprocess.run([sys.executable, str(script), '__import__("os").system("whoami")', "--output", str(target)], capture_output=True, text=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_windows_build_names_smokes_and_uploads_exact_exe(self):
        self.assertIn("runs-on: windows-2022", self.workflow)
        # setup-python@v5 was replaced by a checksum-verified pinned PBS
        # interpreter; no floating python-version input may return.
        self.assertNotIn("python-version:", self.windows_job())
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
        self.assertIn("-d LauncherExe=dist\\vaultline-worker.exe", job)
        self.assertRegex(job, r"wix build .*?-d WorkerExe=dist[\\/](?:lower[\\/])?vaultline-worker-runtime\.exe")
        self.assertIn('vaultline-worker_${Version}_windows-amd64.msi', job)
        # Both MSI builds (release and lower-version upgrade smoke) install the
        # verified Restic and rclone beside the worker from the staged dist copies.
        wix_builds = re.findall(r"(?m)^\s*wix build .*$", job)
        self.assertEqual(len(wix_builds), 2)
        for command in wix_builds:
            with self.subTest(command=command.strip()[:60]):
                self.assertRegex(command, r"-d ResticExe=dist[\\/]restic\.exe")
                self.assertRegex(command, r"-d RcloneExe=dist[\\/]rclone\.exe")
        self.assertIn("Copy-Item -LiteralPath $env:VAULTLINE_RESTIC_BINARY -Destination dist\\restic.exe", job)
        self.assertIn("Copy-Item -LiteralPath $env:VAULTLINE_RCLONE_BINARY -Destination dist\\rclone.exe", job)
        self.assertLess(job.index("python -m PyInstaller"), job.index("Copy-Item -LiteralPath $env:VAULTLINE_RESTIC_BINARY"))
        self.assertLess(job.index("Copy-Item -LiteralPath $env:VAULTLINE_RCLONE_BINARY"), job.index("Remove-Item -LiteralPath $ResticOutput"))
        self.assertLess(job.index("Copy-Item -LiteralPath $env:VAULTLINE_RCLONE_BINARY"), job.index("wix build"))
        # util:ServiceConfig (service recovery) needs the Util extension, pinned
        # to the same version as the wix CLI and added globally so the CLI finds it.
        self.assertIn("wix extension add -g WixToolset.Util.wixext/5.0.2", job)
        self.assertRegex(job, r"dotnet tool install --global wix --version 5\.0\.2")
        self.assertRegex(job, r"wix build .*-ext WixToolset\.Util\.wixext")
        self.assertLess(job.index("wix extension add -g WixToolset.Util.wixext"), job.index("wix build"))
        self.assertIn("vaultline-worker_${{ needs.prepare.outputs.new_version }}_windows-amd64.msi", job)
        self.assertIn("vaultline-worker_${{ needs.prepare.outputs.new_version }}_windows-amd64.exe", job)
        # The versioned portable artifact is the standalone runtime, not the MSI-only stable launcher.
        self.assertLess(job.index("wix build"), job.index("Move-Item -Path dist\\vaultline-worker-runtime.exe"))

    def test_windows_msi_smoke_upgrades_lower_version_checks_service_and_uninstalls(self):
        job = self.windows_job()
        smoke = re.search(r"(?ms)- name: Smoke MSI.*?(?=^      - name: |\Z)", job)
        self.assertIsNotNone(smoke, "an MSI smoke step is required")
        smoke = smoke.group(0)
        self.assertRegex(smoke, r"msiexec.*'/i'.*'/qn'.*'/norestart'")
        self.assertRegex(smoke, r"msiexec.*'/x'.*'/qn'.*'/norestart'")
        self.assertIn("lower-version", smoke)
        self.assertIn("$LowerMsi", smoke)
        self.assertIn("$upgrade = Start-Process msiexec", smoke)
        self.assertIn("'/l*v'", smoke)
        self.assertIn("ExitCode", smoke)
        self.assertIn("Get-Service", smoke)
        self.assertIn("VaultlineWorker", smoke)
        self.assertIn("'Running'", smoke)
        self.assertIn("C:\\Program Files\\Vaultline\\vaultline-worker.exe", smoke)
        self.assertIn("C:\\Program Files\\Vaultline\\vaultline-worker-runtime.exe", smoke)
        self.assertIn("$LauncherHashBeforeUpgrade", smoke)
        self.assertIn("$LauncherHashAfterUpgrade", smoke)
        self.assertIn("wrong runtime version", smoke)
        self.assertIn("C:\\Program Files\\Vaultline\\restic.exe", smoke)
        self.assertIn("C:\\Program Files\\Vaultline\\rclone.exe", smoke)
        self.assertIn("installed tool missing at $tool", smoke)
        self.assertIn("installed tool still present after uninstall", smoke)
        self.assertLess(smoke.index("installed tool missing at $tool"), smoke.index("'/x'"))
        self.assertLess(smoke.index("'/x'"), smoke.index("installed tool still present after uninstall"))
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
        # Mitigation only (not a proven root-cause fix): skip the redundant
        # archive compression for the incompressible exe/MSI upload payload.
        step = re.search(r"(?ms)- name: Upload Windows [^\n]*\n.*?(?=\n      - name: |\Z)", job)
        self.assertIsNotNone(step)
        with self.subTest(scope="windows upload step"):
            self.assertIn("compression-level: 0", step.group(0))
        # Other upload steps keep their default compression settings.
        self.assertEqual(self.workflow.count("compression-level:"), 1)

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

    def test_existing_image_tags_native_amd64_platform_and_worker_options_are_preserved(self):
        image_job = self.image_build_job()
        self.assertEqual(image_job.count("--platform linux/amd64"), 3)
        self.assertNotIn("linux/arm64", image_job)
        self.assertIn("ubuntu-24.04-arm", self.workflow)
        for target in ("backup-runtime", "control-plane", "worker"):
            self.assertIn(f"--target {target}", image_job)
        self.assertIn("ghcr.io/${{ github.repository }}", self.workflow)
        self.assertIn("ghcr.io/${{ github.repository_owner }}/${{ github.event.repository.name }}-control-plane", self.workflow)
        self.assertIn("ghcr.io/${{ github.repository_owner }}/${{ github.event.repository.name }}-worker", self.workflow)
        self.assertIn("type=semver,pattern={{version}},value=${{ needs.prepare.outputs.new_version }}", self.workflow)
        self.assertIn("type=raw,value=latest", self.workflow)
        self.assertIn("INSTALL_DOCKER_CLI=true", self.workflow)

    def test_release_images_use_standard_docker_cli_without_buildx_or_gha_cache(self):
        image_job = self.image_build_job()
        for forbidden in (
            "docker/setup-buildx-action@v3",
            "docker/build-push-action@v6",
            "docker buildx",
            "buildx-stable-1",
            "docker/setup-qemu-action@v3",
            "type=gha",
        ):
            self.assertNotIn(forbidden, image_job)
        self.assertEqual(image_job.count("docker build"), 3)
        self.assertEqual(image_job.count('docker push "$tag"'), 3)

    def test_release_image_builds_use_mirror_pinned_python_base_image(self):
        self.assertRegex(
            self.dockerfile,
            r"(?m)^ARG PYTHON_BASE_IMAGE=python:3\.11\.17-slim-bookworm$",
        )
        self.assertRegex(self.dockerfile, r"(?m)^FROM \$\{PYTHON_BASE_IMAGE\} AS app-base$")

        image_job = self.image_build_job()
        build_steps = dict(
            re.findall(
                r"(?ms)^      - name: Build and push ([^\n]+)\n(.*?)(?=^      - name: |\Z)",
                image_job,
            )
        )
        expected_targets = {
            "backup runtime image": ("backup-runtime", "meta_runtime"),
            "Control Plane image": ("control-plane", "meta_control_plane"),
            "Worker image": ("worker", "meta_worker"),
        }
        self.assertEqual(set(build_steps), set(expected_targets))
        for image_name, (target, metadata_id) in expected_targets.items():
            with self.subTest(target=target):
                step = build_steps[image_name]
                self.assertIn(f"IMAGE_TAGS: ${{{{ steps.{metadata_id}.outputs.tags }}}}", step)
                self.assertIn(f"IMAGE_LABELS: ${{{{ steps.{metadata_id}.outputs.labels }}}}", step)
                self.assertIn(f"--target {target}", step)
                self.assertIn("--platform linux/amd64", step)
                self.assertIn('docker_args+=(--label "$label")', step)
                self.assertIn('docker_args+=(--tag "$tag")', step)
                self.assertIn('docker push "$tag"', step)
                self.assertIn(
                    "PYTHON_BASE_IMAGE=mirror.gcr.io/library/python@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89",
                    step,
                )
                self.assertNotIn("PYTHON_BASE_IMAGE=public.ecr.aws", step)
                self.assertIn("APP_VERSION: ${{ needs.prepare.outputs.new_version }}", step)
                if target == "worker":
                    self.assertIn('INSTALL_DOCKER_CLI=true"', step)

    def test_prerelease_disables_automatic_latest_and_gates_all_stable_aliases(self):
        image_job = self.image_build_job()
        blocks = re.findall(
            r"(?ms)^      - name: Extract metadata for [^\n]+\n(.*?)(?=^      - name: |\Z)",
            image_job,
        )
        self.assertEqual(len(blocks), 3)
        for block in blocks:
            with self.subTest(image=block.split("images:", 1)[-1].splitlines()[0]):
                self.assertIn("flavor: latest=false", block)
                rules = re.findall(r"(?m)^            (type=.*)$", block)
                self.assertEqual(rules, [
                    "type=semver,pattern={{version}},value=${{ needs.prepare.outputs.new_version }}",
                    "type=semver,pattern={{major}}.{{minor}},value=${{ needs.prepare.outputs.new_version }},enable=${{ !inputs.prerelease }}",
                    "type=semver,pattern={{major}},value=${{ needs.prepare.outputs.new_version }},enable=${{ !inputs.prerelease }}",
                    "type=raw,value=latest,enable=${{ !inputs.prerelease }}",
                ])
                # Evaluate the channel selector for both workflow input values.
                for prerelease in (True, False):
                    enabled = [rule.split(",enable=", 1)[0] for rule in rules
                               if ",enable=" not in rule or not prerelease]
                    self.assertEqual(len(enabled), 1 if prerelease else 4)
                    self.assertIn("pattern={{version}}", enabled[0])
                    self.assertEqual(any("value=latest" in rule for rule in enabled), not prerelease)

    def test_prerelease_explicitly_avoids_github_latest_release(self):
        publish = self.workflow.split("  publish-release:", 1)[1]
        self.assertIn("${{ inputs.prerelease && '--latest=false' || '' }}", publish)
        self.assertIn("${{ inputs.prerelease && '--prerelease' || '' }}", publish)

    def image_build_job(self) -> str:
        match = re.search(r"(?ms)^  image-build:\n(.*?)(?=^  [\w-]+:\n|\Z)", self.workflow)
        self.assertIsNotNone(match)
        return match.group(1)


if __name__ == "__main__":
    unittest.main()
