# Integrate recovery and Jobs improvements into default branch
Objective: commit the verified worker-badge fix, fast-forward the default branch with the completed recovery and Jobs work, and publish the next stable release.
Branch: feat/jobs-target-worker-search -> origin/master (repository default, named master rather than main); delivery: direct fast-forward explicitly requested, no PR; runner: .venv/Scripts/python.exe -B -m unittest.

## Specs
S1. "vale, entonces haz commit, integra todo al main y genera release"

## Tasks
T1 | S1 | inline | done | Worker badge RED/GREEN 3 focused, earlier full suite 1088 exit 0 (6 skips), independent verification from prior turn, only own 4 paths staged; commit: cce87c0.
T2 | S1 | inline/background checks | in_progress | Verify candidate, fast-forward origin/master from the feature branch, await CI; commit: pending.
T3 | S1 | inline/background command | pending | Publish the next stable release from updated master through the existing workflow; commit: pending.
T4 | S1 | inline, independent verify for Risk item 5 | pending | Verify exact release source, stable latest, checksums and image aliases; close evidence; commit: pending.

## Log
L1. Original request verbatim: vale, entonces haz commit, integra todo al main y genera release
L2. Repository default branch is master, not main. Current feature branch includes worker asynchronous recovery, prerelease isolation, existing stable 3.15.13 publication notes, Jobs target/worker search and the as-yet-uncommitted worker-badge fix. origin/master remains 265a3b9 (3.15.11 source), an ancestor of the feature branch; stable tag 3.15.13 points to b9e0983. Expected next patch tag 3.15.14 after fresh recheck. The branch has ~819 authored changed lines versus origin/master BEFORE the 50-line badge fix; direct integration was explicitly requested, but review workload exceeds ~400 lines, so preserve prior work-unit commits rather than squashing. Master has no branch protection per GitHub API. CI runs on push. Do not stage unrelated dirty .atl, odd task files, tracked pycache; do not touch the other registered worktree or production hosts. Risk item 3 (shared listing/version refresh), item 5 (merge, release/images). No new source behavior beyond the already verified badge correction is requested. Tests: focused badges and full suite (previous 1088 passed, 6 skipped), CI on master, release workflow and artifact/image evidence. Publication and merge are authorized; installation on production hosts is not.
L3. Badge behavior commit cce87c0 contains only UI, stylesheet, dedicated Node regression test, quickstart wording and this tracker. Focused version tests 3 pass, scoped diff check pass; previous parent full suite 1088/6 skips and independent verifier proved neutral-before-latest, redraw-after-release, and unchanged banner. Committed-only assessment against 673afb3: native medium, escalated high for item 3, reviewDue false, independent verification previously completed. No unrelated dirty files were staged; restore/scratch not touched.
