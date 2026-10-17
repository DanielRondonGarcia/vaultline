# User Password Reset
Branch: `feature/audit-reports`.
Purpose: let administrators reset passwords for non-admin users and preserve literal password characters.
Delivery: `ask-on-risk`; chain strategy inherited from the audit-reports feature: `feature-branch-chain`. T2 slice: commit `7217b47` (260 insertions, 3 deletions). No push or PR is authorized.

## Specs
- S1: “tambien en la parte de ususario, agrega opción de edición, en caso de que se quiera restablecer la contraseña d eun usuario que no sea admin, me reportaron que presentaban probelmas con contraseñas que tuvieran % o $”

## Tasks
- T1 — S1 — route: characterize the existing reset-password path with synthetic `%`/`$` passwords through public behavior; add RED coverage for missing non-admin user action and management-mode visibility — commit: `7217b47` (route characterization and UI RED in `tests/test_user_password_reset.py`).
- T2 — S1 — route: add an administrator-only password-reset action for non-admin accounts when local user management is enabled, using the existing admin reset endpoint and leaving usernames/roles unchanged — commit: `7217b47` (Risk item 2).
- T3 — S1 — route: run focused UI/auth tests, assess the change, perform one independent security verification, and record the result — commit: N/A (verification); record T2 commit evidence.

## Log
- L1: tambien en la parte de ususario, agrega opción de edición, en caso de que se quiera restablecer la contraseña d eun usuario que no sea admin, me reportaron que presentaban probelmas con contraseñas que tuvieran % o $
- L2: Source map: `renderUsers()` has only a create form and read-only table; the backend already exposes an admin-only reset-password POST route and JSON password handling. The current hash/validation path appears to accept `%` and `$` without transformation; verify through an isolated public-flow test before concluding. Scope interpretation: expose reset for non-admin accounts only; do not add username or role editing.
- L3: `GET /api/v1/admin/users` returns user entries plus `auth_management_summary()`, including `management.manageable`; the current UI loader stores only `items` and leaves `state.userManagement` unused. Show reset controls only when management is enabled, so environment-backed accounts do not expose an action the existing route cannot fulfill.
- L4: T1 characterization: `python -m pytest tests/test_user_password_reset.py -q` returned 2 passed and 1 expected UI failure (3 symbol subtests passed). The existing public reset route round-tripped synthetic passwords containing `%`, `$`, and both, kept the target at viewer role, required administrator authorization, and rejected a viewer request without changing credentials. UI RED identifies discarded management metadata and the absent reset action/handler. T1 is complete; T2 starts next.
- L5: T2 implementation complete: the Users loader now retains management metadata; administrators see an inline masked reset form only for manageable non-admin accounts. Submission sends only the entered password to the existing endpoint and refreshes the list; the UI test also confirms the existing create-user flow remains intact. `python -m pytest tests/test_user_password_reset.py tests/test_ui_states.py tests/test_ui_audit_reports.py -q` passed (121 tests, 12 subtests). T3 assessment and independent security verification remain.
- L6: Native ASSESS of committed range `7217b47` returned `risk: high`, `nativeRisk: unassessable`, and no candidate because the repository has undeclared untracked files. Do not declare or inspect unrelated untracked/scratch paths; native assessment will not be retried. Continue with the required independent security verification and record the limitation.
- L7: Independent `gentle-ai-verify` review returned S1 MET. It confirmed admin/manageability gating, non-admin targets, masked non-rendered password handling, a password-only JSON request, list refresh, unchanged create-user flow, and the existing server-side admin guard; the verifier also exercised synthetic `%`/`$` passwords and denied viewer access. Limitations: no live browser or full server process was run. The verifier reported creating `/tmp/odd-verify-nmTJlK` outside the project; this session has not inspected or deleted it and requires explicit authorization before cleanup.
- Risk: item 2 (admin-only password-reset interface).
