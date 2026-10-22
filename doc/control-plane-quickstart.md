# Control Plane — Guía completa

Esta guía cubre el despliegue y la operación de `control_plane` + `worker_agent`
con persistencia SQLite. Para un inicio rápido en 3 comandos, consulta el
[README principal](../README.md).

## 0. Reset de la release mayor

No hay migración: elimina los volúmenes anteriores y configura desde cero;
conserva el `WORKER_ID` explícito.

## 1. Despliegue con Docker

El repositorio separa el despliegue del Control Plane y del Worker en dos
Composes distintos:

- `deploy/control-plane/docker-compose.yml` (y `docker-compose.ghcr.yml`):
  solo el Control Plane.
- `deploy/worker/docker-compose.yml` (y `docker-compose.ghcr.yml`):
  el Worker Agent junto con los servicios que se quieren backupear.

El Worker se ejecuta en el mismo Compose project que los servicios a backupear,
para que detecte automáticamente el proyecto, sus volúmenes y reporte todo al
Control Plane. No se requiere `network_mode` porque el backup es solo de
archivos.

### Conexión entre stacks (red compartida)

El Compose del Worker se conecta por defecto a la red externa
`docker-volume-backup-control-plane_default` (la red que crea el Compose del
Control Plane). Por eso `CONTROL_PLANE_URL` por defecto es
`http://control-plane:8080` y el Worker alcanza al CP por el nombre del servicio
sin necesidad de exponer el puerto del CP al host.

Si el Control Plane está en otro host o puerto, ajusta `CONTROL_PLANE_URL` al
`host:puerto` publicado del CP.

El Compose del Control Plane incluye el servicio interno `redis` (Redis 7 con
AOF y volumen `snapshot_explorer_redis`). El Worker usa por defecto
`redis://redis:6379/0` para cachear únicamente metadatos acotados de Snapshot
Explorer; Redis no se publica al host. El TTL por defecto es de 86400 segundos
(24 horas), con un maximo de 86400 y hasta 1000 entradas por target y
repositorio. El servicio usa `maxmemory 128mb` con `allkeys-lru`, por lo que la
persistencia AOF no evita la expiracion ni la eviccion. La cache es opcional: si
Redis falta, no responde o se configura mal, el Worker vuelve a Restic y el
catalogo del CP continua respaldado por SQLite.

Para desactivarla de forma explícita, define una URL vacía antes de levantar el
Worker:

```powershell
$env:SNAPSHOT_EXPLORER_REDIS_URL=""
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

### Arrancar el Control Plane

```powershell
$env:CONTROL_PLANE_PUBLISHED_PORT="18080"
docker compose -f deploy/control-plane/docker-compose.yml up -d --build
```

Validación mínima del CP:

```powershell
curl http://127.0.0.1:18080/healthz

curl -c cp-cookie.txt -X POST http://127.0.0.1:18080/api/v1/auth/login `
  -H "Content-Type: application/json" `
  -d "{\"username\":\"admin\",\"password\":\"changeme\"}"
```

### Arrancar el Worker + servicios a backupear

Genera en la UI una credencial HMAC, guarda el secreto y expón
`WORKER_ENROLLMENT_TOKEN` solo una vez; el worker lo guarda (`0600`).

Edita `deploy/worker/docker-compose.yml` para añadir tus servicios con sus
volúmenes (el archivo incluye un servicio `demo-app` de ejemplo). Luego:

```powershell
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

Si el Control Plane está en otro host o puerto, ajusta `CONTROL_PLANE_URL`:

```powershell
$env:CONTROL_PLANE_URL="http://192.168.1.10:18080"
docker compose -f deploy/worker/docker-compose.yml up -d --build
```

Verificación del worker:

```powershell
docker compose -f deploy/worker/docker-compose.yml ps
docker compose -f deploy/worker/docker-compose.yml logs --tail=20 worker
```

Confirmar que el worker se registró en el CP:

```powershell
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers
```

Consultar el inventario reportado por el worker (incluye este Compose project
con sus volúmenes):

```powershell
# $WORKER_ID se obtiene del endpoint /api/v1/workers
curl -b cp-cookie.txt http://127.0.0.1:18080/api/v1/workers/$WORKER_ID/inventory
```

### Consumir imágenes publicadas en GHCR

Control Plane:

```powershell
$env:CONTROL_PLANE_PUBLISHED_PORT="18080"
docker compose -f deploy/control-plane/docker-compose.ghcr.yml up -d
```

Worker + servicios:

```powershell
docker compose -f deploy/worker/docker-compose.ghcr.yml up -d
```

Imágenes esperadas:

- `ghcr.io/danielrondongarcia/vaultline-control-plane`
- `ghcr.io/danielrondongarcia/vaultline-worker`

### Detener los stacks

```powershell
docker compose -f deploy/control-plane/docker-compose.yml down
docker compose -f deploy/worker/docker-compose.yml down
```

## 2. Despliegue nativo (sin Docker)

Por defecto, el Control Plane usa SQLite en `control_plane.db`.

```powershell
python -m src.control_plane.main
```

Si ya tenías un proceso previo del Control Plane escuchando en `8080`,
reinícialo antes de validar `/login` y `/api/v1/auth/*`, porque un proceso
levantado antes de estos cambios seguirá sirviendo el router anterior hasta ser
recreado.

Para validaciones limpias también puedes arrancarlo en otro puerto:

```powershell
$env:CONTROL_PLANE_PORT="8091"
python -m src.control_plane.main
```

Variables útiles del Control Plane:

- `CONTROL_PLANE_HOST`
- `CONTROL_PLANE_PORT`
- `CONTROL_PLANE_REPOSITORY=sqlite|memory`
- `CONTROL_PLANE_DB_PATH`
- `CONTROL_PLANE_KEY_FILE`
- `CONTROL_PLANE_MASTER_KEY`
- `CONTROL_PLANE_USERS_JSON`
- `CONTROL_PLANE_USERS_FILE`
- `CONTROL_PLANE_SESSION_KEY`
- `CONTROL_PLANE_SESSION_KEY_FILE`
- `CONTROL_PLANE_SESSION_TTL_SECONDS`
- `CONTROL_PLANE_PUBLIC_URL`: URL pública que la UI usa al generar los comandos de despliegue del worker. Úsala cuando el Control Plane esté detrás de un proxy, en un puerto distinto, o con un dominio. Ejemplo: `https://backups.miempresa.com`. Si no se define, la UI usa la URL del navegador (`window.location`).

Arranque del Worker en modo nativo:

```powershell
$env:CONTROL_PLANE_URL="http://127.0.0.1:8080"
$env:WORKER_RUN_ONCE="false"
$env:WORKER_POLL_INTERVAL_SECONDS="15"
$env:WORKER_HEALTH_PORT="8081"
python -m src.worker_agent.main
```

Variables útiles del Worker:

- `CONTROL_PLANE_URL`
- `WORKER_NAME`
- `WORKER_HOST_NAME`
- `WORKER_VERSION`
- `WORKER_ID`
- `WORKER_LABELS`
- `BACKUP_RUNTIME_IMAGE`
- `WORKER_RUNTIME=docker|kubernetes|native`
- `WORKER_RUN_ONCE`
- `WORKER_POLL_INTERVAL_SECONDS`
- `WORKER_HEALTH_HOST`
- `WORKER_HEALTH_PORT`
- `WORKER_CREDENTIAL_FILE`
- `CONTROL_PLANE_CA_FILE`
- `WORKER_RUNTIME_IDLE_TIMEOUT_SECONDS`: watchdog de inactividad para los runtimes Docker y nativo. Si el proceso de backup no emite ninguna salida (stdout/stderr) durante este tiempo, el worker lo termina y el job falla con `runtime produced no output for N seconds`. Por defecto `900`; se acota entre `30` y `86400`; `0` o vacío lo desactiva. Restic emite una línea de estado por segundo, así que un backup lento pero vivo nunca lo dispara.
- `WORKER_RUNTIME_TIMEOUT_SECONDS`: límite opcional de duración total (wall clock) por job. **Sin valor por defecto**: un backup de varias horas se mantiene `in_progress` mientras siga produciendo salida. Defínelo solo si necesitas un tope absoluto (máximo `86400`); el job falla con `runtime timed out after N seconds`. Los restores conservan su propio límite (`timeout_seconds` en el payload, 6 h por defecto).

### Worker nativo empaquetado (V2 seguro)

La opción recomendada para nuevos workers nativos es el flujo V2 seguro desde la UI: **Workers → Agregar worker → Worker nativo**. La UI mantiene Docker Compose como opción por defecto, pero permite elegir explícitamente la plataforma del host remoto: Linux amd64, Linux arm64 o Windows x64. No se infiere desde el navegador.

El modelo es el mismo en ambas plataformas, al estilo `tailscaled` + `tailscale up`:

1. **Instala el paquete.** El instalador registra y arranca el servicio del SO. El servicio espera el enrolamiento sin salir: reporta `not_ready` en `/healthz` y revisa la credencial en cada ciclo.
2. **Enrola una sola vez** como administrador o root: `vaultline-worker enroll --control-plane-url <url> --bootstrap-token <token>`. La UI genera este comando con el token ya incluido, listo para pegar. Si omites `--bootstrap-token` (y la variable `VAULTLINE_BOOTSTRAP_TOKEN`), el CLI lo pide en un prompt oculto. El comando guarda la credencial durable y la URL del Control Plane (`worker.json`) en el directorio de estado.
3. **Listo.** El servicio toma la credencial en el siguiente ciclo de polling (30 s por defecto) sin reiniciar.

Reglas que no cambian:

- El bootstrap token es de un solo uso y expira según el TTL elegido en la UI (60 minutos por defecto). Pegarlo en el comando `enroll` es aceptable porque deja de servir en cuanto se consume o expira; si quieres, limpia el historial de la terminal después de enrolar. No lo guardes en archivos, scripts, unidades del servicio, variables de entorno persistentes ni logs.
- Si `enroll` falla, el CLI imprime el motivo real ya redactado (por ejemplo `HTTP 400 (code=enrollment_expired)`), conserva el intento pendiente y nombra la salida: con el mismo token puedes reintentar; si el token expiró o ya se usó, genera uno nuevo en la UI y ejecuta antes `vaultline-worker enroll --reset-pending`.
- Descarga el asset desde la UI o desde la [página canónica de releases de Vaultline](https://github.com/DanielRondonGarcia/vaultline/releases) y verifícalo con `SHA256SUMS` cuando esté disponible.
- Restic v0.19.1 and rclone v1.75.2 executables are bundled with native worker packages. The MSI installs them in `C:\Program Files\Vaultline` and the `.deb` in `/usr/lib/vaultline`; the worker uses those copies first and only falls back to the ones embedded in the portable `.exe`. Keep `rclone.conf` and repository credentials in their separate secrets; they are never included in the package. Install the remaining external tools (`tar`, `gpg`, `aws`, `ssh`, and `scp`) separately as required by the backup or storage target.
- V2 seguro separa el bootstrap token de la credencial durable generada por el cliente. V1 legacy con variable de entorno de token sigue documentado solo para workers existentes o despliegues Compose heredados.

| Plataforma | Paquete | Servicio | Estado y credenciales |
|------------|---------|----------|-----------------------|
| Linux amd64/arm64 (`glibc >= 2.17`) | `vaultline-worker_<version>_<arch>.deb` | `docker-volume-backup-worker.service` (usuario `dvb-worker`) | `/var/lib/docker-volume-backup` |
| Windows x64 | `vaultline-worker_<version>_windows-amd64.msi` (instalador) + `.exe` portable para diagnóstico | `VaultlineWorker` (Windows SCM, `LocalSystem`, inicio automático) | `C:\ProgramData\Vaultline\worker` |

Los binarios y el MSI no están firmados: Windows SmartScreen puede advertirlo. Los pasos detallados por plataforma están en [Runner Linux con systemd](#runner-linux-con-systemd) y [Runner Windows como servicio (MSI)](#runner-windows-como-servicio-msi).

Actualizar y desinstalar:

- **Actualizar**: instala el `.deb` o el MSI nuevo encima del anterior. El directorio de estado y la credencial se conservan; no hace falta re-enrolar. En Linux el `postinst` reinicia la unidad si estaba activa; en Windows el MSI detiene y vuelve a arrancar el servicio.
- **Desinstalar**: `sudo apt remove vaultline-worker` detiene y deshabilita la unidad; el MSI (Panel de control o `msiexec /x`) elimina el servicio y el ejecutable. Ninguno borra el directorio de estado, así que una reinstalación posterior arranca ya enrolada. Bórralo a mano si quieres retirar la credencial del host y revoca el worker en la UI.

### Worker nativo para filesystem

Esta sección cubre la ejecución desde un checkout del repositorio (desarrollo o
depuración); los paquetes publicados usan el mismo CLI bajo el nombre
`vaultline-worker`. El wrapper para respaldos de filesystem nativo es:

```powershell
python -m src.worker_agent.cli daemon
```

Ese comando corre en primer plano, selecciona `WORKER_RUNTIME=native` y usa
polling continuo por defecto (`WORKER_RUN_ONCE=false`). Para depuración manual y
sin instalar un runner del SO puedes ejecutar una sola iteración explícita:

```powershell
python -m src.worker_agent.cli daemon --once
```

Diagnóstico local redacted:

```powershell
python -m src.worker_agent.cli self-check
```

The self-check reports configuration presence and executable availability
without printing environment values, URLs, tokens, credentials, signatures, or
credential-file contents. In source/development mode, executables (`restic`,
`rclone`, `gpg`, `aws`, `ssh`, `scp`, and `tar`) are resolved from the host PATH;
not all are required for every target, as requirements depend on the storage
profile and enabled operations.

Configuración recomendada:

- en un checkout de desarrollo, guarda `CONTROL_PLANE_URL`, `WORKER_NAME`,
  `WORKER_CREDENTIAL_FILE` y otros valores no secretos en un archivo de entorno
  protegido (los paquetes publicados toman la URL de `worker.json`, escrito por
  `enroll`; el entorno siempre gana si defines ambos);
- expón `WORKER_ENROLLMENT_TOKEN` (flujo V1 legacy) solo durante el primer
  registro: es de un solo uso y debe retirarse del archivo de arranque después
  de que el worker guarde sus credenciales;
- no escribas tokens, contraseñas, secretos Restic/Rclone ni contenido de
  credenciales en unidades systemd, tareas programadas, scripts compartidos o
  logs;
- protege el archivo de credenciales, el archivo de entorno y el estado local
  con permisos restrictivos (`chmod 600` y dueño del usuario del worker en
  Linux; ACLs equivalentes en Windows);
- concede al usuario del worker lectura en cada `filesystem_path` de origen y
  escritura solo donde el flujo lo requiera (por ejemplo repositorio local,
  cache, restore o estado del worker).

#### Runner Linux con systemd

El paquete `.deb` hace todo el trabajo de instalación: crea el usuario
`dvb-worker`, instala `/usr/bin/vaultline-worker` junto con `restic` y `rclone`
en `/usr/lib/vaultline`, prepara `/var/lib/docker-volume-backup` y habilita y
arranca `docker-volume-backup-worker.service`, que espera el enrolamiento.

La UI (**Workers → Agregar worker → Worker nativo**) genera este bloque con la
URL del asset y el token ya rellenados; se pega completo como root y queda
listo. Descarga, instala (el servicio queda arrancado, esperando el
enrolamiento), enrola y muestra el estado:

```bash
curl -fsSL -o vaultline-worker_<version>_amd64.deb https://github.com/DanielRondonGarcia/vaultline/releases/download/<version>/vaultline-worker_<version>_amd64.deb
sudo dpkg -i vaultline-worker_<version>_amd64.deb
sudo vaultline-worker enroll --control-plane-url https://backups.example.com --bootstrap-token <token>
sudo systemctl status docker-volume-backup-worker.service --no-pager
```

`enroll` escribe `worker_credentials.json` y `worker.json` en
`/var/lib/docker-volume-backup` con dueño `dvb-worker`; el servicio toma la
credencial en el siguiente ciclo (30 s). Para seguir los logs:
`journalctl -u docker-volume-backup-worker.service -f` o
`vaultline-worker service status`.

`/etc/docker-volume-backup/worker.env` es **opcional**. Créalo solo para
overrides no secretos (`WORKER_NAME`, `WORKER_POLL_INTERVAL_SECONDS`,
`WORKER_HEALTH_PORT`, `CONTROL_PLANE_CA_FILE`, ...). El entorno siempre gana
sobre `worker.json`, así que si defines `CONTROL_PLANE_URL` ahí, esa URL es la
que usa el servicio. El paquete deja un ejemplo en
`/usr/share/doc/vaultline-worker/worker.env.example`. No escribas el token ni
ningún secreto permanente en `worker.env` ni en la unidad.

El sandbox de systemd usa `ProtectSystem=full` y `ProtectHome=read-only`: el
usuario del worker debe conservar lectura sobre cada `filesystem_path` de origen,
pero solo puede escribir en las rutas listadas en `ReadWritePaths`
(`/var/lib/docker-volume-backup`). Para restore a destinos bloqueados por
`ProtectHome` o `ProtectSystem`, añade únicamente el destino aprobado (por
ejemplo `/srv/app-restore`) a `ReadWritePaths` con un drop-in
(`systemctl edit docker-volume-backup-worker.service`); no deshabilites el
sandbox globalmente por defecto.

Sin paquete (checkout del repositorio): la misma unidad está en
`deploy/worker/native/docker-volume-backup-worker.service`. Cópiala a
`/etc/systemd/system/`, ajusta `User`, `Group`, `WorkingDirectory`,
`ExecStart` y `ReadWritePaths`, crea los directorios y habilítala a mano:

```bash
sudo install -d -m 0700 -o dvb-worker -g dvb-worker /var/lib/docker-volume-backup
sudo install -d -m 0750 /etc/docker-volume-backup
sudo systemctl daemon-reload
sudo systemctl enable --now docker-volume-backup-worker.service
```

#### Runner Windows como servicio (MSI)

El MSI instala `C:\Program Files\Vaultline\vaultline-worker.exe` junto con
`restic.exe` y `rclone.exe`, añade esa carpeta al `PATH` del sistema, crea
`C:\ProgramData\Vaultline\worker` y
registra y arranca el servicio `VaultlineWorker` (Windows SCM, cuenta
`LocalSystem`, inicio automático). No usa wrappers de terceros. El servicio
espera el enrolamiento escribiendo "Waiting for enrollment" en
`C:\ProgramData\Vaultline\worker\logs\worker.log`.

La UI genera este bloque con la URL del asset y el token ya rellenados; se pega
completo en una PowerShell **elevada** (Run as administrator). Descarga el MSI,
lo instala en silencio esperando a que termine (SmartScreen puede advertir: el
MSI no está firmado), recarga el `PATH` de la sesión (el MSI añade
`C:\Program Files\Vaultline` al `PATH` del sistema, pero la consola abierta no
lo ve hasta recargarlo), excluye el `rclone.exe` instalado de Microsoft Defender
(ver [Defender termina rclone](#defender-termina-rclone-a-mitad-del-backup)),
enrola y muestra el estado:

```powershell
$msi = "$env:TEMP\vaultline-worker_<version>_windows-amd64.msi"
Invoke-WebRequest -Uri https://github.com/DanielRondonGarcia/vaultline/releases/download/<version>/vaultline-worker_<version>_windows-amd64.msi -OutFile $msi
Start-Process msiexec.exe -ArgumentList ('/i "{0}" /qn /norestart' -f $msi) -Wait
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
Add-MpPreference -ExclusionPath "C:\Program Files\Vaultline\rclone.exe"
vaultline-worker enroll --control-plane-url https://backups.example.com --bootstrap-token <token>
vaultline-worker service status
```

El servicio toma la credencial en el siguiente ciclo (30 s). Para ver los logs:
`Get-Content C:\ProgramData\Vaultline\worker\logs\worker.log -Tail 50`.

`service status` imprime JSON con el estado del servicio y la ruta `log_file`.
`enroll` escribe `worker_credentials.json` y `worker.json` (URL del Control
Plane) en `C:\ProgramData\Vaultline\worker` y concede acceso a `SYSTEM` y
`Administradores` con ACLs por SID, de modo que el servicio en `LocalSystem`
pueda leerlos. Para variables no secretas (`WORKER_NAME`,
`WORKER_POLL_INTERVAL_SECONDS`, ...) usa variables de entorno de sistema; el
entorno siempre gana sobre `worker.json`. Reinicia el servicio después de
cambiarlas: `vaultline-worker service stop` y `vaultline-worker service start`.

Actualizar: instala el MSI nuevo encima del anterior; el estado se conserva y no
hace falta re-enrolar. Desinstalar: `msiexec /x vaultline-worker_<version>_windows-amd64.msi /qn`
(o Panel de control) elimina el servicio y el ejecutable, pero conserva
`C:\ProgramData\Vaultline\worker`.

##### Defender termina rclone a mitad del backup

**Síntoma**: un job con repositorio `rclone:` falla con
`rclone stdio connection already closed` y no aparece ninguna línea `rclone:`
en stderr: Restic pierde a su hijo rclone sin que rclone llegue a reportar un
error propio.

**Confirmación**: la detección de comportamiento `Behavior:Win32/SuspRclone.A`
de Microsoft Defender termina el proceso (`rclone.exe` sin firmar, lanzado
desde el servicio en Session 0 y abriendo conexiones FTP/SFTP). Queda
registrada con los eventos 1116 (detección) y 1117 (acción tomada):

```powershell
Get-WinEvent -FilterHashtable @{ LogName = 'Microsoft-Windows-Windows Defender/Operational'; Id = 1116, 1117 } | Where-Object { $_.Message -match 'SuspRclone' } | Select-Object TimeCreated, Id, Message
```

**Solución**: excluye únicamente el `rclone.exe` instalado. El bloque de
instalación ya lo hace; si instalaste sin él, ejecútalo en una PowerShell
elevada:

```powershell
Add-MpPreference -ExclusionPath "C:\Program Files\Vaultline\rclone.exe"
```

Desde esta versión `restic.exe` y `rclone.exe` viven en
`C:\Program Files\Vaultline`, una ruta estable que sí se puede excluir. Las
versiones anteriores extraían rclone a `%TEMP%\_MEIxxxx`, una carpeta distinta
en cada arranque del servicio: ninguna exclusión por ruta la cubría. Si vienes
de una de esas versiones, instala primero el MSI nuevo y luego añade la
exclusión; no excluyas carpetas temporales ni todo `C:\Program Files\Vaultline`.

##### El repositorio Restic queda bloqueado tras un job terminado a la fuerza

**Síntoma**: el backup o la retención fallan con
`unable to create lock in backend: repository is already locked by PID … lock was created at … (<edad> ago)`.
Un `restic` terminado a mitad (Defender, reinicio, kill del servicio) deja su
lock huérfano en el repositorio; `forget --prune` necesita un lock exclusivo
y falla aunque `backup` siga pasando.

**Solución**: automática. El worker detecta ese error en `snapshots`,
`backup` y `forget`, registra
`Restic repository is locked (lock created <edad> ago); running 'restic unlock' to clear stale locks`,
ejecuta `restic unlock` (nunca `--remove-all`: solo retira locks que Restic
considera obsoletos) y reintenta el comando una única vez. Si el segundo
intento sigue bloqueado, el job falla con el mensaje original de Restic: el
lock pertenece a un `restic` vivo en este u otro host; espera a que termine o
libéralo allí.

##### Alternativa: ejecutable portable

El `.exe` portable sigue publicándose para dos usos:

- **Diagnóstico en primer plano**: `.\vaultline-worker.exe daemon` (o
  `daemon --once` para un solo ciclo) desde una consola. Detén antes el
  servicio si ya está instalado para no correr dos workers con la misma
  credencial.
- **Registro manual del servicio** cuando no puedes usar el MSI: desde una
  PowerShell elevada, `.\vaultline-worker.exe service install` registra
  `VaultlineWorker` apuntando al ejecutable en su ubicación actual (no lo
  muevas después) y `service uninstall` lo retira. Luego sigue con `enroll`
  igual que arriba.

El `.exe` tampoco está firmado: SmartScreen puede advertirlo. Aplica ACLs
restrictivas al directorio de estado y a la credencial si los colocas fuera de
`C:\ProgramData\Vaultline\worker`.

El worker publica `GET /healthz` con un payload similar a:

```json
{
  "ok": true,
  "status": "ok",
  "registered": true,
  "control_plane_reachable": true,
  "worker_id": "...",
  "last_successful_control_plane_contact_at": "2026-08-11T18:00:00+00:00"
}
```

Interpretación:

- `ok=true`: el proceso del worker está vivo y el endpoint responde.
- `control_plane_reachable=true|false`: indica si el último intento de contacto
  con el CP fue exitoso.
- `status=degraded`: el worker sigue vivo pero la conectividad con el CP falló
  en el último ciclo.

Flujo mínimo nativo:

1. Arrancar el Control Plane.
2. Arrancar el Worker.
3. Confirmar workers registrados:

```powershell
curl http://127.0.0.1:8080/api/v1/workers
```

4. Consultar inventario:

```powershell
curl http://127.0.0.1:8080/api/v1/workers/<worker_id>/inventory
```

## 3. Autenticación y RBAC

La UI y los endpoints humanos exigen autenticación.

Si no defines `CONTROL_PLANE_USERS_JSON`, el Control Plane crea un archivo local
`.control_plane.users.json` con un usuario bootstrap:

- usuario: `admin`
- contraseña inicial: `changeme`
- rol: `admin`
- cambio de contraseña: obligatorio en el primer login

Roles soportados:

- `admin`
- `operator`
- `viewer`

Permisos por rol en esta fase:

- `viewer`: solo lectura.
- `operator`: lectura + acciones operativas.
- `admin`: lectura + acciones operativas + administración.

Configuración recomendada por variable de entorno:

```powershell
$env:CONTROL_PLANE_USERS_JSON='[
  {"username":"admin","password":"admin-seguro","role":"admin"},
  {"username":"operator1","password":"operator-seguro","role":"operator"},
  {"username":"viewer1","password":"viewer-seguro","role":"viewer"}
]'
```

Uso recomendado: dejar que el usuario bootstrap exista solo para el arranque
inicial y reemplazar la contraseña inmediatamente al primer acceso.

Ejemplo mínimo para obtener una cookie de sesión y reutilizarla en pruebas
manuales:

```powershell
curl -c cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/auth/login `
  -H "Content-Type: application/json" `
  -d "{\"username\":\"admin\",\"password\":\"changeme\"}"

curl -b cp-cookie.txt http://127.0.0.1:8080/api/v1/auth/me

curl -b cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/auth/change-password `
  -H "Content-Type: application/json" `
  -d "{\"current_password\":\"changeme\",\"new_password\":\"AdminSeguro123\"}"

curl -b cp-cookie.txt http://127.0.0.1:8080/api/v1/workers
```

### Administración local de usuarios

Cuando el Control Plane usa `CONTROL_PLANE_USERS_FILE` o el archivo bootstrap
`.control_plane.users.json`, el rol `admin` puede gestionar usuarios locales
desde la UI o la API.

Capacidades actuales:

- listar usuarios locales,
- crear usuarios locales,
- reasignar rol,
- marcar o desmarcar cambio obligatorio de contraseña,
- resetear contraseña y forzar nuevo cambio en el siguiente login.

Si en cambio defines `CONTROL_PLANE_USERS_JSON`, la autenticación queda
respaldada por variables de entorno y la administración local se muestra en modo
solo lectura para dejar claro que no hay persistencia editable desde la UI.

Listar usuarios:

```powershell
curl -b cp-cookie.txt http://127.0.0.1:8080/api/v1/admin/users
```

Crear usuario:

```powershell
curl -b cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/admin/users `
  -H "Content-Type: application/json" `
  -d "{\"username\":\"operator1\",\"password\":\"OperatorTemp123\",\"role\":\"operator\",\"must_change_password\":true}"
```

Actualizar rol o bandera de cambio obligatorio:

```powershell
curl -b cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/admin/users/operator1/update `
  -H "Content-Type: application/json" `
  -d "{\"role\":\"viewer\",\"must_change_password\":true}"
```

Resetear contraseña temporal:

```powershell
curl -b cp-cookie.txt -X POST http://127.0.0.1:8080/api/v1/admin/users/operator1/reset-password `
  -H "Content-Type: application/json" `
  -d "{\"new_password\":\"ViewerTemp123\",\"must_change_password\":true,\"role\":\"viewer\"}"
```

## 4. UI

Una vez levantado el Control Plane, la UI queda disponible en:

- `http://127.0.0.1:8080/`
- `http://127.0.0.1:8080/ui`

La UI actual permite:

- ver workers,
- ver targets,
- ver jobs,
- administrar usuarios locales del Control Plane cuando la autenticación usa
  archivo,
- crear secretos, storage profiles, políticas de retención y targets desde la
  misma UI,
- ver validación, snapshots y stats de un target,
- disparar backup,
- disparar sync de snapshots,
- disparar sync de stats,
- disparar retención,
- disparar restore dry-run.

Con rol `admin`, la UI también expone formularios administrativos para crear
secretos, storage profiles, políticas de retención y targets nuevos.

Notas prácticas de los formularios:

- los campos avanzados aceptan `JSON` válido,
- `volume_targets` admite una ruta por línea o separadas por comas,
- el formulario de targets usa los workers, storage profiles y políticas ya
  registrados para poblar sus selectores,
- los secretos listados en UI muestran solo metadatos públicos; el valor
  cifrado nunca se devuelve,
- si un formulario falla por un `JSON` inválido, la UI lo notifica antes de
  enviar una carga inconsistente al backend.

Avisos de versión: el banner de actualización de la UI y la marca
«Desactualizado» de cada worker aparecen solo cuando la versión publicada
conocida es numéricamente superior a la instalada (se comparan mayor, menor y
patch). Con una versión igual o más nueva instalada no hay aviso, aunque los
datos del release conocido fueran más antiguos; sin datos de versión válidos
tampoco se muestra un aviso de actualización. La versión del worker sigue visible.

## 5. Configuración de backup

### Crear un target de backup

Ejemplo orientado al runtime actual del proyecto:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"grafana-backup\",\"worker_id\":\"<worker_id>\",\"compose_project\":\"grafana\",\"volume_targets\":[\"/backup/grafana-data\"],\"backup_mode\":\"hot\",\"backup_strategy\":\"restic\",\"runtime_image\":\"ghcr.io/danielrondongarcia/vaultline\",\"runtime_environment\":{\"BACKUP_STRATEGY\":\"restic\",\"RESTIC_REPOSITORY\":\"/repo\",\"RESTIC_PASSWORD\":\"change-me\",\"BACKUP_SOURCES\":\"/backup/grafana-data\"},\"runtime_volumes\":{\"grafana-data\":{\"bind\":\"/backup/grafana-data\",\"mode\":\"ro\"},\"restic-repo\":{\"bind\":\"/repo\",\"mode\":\"rw\"}}}"
```

### Crear secretos y storage profiles

Crear un secreto:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/secrets `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"restic-password\",\"scope\":\"global\",\"secret_type\":\"env\",\"plaintext\":\"change-me\"}"
```

Crear un secreto de archivo para `rclone.conf`:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/secrets `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"rclone-config\",\"scope\":\"global\",\"secret_type\":\"file\",\"plaintext\":\"[remote]\\ntype = s3\\nprovider = AWS\"}"
```

Crear un `storage profile` usando referencias a secretos:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/storage-profiles `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"s3-restic\",\"backend_type\":\"restic-rclone\",\"environment\":{\"RESTIC_REPOSITORY\":\"rclone:remote:/docker-backups\",\"RCLONE_CONFIG\":\"/run/secrets/rclone.conf\"},\"secret_refs\":{\"RESTIC_PASSWORD\":\"<secret_id_password>\"},\"file_secret_refs\":{\"/run/secrets/rclone.conf\":\"<secret_id_rclone_conf>\"}}"
```

Listar `storage profiles`:

```powershell
curl http://127.0.0.1:8080/api/v1/storage-profiles
```

Listar secretos:

```powershell
curl http://127.0.0.1:8080/api/v1/secrets
```

### Crear política de retención

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/retention-policies `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"default-retention\",\"keep_daily\":7,\"keep_weekly\":4,\"keep_monthly\":12,\"keep_yearly\":1,\"prune\":true}"
```

Listar políticas:

```powershell
curl http://127.0.0.1:8080/api/v1/retention-policies
```

### Asociar el target a un storage profile y política de retención

Al crear el target, enviar `storage_profile_id` y opcionalmente
`retention_policy_id`:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets `
  -H "Content-Type: application/json" `
  -d "{\"name\":\"grafana-backup\",\"worker_id\":\"<worker_id>\",\"storage_profile_id\":\"<profile_id>\",\"retention_policy_id\":\"<policy_id>\",\"volume_targets\":[\"/backup/grafana-data\"],\"runtime_image\":\"ghcr.io/danielrondongarcia/vaultline\",\"runtime_volumes\":{\"grafana-data\":{\"bind\":\"/backup/grafana-data\",\"mode\":\"ro\"}}}"
```

### Validar configuración de un target

La validación revisa configuración mínima del runtime, Restic, storage profile
y montaje esperado para restore.

```powershell
curl http://127.0.0.1:8080/api/v1/targets/<target_id>/validate
```

## 6. Operaciones

### Disparar backup para un target

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/backup `
  -H "Content-Type: application/json" `
  -d "{\"requested_by\":\"admin\"}"
```

### Sincronizar snapshots de un target

Encola un job para ejecutar `restic snapshots --json` en el worker y persistir
el catálogo en el Control Plane.

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/snapshots-sync `
  -H "Content-Type: application/json" `
  -d "{\"requested_by\":\"admin\"}"
```

Consultar snapshots catalogados:

```powershell
curl http://127.0.0.1:8080/api/v1/targets/<target_id>/snapshots
```

### Sincronizar stats del target

Encola un job para ejecutar `restic stats --mode raw-data --json`.

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/stats-sync `
  -H "Content-Type: application/json" `
  -d "{\"requested_by\":\"admin\"}"
```

Consultar stats persistidos:

```powershell
curl http://127.0.0.1:8080/api/v1/targets/<target_id>/stats
```

### Ejecutar retención

Encola un job para ejecutar `restic forget` con la política configurada para el
target.

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/retention-run `
  -H "Content-Type: application/json" `
  -d "{\"requested_by\":\"admin\"}"
```

### Restore remoto

Dry-run:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/restore/dry-run `
  -H "Content-Type: application/json" `
  -d "{\"requested_by\":\"admin\",\"restore_source\":\"latest\",\"restore_target_path\":\"/restore-target\",\"layout\":\"auto\",\"stop_containers\":true}"
```

Restore real:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/targets/<target_id>/restore/run `
  -H "Content-Type: application/json" `
  -d "{\"requested_by\":\"admin\",\"restore_source\":\"latest\",\"restore_target_path\":\"/restore-target\",\"force_overwrite\":true,\"stop_containers\":true}"
```

### Encolar un job genérico al worker

Self-check:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/workers/<worker_id>/jobs `
  -H "Content-Type: application/json" `
  -d "{\"command\":\"worker.self_check\",\"requested_by\":\"admin\"}"
```

Refrescar inventario:

```powershell
curl -X POST http://127.0.0.1:8080/api/v1/workers/<worker_id>/jobs `
  -H "Content-Type: application/json" `
  -d "{\"command\":\"inventory.refresh\",\"requested_by\":\"admin\"}"
```

### Consultar jobs

El listado siempre es paginado (25 por defecto, máximo 200) y acepta filtros y orden en el servidor:

```powershell
curl "http://127.0.0.1:8080/api/v1/jobs?limit=50&status=failed&sort=started_at&order=desc"
```

### Read model del dashboard (stats y targets)

`GET /api/v1/jobs/stats` y `GET /api/v1/targets` se sirven desde snapshots "last-good" del dashboard, no desde la agregación en caliente:

- Un único hilo de mantenimiento interno refresca por defecto cada 5 segundos (`CONTROL_PLANE_READ_MODEL_REFRESH_SECONDS`) solo las claves activas/cargadas de forma coalescente (máximo un refresco pendiente/en curso por clave). La cola del read model nunca crea jobs (ni de backup ni `stats.get`) ni toca la cola de trabajos del worker.
- Las lecturas exitosas conservan los cuerpos legados (`{total, by_status, by_day}` y `{items}`) y añaden `X-Read-Model-Updated-At`, `X-Read-Model-Age-Seconds` y `Cache-Control: no-store`; un desfase de unos segundos de frescura es esperado y no crítico.
- Mientras el modelo arranca (o si un refresco falla y aún no hay snapshot) la respuesta es `503` con `code: "read_model_warming_up"` y cabecera `Retry-After`; nunca se devuelven datos falsos vacíos. Si un refresco falla con snapshot previo, se sigue sirviendo el último dato bueno sin bloqueo.
- Al reiniciar el Control Plane los snapshots persistidos en SQLite (`dashboard_read_snapshots`) se hidratan antes de servir: la convergencia del arranque no requiere recargar el navegador.

#### Nota operativa: dashboard y targets en la interfaz (T3)

- La UI aplica de forma independiente workers, targets, jobs recientes, estadísticas y demás widgets: cada componente se pinta a medida que responde su propia petición, sin esperas cruzadas ni bloqueo del arranque inicial.
- Mientras el read model está en warm-up la UI muestra `"Preparando estadísticas..."` (dashboard) y `"Preparando targets..."` (tabla de targets); nunca muestra ceros/listas falsas en lugar de datos pendientes. Reintenta automáticamente según la cabecera `Retry-After` (cadencia acotada, sin recargar el navegador) y conserva el último dato bueno ante errores transitorios.
- Con datos del snapshot la UI muestra la frescura como `"Actualizado hace N s (datos de fecha hora)"` usando las cabeceras `X-Read-Model-Updated-At` / `X-Read-Model-Age-Seconds`; la edad avanza cada segundo mientras el dashboard o la tabla estén visibles. El dashboard consulta stats/jobs recientes y targets cada 5 segundos, sin duplicar peticiones pendientes ni reintentos de arranque; la tabla de targets conserva su sondeo de 3 segundos. Los sondeos se pausan al navegar fuera de su vista (todos los timers del read model se detienen al descargar la página).
- Solo stats y targets/lista son snapshot: crear/editar/eliminar targets, backups manuales, syncs y la gestión de usuarios/secretos/canales siguen siendo llamadas live al backend.

## 7. Enrolamiento HMAC y transporte

HMAC usa secreto de un solo uso, digest en CP y archivo `0600`; no hay
URLs/logs con secretos, registro abierto, certificados de cliente, CSR, huellas
ni fallback. Variables: `WORKER_ENROLLMENT_TOKEN`, `WORKER_CREDENTIAL_FILE`,
`WORKER_ID`, `CONTROL_PLANE_URL` y `CONTROL_PLANE_CA_FILE` para HTTPS server-only.
HTTP local no es confidencial; tras cinco fallos el worker termina.
