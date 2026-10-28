Objective: Deliver a Portainer-style, bounded, usable live job-log viewer in both existing Vaultline log panels.
Branch: `feat/portainer-style-job-logs` (based on `67286fd`); delivery strategy: `chain_strategy=feature-branch-chain`; forecast: about 900 authored changed lines across worker, Control Plane, UI, and tests.
Test runner: `python -m pytest` on focused worker, Control Plane, and UI tests, then applicable suite checks.

## Specs

### S1 — Original request
> ven, si veo importante mejorar la parte de los logs, no deja ni hacer scroll pq se va actualizando, al tiempo queda penando mucho en pantalla, entonces si toca limitar los ultimos 1k lineas por ejemplo C:\Users\DANIEL~1.RON\AppData\Local\Temp\orca-paste-1793214785458-78daa946-2aa7-456c-8974-9d98b1298804.png , pq vea, no me deja subir para ver desde donde falla, tampoco deja copiar comodamente, te muestro un ejemplo de como portainer hace ese manejo, que me parece super,sería adopartlo pero aca para vaultline C:\Users\DANIEL~1.RON\AppData\Local\Temp\orca-paste-1793214826269-613a551f-1120-45f0-a6a5-359038601ee5.png

Acceptance checks: live refresh must not force a user who scrolled upward back to the bottom; the visible log history remains bounded to the latest 1,000 lines by default; users can inspect and copy logs while updates continue.

### S2 — Chosen scope
The user selected the exact option label: `Estilo Portainer`.

The selected option was described as: `Lo esencial más búsqueda, marcas de tiempo, ajuste de líneas y controles de visualización/copia similares a la captura.`

The user then selected the exact scope expansion: `Add end-to-end timestamps` (option description: `Expand worker and Control Plane log payload/storage so the timestamp control can work.`).

Acceptance checks: both existing job-log panels expose working search, timestamp display control, adjustable visible line count, display/wrap control, and copy/download actions alongside a live-refresh pause/resume control. Capture real per-line source timestamps through the worker-to-Control-Plane log flow and display them when enabled; do not synthesize timestamps. Assumption to validate: preserve the existing string-log contract and add timestamp metadata compatibly unless the data-flow map demonstrates a safer design. The server already caps each response at 1,000 lines; do not add a redundant backend cap unless evidence shows the existing one is ineffective.

## Tasks

- T1 | S1-S2 | delegated to `gentle-ai-worker` | in progress: UI controls implemented; integrate timestamps after T3 | commit: TBD
- T2 | S2 | delegated to `gentle-ai-worker` | pending: capture Docker source timestamps and preserve them through worker batching, journal, and HTTP | separate commit (worker progress/journal risk)
- T3 | S2 | delegated to `gentle-ai-worker` | pending: persist optional timestamp metadata and expose it through Control Plane job views/events | separate commit (SQLite migration risk)
- T4 | S1-S2 | inline | pending: enable timestamp rendering in both panels and verify copy/download behavior | commit: TBD
- T5 | S1-S2 | inline | pending: run integrated checks and close out the feature | commit: TBD

## Log

- L1 — User request (verbatim): `ven, si veo importante mejorar la parte de los logs, no deja ni hacer scroll pq se va actualizando, al tiempo queda penando mucho en pantalla, entonces si toca limitar los ultimos 1k lineas por ejemplo C:\Users\DANIEL~1.RON\AppData\Local\Temp\orca-paste-1793214785458-78daa946-2aa7-456c-8974-9d98b1298804.png , pq vea, no me deja subir para ver desde donde falla, tampoco deja copiar comodamente, te muestro un ejemplo de como portainer hace ese manejo, que me parece super,sería adopartlo pero aca para vaultline C:\Users\DANIEL~1.RON\AppData\Local\Temp\orca-paste-1793214826269-613a551f-1120-45f0-a6a5-359038601ee5.png`
- L2 — User scope choice (verbatim): `Estilo Portainer`.
- L3 — Read-only exploration evidence: `src/control_plane/application/services/control_plane_service.py:119-122` defines `MAX_JOB_LOG_LINES=1000` and `MAX_JOB_LOG_CHARS=512KiB`; `:4049-4052` bounds the log tail. `src/control_plane/ui/index.html:6017-6021` and `:7038-7054` rebuild the log contents and unconditionally scroll to the bottom on render. `src/control_plane/ui/styles/components.css:1033-1063` defines the existing bounded scroll containers. Existing UI tests are in `tests/test_ui_states.py:910-1015` and `:1296-1653`.
- L4 — Isolation decision: implementation will use this feature worktree/branch based on `67286fd`; unrelated dirty files in the primary worktree are out of scope and must not enter feature commits.
- L5 — T1 started: implement with test-first checks in the allowed UI/test surfaces; verify timestamp source data before rendering timestamps. Then run T2 checks and record any gaps.
- L6 — Engram mirror attempt failed with `unable to open database file (14)`. The local feature document remains authoritative; mirror status is pending until the memory service is available, and safe implementation may proceed.
- L7 — Worker delivered the UI controls in both panels and reported `python -m pytest tests/test_ui_states.py`: RED (6 failed, 106 passed), then GREEN (112 passed); inline-script `node --check` passed. Worker evidence found no per-line source timestamps (only plain log strings and a job-level start time), so the timestamp control is disabled rather than fabricated. Worker stayed within the three allowed UI/test surfaces. The timestamp gap remains open pending the user's scope decision.
- L8 — User scope decision (verbatim): `Add end-to-end timestamps`. The user authorized expanding into worker and Control Plane log payload/storage so the timestamp control can work.
- L9 — Read-only data-flow map: Docker streaming currently calls `container.logs(... timestamps=False ...)` at `src/worker_agent/infrastructure/adapters/docker_runtime.py:829`; worker progress/journal and Control Plane currently require `List[str]` (`worker_agent_service.py:349`, `control_plane_service.py:2076-2114`, `:4030-4052`). SQLite stores only `log_lines_json` (`src/control_plane/infrastructure/repositories/sqlite.py:262`), while `_job_public_view` publishes the job view to API/SSE (`control_plane_service.py:4316-4341`). Docker source timestamps are available; native and several non-streamed paths have no source timestamps. An optional parallel timestamp field is the recommended backward-compatible direction, but its persistence/line alignment and journal replay need tests.
- L10 — User delivery-strategy choice (verbatim): `Feature-branch chain (recommended)`. This selects future PR slicing only; it does not authorize pushing or creating PRs.
