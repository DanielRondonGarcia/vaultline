# Programación autónoma del worker

Seguimiento de la mejora descrita en la incidencia #20. La rama de seguimiento reúne las unidades revisadas antes de integrar la funcionalidad completa en la rama principal.

## Alcance

- Conservar la última programación del worker para ejecutar backups durante desconexiones.
- Guardar resultados temporalmente y sincronizarlos cuando vuelva el Control Plane.
- Evitar ejecuciones duplicadas durante el traspaso de la programación.
- Verificar el backup, la restauración y la reconciliación en un entorno aislado.

La release debe generarse solo después de integrar todas las unidades y verificar los controles correspondientes.
