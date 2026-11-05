# Snapshot sync release

Isolate the snapshot sync fix and publish a stable patch release.

## Specs

- S1: "pues si, hacer ese ajsute y mejorar los ligs, en logs dle contenedor sesa claro que está pasando"
- S2: "dale, pues integrarlo y genera release para ir a ver"
- S3: "Estable 4.0.1"

## Tasks

- T1 [S1] DONE: Isolate the existing fix on the stable base; route inline; commit 995aa1b.
- T2 [S1, S2] DONE: Verify and integrate the fix; route inline; commit 995aa1b, master d5abaf1.
- T3 [S2, S3] DONE: Publish and verify the stable release; route workflow; release commit d5abaf1.

## Log

- L1: "dale, pues integrarlo y genera release para ir a ver"
- L2: "pues si, hacer ese ajsute y mejorar los ligs, en logs dle contenedor sesa claro que está pasando"
- L3: "Estable 4.0.1"
- L4: Current working branch has unrelated commits after 4.0.0; isolate only the four requested fix files on the current master base.
- L5: Engram runtime rejected its contract; mirror unavailable.
- L6: Isolated commit 995aa1b on origin/master; focused suite 103 passed, 72 subtests passed; full tests suite 1455 passed, 37 skipped, 1075 subtests passed. One pre-existing DeprecationWarning appeared in the focused run.
- L7: Workflow 42075727338 succeeded; stable release 4.0.1 points to d5abaf1, is latest, and contains Linux amd64/arm64 packages, Windows exe/MSI and SHA256SUMS. Image publication succeeded. No deployment was performed.
