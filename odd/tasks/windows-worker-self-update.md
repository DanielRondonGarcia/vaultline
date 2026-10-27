# On-demand Windows Worker Self-update
Outcome: Add an explicit `vaultline-worker update` command that fetches the latest stable Windows MSI from GitHub Releases, verifies its SHA256SUMS entry, and invokes the MSI upgrade; publish stable release 3.15.16 for testing.
Scope: Windows MSI path for the current Server 2022 test; no background updater and no Linux package-manager changes.

## Specs
1. S1: “agregar funcionalidad al cli de vaultline de auto update”; clarification: “pues de auto me refiero a el mismo, no que lo haga solo. y que se pueda por comando” — the worker updates itself only when the user invokes the CLI command; do not add a background updater.
2. S2: “Checksum y GitHub Releases” — verify the downloaded MSI against the SHA256SUMS asset from the same stable GitHub release before invoking installation.
3. S3: “genera un release para probar” — create a release after implementing and verifying the command and the Restic/rclone fix.

## Tasks
- T1 [done] | S1-S2 | Parent inline: add `vaultline-worker update` for the latest stable Windows MSI, with strict release asset selection and SHA256SUMS verification | commit: `2db8e41`
- T2 [done] | S1-S2 | Parent inline: hand off MSI execution safely, preserve the existing per-machine MajorUpgrade/service lifecycle, and update CLI help/docs/tests | commit: `2db8e41`; focused suite 82 passed, 21 subtests; full-suite runtime mismatch is recorded in L9 and user accepted release with that gap
- T3 [done] | S1-S3 | Parent inline: dispatch stable release and confirm workflow/assets; no verifier retry | commit: release tag `3.15.16` (peels to `ab0ec327`; workflow `40600335772`)
- T4 [blocked] | S1-S3 | User: install the first MSI manually on Server 2022 and report the Restic/rclone result; actual self-update installation requires a later release | commit: N/A; waiting for user runtime evidence

## Log
- L1: no, si ya terminaste, genera un release para probar. Pero antes, si me leiste lo que te dije antes? agregar funcionalidad al cli de vaultline de auto update
- L2: pues de auto me refiero a el mismo, no que lo haga solo. y que se pueda por comando
- L3: User-selected trust policy: “Checksum y GitHub Releases”.
- L4: Existing CLI parser uses `prog="vaultline-worker"`; Windows MSI is per-machine, uses MajorUpgrade, and controls the VaultlineWorker service during upgrade. Current packages are unsigned; the selected same-release checksum policy detects corruption but does not authenticate a compromised release channel.
- L5: Implementation is scoped to Windows MSI for the Server 2022 validation in this request. Linux package-manager updates remain unchanged. Since the first release containing the command does not yet exist, installing that first MSI still requires an initial manual upgrade; later releases can use the command.
- L6: The Restic/rclone fix is already in local commit `eff40e6`; the requested release must include it with the on-demand updater.
- L7: Started T1 inline after mapping the existing argparse CLI, GitHub release assets, and MSI MajorUpgrade/service lifecycle.
- L8: Commit `2db8e41` contains the updater, docs, and tests as one feature slice (649 changed lines). No further code commit is planned until the release proof/decision blocker is resolved.
- L9: Focused command: `python -m pytest tests/test_worker_cli.py tests/test_worker_update.py tests/test_worker_packaging.py tests/test_release_asset_workflow.py -q` — 82 passed, 21 subtests. Full suite: 1105 passed, 6 skipped, 2 failed, 373 subtests; the failures are runtime-pin tests because this host runs CPython 3.14.5 while `.python-version` requires 3.11.17.
- L10: The independent `gentle-ai-verify` task timed out after 30 minutes because its tool reported no API key; per policy it was not retried. Pre-commit `gentle_review assess` was unassessable because the feature files were untracked then. No release or push occurred at that point.
- L11: User authorization: “na, autorizo publicarlo, yo lo pruebo de una”. Proceed with release publication despite the documented local full-suite runtime mismatch and unavailable independent verifier; user will test the installed build.
- L12: Pushed `feat/jobs-target-worker-search` to `origin` at `ab0ec32` so GitHub Actions can build from the requested feature branch. No PR or merge was created; release workflow dispatch and asset confirmation remain.
- L13: Dispatched stable patch release workflow run `40600335772` from branch `feat/jobs-target-worker-search` at `ab0ec32`; latest stable was `3.15.15`, and no `3.15.16` tag existed at preflight.
- L14: Run `40600335772` completed successfully on `feat/jobs-target-worker-search`, commit `ab0ec327bba267e9f151d63391f829f2a2ad834e`. Stable release `3.15.16` was published 2026-10-27; the tag peels to `ab0ec32`. The five assets are amd64 `.deb`, arm64 `.deb`, Windows x64 `.exe`, Windows x64 `.msi`, and `SHA256SUMS`. The downloaded checksum manifest entries match GitHub API asset digests for all four binaries. No PR or merge was created.
- L15: The user will test on Server 2022. The first MSI containing `vaultline-worker update` must still be installed manually because earlier binaries lack the command; since `3.15.16` is latest, an actual self-update installation path needs a later release. No live Server 2022 installation or Restic run was performed by the agent.
- L16: Release completed with non-blocking runner annotations for Node.js 20 deprecation and the upcoming `ubuntu-latest` migration; no workflow job failed.
- L17: Closing delivery boundary: source/tests/docs remain in code commit `2db8e41`; stable release tag `3.15.16` targets `ab0ec32`. The next local work-unit commit contains only the two ODD task-record updates; no additional source changes or push are part of that slice.
