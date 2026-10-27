# Jobs progress and log-view recovery
Track the worker-filtered in-progress job report and the Jobs log-view auto-close report.
Keep both symptom clusters open until their named tests and runtime evidence are accounted for.

## Specs
### S1
> "aqui esto no volvio a retomar el progreso, osea que debe de ser un bug, claramente estoy filtrando por  un worker en la ultima version C:\Users\DANIEL~1.RON\AppData\Local\Temp\orca-paste-1793134823220-4a05ce00-7a14-4735-885c-f8d6fbfea924.png"

### S2
> "dicional, esa vista se cierra al rato solo, osea le doy en ver logs y dura menos de 3 segundos y ce cierra"

## Tasks
- T1 — S1 — Trace and diagnose worker progress resumption; route: inline after sanitized runtime evidence; commit: pending.
- T2 — S2 — Reproduce and fix the log panel closing during Jobs refresh; route: inline; commit: 67286fd.
- T3 — S1-S2 — Verify named behavior and document any missing runtime proof; route: inline; commit: pending.

## Log
- L1: mira, aqui esto no volvio a retomar el progreso, osea que debe de ser un bug, claramente estoy filtrando por  un worker en la ultima version C:\Users\DANIEL~1.RON\AppData\Local\Temp\orca-paste-1793134823220-4a05ce00-7a14-4735-885c-f8d6fbfea924.png

dicional, esa vista se cierra al rato solo, osea le doy en ver logs y dura menos de 3 segundos y ce cierra
- L2: Screenshot shows a filtered Jobs page with an in-progress backup, the `worker_lease_expired` warning, and an indeterminate progress display. Source trace found the log accordion is nested in `tbody`, while the 3-second jobs polling path replaces `tbody.innerHTML` on changed results. Worker/Control Plane source preserves late progress for the same owner and token, so the screenshot alone does not identify why this job's progress is absent.
- L3: Runtime evidence needed for S1 must exclude lease tokens and secrets: worker progress-spool presence/sequence and relevant worker log lines; Control Plane job status, owner identity match, last accepted progress sequence, and last progress timestamp.
- L4: T2 was implemented in commit 67286fd. `python -m pytest tests/test_ui_states.py -q` passed with 106 tests; independent verification found no blockers. A real-browser interaction was not exercised.
