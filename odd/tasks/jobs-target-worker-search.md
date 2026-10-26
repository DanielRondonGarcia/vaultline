# Readable Jobs target and worker search
Objective: display target and worker names together and search the complete paginated Jobs history by either name.
Branch: feat/jobs-target-worker-search; delivery: feature-branch-chain, local commits only; runner: .venv/Scripts/python.exe -B -m unittest.

## Specs
S1. "para que en target salga el nombre y no el ID, pq es dificil de identificar así y tambein agrega el worker"
S2. "puedes quedar tipo, 1 sola columna que tenga worker y target para optimizar la visualización, y de primer y mas grande salga el nombre lejible del target y debajitoun poco mas opaco y pequeño, salga el worker name."
S3. "Y el filtro sea string sin select list, para poder buscar alli por worker o target"
S4. "C:\Users\DANIEL~1.RON\AppData\Local\Temp\orca-paste-1793027812688-62052298-26dd-4846-bc61-eb89fc1b1bfb.png"

## Tasks
T1 | S1,S3 | inline | done | Optional server-side name/ID search, page names and exact target_id compatibility across both repositories; RED 3 failures, GREEN 61 tests; commit: 41ddcec.
T2 | S1-S4 | inline | done | Stacked target/worker names and text filter, server-side query; RED 2 UI tests, GREEN 103 UI tests, existing origin/status/date filters preserved; commit: UI work-unit commit.
T3 | S1-S4 | inline, independent verify for Risk item 3 | in_progress | Run focused/full tests, differential check and verify UI; close evidence; commit: pending.

## Log
L1. Original request verbatim: ven, mejora aqui la tabla de jobs, para que en target salga el nombre y no el ID, pq es dificil de identificar así y tambein agrega el worker, puedes quedar tipo, 1 sola columna que tenga worker y target para optimizar la visualización, y de primer y mas grande salga el nombre lejible del target y debajitoun poco mas opaco y pequeño, salga el worker name. Y el filtro sea string sin select list, para poder buscar alli por worker o target

C:\Users\DANIEL~1.RON\AppData\Local\Temp\orca-paste-1793027812688-62052298-26dd-4846-bc61-eb89fc1b1bfb.png
L2. Jobs list is server-paginated. The current target dropdown sends exact target_id and list projections carry only IDs; a client-side search would miss jobs on other pages. Implement an optional name/ID substring search before pagination, preserving the exact target_id filter for existing API users. Names are resolved from current target/worker records; missing/deleted names fall back to IDs. No migration, remote deployment, push, or release authorized. Preserve unrelated dirty worktree. Risk: item 3 (shared listing parser/filter, existing API consumers). Forecast 250-400 authored changed lines over backend and UI commits. No independent behavior units justify writer delegation.
L3. T1: test-first RED 3 failures (two repositories and route). GREEN 61 backup-observability tests; optional target_worker filter matches name/ID before pagination, literal wildcard and overlong input cases, keeps exact target_id behavior. Jobs page projection returns current target/worker names with ID fallback; API contract documented. Scoped diff check passes. Commit 41ddcec.
L4. T2: UI tests RED 2 then GREEN 103. A malformed targeted edit on the previously clean UI test file was restored from HEAD before the valid test-first change, without affecting unrelated work. One combined Target / Worker column uses accessible hierarchy (13px bold target, 11px muted worker), escaped names from the listing API with ID fallback; a 300ms debounced text input sends target_worker before server pagination. Existing command/origin/status/date options remain. Node rendering/query test and UI documentation updated. No deployment or browser runtime tested yet.
