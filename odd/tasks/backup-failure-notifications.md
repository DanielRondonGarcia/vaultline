# Backup Failure Notifications
Objective: Add administrator-configurable webhook and SMTP notifications for failed backups, with one best-effort attempt and a safe summary.
Branch: `feature/audit-reports` | Delivery: `ask-on-risk` (forecast ~700 authored changed lines; no push or PR authorized).
Tests: `python -m pytest`; do not rerun the full suite without explicit authorization.

## Specs
- S1 — User request: “Ahora necesito que implementes un apartado de notificaciones, que se peuda configurar varios, webhook, mail, etc. y envia los datos del backup fallido”
- S2 — Selected channels: “Webhook y correo SMTP”
- S3 — Selected payload: “Resumen seguro (recomendado)”
- S4 — Selected webhook reachability: “Permitir redes internas”
- S5 — Selected delivery behavior: “Un intento, mejor esfuerzo”

## Tasks
- T1 — [S1-S2] Add notification channel records, persistence, encrypted credential handling, admin CRUD API, and focused tests; route: inline; status: in_progress (implementation verified; commit recording); RED: 3 focused tests fail; models/repositories/service/admin API implemented; focused notification+SQLite+dispatch suites pass (97 tests, 48 subtests); owner approved one ~637-line local commit; commit: pending.
- T2 — [S1-S5] Implement webhook and SMTP senders; emit a bounded safe summary for failed `backup.run` jobs only, allow configured internal destinations, make one non-blocking attempt, and deduplicate repeated job/channel events; route: inline; status: pending; commit: pending.
- T3 — [S1-S2] Add admin UI for multiple channel configurations and user-facing setup documentation; route: inline; status: pending; commit: pending.
- T4 — [S1-S5] Run focused verification, independent security verification, and native assessment; record work-unit commit evidence and close the feature; route: inline plus verifier after assessment; status: pending; commit: pending.

## Log
- L1 — Original request (verbatim): “Ahora necesito que implementes un apartado de notificaciones, que se peuda configurar varios, webhook, mail, etc. y envia los datos del backup fallido”
- L2 — 2026-10-17 owner selections (verbatim): “Webhook y correo SMTP”; “Resumen seguro (recomendado)”; “Permitir redes internas”; “Un intento, mejor esfuerzo”.
- L3 — Exploration: backup jobs use `backup.run` (`src/control_plane/application/services/control_plane_service.py:1297`). Worker failures are finalized in `update_job_status` and lease expiry is reconciled through `_reconcile_expired_jobs`; repository reconciliation can republish an already-failed job. Reuse `_safe_result_summary` and `_redact_job_text` (`control_plane_service.py:3091,3150`) and exclude raw logs. A persistent per-job/per-channel attempt marker is needed to suppress duplicate notifications; delivery failures must not change backup status or block the worker response.
- L4 — Existing integration points: Settings API and UI are admin-only; `SecretCodec` encrypts values before persistence. No notification delivery channels currently exist. Place channel management in the existing admin Settings view unless implementation evidence requires a separate route.
- L5 — Risk: item 2 (security), due to outbound webhook requests, private-network destinations, and SMTP credentials. Initial and post-document Engram saves failed with `unable to open database file`; this repository file is authoritative for now, and the full Engram mirror remains pending.
- L6 — Delivery forecast and constraint: approximately 700 authored changed lines across the feature; retain `ask-on-risk` and ask before a commit if the running authored count would exceed the review threshold. Work-unit commits are local only; no push or PR is authorized.
- L7 — Test-first evidence for T1: `python -m pytest -q tests/test_notification_channels.py` returned exit 1 before implementation with three expected failures: channel creation is absent, unsupported types are not rejected by a channel API, and `/api/v1/notification-channels` does not enforce admin authorization.
- L8 — T1 implementation in progress: added the encrypted `NotificationChannelRecord`, repository port, in-memory and SQLite implementations, service CRUD/validation/public-view methods, and admin-only collection/item API wiring. Engram remains unavailable (`unable to open database file`), so its full-document mirror is pending.
- L9 — Initial post-implementation focused run returned 1 failed, 2 passed: the SMTP test fixture supplied a password without a username, which validation correctly rejects. Added a username to the fixture.
- L10 — Second focused run returned 1 failed, 2 passed: the confidentiality test's JSON helper attempted to serialize datetime via `__dict__`. Corrected the test to serialize record dictionaries with string timestamps and added SQLite restart/delete coverage.
- L11 — `python -m pytest -q tests/test_notification_channels.py` passed (5 tests). Coverage verifies encrypted webhook/SMTP credentials, non-leaking public views, SQLite persistence/deletion, invalid-type rejection without saving, and non-admin GET/POST rejection.
- L12 — Added an authorized API-route test covering list/create/update/delete with admin authentication. The focused T1 suite passed: 6 tests; only existing `datetime.utcnow()` deprecation warnings were emitted. T1 implementation is complete; local work-unit commit evidence remains pending.
- L13 — Broader focused verification passed: `python -m pytest -q tests/test_notification_channels.py tests/test_sqlite_runtime.py tests/test_control_plane_dispatch.py` — 97 passed, 48 subtests. Scoped `git diff --check` passed. The T1 changed paths total approximately 637 authored lines (371 tracked source additions plus the 235-line focused test and 31-line task document); per L6, paused before committing.
- L14 — Owner selected “One larger T1 commit” for the ~637-line local work unit. This authorizes only that local commit; no push or PR.
