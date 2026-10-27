# Windows Native Restic/rclone Failure
Outcome: Frozen Windows worker Restic commands use an explicit absolute path to the packaged rclone for rclone-backed repositories.
Scope: Preserve source-mode and non-rclone behavior; do not publish artifacts.

## Specs
1. S1: “y tengo este bug, acabé de instalarlo en un Microsoft Windows Server 2022 Standard 10.0.20348 20348 - C:\Users\DANIEL~1.RON\AppData\Local\Temp\orca-paste-1793137812864-5ce8435b-4088-4ad1-9e1e-7c00fe077876.png y vea, falla” — address the Restic initialization failure shown in the screenshot, where Restic rejects the implicit relative `rclone.exe` executable and directs callers to set `-o rclone.program`.

## Tasks
- T1 | S1 | Parent inline: add regression coverage and explicitly configure the packaged rclone path for frozen rclone-backed Restic commands | status: done (4 focused tests pass) | commit: pending
- T2 | S1 | Parent + gentle-ai-verify: run focused and relevant suites; independently inspect shared Restic command behavior | status: done (213 suite tests pass; independent S1 verdict: met) | commit: pending

## Log
- L1: y tengo este bug, acabé de instalarlo en un Microsoft Windows Server 2022 Standard 10.0.20348 20348 - C:\Users\DANIEL~1.RON\AppData\Local\Temp\orca-paste-1793137812864-5ce8435b-4088-4ad1-9e1e-7c00fe077876.png y vea, falla
- L2: Screenshot log: `Failed to initialize restic repository: Command '['C:\\Program Files\\Vaultline\\restic.exe', 'init']' returned non-zero exit status 1.`; `cannot implicitly run relative executable rclone.exe found in current directory, use -o rclone.program=./<program> to override` (repository redacted).
- L3: Code map: `src/restic_executable.py:19-43` prepends the packaged rclone directory to PATH when frozen; Restic documentation specifies `-o rclone.program` as the executable-path override. The screenshot proves implicit lookup still fails; exact current-directory lookup mechanics remain an inference.
- L4: Explorer mapped the worker backup, restore, metadata, snapshot, and policy-validated runtime command paths; no command path currently adds `-o rclone.program`.
- L5: Test-first evidence: the focused helper test failed before implementation because `restic_command_with_backend_options` was absent. After the helper and call-site wiring, helper tests passed (2), native runtime policy-path test passed (1), and Restic backup/init/backup/forget integration test passed (1).
- L6: Relevant suites: `python -m pytest tests/test_backup_observability.py tests/test_native_restore.py tests/test_native_tool_executable.py tests/test_worker_packaging.py tests/test_snapshot_browser.py tests/test_control_plane_dispatch.py -q` → 213 passed, 1 skipped (the Windows-inapplicable POSIX executable-bit check), 122 subtests passed; 1,412 pre-existing datetime deprecation warnings. `git diff --check` emitted no whitespace errors.
- L7: `gentle_review assess` could not create a candidate because the new feature task file is untracked and needs an explicit intended-untracked selection; the report has `rddLine: off`. No native review was started.
- L8: Independent verifier returned S1 met with no blockers: all Restic call paths, option order, fail-closed path resolution, policy-before-injection, and source/non-rclone behavior checked. Its additional scratch boundary probe passed 12/12; the verifier made no repository changes.
