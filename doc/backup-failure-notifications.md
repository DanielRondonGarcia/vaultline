# Notificaciones de backups fallidos

Configura uno o más webhooks o destinos de correo en **Settings → Notificaciones de backups fallidos**. El Control Plane envía un resumen saneado cuando falla un job `backup.run`; la entrega es asíncrona y no bloquea el backup.

## Configuración rápida

1. Inicia sesión con una cuenta **administradora** y abre **Settings**.
2. En **Notificaciones de backups fallidos**, selecciona **Agregar canal** y elige **Webhook** o **Correo SMTP**.
3. Completa los datos del destino y guarda. Repite para añadir otros destinos; cada canal puede activarse o desactivarse por separado.
4. Para validar un canal SMTP, selecciona **Enviar correo de prueba** en su tarjeta. La ventana modal permite elegir los destinatarios solo para ese envío: marca uno o varios de los configurados (inicialmente ninguno marcado) o cambia a **Correo personalizado** y escribe una única dirección propia, por ejemplo para probar con otro correo sin molestar al resto. Los modos son excluyentes: al elegir el correo personalizado se ignoran los marcados. El envío se bloquea si no hay ninguna selección, reabrir la ventana la limpia, y el sistema indica los destinatarios reales a los que llegó el correo. La selección no edita los destinatarios del canal; utiliza las credenciales guardadas y no inicia un backup. Los webhooks no tienen envío de prueba en la interfaz.

## Webhook

- Usa una URL absoluta `http://` o `https://`, sin usuario/contraseña embebidos ni fragmento `#`.
- Se permiten destinos en redes internas, por ejemplo `http://notifications.internal/hooks/backup`. El administrador es responsable de verificar la URL y el acceso de salida del Control Plane.
- Se envía un `POST` JSON. Las redirecciones HTTP no se siguen.
- La URL completa se cifra al guardarse y nunca se vuelve a mostrar; la lista muestra solo esquema y host. Al editar, deja vacía la URL para conservar el destino actual o escribe una nueva para reemplazarlo.
- Prefiere HTTPS cuando el destino y la red lo permitan, especialmente si la URL contiene un token.

## Correo SMTP

| Campo | Descripción |
|---|---|
| Servidor, puerto | Host SMTP y puerto entre 1 y 65535. |
| Remitente, destinatarios | Direcciones de correo; separa varios destinatarios con comas. |
| Seguridad | `STARTTLS` (por defecto y recomendado), `SSL/TLS` o `Sin TLS`. Usa Sin TLS solo en una red confiable. |
| Usuario y contraseña | Opcionales, pero deben configurarse juntos. Se cifran al guardarse; nunca se devuelven a la interfaz. En edición, déjalos vacíos para conservar las credenciales guardadas; para rotarlas, indica ambos valores. |

En la ventana modal de prueba elige al menos un destinatario: marcas sobre los configurados o un correo personalizado (modos excluyentes; sin selección no se envía nada). Las direcciones elegidas van solo para ese envío y el aviso muestra los destinatarios reales del envío. Los avisos reales de `backup.failed` y los webhooks siguen llegando a todos los destinatarios configurados.

El endpoint de administrador `POST /api/v1/notification-channels/{id}/test-email` acepta un cuerpo JSON objeto con un campo opcional `recipients`: si se omite se envía a los destinatarios configurados (comportamiento anterior); si se incluye, debe ser una lista no vacía de direcciones válidas y solo se envía a esas direcciones, sin modificar el canal. Un cuerpo que no sea un objeto JSON se rechaza con HTTP 400 (`request body must be a JSON object`), igual que una lista vacía, nula o con direcciones inválidas; ambas validaciones ocurren antes de intentar el envío.

## Qué se envía y cómo se comporta

- El evento se llama `backup.failed` e incluye identificadores del job/target, estado, fechas y un resumen de error saneado. Los webhooks reciben siempre ese JSON de resumen; nunca incluyen logs.
- **No se incluyen credenciales de los canales.** Los valores sensibles reconocidos en el contexto del job (contraseñas, tokens y credenciales configurados en el target o en Settings, con su patrón `clave=valor`) se redactan; otros secrets que solo aparecieron en los logs sin ese contexto pueden no detectarse, revisa siempre el correo antes de reenviarlo.
- Se intenta una entrega por job y canal activo. La reclamación se guarda para evitar duplicados si el Control Plane vuelve a publicar el mismo fallo, incluso después de reiniciarse.
- Es un intento de mejor esfuerzo: no hay cola de reintentos. Un fallo de webhook o SMTP no cambia el resultado del backup; cada conexión tiene un timeout de 5 segundos y se ejecuta en segundo plano.
- Desactiva un canal para detener avisos a ese destino. Elimina un canal si ya no debe conservarse.

## Presentación del correo SMTP

Los avisos SMTP llegan como correo HTML con alternativa de texto plano (no JSON):

- Con la URL pública del Control Plane configurada (`control_plane_public_url` en Settings, por ejemplo `https://backups.miempresa.com`), el correo incluye el enlace **Ver trabajos en el sistema de backups** hacia la lista de jobs; los links solo se generan con URLs absolutas `http://` o `https://`.
- Al final del correo, después del enlace, va la sección **Registro de error**: una vista previa acotada con las primeras y últimas líneas del log (máx. 50 líneas o 4 KiB en total). Si se cortaron líneas del medio, aparece un marcador `Contenido omitido`; la última línea `ERROR` se conserva siempre, incluso con líneas Unicode muy largas, y el texto indica que el registro completo va en el archivo adjunto.
- El log completo retenido del job se adjunta como `backup-<id-del-job>.log.txt` en UTF-8 (`text/plain`); el log se redacta igual que la vista previa. Solo incluye el log aún retenido por el Control Plane (límite actual: 1000 líneas / 512 Ki caracteres), no el historial que ya se descartó.
- El correo de prueba SMTP sigue siendo una notificación `notification.test` sin log ni adjunto; los webhooks no cambian su comportamiento.

## Seguridad operativa

Solo administradores pueden gestionar canales. Las configuraciones se cifran con la clave del Control Plane; protege y conserva esa clave junto con los respaldos de la base de datos. Los webhooks internos están permitidos por diseño, así que guarda esta capacidad para administradores de confianza y limita la salida de red del Control Plane según la política de tu entorno.
