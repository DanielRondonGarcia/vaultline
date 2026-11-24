# Job log lifecycle
Branch: `feat/job-log-lifecycle` · Delivery: ask-on-risk (forecast >400 authored lines; separate storage and policy slices).
Checks: local Python pytest, public API/log viewer, isolated SQLite, and independent verification. No production rollout.

## Specs
- S1: “es posible implementar algun tipado de compresión de esos logs?”; “solo si se da en ver log en jobs mas viejo de 1 mes para atras, pues inicia la descompresión rapida y ver lo util”; “si, para la compresión sería usar Brotli, no? es la mejor para texto plano.”
- S2: “los ultimos 3 meses se mantienen, de resto se va eliminando”.
- S3: “y eso de cuota, agregarle configuración en la parte de settings global”; “con alerta y con borrado, porque es cuota”.

## Tasks
- T1 · S1 · inline, storage and read path · in progress · commit pending · Risk: item 1 (archives existing log data).
- T2 · S1–S3 · inline, runtime policy and Global Settings (depends on T1) · pending · commit pending · Risk: item 1 (automated deletion of retained logs).

## Log
- L1: mmm toca analizar como optimizar esa información, es posible implementar algun tipado de compresión de esos logs? que supongo que los logs son los que mas pesan y no es necesario verlo directamente, solo si se da en ver log en jobs mas viejo de 1 mes para atras, pues inicia la descompresión rapida y ver lo util, tambien funcionalidad de poner una cuota, para que se ponga por ejemplo, los ultimos 3 meses se mantienen, de resto se va eliminando
- L2: si, para la compresión sería usar Brotli, no? es la mejor para texto plano.

  y eso de cuota, agregarle configuración en la parte de settings global
- L3: con alerta y con borrado, porque es cuota
- Decision: quota breaches warn and evict oldest eligible logs, even before the age cutoff. This never deletes job metadata, result summaries, snapshots, or backups. Age is measured from completion. Settings default to 30/90 days when enabled; opt in to automated maintenance on upgraded installations. Synthetic lease-expiry jobs are excluded while they can still accept late reports. No production writes or automatic VACUUM.
