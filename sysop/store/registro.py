"""Registro en archivo plano de lo que importa. Sobrevive a que se rompa el sqlite.

El almacén SQLite es donde trabaja el observador: mediciones, histéresis,
tendencia. Este archivo es otra cosa: la lista de **eventos** que hay que poder
reconstruir aunque la base esté corrupta, bloqueada o borrada. Qué incidentes se
abrieron y cerraron, qué alertas salieron, qué informes, cuánto se gastó en el
modelo y cuándo falló el propio almacén.

Por qué eventos y no mediciones: la historia completa son ~240.000 filas por día
en srv2, y leer 30 días de eso desde un archivo costaba 20 s de CPU por consulta
(medido el 2026-09-14). Eventos son decenas por día.

Formato: `<STATE_DIR>/registro/AAAA-MM-DD.jsonl`, un archivo por día UTC, una
línea JSON por evento. Se lee con `cat`, `grep` o `tail` sin Python.

TRES DECISIONES QUE NO SON OBVIAS:

- **Un solo `os.write` por línea, con `O_APPEND`.** Lo escriben tres procesos a
  la vez (daemon, bot, informe). Probado con 60.000 líneas concurrentes: ni una
  rota ni mezclada.
- **Nunca lanza.** Si no se puede escribir (disco lleno) la línea queda en un
  buffer en memoria y se reintenta en la próxima escritura. Un registro que
  tumba al que lo llama sería peor que no tenerlo.
- **La lectura incluye lo que quedó en el buffer.** Así, con el disco lleno, el
  mismo proceso sigue viendo lo que anotó: el tope de gasto del modelo y el
  dedupe de alertas se apoyan en eso.

No va en logrotate: un archivo, un solo rotador. `podar()` lo corre el daemon.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sysop import settings

log = logging.getLogger("sysop.registro")

# Quién escribe y dónde. Los fija `configurar()`, que llama cada punto de
# entrada (daemon, bot, cli).
proceso = "sysop"
_estado: Optional[Path] = None

# Cuántas líneas se guardan en memoria mientras no se puede escribir. Con disco
# lleno durante horas, perder las más viejas es mejor que crecer sin techo.
MAX_PENDIENTES = 1000

# Un string largo en el registro es casi siempre un error, y una línea gigante
# deja de ser un solo `write` razonable.
MAX_TEXTO = 400

# Identificadores: van tal cual. La redaccion confunde los digitos de un id con
# un telefono (`INC-a1b2c3d4-20260914T1930` salia `INC-a1b2c3d[telefono]T1930`)
# y el dedupe de alertas dejaba de encontrar nada. Todo lo demas -- `actual`,
# mensajes, asuntos -- pasa por la misma redaccion que lo que va al modelo.
_IDENTIFICADORES = frozenset({
    "incident_id", "fp", "periodo", "tipo", "check_id", "resource_id",
    "severity", "reason_code", "clase", "modelo", "via", "de", "a", "ok",
    "costo_usd", "tokens_in", "tokens_out"})

_lock = threading.Lock()
_pendientes: list[tuple[str, bytes]] = []
_fallando = False


def configurar(nombre: str, estado: Optional[Path] = None) -> None:
    """Activa el registro para este proceso.

    SIN CONFIGURAR NO HACE NADA, ni escribe ni lee, y es a proposito: el
    directorio por defecto es el del servidor (`/var/lib/sysop-agent`), y un test
    corrido como root en srv2 escribiria en el registro real de produccion. Ya
    paso algo parecido con el almacen del bot. Solo lo activan el daemon, el
    bot, la CLI y los tests que lo prueban, con su propio directorio.
    """
    global proceso, _estado
    proceso = nombre
    _estado = Path(estado) if estado else settings.STATE_DIR
    with _lock:
        _pendientes.clear()


def directorio() -> Optional[Path]:
    return _estado / "registro" if _estado is not None else None


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def anotar(evento: str, **datos: Any) -> bool:
    """Agrega un evento. Devuelve si quedó escrito en disco (no solo en memoria)."""
    from sysop.llm.contexto import redactar                 # noqa: PLC0415

    if _estado is None:
        return False
    ahora = _ahora()
    linea = {"at": ahora.isoformat(), "host": settings.HOST_ID,
             "proceso": proceso, "evento": evento}
    for k, v in datos.items():
        linea[k] = v if k in _IDENTIFICADORES else redactar(v, max_texto=MAX_TEXTO)
    b = (json.dumps(linea, default=str, ensure_ascii=False) + "\n").encode()
    with _lock:
        _pendientes.append((ahora.strftime("%Y-%m-%d"), b))
        if len(_pendientes) > MAX_PENDIENTES:
            del _pendientes[0]
            log.warning("buffer del registro lleno: se descarto la linea mas vieja")
        return _vaciar()


def _vaciar() -> bool:
    """Escribe lo pendiente en orden. Se llama con `_lock` tomado."""
    global _fallando
    while _pendientes:
        dia, b = _pendientes[0]
        try:
            _escribir(dia, b)
        except OSError as e:
            if not _fallando:
                # Una vez por racha: con el disco lleno esto se repetiria en
                # cada evento y taparia el log.
                log.error("no se puede escribir el registro (%d linea(s) en "
                          "memoria): %s", len(_pendientes), e)
            _fallando = True
            return False
        del _pendientes[0]
    if _fallando:
        log.info("el registro vuelve a escribirse")
        _fallando = False
    return True


def _escribir(dia: str, b: bytes) -> None:
    d = directorio()
    if d is None:
        raise OSError("el registro no esta configurado")
    nuevo_dir = not d.exists()
    d.mkdir(mode=0o750, parents=True, exist_ok=True)
    if nuevo_dir:
        _al_dueno_del_estado(d)
    ruta = d / f"{dia}.jsonl"
    nuevo = not ruta.exists()
    fd = os.open(str(ruta), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if nuevo:
            _al_dueno_del_estado(ruta, fd)
        os.write(fd, b)
        # Son pocos eventos por dia: el fsync no cuesta nada y un corte de luz
        # no se lleva justo la linea que decia que se mando la alerta.
        os.fsync(fd)
    finally:
        os.close(fd)


def _al_dueno_del_estado(ruta: Path, fd: Optional[int] = None) -> None:
    """Si lo crea root, se lo da al dueño del directorio de estado.

    Un `sysop-agent informe --forzar` corrido a mano como root crearia el archivo
    del dia como root:root 0600, y el daemon (usuario sysop-agent) no podria
    escribir en todo el dia. Es la misma trampa que ya paso con los `-wal` del
    sqlite creados por root.
    """
    if os.geteuid() != 0 or _estado is None:
        return
    try:
        st = os.stat(_estado)
        if fd is not None:
            os.fchown(fd, st.st_uid, st.st_gid)
        else:
            os.chown(ruta, st.st_uid, st.st_gid)
    except OSError as e:
        log.warning("no se pudo dar %s al dueño del estado: %s", ruta, e)


def leer(desde: datetime, eventos: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    """Los eventos desde `desde`, del más viejo al más nuevo.

    Una línea que no es JSON se saltea: es la última de un corte a mitad de
    escritura, y no puede impedir leer todo lo demás.
    """
    d = directorio()
    if d is None:
        return []
    quiero = set(eventos) if eventos else None
    corte = desde.isoformat()
    out: list[dict[str, Any]] = []
    vistas: set[bytes] = set()

    dia = desde.astimezone(timezone.utc).date()
    hoy = _ahora().date()
    while dia <= hoy:
        ruta = d / f"{dia.isoformat()}.jsonl"
        try:
            with open(ruta, "rb") as f:
                for crudo in f:
                    _sumar(crudo, corte, quiero, out)
                    vistas.add(crudo)
        except OSError:
            pass
        dia += timedelta(days=1)

    # Lo que todavia no llego al disco. `vistas` evita contar dos veces una
    # linea que se escribio entre que se leyo el archivo y se miro el buffer.
    with _lock:
        pendientes = [b for _, b in _pendientes]
    for crudo in pendientes:
        if crudo not in vistas:
            _sumar(crudo, corte, quiero, out)

    out.sort(key=lambda e: e.get("at", ""))
    return out


def _sumar(crudo: bytes, corte: str, quiero: Optional[set[str]],
           out: list[dict[str, Any]]) -> None:
    try:
        e = json.loads(crudo)
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(e, dict) or str(e.get("at", "")) < corte:
        return
    if quiero is None or e.get("evento") in quiero:
        out.append(e)


def podar(dias: int) -> int:
    """Borra los archivos de más de `dias` días. Devuelve cuántos borró."""
    limite = (_ahora() - timedelta(days=int(dias))).strftime("%Y-%m-%d")
    borrados = 0
    d = directorio()
    if d is None:
        return 0
    try:
        archivos = list(d.glob("*.jsonl"))
    except OSError:
        return 0
    for a in archivos:
        # El nombre ES la fecha: comparar el texto alcanza y no depende del
        # mtime, que cambia si alguien copia el directorio.
        if a.stem < limite:
            try:
                a.unlink()
                borrados += 1
            except OSError as e:
                log.warning("no se pudo podar %s: %s", a, e)
    return borrados


# ---------------------------------------------------------------------------
# Latido
# ---------------------------------------------------------------------------

def ruta_latido() -> Optional[Path]:
    return _estado / "latido" if _estado is not None else None


def latir() -> None:
    """Lo toca el daemon en cada tick. Nunca lanza.

    Es como el informe y el bot saben si el daemon esta vivo SIN mirar la base.
    Antes lo deducian del ultimo resultado guardado, y con la base borrada eso
    daba "observador caido" con el daemon corriendo. Cambiar el mtime no pide
    bloques libres: sigue andando con el disco lleno.
    """
    r = ruta_latido()
    if r is None:
        return
    try:
        os.utime(r)
    except FileNotFoundError:
        try:
            r.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(str(r), os.O_WRONLY | os.O_CREAT, 0o600))
        except OSError as e:
            log.warning("no se pudo crear el latido %s: %s", r, e)
    except OSError as e:
        log.warning("no se pudo tocar el latido %s: %s", r, e)


def edad_latido() -> Optional[float]:
    """Segundos desde el último latido, o None si no hay archivo."""
    r = ruta_latido()
    if r is None:
        return None
    try:
        return max(0.0, _ahora().timestamp() - os.stat(r).st_mtime)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Consultas que usan los demás módulos
# ---------------------------------------------------------------------------

# Lo que da por terminado un problema a efectos de volver a avisar. `recuperado`
# NO: tiene quince minutos para volver como el mismo incidente.
_CIERRES = ("incidente_cerrado", "emergencia_recuperado")


def alerta_vigente(fp: str, horas: int = 24) -> Optional[dict[str, Any]]:
    """¿Ya se avisó de este problema y sigue abierto según el registro?

    Es el dedupe que sobrevive a perder el sqlite. Si la base se recrea, el
    mismo problema abre un incidente nuevo con otro id, y la bandeja de salida
    (nueva y vacía) lo mandaría otra vez. Medido el 2026-09-14: borrar la base
    con un crítico abierto hacía salir la alerta dos veces.

    `fp` son los 8 primeros caracteres del fingerprint, que es lo que lleva el
    id del incidente. Se recorre la historia EN ORDEN, como una máquina de
    estados: una alerta lo pone vigente, un cierre o una recuperación lo
    apagan, y una reapertura lo vuelve a prender si ya se había avisado.

    `recuperado` cuenta como fin a propósito: si la base se pierde en los quince
    minutos entre recuperado y cerrado, nunca se anota el cierre, y tomar solo
    `cerrado` tapaba la recurrencia por un día (revisión). El precio es un aviso
    de más si el problema vuelve justo en esa ventana, que es preferible a uno
    de menos.
    """
    vigente: Optional[dict[str, Any]] = None
    ultima: Optional[dict[str, Any]] = None
    for e in leer(_ahora() - timedelta(hours=int(horas)),
                  ("correo_enviado", "incidente_recuperado", "incidente_reabierto")
                  + _CIERRES):
        if e.get("fp") != fp:
            continue
        ev = e["evento"]
        if ev == "correo_enviado":
            if e.get("tipo") == "urgente":
                vigente = ultima = e
        elif ev == "incidente_reabierto":
            vigente = ultima
        else:
            vigente = None
    return vigente


def informe_enviado(periodo: str) -> bool:
    """Idempotencia del informe sin la bandeja de salida."""
    for e in leer(_ahora() - timedelta(days=2), ("correo_enviado",)):
        if e.get("tipo") == "informe" and e.get("periodo") == periodo:
            return True
    return False


def gastado_hoy(origen: Optional[str] = None, prefijo_chat: str = "telegram:") -> float:
    """Lo gastado hoy en el modelo según el registro. Mismo criterio que la base."""
    hoy = _ahora().replace(hour=0, minute=0, second=0, microsecond=0)
    total = 0.0
    for e in leer(hoy, ("llm_gasto",)):
        es_chat = str(e.get("incident_id") or "").startswith(prefijo_chat)
        if origen == "diagnostico" and es_chat:
            continue
        if origen == "chat" and not es_chat:
            continue
        try:
            total += float(e.get("costo_usd") or 0.0)
        except (TypeError, ValueError):
            pass
    return total


def intentos_hoy() -> dict[str, int]:
    """Cuántas veces se le preguntó hoy al modelo, por incidente. Una sola lectura."""
    hoy = _ahora().replace(hour=0, minute=0, second=0, microsecond=0)
    out: dict[str, int] = {}
    for e in leer(hoy, ("llm_gasto",)):
        iid = str(e.get("incident_id") or "")
        out[iid] = out.get(iid, 0) + 1
    return out


def abiertos_segun_registro(desde: datetime) -> list[dict[str, Any]]:
    """Los incidentes que el registro da por abiertos. Para cuando no hay base.

    Arma cada uno desde su `incidente_abierto` y lo saca con el cierre. Un
    incidente abierto antes de `desde` no aparece: el registro se lee por
    ventana, y eso se dice en el informe.
    """
    vivos: dict[str, dict[str, Any]] = {}
    for e in leer(desde, ("incidente_abierto", "incidente_recuperado",
                          "incidente_reabierto", "incidente_agravado",
                          "incidente_atenuado") + _CIERRES
                  + ("emergencia_abierto",)):
        iid = e.get("incident_id")
        if not iid:
            continue
        ev = e["evento"]
        if ev in ("incidente_abierto", "emergencia_abierto"):
            vivos[iid] = {"incident_id": iid, "fp": e.get("fp", ""),
                          "check_id": e.get("check_id", ""),
                          "resource_id": e.get("resource_id", ""),
                          "severity": e.get("severity", "warning"),
                          "state": "OPEN", "opened_at": e.get("at", ""),
                          "cause": {"reason_code": e.get("reason_code", ""),
                                    "actual": e.get("actual")}}
        elif iid in vivos:
            if ev in _CIERRES:
                vivos.pop(iid)
            elif ev == "incidente_recuperado":
                vivos[iid]["state"] = "RECOVERED"
            elif ev == "incidente_reabierto":
                vivos[iid]["state"] = "OPEN"
            elif ev in ("incidente_agravado", "incidente_atenuado") and e.get("a"):
                vivos[iid]["severity"] = e["a"]

    # Un problema, una fila. El mismo fingerprint puede estar dos veces: abierto
    # por el motor y despues por el camino de emergencia (el arnes lo conto como
    # dos activos). Gana el del motor, que es el que tiene historia.
    por_fp: dict[str, dict[str, Any]] = {}
    for i in vivos.values():
        clave = i["fp"] or i["incident_id"]
        previo = por_fp.get(clave)
        if previo is None or (previo["incident_id"].startswith("EMG-")
                              and not i["incident_id"].startswith("EMG-")):
            por_fp[clave] = i
    return list(por_fp.values())
