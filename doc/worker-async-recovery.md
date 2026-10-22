# Recuperación de estados y logs tras una desconexión

Una desconexión del Control Plane no demuestra que el backup haya fallado. El worker conserva los datos pendientes y reintenta entregarlos sin volver a ejecutar el backup.

## Datos conservados por el worker

- Los lotes de progreso y logs se escriben antes de enviarlos en `worker_progress/`, junto al archivo de recuperación del worker. Cada ejecución tiene un archivo privado identificado mediante hash; incluye la identidad del worker y la prueba de lease.
- El emisor usa un hilo separado: un timeout HTTP no bloquea la salida del runtime. Reintenta con espera creciente de 1 a 30 segundos.
- Cada lote tiene una secuencia. Al reconectar, se reenvía el mismo lote hasta recibir una respuesta satisfactoria; el CP ignora secuencias duplicadas.
- El resultado final se escribe en el journal `*.terminal.json` antes del primer intento de entrega. Los logs pendientes se entregan antes del resultado final.
- Tras reiniciar, el siguiente ciclo de consulta carga los archivos pendientes. La ejecución no se repite por un fallo de entrega.

## Límites y protección

Los logs se redactan antes de guardarlos. Los archivos usan permisos privados y escritura atómica. La cola admite 64 lotes por ejecución, hasta 64 KiB por lote, con una antigüedad máxima de 7 días. Bajo presión de retención, se conservan los datos más recientes; una advertencia señala la omisión de líneas antiguas. El journal de resultados conserva hasta 32 resultados de hasta 2 MiB cada uno.

La persistencia requiere que el directorio de estado del worker sea escribible y sobreviva al reinicio del proceso/contenedor. Un error de disco se registra; no equivale a una garantía de persistencia. No borres estos archivos para resolver una desconexión: contienen información todavía no confirmada.
