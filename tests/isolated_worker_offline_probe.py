"""Run manually inside an isolated worker container; never uses production state."""

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from src.worker_agent.application.services.local_schedule_service import LocalScheduleService
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.native_runtime import NativeRuntimeAdapter
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient
from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore
from src.worker_agent.infrastructure.security.offline_result_outbox import OfflineResultOutbox
from src.worker_agent.infrastructure.security.schedule_state_journal import WorkerScheduleStateJournal
from tests.test_control_plane_schedule_state import WorkerScheduleRouteTests


def offline(*_args):
    raise ConnectionError("isolated control plane unavailable")


def main():
    with tempfile.TemporaryDirectory(prefix="vaultline-offline-probe-") as directory:
        root = Path(directory)
        source = root / "input"
        source.mkdir()
        with (source / "fixture.bin").open("wb") as output:
            for _ in range(100):
                output.write(b"x" * (1024 * 1024))
        env = {**os.environ, "RESTIC_REPOSITORY": str(root / "repo"), "RESTIC_PASSWORD": "test-only-password"}
        subprocess.run(["restic", "init"], env=env, check=True, capture_output=True, text=True)
        runtime = NativeRuntimeAdapter(timeout_seconds=180)
        worker = WorkerAgentService(
            WorkerAgentConfig("http://unreachable.invalid", "worker-a", "test", worker_id="worker-a"),
            Mock(), runtime, recovery_file=str(root / "recovery.json"),
        )
        payload = {
            "target_id": "target-a", "runtime_type": "native", "backup_mode": "hot",
            "backup_strategy": "restic", "filesystem_paths": [str(source)],
            "environment": {"RESTIC_REPOSITORY": str(root / "repo"), "RESTIC_PASSWORD": "test-only-password"},
        }
        journal = WorkerScheduleStateJournal(root / "schedule.json")
        outbox = OfflineResultOutbox(root / "results.json")
        journal.save({"version": 1, "revision": "test", "timezone": "UTC", "entries": [
            {"target_id": "target-a", "cron_expression": "* * * * *", "enabled": True, "payload": payload}
        ]})
        now = lambda: datetime(2025, 5, 5, 3, 15, tzinfo=timezone.utc)
        schedule = LocalScheduleService(journal=journal, outbox=outbox, fetch=offline, deliver=offline,
                                        executor=worker.execute_offline_backup, now=now)
        result = schedule.poll("worker-a")
        pending = outbox.pending()
        if result["executed"] != 1 or len(pending) != 1 or pending[0]["status"] != "succeeded":
            raise AssertionError(f"offline execution failed: {result!r} {pending!r}")
        snapshots = json.loads(subprocess.run(["restic", "snapshots", "--json"], env=env, check=True,
                                              capture_output=True, text=True).stdout)
        if len(snapshots) != 1:
            raise AssertionError(f"expected one actual restic snapshot, got {len(snapshots)}")
        restored = root / "restored"
        subprocess.run(["restic", "restore", snapshots[0]["id"], "--target", str(restored)], env=env,
                       check=True, capture_output=True, text=True)
        files = list(restored.rglob("fixture.bin"))
        if len(files) != 1 or files[0].stat().st_size != 100 * 1024 * 1024:
            raise AssertionError("restored fixture has the wrong size")
        with (source / "fixture.bin").open("rb") as original, files[0].open("rb") as recovered:
            if hashlib.file_digest(original, "sha256").digest() != hashlib.file_digest(recovered, "sha256").digest():
                raise AssertionError("restored fixture checksum differs")
        fixture = WorkerScheduleRouteTests()
        fixture.setUp()
        try:
            host, port = fixture.server.server_address
            credentials = WorkerCredentialStore(root / "worker_credentials.json")
            credentials.save("worker-a", fixture.WORKER_SECRET)
            client = ControlPlaneClient(f"http://{host}:{port}", credential_store=credentials, worker_id="worker-a")
            resumed = LocalScheduleService(journal=WorkerScheduleStateJournal(root / "schedule.json"),
                                           outbox=OfflineResultOutbox(root / "results.json"), fetch=offline,
                                           deliver=client.submit_offline_result,
                                           executor=worker.execute_offline_backup, now=now)
            if resumed.evaluate("worker-a")["executed"] != 0 or resumed.replay("worker-a") != 1:
                raise AssertionError("restart deduplication or authenticated result delivery failed")
            if outbox.pending() or fixture.service.get_job(pending[0]["id"]) is None:
                raise AssertionError("control plane did not persist the result")
            if not client.submit_offline_result("worker-a", pending[0]).get("duplicate"):
                raise AssertionError("duplicate submission was not acknowledged")
        finally:
            fixture.stop_server()
        print("PASS: 100 MiB, real restic snapshot and restore, offline worker, new service instance and HMAC HTTP replay")


if __name__ == "__main__":
    main()
