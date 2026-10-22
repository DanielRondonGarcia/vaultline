# Isolated asynchronous recovery prerelease
Objective: publish the verified recovery feature for external exploratory testing without moving stable image channels.
Branch: feat/worker-async-recovery; delivery: single work unit plus publication evidence; runner: .venv/Scripts/python.exe -B -m unittest.

## Specs
S1. "mm, venga, genera el release para probar directamente."
S2. User selected "Prerelease 3.15.12 aislado (recomendado)": "Ajustaré el workflow para publicar solo el tag 3.15.12, sin mover latest ni los canales estables; lo publicaré desde la rama de esta funcionalidad."

## Tasks
T1 | S1-S2 | inline (bounded single workflow behavior, no independent writers) | done | Disable implicit latest and gate major/minor/explicit latest on !prerelease; prerelease --latest=false; two new tests RED (4 failures) then full focused 18 GREEN, docs updated; commit pending.
T2 | S1-S2 | independent verifier (Risk item 5 release workflow) | done | ASSESS high by item 5, native unavailable due untracked selection; separate verifier 18 release tests +9 runtime integration pass, exact stable/prerelease metadata differential and YAML parse pass; full suite 1082 tests exit 0, 6 skipped (job-10). Commit evidence below.
T3 | S1-S2 | inline/background shell | in_progress | Push feature branch, dispatch patch prerelease, await CI and release, verify assets/images and stable tags; publication pending.

## Log
L1. Original request verbatim: mm, venga, genera el release para probar directamente.
L2. User selected: Prerelease 3.15.12 aislado (recomendado). Authorization includes workflow change, feature branch push and prerelease/image/installer publication; not master merge or stable alias updates.
L3. Origin: github.com/DanielRondonGarcia/vaultline. Latest published 3.15.11. origin/master is 265a3b96e4b1d1adfb962efd927498574ba5179f; current verified source 96358568aea5fb4dd13d8c25970d720d9b77b6f6. No changes authored after verified recovery except task evidence. Existing unrelated .atl/tasks/pycache dirty files excluded from staging.
L4. Existing workflow manually bumps patch, builds three amd64 images, native amd64/arm64 Debian packages, Windows exe/MSI and checksums; prerelease input currently still moves latest/major/minor image aliases. Fix must suppress metadata automatic latest as well as explicit aliases for prereleases. Stable release tags remain unchanged. Engram previously unavailable; preserve local tracker.
L6. Checks: new two tests RED (4 subtest failures), focused 18 GREEN; independent YAML/rule differential: stable four tag sets identical, prerelease exact version only for all three images. Full suite job-10 exit 0: 1082 tests, 6 skipped. No proof claimed for remote builds yet. Assessment RDD off, high by item 5; native unassessable due untracked declaration; independent verifier ran and passed without blockers.
L5. Risk: item 5 (release/CI/environment), item 3 (shared tag generation consumed by stable releases). Forecast under 150 authored lines; rollback boundary workflow, targeted tests and release documentation. Publishing external prerelease is irreversible public delivery; explicitly authorized in S1-S2.
