# Flujo de restauración de snapshots

Esta guía describe cómo la interfaz de control plane pide, confirma y ejecuta una restauración, en particular las opciones de archivos existentes (preservar o reemplazar) y la elección de destino para targets con worker nativo.

## Resumen de la restauración

Desde la lista de snapshots de un target se abre el modal **Restaurar snapshot**. El modal agrupa las decisiones en bloques:

- **Destino en el host** (solo targets nativos).
- **Archivos existentes** (preservar o reemplazar).
- **Servicios durante la restauración** (solo targets de Docker/Compose).
- **Permisos de archivos** (conservar los del backup o aplicar valores UID:GID manuales).

Al confirmar, la UI llama al endpoint existente `POST /api/v1/targets/{target_id}/restore/run` con el mismo contrato de siempre: `restore_target_path`, `force_overwrite`, `restore_ownership`, `stop_containers`.

## Archivos existentes: conservar o reemplazar

| Opción | Qué hace | Confirmación |
| --- | --- | --- |
| **Conservar archivos existentes** (recomendado) | Restaura solo los archivos que faltan; sin borrar ni sobrescribir nada. | No exige confirmación destructiva. |
| **⚠ Reemplazar archivos existentes** | **Borra todo el contenido del destino antes de restaurar**; puede eliminar archivos que no están en el snapshot, incluso si se crearon antes del snapshot. | Exige marcar la casilla de confirmación antes de poder restaurar. |

Al elegir **Reemplazar**, la propia opción muestra «⚠ Reemplazar archivos existentes» y el selector se marca en estado de advertencia (borde rojo y aviso visible al lado). El aviso destructivo aparece junto a la casilla y aclara que el reemplazo borra todo el contenido del destino: puede eliminar archivos que no están en el snapshot, incluso si se crearon antes. La confirmación es temporal: si cambias el destino, la ruta, los valores de permisos o vuelves a elegir **Conservar**, la casilla se desmarca y tendrás que confirmar de nuevo antes de restaurar.

La opción de conservar es la que respeta los archivos existentes: si un archivo del backup ya existe en el destino, no se toca; solo se restauran los que faltan. No se pide confirmación destructiva porque no hay reemplazo.

## Destino en el host (targets nativos)

El bloque de destino ofrece dos rutas:

1. **Otra ruta (restaurar en una carpeta nueva)** — opción por defecto. Restaura el contenido del backup directamente dentro de la ruta que escribas (sin recrear la letra de unidad ni la ruta original); útil para revisar el resultado antes de mover nada.
2. **Restaurar en la ruta original** — aparece solo si el target tiene rutas configuradas. Restaura directamente en una de las rutas del target.

Reglas del destino:

- Si el target tiene **una sola ruta** configurada, la opción de ruta original la usa directamente.
- Si el target tiene **varias rutas**, debes elegir cuál corresponde a este snapshot; la UI no restaura todas a la vez silenciosamente.
- La ruta elegida se envía tal cual está configurada en el target (incluidas rutas de Windows con `\` o `&`). La restauración directa en origen admite directorios Linux y Windows con letra de unidad. Las fuentes que son archivos individuales o rutas UNC se rechazan antes de modificar el destino; la UI no ofrece rutas UNC como origen directo. Puedes seguir usando otro destino UNC.
- Si envías la **misma ruta configurada del target**, el motor detecta la igualdad y restaura solo el subárbol de esa ruta.
- Con un **destino personalizado** (distinto de las rutas configuradas), el motor coloca el contenido de cada ruta configurada directamente dentro del destino, como en el flujo de contenedores: no recrea componentes del snapshot (por ejemplo, no crea una carpeta `L`). Con varias rutas configuradas se restauran todas al mismo destino, y cada fuente se valida con `restic ls` antes de modificar el destino. Solo cuando el destino es una **raíz** (la raíz POSIX `/`, una raíz de disco Windows como `L:\` o la raíz de un recurso UNC) se mantiene el árbol completo del snapshot tal cual está grabado.
- Si el target no tiene rutas configuradas, la opción de ruta original no se ofrece; siempre verás la ruta personalizada. Restaurar en un destino personalizado **falla de forma segura antes de modificar el destino** cuando el target nativo no tiene rutas configuradas.

## Permisos de archivos

- **Conservar permisos del backup** (recomendado): no aplica identidades automáticas. Los archivos existentes que se conservan no cambian de permisos ni de propietario.
- **Aplicar permisos manuales**: requiere escribir valores numéricos `UID:GID` por volumen, con la clave `compose:proyecto:volumen`.

Aplicar permisos manuales exige su propia confirmación en la casilla; es independiente de la confirmación de reemplazo. La confirmación cubre **solo los archivos que se restauren en esa operación**: los archivos existentes que se conservan mantienen sus permisos y propietarios actuales, y la UI no promete aplicar identidades del backup sobre ellos. Cambiar el destino, la ruta o los valores de permisos desmarca la casilla; confirma de nuevo antes de restaurar.

Conservar archivos requiere espacio temporal para extraer el snapshot antes de copiar solo lo que falta. Si se interrumpe, pueden quedar archivos nuevos parcialmente restaurados; los archivos que ya existían no se reemplazan.

## Qué esperar tras confirmar

- Se abre el panel de logs del job de restauración; desde ahí puedes seguir el progreso y el resultado.
- La restauración no detiene contenedores en targets nativos; en targets de Docker/Compose puedes elegir entre mantener servicios activos o detenerlos antes.
