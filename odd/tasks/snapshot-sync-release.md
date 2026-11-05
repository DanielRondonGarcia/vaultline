# Snapshot sync release

Isolate the snapshot sync fix and publish a stable patch release.

## Specs

- S1: "pues si, hacer ese ajsute y mejorar los ligs, en logs dle contenedor sesa claro que está pasando"
- S2: "dale, pues integrarlo y genera release para ir a ver"
- S3: "Estable 4.0.1"

## Tasks

- T1 [S1] DONE: Isolate the existing fix on the stable base; route inline; commit 995aa1b.
- T2 [S1, S2] IN PROGRESS: Verify and integrate the fix; route inline; commit 995aa1b.
- T3 [S2, S3] Publish and verify the stable release; route workflow; commit pending.

## Log

- L1: "dale, pues integrarlo y genera release para ir a ver"
- L2: "pues si, hacer ese ajsute y mejorar los ligs, en logs dle contenedor sesa claro que está pasando"
- L3: "Estable 4.0.1"
- L4: Current working branch has unrelated commits after 4.0.0; isolate only the four requested fix files on the current master base.
- L5: Engram runtime rejected its contract; mirror unavailable.
- L6: Isolated commit 995aa1b on origin/master; focused suite 103 passed, 72 subtests passed; full tests suite 1455 passed, 37 skipped, 1075 subtests passed. One pre-existing DeprecationWarning appeared in the focused run.
