# Snapshot read unconfirmed recovery

Expired snapshot reads must stop blocking retries and their outcome must show as unconfirmed.

## Specs

- S1: "si porfa" (approved: "que los trabajos de lectura con lease vencido dejen de bloquear nuevas consultas y muestren claramente que su resultado no fue confirmado, sin cambiar la protección de operaciones que modifican datos")
- S2: "si, si era eso" (canceled stuck job 37683e66 unblocked retry; user: "debe de cerrarse ese job de una, veo ahí un bug")

## Tasks

- T1 [S1] DONE: Expired read-only snapshot jobs flip to unconfirmed on first reconcile (SQLite + in-memory); route inline; commit pending.
- T2 [S1] DONE: Late terminal snapshot reports recorded without overwriting catalog; route inline; commit pending.
- T3 [S1] DONE: UI rejects unconfirmed snapshot results with snapshot-result-unconfirmed; route inline; commit pending.
- T4 [S1, S2] DONE: Independent verifier PASS, incl. baseline comparison; pre-existing failures unrelated to this diff.

## Log

- L1: "si, si era eso, ... pero pilas, pq si fallí, debe de cerrarse ese job de una, veo ahí un bug"
- L2: "si porfa" (approved the proposed read-only fix)
- L3: User machine shutdown voided the first verifier launch; verifier relaunched after user instruction and returned PASS.
- L4: Verifier advisory applied: snapshot.search added to READ_ONLY_SNAPSHOT_COMMANDS and its test case.
- L5: Verifier advisories accepted as designed: renewal of a still-alive read after expiry fails and the late report remains the recovery path; fail-open deadline for read-only rows with hand-edited summaries.
