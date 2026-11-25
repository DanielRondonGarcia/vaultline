# Job log lifecycle
Branch: `feat/job-log-lifecycle` · Delivery: ask-on-risk (forecast >400 authored lines; separate storage and policy slices).
Checks: local Python pytest, public API/log viewer, isolated SQLite, and independent verification. No production rollout.

## Specs
- S1: “es posible implementar algun tipado de compresión de esos logs?”; “solo si se da en ver log en jobs mas viejo de 1 mes para atras, pues inicia la descompresión rapida y ver lo util”; “si, para la compresión sería usar Brotli, no? es la mejor para texto plano.”
- S2: “los ultimos 3 meses se mantienen, de resto se va eliminando”.
- S3: “y eso de cuota, agregarle configuración en la parte de settings global”; “con alerta y con borrado, porque es cuota”.
- S4: “es mejor ahcer el prune al principio, que se pueda seleccionar facilemtne una fecha hasta para limpair todo esos logs”.
- S5: “y ahi en adeltante pues si ira comprimiendo logs de 1 mes para atras”; “siempre si o si la compresion automatica, lo de la cuota y el prune configurable”.

## Tasks
- T1 · S1 · parent RED + Context backstop writer, storage and read path · done · `094fd30` · Risk: item 1 (archives existing log data).
- T2 · S1–S3 · Context backstop writer, runtime policy and Global Settings (depends on T1) · done · `fda42d1` · Risk: item 1 (automated deletion of retained logs).
- T3 · S4–S5 · Context backstop writer, cutoff prune and independent archive/retention controls · in progress · commit pending · Risk: item 1 (irreversible historical log removal).

## Log
- L1: mmm toca analizar como optimizar esa información, es posible implementar algun tipado de compresión de esos logs? que supongo que los logs son los que mas pesan y no es necesario verlo directamente, solo si se da en ver log en jobs mas viejo de 1 mes para atras, pues inicia la descompresión rapida y ver lo util, tambien funcionalidad de poner una cuota, para que se ponga por ejemplo, los ultimos 3 meses se mantienen, de resto se va eliminando
- L2: si, para la compresión sería usar Brotli, no? es la mejor para texto plano.

  y eso de cuota, agregarle configuración en la parte de settings global
- L3: con alerta y con borrado, porque es cuota
- Decision: quota breaches warn and evict oldest eligible logs, even before the age cutoff. This never deletes job metadata, result summaries, snapshots, or backups. Age is measured from completion. Settings default to 30/90 days when enabled; opt in to automated maintenance on upgraded installations. Synthetic lease-expiry jobs are excluded while they can still accept late reports. No production writes or automatic VACUUM.
- T1 evidence: parent observed missing-method RED, then 80 focused tests and 77 subtests passed; independent verifier passed isolated boundary, compare-and-swap, reader compatibility, corrupt archive, and rollback probes. Previous releases cannot read archived logs without restoring the raw content. Engram mirror pending (service unavailable).
- T2 first pass: writer's 315 focused tests and differential passed. Independent verifier confirmed lifecycle and HTTP settings but blocked deployment: candidate selection scanned wide jobs rows and read overflow pages while holding the SQLite coordinator (and, for deletion, a write transaction). Correction: move eligibility and ordering onto compact indexed metadata; preserve atomic policy checks. Full suite (local Python 3.12): 1,607 passed, 40 skipped, 3 failed; the same three failures reproduced on base `4d2bb18`.
- T2 final checks: independent reverification confirmed compact index selection (about 1 ms at 682 MB, no wide-table scan), guarded removal, and quota stop at or below the limit. A cleared/disabled quota blocks new eviction batches. Focused suite: 324 passed and 176 subtests passed. Final full suite: 1,616 passed, 40 skipped, same 3 base failures (worker relay stderr and 2 pinned Python checks); the intermediate quota fixture tie was fixed and the ownership-order failure did not recur. No production test or rollout.
- Local slice boundaries: `094fd30` is the archival format; `fda42d1` is the runtime policy and its settings/test coverage. T2 exceeds the advisory size because configuration, migration, bounded writes, visibility and behavioral tests form one deployable policy. No push/PR planned.
- L4: no, pero quiero una actualización inteligente, precisamente para estos casos donde está consumiendo demasiado espacio en disco, y entonces pues como la compresión es muy pesado, es mejor ahcer el prune al principio, que se pueda seleccionar facilemtne una fecha hasta para limpair todo esos logs y ahi en adeltante pues si ira comprimiendo logs de 1 mes para atras
- L5: siempre si o si la compresion automatica, lo de la cuota y el prune configurable
- Amendment implementation decisions: automatic Brotli at 30 days is active even on old/default-disabled configurations, but this never implicitly authorizes deletion; the existing maintenance opt-in remains the retention toggle and the optional quota becomes independent, preserving existing opt-ins and dormant quota safety. A confirmed admin cutoff prune runs as a resumable bounded task with priority over archive and recurring deletion. Preserve job rows/result summaries and protect active/late-report jobs. No production action or automatic VACUUM.
- T3 evidence: writer observed RED and then 353 focused tests + 220 subtests passed. Independent verifier reproduced a cursor gap for late-indexed legacy rows, and the correction was verified with 56/56 pruned across two processes after indexing. Exact UTC midnight stays outside the cutoff; refreshed UI sees pending state through the real HTTP payload and Node harness. Final full suite: 1,645 passed, 40 skipped, the same three baseline failures; parent focused spot-check hit the 30s harness limit and was not retried. `git diff --check` passed. T3 is one deployable policy slice over the advisory review budget (separate from the already committed archive and recurring quota units).
