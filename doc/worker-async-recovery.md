# Recuperación de estados y logs tras una desconexión

Una desconexión del Control Plane no demuestra que el backup haya fallado. El worker conserva los datos pendientes y reintenta entregarlos sin volver a ejecutar el backup.

## Datos conservados por el worker

- Los lotes de progreso y logs se escriben antes de enviarlos en `worker_progress/`, junto al archivo de recuperación del worker. Cada ejecución tiene un archivo privado identificado mediante hash; incluye la identidad del worker y la prueba de lease.
- El emisor usa un hilo separado: un timeout HTTP no bloquea la salida del runtime. Reintenta con espera creciente de 1 a 30 segundos.
- Cada lote tiene una secuencia. Al reconectar, se reenvía el mismo lote hasta recibir una respuesta satisfactoria; el CP ignora secuencias duplicadas.
- El resultado final se escribe en el journal `*.terminal.json` antes del primer intento de entrega. Los logs pendientes se entregan antes del resultado final.
- Tras reiniciar, el siguiente ciclo de consulta carga los archivos pendientes. La ejecución no se repite por un fallo de entrega.

## Estado en el Control Plane

Al vencer el lease sin resultado final, el trabajo permanece `in_progress`, sin fecha de finalización, con el diagnóstico `worker_lease_expired`. La interfaz explica que espera confirmación; no se envía `backup.failed` por la desconexión. El propietario puede renovar el lease o entregar progreso con la misma prueba de ejecución, incluso después del vencimiento. Una renovación correcta limpia el diagnóstico de desconexión.

## Resultado desconocido tras 24 horas (límite de confirmación)

La ausencia de confirmación durante al menos 24 horas no convierte el trabajo ni en fallo ni en éxito. El resultado se registra como **desconocido** con el estado `unconfirmed` («Resultado desconocido»), sin fecha de finalización y sin alerta de fallo. En ese punto:

- Se permiten nuevas tareas: el scheduler puede lanzar nuevos backups del target y el catálogo puede lanzar nuevas sincronizaciones; el job antiguo ya no cuenta como ejecución activa.
- El propietario autenticado (mismo worker con la misma prueba de lease) puede seguir entregando el resultado real tardío; lo sustituye por el diagnóstico y notifica solo si confirma un fallo real. En este estado ya solo se acepta el resultado terminal: progreso y renovación se rechazan.
- La cancelación explícita del operador sigue disponible para cerrar el trabajo y sigue siendo definitiva.
- La línea de tiempo es determinista: el límite se fija al expirar el lease (expiración + 24 h) y se reprograma cada vez que el propietario muestra vida entregando progreso con su prueba de lease. Así, si el CP puede saber que la ejecución sigue activa (comunicación reciente del propietario), el trabajo sigue bloqueando tareas nuevas y no se lanza un duplicado. Una renovación limpia el diagnóstico y el límite, y rearranca el ciclo normal del lease.

**Limitación de solapamiento inevitable:** si un worker sigue ejecutando el backup pero no consigue comunicar nada con el Control Plane (ni progreso, ni renovación, ni resultado), el CP no tiene forma de saber que la ejecución sigue viva. Tras el plazo de 24 horas, un nuevo backup programado puede solaparse con la ejecución original sin confirmar. Este riesgo no se puede eliminar desde el Control Plane; los datos observados del diagnóstico (`lease_diagnostics`) y el estado del worker ayudan a decidir, y si el solapamiento fuera crítico, hay que aplicar candados a nivel de target o de entorno.

Jobs guardados por versiones anteriores con el diagnóstico `worker_lease_expired` y sin límite almacenado usan, como retro-compatibilidad, la hora de escritura del diagnóstico + 24 h. `reconcile_expired_leases()` sigue contando jobs únicos reconcilidos: si la expiración y el cierre a desconocido ocurren en la misma pasada, la misma cuenta una vez.

Además: si el worker nunca regresa, el resultado deja de esperar confirmación al cumplirse el plazo de 24 horas descrito arriba. Los antiguos fallos sintéticos guardados antes de esta versión siguen admitiendo el resultado tardío del propietario.

El resultado real posterior sustituye el diagnóstico. Si confirma un fallo, se envía la alerta habitual; un resultado exitoso cierra el trabajo sin alerta de fallo. Reenviar exactamente el mismo resultado final solo confirma recepción: no cambia fechas, no duplica logs ni notificaciones. Otro resultado, otro worker o un token incorrecto se rechazan. La cancelación explícita del operador sigue siendo definitiva.

Si el worker nunca regresa, el job pasa a «Resultado desconocido» (estado `unconfirmed`) 24 horas después de la expiración del lease. Investiga el host y el runtime antes de esa marca si puedes; pasado el plazo se permiten nuevas tareas y, si corresponde, cancela el trabajo desde el CP para cerrarlo. No se declara éxito ni fallo basándose únicamente en la ausencia de comunicación.

## Límites y protección

Los logs se redactan antes de guardarlos. Los archivos usan permisos privados y escritura atómica. La cola admite 64 lotes por ejecución, hasta 64 KiB por lote, con una antigüedad máxima de 7 días. Bajo presión de retención, se conservan los datos más recientes; una advertencia señala la omisión de líneas antiguas. El journal de resultados conserva hasta 32 resultados de hasta 2 MiB cada uno.

## Retención vacía y versión del runtime

Un `RESTIC_KEEP_YEARLY=""` procedente de una política explícita del target significa que esa regla no se aplica; no debe convertirse con `int("")` ni reinterpretarse como `0`. El parser actual usa `RESTIC_RETENTION_POLICY_CONFIGURED=true` para distinguir la política explícita de los valores heredados. Sin ese indicador, una cadena vacía es configuración inválida y se informa con el nombre de la variable. Un runtime antiguo que todavía use `int(os.environ.get(...))` necesita actualizarse: los reintentos de conexión no corrigen ese error de ejecución.

La persistencia requiere que el directorio de estado del worker sea escribible y sobreviva al reinicio del proceso/contenedor. Un error de disco se registra; no equivale a una garantía de persistencia. No borres estos archivos para resolver una desconexión: contienen información todavía no confirmada.
