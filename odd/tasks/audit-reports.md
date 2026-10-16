# Audit Reports
Branch: `feature/audit-reports`.
Purpose: deliver the requested audit report section and exports; record task evidence below.

## Specs
- S1: “un apartado de informes o reportes, utiles para pasarlo a una auditoria”
- S2: “una matriz de backups, que servidores que servicios tiene, periodicidad de backups, su retención, su porcentaje exitoso y porcentaje fallido, etc.”
- S3: “Y que sea en varios formatos, excel, csv, json, pdf”
- S4: “Rango seleccionable (recomendado)”

## Tasks
- T1 — S1, S2, S4 — route: date-bounded report matrix and calculation service — commit: pending
- T2 — S1, S2, S4 — route: viewer-authenticated JSON report-data API — commit: pending
- T3 — S3 — route: downloadable JSON, CSV, Excel, and PDF formats — commit: pending
- T4 — S1–S4 — route: reports navigation, date-range controls, preview, downloads, and UI verification — commit: pending

## Log
- L1: vale, necesito que implementes un apartado de informes o reportes, utiles para pasarlo a una auditoria, y pregunten tipo, una matriz de backups, que servidores que servicios tiene, periodicidad de backups, su retención, su porcentaje exitoso y porcentaje fallido, etc. Y que sea en varios formatos, excel, csv, json, pdf
- L2: Rango seleccionable (recomendado)
- L3: Source evidence: `WorkerRecord.host_name`, `BackupTargetRecord` schedule/resources, and `RetentionPolicyRecord` fields are in `src/control_plane/domain/models.py`; backup execution uses command `backup.run` in `src/control_plane/application/services/control_plane_service.py:1294`. Existing `job_stats` is global history plus a seven-day daily window, so reporting requires a new date-bounded per-target aggregation.
- L4: Implementation decision: treat selected dates as inclusive in the scheduler's configured timezone and query with a half-open UTC window; calculate success/failure percentages over completed `backup.run` jobs (`succeeded` + `failed`) and report other states separately.
- L5: Route/aggregation plan: keep reports read-only under the existing viewer role; query only `backup.run` jobs by start time, join target/worker/retention metadata, and never include runtime environment or secret fields in exported data.
- L6: Plan refinement: split the report data layer and viewer API into separate reviewable work units before adding the four file downloads and UI.
