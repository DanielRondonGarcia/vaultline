"""Manual isolated Docker-runtime probe; creates only uniquely named test volumes."""
import hashlib
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import docker

from src.worker_agent.application.services.local_schedule_service import LocalScheduleService
from src.worker_agent.application.services.worker_agent_service import WorkerAgentService
from src.worker_agent.domain.models import WorkerAgentConfig
from src.worker_agent.infrastructure.adapters.docker_runtime import DockerRuntimeAdapter
from src.worker_agent.infrastructure.api_client.control_plane_client import ControlPlaneClient
from src.worker_agent.infrastructure.security.credential_store import WorkerCredentialStore
from src.worker_agent.infrastructure.security.offline_result_outbox import OfflineResultOutbox
from src.worker_agent.infrastructure.security.schedule_state_journal import WorkerScheduleStateJournal
from tests.test_control_plane_schedule_state import WorkerScheduleRouteTests

HELPER = "docker-volume-backup-worker-local:dev"
RUNTIME = "docker-volume-backup-runtime-local:dev"
PASSWORD = "test-only-password"


def offline(*_args):
    raise ConnectionError("isolated control plane unavailable")


def helper(client, source, repo, program):
    return client.containers.run(
        HELPER, ["-c", program], entrypoint="python", remove=True, network_mode="none",
        mem_limit="512m", tmpfs={"/verify": "size=256m"},
        volumes={source: {"bind": "/fixture", "mode": "rw"}, repo: {"bind": "/repo", "mode": "rw"}},
        environment={"RESTIC_REPOSITORY": "/repo", "RESTIC_PASSWORD": PASSWORD},
    )


def main():
    client = docker.from_env()
    suffix = uuid.uuid4().hex
    source_name, repo_name = f"vaultline-probe-{suffix}-data", f"vaultline-probe-{suffix}-repo"
    source = repo = None
    print(f"Isolated test volumes: {source_name}, {repo_name}", flush=True)
    try:
        source = client.volumes.create(name=source_name, labels={"vaultline.probe": suffix})
        repo = client.volumes.create(name=repo_name, labels={"vaultline.probe": suffix})
        helper(client, source_name, repo_name,
               "import subprocess; f=open('/fixture/data','wb'); "
               "[f.write(b'x'*1048576) for _ in range(100)]; f.close(); "
               "subprocess.run(['restic','init'],check=True)")
        scratch_root = Path('.tmp')
        scratch_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="worker-probe-", dir=str(scratch_root)) as directory:
            root = Path(directory)
            payload = {
                "target_id": "target-a", "runtime_type": "docker", "backup_mode": "hot",
                "backup_strategy": "restic", "image": RUNTIME, "network_mode": "none",
                "environment": {"RESTIC_REPOSITORY": "/repo", "RESTIC_PASSWORD": PASSWORD,
                                "BACKUP_SOURCES": "/fixture", "BACKUP_STOP_CONTAINERS": "false", "BACKUP_STRATEGY": "restic",
                                "BACKUP_RUNTIME_TYPE": "native"},
                "volumes": {source_name: {"bind": "/fixture", "mode": "ro"},
                            repo_name: {"bind": "/repo", "mode": "rw"}},
            }
            worker = WorkerAgentService(WorkerAgentConfig("http://unreachable.invalid", "worker-a", "test", worker_id="worker-a"),
                                        Mock(), DockerRuntimeAdapter(timeout_seconds=180), recovery_file=str(root / "recovery.json"))
            journal = WorkerScheduleStateJournal(root / "worker_schedule_state.json")
            outbox = OfflineResultOutbox(root / "worker_offline_results.json")
            journal.save({"version": 1, "revision": "test", "timezone": "UTC", "entries": [
                {"target_id": "target-a", "cron_expression": "* * * * *", "enabled": True, "payload": payload}
            ]})
            now = lambda: datetime(2025, 5, 5, 3, 15, tzinfo=timezone.utc)
            schedule = LocalScheduleService(journal=journal, outbox=outbox, fetch=offline, deliver=offline,
                                            executor=worker.execute_offline_backup, now=now)
            result = schedule.poll("worker-a")
            pending = outbox.pending()
            if result["executed"] != 1 or len(pending) != 1 or pending[0]["status"] != "succeeded":
                raise AssertionError(f"offline Docker-runtime backup failed: {result!r} {pending!r}")
            output = helper(client, source_name, repo_name,
                            "import hashlib,json,subprocess,pathlib; "
                            "s=json.loads(subprocess.check_output(['restic','snapshots','--json'])); "
                            "assert len(s)==1; subprocess.run(['restic','restore',s[0]['id'],'--target','/verify'],check=True); "
                            "p=list(pathlib.Path('/verify').rglob('data')); assert len(p)==1; "
                            "assert p[0].stat().st_size==104857600; "
                            "a=hashlib.sha256(open('/fixture/data','rb').read()).digest(); "
                            "b=hashlib.sha256(p[0].read_bytes()).digest(); assert a==b; print('snapshot restored and verified')")
            print(output.decode('utf-8', errors='replace').strip(), flush=True)
            fixture = WorkerScheduleRouteTests()
            fixture.setUp()
            try:
                host, port = fixture.server.server_address
                credentials = WorkerCredentialStore(root / "worker_credentials.json")
                credentials.save("worker-a", fixture.WORKER_SECRET)
                cp = ControlPlaneClient(f"http://{host}:{port}", credential_store=credentials, worker_id="worker-a")
                restarted = LocalScheduleService(journal=WorkerScheduleStateJournal(root / "worker_schedule_state.json"),
                                                 outbox=OfflineResultOutbox(root / "worker_offline_results.json"), fetch=offline,
                                                 deliver=cp.submit_offline_result, executor=worker.execute_offline_backup, now=now)
                if restarted.evaluate("worker-a")["executed"] != 0:
                    raise AssertionError("same-slot backup repeated after recreating the scheduler")
                # Run the actual worker main in a new OS process. Orphan sweeping
                # is stubbed solely to guarantee this local test cannot touch
                # temporary runtime containers belonging to other sessions.
                target = fixture.service.target_repository.get("target-a")
                target.cron_expression = None
                fixture.service.target_repository.save(target)
                child_env = {**os.environ, "WORKER_STATE_DIR": str(root.resolve()),
                             "WORKER_CREDENTIAL_FILE": str((root / "worker_credentials.json").resolve()),
                             "CONTROL_PLANE_URL": f"http://{host}:{port}", "WORKER_RUNTIME": "docker",
                             "WORKER_RUN_ONCE": "true", "WORKER_HEALTH_PORT": "0", "PYTHONDONTWRITEBYTECODE": "1"}
                program = ("from unittest.mock import patch; "
                           "from src.worker_agent.application.services.worker_agent_service import WorkerAgentService; "
                           "from src.worker_agent import main; "
                           "patch.object(WorkerAgentService,'cleanup_orphaned_runtime_containers',return_value={}).start(); "
                           "main.main()")
                completed = subprocess.run([sys.executable, "-c", program], env=child_env, capture_output=True, text=True, timeout=90)
                if completed.returncode != 0:
                    raise AssertionError(f"restarted worker failed: {completed.stderr[-4000:]}")
                if outbox.pending() or fixture.service.get_job(pending[0]["id"]) is None:
                    raise AssertionError("restarted worker did not deliver the offline result")
                if not cp.submit_offline_result("worker-a", pending[0]).get("duplicate"):
                    raise AssertionError("duplicate delivery was not acknowledged")
            finally:
                fixture.stop_server()
        print("PASS: 100 MiB real Docker-runtime backup/restore, worker-main process restart and HMAC replay (orphan sweep stubbed)", flush=True)
    finally:
        for volume in (repo, source):
            if volume is not None:
                volume.remove()
                print(f"Removed test volume {volume.name}", flush=True)


if __name__ == "__main__":
    main()
