# Encrypted TAR backups
Status: in progress

## Specs
1. S1: "vale, entonces si no lo recomeindas, elimina esa estrategia y sería full restic rcone"
2. S2: "perfecto, entonces sería quitar TAR del sistema, actualiza el formulario creación del target ya no es necesario seleccionar estrategia, que quede solo informativo, y claramente será restic."
3. S3: "Eliminar TAR por completo"

## Tasks
- [dropped] T1: Implement encrypted TAR configuration, execution and UI; superseded by L5.
- [dropped] T2: Bundle GPG for Windows native workers; superseded by L5.
- [dropped] T3: Verify Docker/native TAR behavior and security; superseded by L5.
- [~] T4 (S1,S3): Remove TAR backup and restore execution from the application; route worker; verification passed, commit pending.
- [ ] T5 (S1,S2,S3): Make control-plane target strategy Restic-only and informational; route inline; commit pending.
- [ ] T6 (S1-S3): Verify integration and affected interfaces; route independent verification; commit pending.

## Log
L1: "tambien aprovechemos y haz ajuste del metodo TAR, actualmente sale elegible via docker y native, pero falla, porque nunca lo hace, entonces se debe de finalizar dicha forma, al seleccionarse, se debe de aparecer opción de ruta de salida, osea, donde va a quedar el tar, e importante, siempre siepre debe de ir ccifrado con al contraseña seleccionada, y entonces al seleccionar TAR, no debe de salir ningunddestino rclone"
L2: "2"
L3: Engram mirror creation blocked: runtime contract missing or unavailable (HTTP 404); source implementation has not started. Existing TAR writes a plaintext archive before optional GPG encryption, and native Windows packaging does not currently include GPG. Existing UI and test-runner changes in the worktree are unrelated and must be preserved.
L4: "dale, usa odd, tambien analizaste el tipo de backup al ser tar? pq se necesita el completo y luego el delta, diferencial.  Eso es posible? o es complicarnos demasiado y sería dejar solo restic como estrategia?"
L5: "vale, entonces si no lo recomeindas, elimina esa estrategia y sería full restic rcone"
L6: Read-only production query on ACT-TWC-11 returned 21 restic targets and no TAR targets. No production state was modified.
L7: "perfecto, entonces sería quitar TAR del sistema, actualiza el formulario creación del target ya no es necesario seleccionar estrategia, que quede solo informativo, y claramente será restic.

--
He actualizado el engram, ya debería de funcionar"
L8: After the reported Engram update, mem_context still fails with HTTP 404 missing or unavailable engram.pi-http-runtime/v1 contract; no source implementation has started.
L9: With explicit user authorization, restarted only local Engram server C:\Users\daniel.rondon\go\bin\engram.exe serve. The replacement process listens on 127.0.0.1:7437 and reports version 3.1.0, but mem_context and mem_doctor report startup readiness timeout; the mandatory mirror remains unavailable. No source implementation has started.
L10: Updated user ENGRAM_BIN to C:\Users\daniel.rondon\AppData\Local\engram\bin\engram.exe (3.6.2). User authorized temporary server stop and backup to C:\Users\daniel.rondon\engram-backup-before-3.6.2. SQLite online backup quick_check returned ok; Engram 3.6.2 offline compatibility returned unknown_shape with offline_automatic_opening_authorized=false and active_reuse_authorized=false. The old 3.1.0 server automatically resumed on port 7437. No upgrade or source implementation was performed.
L11: User authorized migration of the backup copy into C:\Users\daniel.rondon\engram-migration-test-3.6.2. The 3.6.2 migrate-copy command returned exit 1: unsupported database catalog; use a core supporting this store's exact shape; incomplete output retained at that path; do not use or promote it. No live store migration, old binary removal, or source implementation was performed.
L12: User explicitly rejected migration and authorized a new empty store at C:\Users\daniel.rondon\engram-new-3.6.2. Started 3.6.2 on 127.0.0.1:7438, health returned 200 with status ok and version 3.6.2. User ENGRAM_URL, ENGRAM_PORT, and ENGRAM_DATA_DIR point to the new instance; the current Pi process still has the old environment and must restart to verify its memory contract. No old store was changed or removed.
L13: Engram connection restored after Pi restart; task document mirrored to the new empty store. Preserve the user's unrelated modal layout and silent-test-runner changes. User requested no test-created visible windows.
L14: Read-only exploration found TAR is also used for restoring existing archives and is the standalone runtime's current default. ACT-TWC-11 has no TAR targets, but TAR archives elsewhere cannot be ruled out. Await a decision on whether legacy TAR restoration must remain available before deleting the strategy.
L15: "Eliminar TAR por completo"
L16: T4 worker observed RED then GREEN for Restic-only backup/restore and candidate listing. Parent silent-runner checks: 101 tests passed (3 skipped); subsequent 42 passed (1 skipped). Independent verifier confirmed T4 S1/S3 and tested local Restic and rclone repository handling; control-plane/UI remains T5. Worker/test-verifier initially used OS temporary paths outside the project without authorization; no manual cleanup was performed. Existing silent-runner and modal UI changes remain outside the T4 commit.
