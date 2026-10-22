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

El resultado real posterior sustituye el diagnóstico. Si confirma un fallo, se envía la alerta habitual; un resultado exitoso cierra el trabajo sin alerta de fallo. Reenviar exactamente el mismo resultado final solo confirma recepción: no cambia fechas, no duplica logs ni notificaciones. Otro resultado, otro worker o un token incorrecto se rechazan. La cancelación explícita del operador sigue siendo definitiva.

Si el worker nunca regresa, el resultado permanece sin confirmar. Investiga el host y el runtime; si corresponde, cancela el trabajo desde el CP. No se declara éxito ni fallo basándose únicamente en la ausencia de comunicación. Los antiguos fallos sintéticos guardados antes de esta versión siguen admitiendo el resultado tardío del propietario.

## Límites y protección

Los logs se redactan antes de guardarlos. Los archivos usan permisos privados y escritura atómica. La cola admite 64 lotes por ejecución, hasta 64 KiB por lote, con una antigüedad máxima de 7 días. Bajo presión de retención, se conservan los datos más recientes; una advertencia señala la omisión de líneas antiguas. El journal de resultados conserva hasta 32 resultados de hasta 2 MiB cada uno.

## Retención vacía y versión del runtime

Un `RESTIC_KEEP_YEARLY=""` procedente de una política explícita del target significa que esa regla no se aplica; no debe convertirse con `int("")` ni reinterpretarse como `0`. El parser actual usa `RESTIC_RETENTION_POLICY_CONFIGURED=true` para distinguir la política explícita de los valores heredados. Sin ese indicador, una cadena vacía es configuración inválida y se informa con el nombre de la variable. Un runtime antiguo que todavía use `int(os.environ.get(...))` necesita actualizarse: los reintentos de conexión no corrigen ese error de ejecución.

La persistencia requiere que el directorio de estado del worker sea escribible y sobreviva al reinicio del proceso/contenedor. Un error de disco se registra; no equivale a una garantía de persistencia. No borres estos archivos para resolver una desconexión: contienen información todavía no confirmada.
