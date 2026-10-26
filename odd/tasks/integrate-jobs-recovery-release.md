# Integrate recovery and Jobs improvements into default branch
Objective: commit the verified worker-badge fix, fast-forward the default branch with the completed recovery and Jobs work, and publish the next stable release.
Branch: feat/jobs-target-worker-search -> origin/master (repository default, named master rather than main); delivery: direct fast-forward explicitly requested, no PR; runner: .venv/Scripts/python.exe -B -m unittest.

## Specs
S1. "vale, entonces haz commit, integra todo al main y genera release"

## Tasks
T1 | S1 | inline | in_progress | Commit only the verified worker-badge behavior, test and docs; preserve unrelated dirty files; commit: pending.
T2 | S1 | inline/background checks | pending | Verify candidate, fast-forward origin/master from the feature branch, await required CI; commit: pending.
T3 | S1 | inline/background command | pending | Publish the next stable release from updated master through the existing workflow; commit: pending.
T4 | S1 | inline, independent verify for Risk item 5 | pending | Verify exact release source, stable latest, checksums and image aliases; close evidence; commit: pending.

## Log
L1. Original request verbatim: vale, entonces haz commit, integra todo al main y genera release
L2. Repository default branch is master, not main. Current feature branch includes worker asynchronous recovery, prerelease isolation, existing stable 3.15.13 publication notes, Jobs target/worker search and the as-yet-uncommitted worker-badge fix. origin/master remains 265a3b9 (3.15.11 source), an ancestor of the feature branch; stable tag 3.15.13 points to b9e0983. Expected next patch tag 3.15.14 after fresh recheck. The branch has ~819 authored changed lines versus origin/master BEFORE the 50-line badge fix; direct integration was explicitly requested, but review workload exceeds ~400 lines, so preserve prior work-unit commits rather than squashing. Master has no branch protection per GitHub API. CI runs on push. Do not stage unrelated dirty .atl, odd task files, tracked pycache; do not touch the other registered worktree or production hosts. Risk item 3 (shared listing/version refresh), item 5 (merge, release/images). No new source behavior beyond the already verified badge correction is requested. Tests: focused badges and full suite (previous 1088 passed, 6 skipped), CI on master, release workflow and artifact/image evidence. Publication and merge are authorized; installation on production hosts is not.
