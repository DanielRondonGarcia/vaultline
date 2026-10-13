# Native Worker Service Login (Tailscale-style install + enroll)

## Objective

Make the native worker behave like `tailscaled` + `tailscale up`: the OS service is installed and started by the package (Windows MSI, Linux `.deb`), idles safely while unenrolled, and `vaultline-worker enroll` is the "login" an administrator runs afterwards. The running service picks up the durable credential without a restart.

## User-approved direction

- Windows: ship an `.msi` that installs `vaultline-worker.exe`, registers a Windows SCM service (`VaultlineWorker`, `LocalSystem`, auto-start) and starts it. The SCM integration lives inside the PyInstaller executable via `pywin32` (no third-party wrapper). Keep publishing the portable `.exe` as well.
- Linux: the `.deb` enables and starts `docker-volume-backup-worker.service` at install; the daemon idles until enrolled.
- `vaultline-worker enroll --control-plane-url <url>` is the login for both platforms. It persists the Control Plane URL (and optional CA path) into the machine state directory so the service does not need a separately edited env file.
- The prior decision "no MSI wizard / no Windows SCM service / Task Scheduler guidance" in `odd/tasks/native-worker-installers.md` is superseded by this record.

## Design decisions

- **State directory**: new `WORKER_STATE_DIR`. Resolution order: env `WORKER_STATE_DIR`; else, when frozen, platform default (`/var/lib/docker-volume-backup` on Linux, `%ProgramData%\Vaultline\worker` on Windows); else the current working directory (developer mode). `WORKER_CREDENTIAL_FILE` keeps precedence and defaults to `<state_dir>/worker_credentials.json`. Job recovery file stays beside the credential file.
- **Enrollment config**: `enroll` writes `<state_dir>/worker.json` (`control_plane_url`, optional `control_plane_ca_file`, `worker_name` when given). The daemon reads it as fallback when `CONTROL_PLANE_URL` is not set in the environment. Environment always wins.
- **Idle until enrolled**: when no durable credential exists and no V1 enrollment token is configured, the daemon logs "waiting for enrollment", reports `not_ready` on `/health`, sleeps the poll interval and re-checks the credential store. It never exits for that reason. The existing 5-attempt startup cap remains for real failures (credential/token present but Control Plane errors). `worker_id` is re-resolved from the credential store on each loop instead of being frozen at `build_service()`.
- **Credential ownership for the service account**:
  - Windows: `icacls` grants `*S-1-5-18` (SYSTEM) and `*S-1-5-32-544` (Administrators) in addition to the invoking user, using SIDs so it is locale-independent.
  - Linux: when `enroll` runs as root and the state directory is owned by another user (`dvb-worker`), written files are chowned to the directory owner.
- **Graceful stop**: `worker_main` exposes a stop request (event) honored by the main loop sleeps so the Windows service can stop cleanly; a job in flight finishes its current loop iteration.
- **Windows service**: `src/worker_agent/windows_service.py` with a `win32serviceutil.ServiceFramework` subclass; CLI `service install|uninstall|start|stop|status|run` (`run` is the SCM entry used by the registered command line `vaultline-worker.exe service run`). On Linux the same subcommand delegates `start|stop|status` to `systemctl` for the packaged unit. pywin32 is a Windows-only dependency (`pywin32; sys_platform == "win32"`) and its hidden imports go inside the existing single `win32` block of the spec.
- **MSI**: WiX (v5 via `dotnet tool install --global wix`) on the `windows-2022` runner, source `packaging/windows/vaultline-worker.wxs`: per-machine install to `ProgramFiles64\Vaultline\`, creates `%ProgramData%\Vaultline\worker`, `ServiceInstall` + `ServiceControl` (start on install, stop/remove on uninstall), adds the install dir to the system `PATH`, `MajorUpgrade` with a fixed `UpgradeCode`. Asset `vaultline-worker_<version>_windows-amd64.msi`, listed in SHA256SUMS and in the Control Plane allowlist as `windows-amd64-msi`. CI smoke: silent install, `sc query VaultlineWorker` running, `vaultline-worker --help` from PATH, silent uninstall.
- **Debian**: `postinst` enables and starts the unit (`deb-systemd-helper`/`systemctl enable --now`, tolerant of no systemd); new `prerm` stops/disables on remove. Control description updated.
- **Unsigned binaries**: still no Authenticode; keep the SmartScreen note for both `.exe` and `.msi`.

## Compatibility / non-goals

- Preserve Docker Compose workers, V1 env-token enrollment, existing `WORKER_CREDENTIAL_FILE` users, service/unit/config/state identifiers on Linux (`docker-volume-backup-worker.service`, `/etc/docker-volume-backup`, `/var/lib/docker-volume-backup`, user `dvb-worker`).
- No auto-update, no package repository, no automatic installation of `restic`/`tar`/etc.
- No push, PR, merge, or release without separate user authorization. Local work-unit commits are used.
- Preserve unrelated `.atl/`, tracked `__pycache__`, `.codegraph/`, `.playwright-mcp/` changes.

## Verification

- TDD per unit with stdlib unittest; pywin32 and `systemctl` are mocked in tests. Full suite: `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_*.py'`; `git diff --check`.
- Windows service runtime and MSI behavior are proven in the release workflow smoke (install/running/uninstall); local admin-level service installation is not run here.

## Tasks

### NWS-1 — State dir, persisted enrollment config, idle-until-enrolled daemon

- Status: done.
- Evidence: `python -m unittest tests.test_worker_state_paths tests.test_hmac_auth tests.test_docker_runtime_safety tests.test_worker_cli tests.test_worker_enrollment_cli` — 102 tests passed, 1 skipped; full suite passed 552 tests, 2 skipped. Follow-up review hardening: managed Windows state files are owned by Administrators after atomic replacement; enrollment files are checked before adoption. Focused follow-up suite passed 67 tests, 1 skipped; full suite passed 651 tests, 2 skipped.
- Commit: `6e66e50 feat(worker): idle until enrolled and persist enrollment config`.
- Scope: `src/worker_agent/cli.py` (`enroll --control-plane-url/--ca-file/--worker-name`, state dir resolution, `worker.json`), `src/worker_agent/main.py` (config fallback, per-loop credential/worker_id resolution, idle wait, stop event), `src/worker_agent/infrastructure/security/credential_store.py` (SID grants, root chown), tests.
- Acceptance: daemon with no credential idles and reports `not_ready`; credential appearing later is picked up without restart; `test_startup_retry_cap` still covers real failures; enroll persists URL and the daemon uses it when env is absent.

### NWS-2 — Windows SCM service and `service` subcommand

- Status: done.
- Evidence: mocked pywin32 tests cover registration, lifecycle, status, stop checkpoints, failure reporting and rotating service logs. The worker continues retrying Control Plane failures after initial registration. Focused verification passed; full suite after final hardening passed 651 tests, 2 skipped.
- Commit: `e318f27 feat(worker): host the native worker as a Windows service`.
- Scope: `src/worker_agent/windows_service.py`, `service` subcommand in `cli.py`, spec hidden imports, `requirements.txt` marker, tests with mocked pywin32/systemctl.
- Acceptance: `service install` registers `vaultline-worker.exe service run` as auto-start LocalSystem service; `service run` hosts the worker loop and stops via the stop event; Linux `service status` maps to `systemctl`.

### NWS-3 — MSI build, release assets, Debian service start, Control Plane allowlist

- Status: done.
- Evidence: WXS parses as XML; release workflow YAML parses; Git Bash syntax checks passed for the Debian builder/postinst/prerm. MSI XML tests assert protected DACL SDDL `D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)` on state and log folders. Full tests pass. WiX/MSI build/install not run locally; Windows release workflow contains the install/start/uninstall smoke.
- Commit: `c59469b feat(packaging): ship Windows MSI and auto-start native services`.
- Scope: `packaging/windows/vaultline-worker.wxs`, `release-dispatch.yml` (`native-windows` builds exe + msi, smoke install, publish lists `.msi`), `packaging/debian/postinst` + `prerm` + `control.in`, `src/control_plane/main.py` allowlist, tests.
- Acceptance: workflow YAML parses; required-asset set includes the `.msi`; `postinst` enables/starts the unit; API returns `windows-amd64-msi` for the latest release.

### NWS-4 — Docs and enrollment UI

- Status: done.
- Evidence: UI and docs teach install → service waits → one `enroll` → status for MSI and .deb; portable executable retained as secondary diagnostic path. UI inline JavaScript `node --check` passed; focused docs/UI tests passed; full suite passed 619 tests, 2 skipped before security follow-up tests.
- Commit: `bcf40ec docs(worker): teach the install-then-enroll native worker flow`.

### NWS-5 — Independent-review hardening

- Status: done.
- Scope: protected the MSI state/log DACLs from inherited write permissions using WiX core `PermissionEx`; set managed enrolled file owner to Administrators after atomic replace; kept standard-user owner rejection; cap startup retries only before first registration; report nonzero SCM exit on unexpected failure; report stop-pending checkpoints during graceful shutdown; improve unenrolled `daemon --once` message.
- Evidence: focused suite passed 72 tests, 1 skipped after ACL owner fix; full suite passed 651 tests, 2 skipped; final parent spot-check passed 72 tests, 1 skipped; independent read-only review confirmed expected owner transition and DACL declarations. Real WiX build, MSI install, live SCM and real Windows ACL enforcement remain unverified locally.
- Changed files: `packaging/windows/vaultline-worker.wxs`, `src/worker_agent/infrastructure/security/credential_store.py`, `src/worker_agent/state_paths.py`, `src/worker_agent/main.py`, `src/worker_agent/windows_service.py`, `src/worker_agent/cli.py`, `.github/workflows/release-dispatch.yml`, related tests.
- Scope: `doc/control-plane-quickstart.md`, `README.md`, `src/control_plane/ui/index.html` native tab (MSI first, portable exe secondary, service-based commands), tests.
- Acceptance: instructions are install → enroll → service already running, for both platforms; no Task Scheduler guidance remains as the primary path.

## Next step

No implementation tasks remain. The feature commits and hardening commit are local only until a release target is confirmed; no push or release has been performed. Do not publish to a ref that does not contain the requested worker commits.
