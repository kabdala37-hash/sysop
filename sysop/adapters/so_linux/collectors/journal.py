"""Lectura incremental del journal del kernel y de las unidades.

La clave es el cursor. Sin él, cada ciclo volvería a leer las mismas líneas y
reportaría el mismo error para siempre; o habría que filtrar por tiempo, que
pierde eventos cuando el reloj salta o cuando el ciclo se atrasa.

El cursor de systemd es opaco y estable: se guarda el del último mensaje leído
y la próxima corrida arranca justo después. Se persiste en el almacén local, así
que un reinicio del observador no re-reporta lo que ya vio ni se saltea lo que
pasó mientras estaba caído.
"""
from __future__ import annotations

from datetime import datetime, timezone

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable, Optional

log = logging.getLogger("sysop.so.journal")

# Prioridades de syslog: 0 emerg, 1 alert, 2 crit, 3 err, 4 warning.
PRIO_ERROR = 3


@dataclass(frozen=True)
class Mensaje:
    cursor: str
    timestamp_us: int
    prioridad: int
    unidad: str
    mensaje: str

    @property
    def es_error(self) -> bool:
        return self.prioridad <= PRIO_ERROR


def leer(desde_cursor: Optional[str] = None,
         solo_kernel: bool = False,
         unidad: Optional[str] = None,
         prioridad_max: int = 4,
         limite: int = 500,
         timeout_sec: float = 10.0) -> tuple[list[Mensaje], Optional[str], bool]:
    """Mensajes nuevos, el cursor para la próxima vez, y si se pudo leer.

    Devuelve `(mensajes, cursor_nuevo, ok)`. El tercer valor NO es decorativo:
    sin el, "el journal no tiene errores" y "no tengo permiso para leer el
    journal" devuelven lo mismo (una lista vacia) y el control informa OK sin
    haber visto nada. Paso de verdad en srv3: el usuario del servicio no estaba
    en el grupo `systemd-journal` y los controles de OOM y de errores de disco
    daban verde.

    Si no hay nada nuevo, el cursor vuelve sin cambios: nunca se retrocede.

    El límite es una red de contención real. Si el kernel escupe cien mil líneas
    de errores de I/O, leerlas todas colgaría el ciclo y llenaría el almacén
    justo cuando el disco es el problema.
    """
    cmd = ["journalctl", "--output=json", "--no-pager",
           f"--priority={prioridad_max}", f"--lines={limite}"]
    if solo_kernel:
        cmd.append("--dmesg")
    if unidad:
        cmd += ["-u", unidad]
    if desde_cursor:
        cmd += ["--after-cursor", desde_cursor]
    else:
        # Sin cursor previo, sólo lo reciente. Arrancar leyendo el journal
        # entero produciría una avalancha de incidentes históricos en la
        # primera corrida, con cosas que ya se resolvieron hace meses.
        cmd += ["--since", "-10min"]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("journalctl fallo: %s", e)
        return [], desde_cursor, False
    if r.returncode != 0:
        log.warning("journalctl devolvio %d: %s", r.returncode, r.stderr[:200])
        return [], desde_cursor, False

    mensajes: list[Mensaje] = []
    ultimo = desde_cursor
    for linea in r.stdout.splitlines():
        try:
            d = json.loads(linea)
        except json.JSONDecodeError:
            continue
        cur = d.get("__CURSOR")
        if not cur:
            continue
        ultimo = cur
        try:
            prio = int(d.get("PRIORITY", 6))
        except (TypeError, ValueError):
            prio = 6
        mensajes.append(Mensaje(
            cursor=cur,
            timestamp_us=int(d.get("__REALTIME_TIMESTAMP", 0) or 0),
            prioridad=prio,
            unidad=d.get("_SYSTEMD_UNIT") or d.get("SYSLOG_IDENTIFIER") or "",
            mensaje=(d.get("MESSAGE") or "")[:500],
        ))

    if ultimo is None:
        # No hubo mensajes que cumplan el filtro, que es el caso normal en un
        # servidor sano. Sin esto el cursor nunca se establece: cada corrida
        # volvería a barrer los últimos diez minutos, y en cuanto aparezca un
        # error se reportaría una vez por ciclo hasta que salga de esa ventana.
        # Se toma el cursor de la última entrada del journal, aunque no
        # coincida, para dejar la marca de "hasta acá miré".
        ultimo = cursor_actual(solo_kernel, timeout_sec) or desde_cursor

    return mensajes, ultimo, True


def cursor_actual(solo_kernel: bool = False,
                  timeout_sec: float = 5.0) -> Optional[str]:
    """Cursor de la última entrada del journal, sin filtrar por prioridad."""
    cmd = ["journalctl", "--output=json", "--no-pager", "-n", "1"]
    if solo_kernel:
        cmd.append("--dmesg")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    for linea in r.stdout.splitlines():
        try:
            return json.loads(linea).get("__CURSOR")
        except json.JSONDecodeError:
            continue
    return None


# ---------------------------------------------------------------------------
# Cursores persistidos
# ---------------------------------------------------------------------------

def cursor_guardado(conn: Any, clave: str) -> Optional[str]:
    f = conn.execute("SELECT value FROM cursor WHERE key = ?", (clave,)).fetchone()
    return f["value"] if f else None


def guardar_cursor(conn: Any, clave: str, valor: str) -> None:
    conn.execute(
        "INSERT INTO cursor (key, value, updated_at) VALUES (?,?,datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at",
        (clave, valor),
    )


# ---------------------------------------------------------------------------
# Patrones
# ---------------------------------------------------------------------------

# El kernel avisa de un OOM con varias líneas distintas según la versión y según
# si mató un proceso o sólo entró en la rutina. Se buscan varias a propósito:
# quedarse con "Out of memory" pierde los casos de cgroup, que son los que
# aparecen cuando un servicio tiene límite de memoria.
PATRONES_OOM = (
    "out of memory: killed process",
    "oom-killer",
    "memory cgroup out of memory",
    # El reaper aparece DESPUES del kill, y a veces es la unica linea que queda
    # cuando el mensaje original se perdio por rate limiting del journal.
    "oom_reaper",
    # Generico, al final a proposito: cubre variantes de kernels viejos. Solo se
    # aplica al stream del kernel (--dmesg), donde "killed process" practicamente
    # siempre es un OOM. En el journal completo daria falsos positivos.
    "killed process",
)

# Errores de disco y filesystem. `remounting filesystem read-only` está acá
# porque es la consecuencia más grave y la que rompe la aplicación en silencio.
PATRONES_IO = (
    "i/o error",
    "ext4-fs error",
    "xfs (",
    "buffer i/o error",
    "critical medium error",
    "remounting filesystem read-only",
    "failed command: read fpdma",
    "failed command: write fpdma",
    "nvme: ",
    "device offline",
)


def coincide(texto: str, patrones: Iterable[str]) -> Optional[str]:
    t = texto.lower()
    for p in patrones:
        if p in t:
            return p
    return None


# ---------------------------------------------------------------------------
# Lo que cron dice haber lanzado
# ---------------------------------------------------------------------------
# Segunda fuente para los controles de trabajos programados, y la razon es de
# fondo: hasta ahora la unica fuente era `rc_cron_runs`, una tabla que escribe
# **la propia aplicacion observada**. Cuando el observado se calla, un observador
# de una sola fuente no dice "no se", dice "esta roto".
#
# Paso en srv2 el 2026-09-03: los cron.d se habian renderizado dos semanas antes
# de que existiera el envoltorio que escribe el ledger, asi que la tabla estaba
# vacia desde siempre. El observador informo trece veces `NUNCA_SE_LANZO` sobre
# trabajos que corrian perfectamente --los mails de ese mismo dia los mando el
# cron-- y nadie podia distinguir eso de trece trabajos caidos de verdad.
#
# El journal no depende de la aplicacion: lo escribe cron. Por eso responde una
# pregunta que la tabla no puede responder cuando esta vacia.
#
# `sysop-agent` puede leerlo porque esta en el grupo `systemd-journal`. Lo que NO
# sirve es /var/log/syslog: es `syslog:adm 0640` y el observador no esta en `adm`.

# Los CMD de cron son prioridad `info` (6), no error: el filtro por defecto de
# `leer()` (prioridad_max=4) los deja afuera enteros.
PRIORIDAD_INFO = 6

_RE_CMD = None


def disparos_de_cron(desde: datetime, limite: int = 2000,
                     timeout_sec: float = 10.0) -> tuple[list[dict[str, Any]], bool]:
    """Lo que la unidad `cron` lanzo desde `desde`. Devuelve (disparos, ok).

    El `ok` importa tanto como la lista: sin permiso sobre el journal,
    `journalctl` devuelve vacio sin fallar, y "cron no lanzo nada" es
    indistinguible de "no pude mirar". Un control que confunde esas dos cosas es
    justamente el que estamos arreglando.
    """
    global _RE_CMD                                            # noqa: PLW0603
    if _RE_CMD is None:
        import re                                             # noqa: PLC0415
        # `(usuario) CMD (el comando entero)`
        _RE_CMD = re.compile(r"\(([^)]*)\)\s+CMD\s+\((.*)\)\s*$")

    import json as _json                                      # noqa: PLC0415
    import subprocess                                         # noqa: PLC0415

    cmd = ["journalctl", "--output=json", "--no-pager", "--unit=cron",
           "--priority=%d" % PRIORIDAD_INFO,
           "--since=%s" % desde.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
           "--lines=%d" % int(limite)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_sec, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("no se pudo leer el journal de cron: %s", e)
        return [], False
    if p.returncode != 0:
        log.warning("journalctl -u cron devolvio %s: %s",
                    p.returncode, (p.stderr or "")[:160])
        return [], False

    out: list[dict[str, Any]] = []
    for linea in p.stdout.splitlines():
        linea = linea.strip()
        if not linea:
            continue
        try:
            d = _json.loads(linea)
        except ValueError:
            continue
        m = _RE_CMD.search(str(d.get("MESSAGE") or ""))
        if not m:
            continue                       # las de sesion de PAM y demas ruido
        us = d.get("__REALTIME_TIMESTAMP")
        try:
            cuando = datetime.fromtimestamp(int(us) / 1_000_000, tz=timezone.utc)
        except (TypeError, ValueError):
            continue
        out.append({"at": cuando, "usuario": m.group(1), "cmd": m.group(2)})
    out.sort(key=lambda x: x["at"])
    return out, True


def ultimo_disparo(disparos: Iterable[dict[str, Any]], job_id: Any,
                   comando: Optional[str] = None) -> Optional[datetime]:
    """Cuando fue la ultima vez que cron lanzo ESTE trabajo.

    Dos formas de reconocerlo, en orden:

    1. Por `--job-id N`, que es lo que pone el envoltorio `job_runner.py`. Es
       exacta y no se confunde entre trabajos parecidos.
    2. Por el comando, para los cron.d viejos que no pasan por el envoltorio
       --que son justamente los que dejan el ledger vacio y hacen falta acá.

    El comando se compara por su parte distintiva y no entero: en el cron.d va
    envuelto en `bash -lc '...'` y con comillas alrededor de los argumentos, así
    que una igualdad literal no matchearia nunca.
    """
    marca = "--job-id %s " % job_id
    distintivo = _distintivo(comando) if comando else None
    ultimo: Optional[datetime] = None
    for d in disparos:
        cmd = d.get("cmd") or ""
        if marca in cmd + " " or (distintivo and distintivo in cmd):
            if ultimo is None or d["at"] > ultimo:
                ultimo = d["at"]
    return ultimo


def _distintivo(comando: str) -> Optional[str]:
    """La parte del comando que lo identifica: el script y su primer argumento.

    Con el script solo no alcanza: `dept_email_report.py --client_id clientea` y
    `--client_id clienteb` son dos trabajos distintos con el mismo script, y
    confundirlos daria por lanzado uno que no corrio.
    """
    partes = [p for p in (comando or "").split() if p]
    script = next((p for p in partes if p.endswith(".py") or p.endswith(".sh")), None)
    if not script:
        return None
    i = partes.index(script)
    cola = partes[i + 1:i + 3]           # tipicamente `--client_id <tenant>`
    return " ".join([script] + cola) if cola else script
