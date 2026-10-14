import os
import unittest
from unittest.mock import patch

from src.worker_agent.main import _resolve_live_helper_image, _resolve_worker_version


class LiveHelperImageResolutionTests(unittest.TestCase):
    def test_explicit_live_helper_image_precedes_worker_image(self):
        with patch.dict(
            os.environ,
            {
                "LIVE_FILE_HELPER_IMAGE": "registry.example/helper:custom",
                "WORKER_IMAGE": "registry.example/worker:custom",
                "APP_VERSION": "3.3.1",
            },
            clear=True,
        ):
            self.assertEqual(_resolve_live_helper_image(), "registry.example/helper:custom")

    def test_worker_image_precedes_version_inference(self):
        with patch.dict(
            os.environ,
            {
                "WORKER_IMAGE": "registry.example/worker:custom",
                "WORKER_VERSION": "3.3.1",
            },
            clear=True,
        ):
            self.assertEqual(_resolve_live_helper_image(), "registry.example/worker:custom")

    def test_concrete_worker_or_app_version_infers_published_worker_image(self):
        for variable in ("WORKER_VERSION", "APP_VERSION"):
            with self.subTest(variable=variable):
                with patch.dict(os.environ, {variable: "3.3.1"}, clear=True):
                    self.assertEqual(
                        _resolve_live_helper_image(),
                        "ghcr.io/danielrondongarcia/vaultline-worker:3.3.1",
                    )

    def test_non_concrete_versions_keep_local_fallback(self):
        for version in ("dev", "latest", "ghcr", "docker", "3.3"):
            with self.subTest(version=version):
                with patch.dict(os.environ, {"APP_VERSION": version}, clear=True):
                    self.assertEqual(_resolve_live_helper_image(), "docker-volume-backup-worker-local:dev")


class WorkerVersionResolutionTests(unittest.TestCase):
    def test_packaged_build_version_is_used_when_environment_is_generic(self):
        with patch.dict(os.environ, {"WORKER_VERSION": "dev", "APP_VERSION": "latest"}, clear=True), patch(
            "src.worker_agent.main.BUILD_VERSION", "3.5.3"
        ):
            self.assertEqual(_resolve_worker_version(), "3.5.3")

    def test_explicit_worker_version_precedes_app_version(self):
        with patch.dict(os.environ, {"WORKER_VERSION": "3.5.4", "APP_VERSION": "3.5.3"}, clear=True):
            self.assertEqual(_resolve_worker_version(), "3.5.4")


if __name__ == "__main__":
    unittest.main()
