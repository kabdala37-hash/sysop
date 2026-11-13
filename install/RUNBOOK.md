# Runbook de instalación — sysop-agent

> **Cambio del 2026-08-28.** La configuración se partió en tres capas
> (`config/so.yaml`, `config/app.yaml`, `config/hosts/<host>.yaml`), el panel web
> del puerto 11001 se dio de baja en favor de `sysop-agent estado`, y el informe
> periódico pasó a dispararse por `/etc/cron.d/sysop-agent` en vez de por el
> daemon. Donde este runbook diga otra cosa, manda esta nota.

Qué se hace en un servidor para dejarlo listo, qué se verificó, y cómo replicarlo.

**Ejecutado en srv3 el 2026-08-27.** Pendiente en srv2.

Todo lo de acá lo hace `install.sh`. Este documento explica **por qué**, para que
el día que el script falle en un servidor distinto se sepa qué está intentando.

---

## Resumen

El instalador deja tres cosas: un usuario de sistema sin shell, cuatro
directorios con permisos separados, y un usuario de MySQL de solo lectura con
los privilegios exactos que los controles necesitan.

No instala el servicio ni lo arranca. Eso es fase 1, cuando haya algo que correr.
Y no toca `/opt/plataforma` ni ninguno de sus servicios.

---

## Uso

```bash
# 1. Copiar el repo y el instalador
tar --exclude=venv --exclude=__pycache__ -cf - . \
  | ssh <server> 'rm -rf /tmp/sysop_src && mkdir -p /tmp/sysop_src \
                  && tar -xf - -C /tmp/sysop_src'
scp install/install.sh <server>:/tmp/sysop_install.sh

# 2. Relevar sin tocar nada. Dice qué falta y sale.
ssh <server> 'bash /tmp/sysop_install.sh --host-id <id> --check'

# 3. Aplicar
ssh <server> 'SYSOP_SRC=/tmp/sysop_src bash /tmp/sysop_install.sh \
              --host-id <id> --stage all'

# 4. Arrancar. El instalador NO lo hace: es una decisión explícita.
ssh <server> 'systemctl start sysop-agent && systemctl status sysop-agent'
```

### El `--host-id` no es opcional

Tiene que coincidir con el nombre del YAML en `config/hosts/`. **No se
deduce del hostname**: en srv3 el hostname es `ip-10-0-0-2` y el archivo se
llama `srv3.yaml`. Deducirlo hace que el servicio arranque y muera en bucle.

El instalador verifica que el YAML exista **antes** de instalar la unidad, así
que el error aparece en la consola y no cinco reinicios después en el journal.

Es idempotente: correrlo dos veces no cambia nada. En particular **reusa la
password que ya esté guardada** en vez de generar una nueva, porque regenerarla
dejaría al servicio en marcha sin poder conectarse. Eso importa poco en srv3 y
mucho en srv2.

Etapas sueltas: `--stage base` (usuario y directorios) o `--stage db` (MySQL).

---

## Qué crea

### Usuario de sistema

```
useradd --system --no-create-home --shell /usr/sbin/nologin sysop-agent
```

Sin shell y sin home a propósito: no es una cuenta desde la que se trabaja. Si
alguien logra ejecutar código como `sysop-agent`, no consigue una sesión.

El script verifica además que **no esté en el grupo `docker`**. Pertenecer a ese
grupo es equivalente a ser root, porque quien habla con el socket de Docker puede
montar el filesystem del host dentro de un contenedor privilegiado.

### Directorios

| Ruta | Permisos | Dueño | Por qué |
|---|---|---|---|
| `/etc/sysop-agent/` | 0750 | `root:sysop-agent` | Ahí viven los secretos. Root escribe, el servicio solo lee |
| `/var/lib/sysop-agent/` | 0750 | `sysop-agent:sysop-agent` | Almacén local SQLite |
| `/var/log/sysop-agent/` | 0750 | `sysop-agent:sysop-agent` | Auditoría |
| `/opt/sysop-agent/` | 0755 | `root:root` | Código. El servicio no puede modificarse a sí mismo |

La separación entre quién escribe `/etc` y quién lo lee es lo que hace que un bug
en el observador no pueda reescribir su propia configuración ni sus credenciales.

### Usuario de MySQL

```sql
CREATE USER 'sysop_ro'@'localhost' IDENTIFIED BY '<generada>';
CREATE USER 'sysop_ro'@'127.0.0.1' IDENTIFIED BY '<generada>';

GRANT SELECT ON plataforma.*     TO 'sysop_ro'@...;
GRANT SELECT ON performance_schema.* TO 'sysop_ro'@...;
GRANT PROCESS ON *.*                 TO 'sysop_ro'@...;
```

**Dos hosts, no uno.** En MySQL `'localhost'` matchea las conexiones por socket
Unix; las que llegan por TCP a 127.0.0.1 necesitan su propia entrada. El
observador se conecta por TCP.

**`PROCESS` y `performance_schema` no son opcionales.** La especificación decía
que se otorgaran "únicamente si son imprescindibles". Lo son: verificado el
2026-08-27 contra srv2 con un usuario de solo `SELECT`, `performance_schema` da
error 1142 e `information_schema.processlist` devuelve una fila de las 232
conexiones reales. Sin esto, los controles DB-03 a DB-08 no se pueden
implementar.

Deliberadamente **no** se otorgan: `INSERT`, `UPDATE`, `DELETE`, `FILE`, `SUPER`,
`SHUTDOWN`, `GRANT OPTION`, ni nada sobre `mysql.*`.

### El archivo de entorno

`/etc/sysop-agent/sysop-agent.env`, con permisos `0640 root:sysop-agent`. Lo escribe
el instalador. Contiene la password del usuario de monitoreo.

**El valor de la password vive únicamente ahí.** No está en este repositorio, ni
en la documentación, ni en el historial de comandos: el script la genera con
`openssl rand` y nunca la imprime. Para leerla:

```bash
ssh <server> 'grep SYSOP_DB_PASSWORD /etc/sysop-agent/sysop-agent.env'
```

---

## Verificación

El instalador no se conforma con que los `GRANT` no hayan dado error. Prueba
cuatro cosas contra la base real:

| Prueba | Cómo |
|---|---|
| Conecta | `SELECT 1` con las credenciales generadas |
| Lee `performance_schema` | cuenta hilos; si da 1142, aborta |
| Tiene `PROCESS` | lo busca en `SHOW GRANTS` |
| **No puede escribir** | intenta `CREATE TABLE` y exige que falle |

La última es la más importante y la más fácil de olvidar: si algún día alguien le
suma un `GRANT ALL` de apuro, esta prueba lo detecta antes de que el observador
tenga poder de escritura sobre producción.

`PROCESS` se verifica leyendo el `GRANT`, no contando conexiones. Contarlas es un
test débil: en un servidor con poca carga el usuario ve su propia sesión y el
número da igual con permiso y sin permiso, o sea verde falso. En srv3 fue
exactamente lo que pasó, con "3 de 2 conexiones" pasando trivialmente.

---

## Las tres trampas del servicio

Las tres aparecieron al arrancar el daemon de verdad en srv3, y ninguna se ve
corriendo el código a mano.

### 1. `ProtectSystem=strict` hace que el observador se mienta a sí mismo

El hardening de la unidad remonta el filesystem como read-only **para el
proceso**. Leyendo `/proc/self/mountinfo`, el observador ve su propia vista
restringida:

```
/proc/self/mountinfo  ->  ro,nosuid,relatime      (lo que ve el proceso)
/proc/1/mountinfo     ->  rw,relatime             (lo que pasa de verdad)
```

En el primer arranque abrió **tres incidentes críticos falsos**, uno por mount,
diciendo que el filesystem estaba en solo lectura. Y lo haría en cualquier host,
siempre.

Se lee `/proc/1/mountinfo`, que corre en el namespace real del sistema y es
legible por cualquier usuario.

### 2. Sin el grupo `systemd-journal`, dos controles dan verde falso

`journalctl` sin permisos **no falla con un error claro**: devuelve vacío. Los
controles de OOM y de errores de disco encuentran cero eventos y concluyen `OK`.

Dos cosas se arreglaron a la vez: el instalador agrega el usuario al grupo
(solo lectura sobre el journal), y el collector ahora devuelve un tercer valor
que dice si pudo leer. Si no pudo, el control da `UNKNOWN` con
`JOURNAL_ILEGIBLE`, nunca `OK`.

Es el mismo patrón que ya había aparecido con `PROCESS` en MySQL: **una fuente
que no se puede leer devuelve lo mismo que una fuente vacía**, y hay que
preguntar explícitamente cuál de las dos es.

### 3. `StartLimitIntervalSec` va en `[Unit]`, no en `[Service]`

Puesto en la sección equivocada, systemd lo ignora con un `Unknown key name` en
el journal que es fácil de pasar por alto. El efecto es que el bucle de reinicio
no se corta nunca, que es exactamente lo que esas dos líneas existen para evitar.

## La trampa del TLS

**El observador se conecta a MySQL con TLS.** Va contra la costumbre del repo de
la plataforma, que usa `--skip-ssl` en todos lados. Dos razones, las dos
aprendidas rompiendo esto en srv3 el 2026-08-27:

1. **El cliente de MySQL 8.0.46 de Ubuntu 24.04 no conoce `--skip-ssl`** y aborta
   con `unknown option`. Y `mysql --help` no lista `--ssl-mode`, así que tampoco
   se puede decidir mirando la ayuda. Este mismo problema ya está documentado en
   `validar_deploy.sh` del repo de la plataforma.

2. **Con `caching_sha2_password` una conexión TCP sin canal seguro falla apenas
   rota la credencial:**

   ```
   ERROR 2061 (HY000): Authentication plugin 'caching_sha2_password'
   reported error: Authentication requires secure connection.
   ```

   El servidor pierde el caché al cambiar la password y la primera
   autenticación siguiente exige TLS. O sea: deshabilitar SSL parece funcionar,
   hasta el día que alguien rota la contraseña. Ahí se cae, y el síntoma no
   apunta a la causa.

El servidor ya tiene TLS (`have_ssl = YES`). No hay nada que ganar apagándolo.

---

## Dónde vive cada cosa

```
/opt/sysop-agent/config/   expectativas (versionadas, van a git)
/etc/sysop-agent/          env y secretos (NO se versionan)
```

La separación importa: las expectativas se versionan junto a los controles que
las usan, y los secretos por definición no. El daemon busca las expectativas en
`SYSOP_CONFIG_DIR`, que la unidad fija en `/opt/sysop-agent/config`.

## Estado verificado en srv3

```
usuario sysop-agent         creado, shell nologin, fuera del grupo docker
/etc/sysop-agent            0750 root:sysop-agent
/var/lib/sysop-agent        0750 sysop-agent:sysop-agent
/var/log/sysop-agent        0750 sysop-agent:sysop-agent
/opt/sysop-agent            0755 root:root
sysop_ro              localhost y 127.0.0.1
  conecta                                              OK
  lee performance_schema (39 hilos)                    OK
  tiene PROCESS (verificado en SHOW GRANTS)            OK
  ve conexiones de 3 usuarios distintos                OK
  no puede escribir (rechazado)                        OK
grupo systemd-journal      OK (puede leer el journal)
servicio sysop-agent        active/running, 0 reinicios, 21,6 MiB
  33 controles: 26 OK, 4 UNKNOWN honestos, 3 detenidos por expectativa
  0 incidentes falsos
```

---

## Replicar en srv2

Mismo procedimiento, con cuatro diferencias que importan porque es producción.

**1. Correr primero `--check`, y leer la salida antes de aplicar.** En srv3 se
podía aplicar a ciegas; acá no.

**2. Confirmar que el `mysql` local entra como root.** El instalador lo necesita
para crear el usuario:

```bash
ssh srv2 'mysql -N -e "SELECT CURRENT_USER()"'
```

Si no entra por socket, hay que pasarle credenciales, y conviene hacerlo con un
archivo de opciones y no por línea de comandos.

**3. `MAIL_*` del tenant `acme`.** Verificado el 2026-08-27: ese tenant no
tiene ninguna clave de correo cargada en `client_config_vars`, solo `ACME_DB_*` y
`COACHING_TRANSCRIBER_AGENT`. Sí tiene credencial de Gemini. Sin las claves de
correo el observador no puede notificar.

**4. Completar `config/hosts/srv2.yaml`.** Hoy está incompleto a
propósito: desde el NAS no hay SSH a srv2, así que las unidades esperadas no se
relevaron. Se llena con:

```bash
systemctl list-units --type=service --all --plain
systemctl list-unit-files --type=service --plain | grep -i acme
findmnt -rno TARGET,SOURCE,FSTYPE -t ext4,xfs
```

Mientras esté incompleto, el agente descubre las unidades y las reporta
`UNCLASSIFIED`. Es el comportamiento correcto: no inventa que algo debería estar
prendido. Pero tampoco avisa si se cae, así que completarlo es parte del trabajo,
no un detalle.

---

## Revertir

```bash
mysql -e "DROP USER IF EXISTS 'sysop_ro'@'localhost',
                              'sysop_ro'@'127.0.0.1'"
userdel sysop-agent
rm -rf /etc/sysop-agent /var/lib/sysop-agent /var/log/sysop-agent /opt/sysop-agent
```

Nada de esto afecta a la plataforma: el observador no comparte usuario, ni
directorios, ni base, ni unidad systemd con `plataforma`.

---

## Configuración por servidor

Las unidades esperadas, los mounts y los umbrales **no están en el código**. Viven
en `config/hosts/`:

```
_comun.yaml    lo que vale para toda instalación de plataforma
srv3.yaml       lo propio de desarrollo
srv2.yaml       lo propio de producción
```

Se fusionan con el del host encima del común. Un diccionario se completa; **una
lista se reemplaza**, para que un servidor pueda quitar algo heredado en vez de
solo agregar.

Mover la plataforma a un servidor nuevo es escribir un YAML, no tocar Python. Y
si falta el YAML del host, el observador **falla al arrancar** en vez de asumir
un default: sin declaración no hay nada contra qué comparar, y arrancar igual
sería fingir que observa.

Las unidades que no figuran en ninguna lista no se ignoran: se reportan
`UNCLASSIFIED`. Encontrar algo no es aprobarlo.


---

## Sobrecarga: dónde se va el tiempo

El criterio de aceptación es **1% de un core y 150 MiB**. Con 79 controles en
srv3 el promedio ronda ese 1%, así que no es un margen cómodo y conviene medirlo
en cada servidor nuevo, no darlo por hecho.

Para medirlo, dos ventanas de cinco minutos (una sola de treinta segundos
engaña: los controles de cinco minutos vencen juntos y el número sale del doble
o de la mitad según dónde caiga la ventana):

```bash
a=$(systemctl show sysop-agent -p CPUUsageNSec --value); t0=$(date +%s%N)
sleep 300
b=$(systemctl show sysop-agent -p CPUUsageNSec --value); t1=$(date +%s%N)
python3 -c "print(f'{($b-$a)/($t1-$t0)*100:.2f}% de un core')"
```

Para saber **qué** control es el caro, en vez de adivinar, cronometrar uno por
uno con el mismo contexto que usa el daemon. Lo medido en srv3:

| Control | Costo | Por qué |
|---|---:|---|
| `OPS-08` | 701 ms | full scan de `rc_token_usage`. **Corregido**: enumerar los tenants usa el índice |
| `WRK-01` | 118 ms | llamada HTTP al back |
| `DB-01` | 78 ms | abre su propia conexión TLS a propósito |
| resto | < 55 ms | |

Las tres cosas que bajaron el costo, en orden de impacto:

1. **Respetar la cadencia de cada control.** Sin el planificador, los 33
   controles corrían en cada tick: 1,75%.
2. **Compartir dentro del ciclo.** Una conexión MySQL y una lectura de systemd
   por ciclo, no por control: eran cuatro handshakes TLS y catorce procesos
   `systemctl` por vuelta.
3. **Consultar agregado, no por elemento.** Tres consultas por cada trabajo son
   cincuenta y una por ronda con los diecisiete de producción; dos consultas
   agregadas y repartidas después hacen lo mismo.

Y una que es de la base y no del observador: **`rc_token_usage` no tiene índice
por `fecha_registro`.** `idx_client_fecha` empieza por `client_id`, así que
cualquier consulta que filtre sólo por fecha recorre la tabla entera. Enumerando
los tenants el plan pasa a `range`. Vale para cualquier consulta de la
plataforma sobre esa tabla, no sólo para la del observador.
