# Backup Failure Email Clarity Release
Objective: Clarify failed-backup email reports and deliver the change in a stable patch release.
Branch: `fix/backup-failure-email-clarity` | Delivery: commit only scoped paths, fast-forward master, push, wait for green CI, publish patch release; preserve all pre-existing changes.

## Specs
- S1: “Mira, mejora el correo, no se sabe a simple vista que falló y en cual servidor o host C:\Users\DANIEL~1.RON\AppData\Local\Temp\orca-paste-1792604118769-497d5f82-f907-43c1-bae8-171badbf747d.png”
- S2: “INTEGRA Y GENERA RELEASE”

## Tasks
- T1 — [S1] Highlight the failure reason, target, and worker host in SMTP reports; keep webhook payloads unchanged; add focused tests and operator-guide notes; route: parent; status: done; Risk: item 3 (shared SMTP preview template); focused tests: 35 tests, 19 subtests; commit: `2645434`.
- T2 — [S1-S2] Commit the scoped implementation and tests on the feature branch; integrate by fast-forwarding master; route: parent; status: in progress; commit: `2645434`.
- T3 — [S2] Push the integrated change, require green remote CI, dispatch the stable patch release, and verify the tag and assets; route: parent; status: pending; Risk: item 5 (remote delivery and publication); commit: pending.
- T4 — [S2] Record delivery evidence and close the feature without including unrelated worktree changes; route: parent; status: pending; commit: pending.

## Log
- L1 — Original request (verbatim): “Mira, mejora el correo, no se sabe a simple vista que falló y en cual servidor o host C:\Users\DANIEL~1.RON\AppData\Local\Temp\orca-paste-1792604118769-497d5f82-f907-43c1-bae8-171badbf747d.png”
- L2 — Delivery instruction (verbatim): “INTEGRA Y GENERA RELEASE”
- L3 — The SMTP report now puts the failure reason first and shows the resolved target name and worker host; the email-only display context is not added to webhook JSON. Focused notification tests passed: 35 tests and 19 subtests. The current master HEAD is 072637ffb3b88f5ece5597fcab73def05f3b9d4d and matches origin/master. Fetching tags found the published stable 3.15.8 tag; this bug-fix release therefore uses the workflow's next stable patch, 3.15.9. Existing unrelated dirty files are excluded.
- L4 — Commit `2645434` (`fix(notifications): clarify failure email context`) records only the SMTP service, notification delivery test, operator guide, and this release task file. The feature branch was created from the current master; pre-existing unrelated edits were not staged.
