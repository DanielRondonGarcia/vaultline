# Notificaciones de backups fallidos

Configura uno o más webhooks o destinos de correo en **Settings → Notificaciones de backups fallidos**. El Control Plane envía un resumen saneado cuando falla un job `backup.run`; la entrega es asíncrona y no bloquea el backup.

## Configuración rápida

1. Inicia sesión con una cuenta **administradora** y abre **Settings**.
2. En **Notificaciones de backups fallidos**, selecciona **Agregar canal** y elige **Webhook** o **Correo SMTP**.
3. Completa los datos del destino y guarda. Repite para añadir otros destinos; cada canal puede activarse o desactivarse por separado.
4. Comprueba la entrega con el siguiente backup que falle. La interfaz no envía mensajes de prueba.

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

## Qué se envía y cómo se comporta

- El evento se llama `backup.failed` e incluye identificadores del job/target, estado, fechas y un resumen de error saneado.
- **No se incluyen logs completos ni credenciales de los canales.** Los valores sensibles reconocidos en el contexto del job se redactan y el payload se limita a 8 KiB.
- Se intenta una entrega por job y canal activo. La reclamación se guarda para evitar duplicados si el Control Plane vuelve a publicar el mismo fallo, incluso después de reiniciarse.
- Es un intento de mejor esfuerzo: no hay cola de reintentos. Un fallo de webhook o SMTP no cambia el resultado del backup; cada conexión tiene un timeout de 5 segundos y se ejecuta en segundo plano.
- Desactiva un canal para detener avisos a ese destino. Elimina un canal si ya no debe conservarse.

## Seguridad operativa

Solo administradores pueden gestionar canales. Las configuraciones se cifran con la clave del Control Plane; protege y conserva esa clave junto con los respaldos de la base de datos. Los webhooks internos están permitidos por diseño, así que guarda esta capacidad para administradores de confianza y limita la salida de red del Control Plane según la política de tu entorno.
