"""Lo que el agente puede hacer por su cuenta cuando le falta información.

El paquete de evidencia lleva lo que ya sabemos que sirve, pero no puede
anticipar todo: un extractor puede no haber capturado la línea que importa, o el
diagnóstico puede depender de algo que sólo se sabe mirando. Para eso el agente
tiene manos, y son de LECTURA:

    listar      qué hay en un directorio
    leer        el contenido de un archivo, con filtro y cola
    telemetria  una lectura fresca del sistema, ahora
    journal     los últimos mensajes de una unidad de systemd

**Puede moverse por donde necesite.** No hay lista de rutas permitidas: hay un
mapa que le dice dónde está parado y qué contiene cada cosa, que es lo que hace
que no ande a ciegas.

Tres límites, y ninguno restringe por dónde puede mirar:

1. **Sólo lectura.** No hay forma de escribir, borrar ni ejecutar.
2. **Los archivos que son puro secreto no se leen**: `.env`, claves privadas,
   `shadow`. No porque el agente no deba mirar ahí, sino porque su contenido
   viajaría al proveedor del modelo en la respuesta, y no hay ningún
   diagnóstico que lo necesite.
3. **Lo que vuelve pasa por el redactor**, igual que el resto de la evidencia.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
from collections import deque
from pathlib import Path
from typing import Any, Optional

from sysop.llm import contexto as ctx_mod

log = logging.getLogger("sysop.llm.tools")

# Estos topes importan mas de lo que parece. En function calling el resultado
# de cada herramienta queda en el historial y se REENVIA en cada vuelta
# siguiente, asi que un log generoso se paga cuatro veces. Medido: con 24 KB por
# lectura, un diagnostico con cuatro consultas costo 28.812 tokens de entrada
# contra 3.106 sin herramientas.
MAX_BYTES = 8_000           # por lectura
MAX_LINEAS = 120
# A partir de aca no se recorre el archivo entero: se mira solo la cola. En el
# server hay logs de 186 MB y journals de 54 MB.
MAX_ARCHIVO_BYTES = 64 * 1024 * 1024
COLA_BYTES = 2 * 1024 * 1024
MAX_ENTRADAS = 120          # por listado

# Archivos cuyo contenido es credencial y nada más. No es una restricción de
# movimiento: es no mandarle secretos a un tercero.
SECRETOS = (
    "*.env", ".env*", "*.key", "*.pem", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.kdbx",
    "shadow", "gshadow", "master.key", "credentials*", ".pgpass", ".my.cnf",
    "*.jwt", "*token*", "*secret*",
    # --- agregados el 2026-08-31, los tres estaban PERMITIDOS ---
    # El almacen del propio observador. Ya no guarda credenciales (la copia
    # local de la config se saco el 2026-09-02), pero sigue vedado: tiene
    # todas las mediciones y es el caso mas facil de pedir sin querer,
    # porque el agente ya sabe donde vive.
    "*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3",
    # `/proc/<pid>/environ`: el entorno del proceso, o sea TODAS sus variables.
    # Ahi viven SYSOP_DB_PASSWORD e INTERNAL_API_KEY.
    "environ",
    # La configuracion de la plataforma que observa. `build/settings.py` tiene
    # JWT_SECRET y la API key, y esta en 0775: legible para cualquiera.
    "settings.py", "settings_local.py", "config.py",
)


def _es_secreto(p: Path) -> bool:
    n = p.name.lower()
    return any(fnmatch.fnmatch(n, pat) for pat in SECRETOS)


def _seguro(ruta: str) -> tuple[Optional[Path], Optional[str]]:
    try:
        p = Path(ruta).expanduser().resolve()
    except (OSError, RuntimeError) as e:
        return None, f"ruta invalida: {e}"
    if _es_secreto(p):
        return None, ("ese archivo contiene credenciales y no se lee. No hace "
                      "falta para diagnosticar: si el problema es de acceso, "
                      "mirar el error del servicio, no la credencial.")
    return p, None


# ---------------------------------------------------------------------------
# Las herramientas
# ---------------------------------------------------------------------------

def listar(ruta: str = "/var/log") -> dict[str, Any]:
    """Qué hay en un directorio, con tamaño y fecha."""
    p, err = _seguro(ruta)
    if err:
        return {"error": err}
    if not p.exists():
        return {"error": f"{p} no existe"}
    if not p.is_dir():
        return {"error": f"{p} no es un directorio", "sugerencia": "usar leer"}
    try:
        hijos = sorted(p.iterdir(), key=lambda x: x.name)[:MAX_ENTRADAS]
    except PermissionError:
        return {"error": f"sin permiso de lectura sobre {p}"}
    except OSError as e:
        return {"error": str(e)[:200]}

    out = []
    for h in hijos:
        try:
            st = h.stat()
            out.append({"nombre": h.name,
                        "tipo": "dir" if h.is_dir() else "archivo",
                        "kib": round(st.st_size / 1024, 1) if h.is_file() else None,
                        "modificado": _iso(st.st_mtime),
                        "secreto": _es_secreto(h) or None})
        except OSError:
            out.append({"nombre": h.name, "tipo": "?", "error": "no se pudo leer"})
    return {"ruta": str(p), "entradas": out, "total": len(out)}


def leer(ruta: str, buscar: Optional[str] = None, lineas: int = 80,
         desde_el_final: bool = True) -> dict[str, Any]:
    """El contenido de un archivo.

    Por defecto trae el final, que es donde está lo reciente. Con `buscar`
    devuelve sólo las líneas que contienen ese texto, que es lo que hay que
    hacer con un log grande: pedir mil líneas de las que novecientas setenta
    dicen que todo salió bien no ayuda a nadie y cuesta tokens.
    """
    p, err = _seguro(ruta)
    if err:
        return {"error": err}
    if not p.exists():
        return {"error": f"{p} no existe"}
    if p.is_dir():
        return {"error": f"{p} es un directorio", "sugerencia": "usar listar"}

    lineas = max(1, min(int(lineas or 80), MAX_LINEAS))

    # NUNCA SE CARGA EL ARCHIVO ENTERO EN MEMORIA. Antes esto hacia
    # `f.readlines()` y recien despues recortaba a 80 lineas, o sea que para
    # devolver 8 KB levantaba el archivo completo. El observador esta en el
    # grupo `systemd-journal`, asi que puede pedir un `.journal` de 54 MB, y en
    # el server hay un `syslog.1` de 186 MB — que como lista de objetos `str` de
    # Python son varias veces eso. srv3 tiene 3,8 GB de RAM, 1,9 disponibles y
    # NO tiene swap: el proceso que vigila el server se lo llevaba puesto.
    #
    # Ahora se itera y se guardan solo las lineas que van a salir (`deque` con
    # tope). La memoria queda acotada por `lineas`, no por el tamaño del
    # archivo.
    try:
        tam = p.stat().st_size
    except OSError as e:
        return {"error": str(e)[:200]}

    parcial = False
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            # Un archivo enorme tampoco se RECORRE entero: mirar 186 MB para
            # devolver 80 lineas es I/O que no le sirve a nadie. Se salta al
            # final y se mira solo la cola. La primera linea despues del salto
            # se descarta: casi seguro esta cortada al medio.
            if tam > MAX_ARCHIVO_BYTES:
                parcial = True
                f.seek(max(0, tam - COLA_BYTES))
                f.readline()

            total = 0
            if buscar:
                aguja = buscar.lower()
                # UN NUMERO SUELTO SE BUSCA COMO PALABRA ENTERA. Medido en srv2
                # el 2026-09-17: el agente busco "401" en el log del sync, le
                # volvieron `quedan 2401s` y `en 401ms`, y diagnostico un 401
                # de Telefonia que no existia.
                entero = (re.compile(r"(?<![0-9a-z])" + re.escape(aguja)
                                     + r"(?![0-9a-z])")
                          if aguja.isdigit() else None)
                sel: deque = deque(maxlen=lineas)
                coincidencias = 0
                for i, l in enumerate(f):
                    total += 1
                    if (entero.search(l.lower()) if entero
                            else aguja in l.lower()):
                        coincidencias += 1
                        sel.append((i + 1, l))
                cuerpo = [f"{n}: {l.rstrip()}" for n, l in sel]
                meta = {"lineas_totales": total, "coincidencias": coincidencias,
                        "mostradas": len(sel), "buscando": buscar}
            elif desde_el_final:
                dq: deque = deque(maxlen=lineas)
                for l in f:
                    total += 1
                    dq.append(l)
                base = total - len(dq)
                cuerpo = [f"{base + i + 1}: {l.rstrip()}" for i, l in enumerate(dq)]
                meta = {"lineas_totales": total, "mostradas": len(dq),
                        "desde_el_final": True}
            else:
                # Las primeras N: se corta apenas se llega, sin leer el resto.
                primeras = []
                for l in f:
                    primeras.append(l)
                    if len(primeras) >= lineas:
                        break
                cuerpo = [f"{i + 1}: {l.rstrip()}" for i, l in enumerate(primeras)]
                meta = {"mostradas": len(primeras), "desde_el_final": False}
    except PermissionError:
        return {"error": f"sin permiso de lectura sobre {p}"}
    except OSError as e:
        return {"error": str(e)[:200]}

    if parcial:
        # Se dice explicito: un recorte silencioso hace que el agente
        # diagnostique sobre un pedazo creyendo que vio todo.
        meta["archivo_mib"] = round(tam / 1048576.0, 1)
        meta["parcial"] = (f"el archivo pesa {meta['archivo_mib']} MiB; se miraron "
                           f"solo los ultimos {COLA_BYTES // 1024} KB")
        meta.pop("lineas_totales", None)

    texto = "\n".join(cuerpo)
    if len(texto) > MAX_BYTES:
        texto = texto[-MAX_BYTES:]
        meta["truncado"] = True
    return {"ruta": str(p), **meta,
            "contenido": ctx_mod.redactar(texto, max_texto=MAX_BYTES)}


def telemetria(que: str = "todo") -> dict[str, Any]:
    """Una lectura del sistema, ahora. No lo que se midió hace un rato."""
    from sysop.adapters.so_linux.collectors import host, net   # noqa: PLC0415

    q = (que or "todo").lower()
    out: dict[str, Any] = {}
    try:
        if q in ("todo", "memoria"):
            m = host.memoria()
            if m:
                out["memoria"] = {k: v for k, v in m.__dict__.items()}
        if q in ("todo", "disco", "discos"):
            out["discos"] = []
            # `Mount.target`, no `destino`. Estuvo mal desde que se escribio y
            # no se noto porque no fallaba: `str(mp)` daba
            # "Mount(target='/', ...)", `os.statvfs` de eso tira OSError,
            # `host.uso` devuelve None y la lista quedaba VACIA sin un error.
            # O sea que el agente no podia ver los discos, justo en HST-09, que
            # es el incidente de disco lleno.
            for mp in host.mounts_administrados(("tmpfs", "devtmpfs", "squashfs",
                                                 "overlay", "proc", "sysfs")):
                punto = getattr(mp, "target", None) or str(mp)
                u = host.uso(punto)
                if u:
                    d = {k: v for k, v in u.__dict__.items()}
                    d.setdefault("mount", punto)
                    out["discos"].append(d)
        if q in ("todo", "carga"):
            out["loadavg"] = host.loadavg()
            out["uptime_seg"] = host.uptime_seg()
        if q in ("todo", "puertos", "red"):
            out["escuchando"] = [
                {"puerto": l.puerto, "ip": l.ip} for l in net.listeners()[:40]]
    except Exception as e:  # noqa: BLE001
        out["error_parcial"] = str(e)[:200]
    return out or {"error": f"no se reconoce {que!r}",
                   "opciones": ["todo", "memoria", "disco", "carga", "puertos"]}


def journal(unidad: Optional[str] = None, lineas: int = 60,
            solo_errores: bool = True) -> dict[str, Any]:
    """Los últimos mensajes del journal, de una unidad o del sistema."""
    import subprocess                                        # noqa: PLC0415

    cmd = ["journalctl", "--no-pager", "-o", "short-iso",
           "-n", str(max(1, min(int(lineas or 60), MAX_LINEAS)))]
    if unidad:
        cmd += ["-u", str(unidad)]
    if solo_errores:
        cmd += ["-p", "4"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": str(e)[:200]}
    if r.returncode != 0:
        return {"error": (r.stderr or "").strip()[:300] or "journalctl fallo"}
    texto = r.stdout.strip()
    if not texto:
        return {"unidad": unidad, "lineas": 0,
                "nota": ("sin mensajes. Si esperabas ver algo, puede que el "
                         "servicio no escriba al journal o que el filtro de "
                         "prioridad los deje afuera: reintentar con "
                         "solo_errores=false")}
    return {"unidad": unidad,
            "contenido": ctx_mod.redactar(texto[-MAX_BYTES:], max_texto=MAX_BYTES)}


def _iso(ts: float) -> str:
    from datetime import datetime, timezone                   # noqa: PLC0415
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


# Lo que se le declara al modelo. Las descripciones son parte del contrato: el
# modelo elige la herramienta leyendo esto, así que dicen para qué sirve cada
# una y no sólo qué hace.
DECLARACIONES = [
    {
        "name": "listar",
        "description": ("Lista un directorio del servidor, con tamano y fecha "
                        "de cada archivo. Usalo para orientarte cuando no sabes "
                        "que archivos hay, por ejemplo antes de leer un log."),
        "parameters": {
            "type": "object",
            "properties": {"ruta": {"type": "string",
                                    "description": "ruta absoluta del directorio"}},
            "required": ["ruta"],
        },
    },
    {
        "name": "leer",
        "description": ("Lee un archivo del servidor. Por defecto trae el final, "
                        "que es lo mas reciente. Pasa `buscar` para traer solo "
                        "las lineas que contienen un texto: en un log grande eso "
                        "es lo que hay que hacer, no leerlo entero."),
        "parameters": {
            "type": "object",
            "properties": {
                "ruta": {"type": "string", "description": "ruta absoluta"},
                "buscar": {"type": "string",
                           "description": ("opcional: solo lineas con este "
                                           "texto. Un numero solo se busca "
                                           "como palabra entera")},
                "lineas": {"type": "integer",
                           "description": "cuantas lineas traer, maximo 300"},
                "desde_el_final": {"type": "boolean",
                                   "description": "true (por defecto) trae el final"},
            },
            "required": ["ruta"],
        },
    },
    {
        "name": "telemetria",
        "description": ("Lectura FRESCA del sistema ahora mismo: memoria, "
                        "discos, carga, puertos escuchando. Distinta de la "
                        "evidencia del incidente, que es de cuando se midio."),
        "parameters": {
            "type": "object",
            "properties": {"que": {"type": "string",
                                   "description": "todo | memoria | disco | carga | puertos"}},
        },
    },
    {
        "name": "journal",
        "description": ("Ultimos mensajes del journal de systemd. Con `unidad` "
                        "los de un servicio puntual. Es lo primero que hay que "
                        "mirar cuando un servicio se cae o reinicia."),
        "parameters": {
            "type": "object",
            "properties": {
                "unidad": {"type": "string",
                           "description": "por ejemplo plataforma-api.service"},
                "lineas": {"type": "integer"},
                "solo_errores": {"type": "boolean",
                                 "description": "true por defecto: prioridad <= 4"},
            },
        },
    },
]

EJECUTORES = {"listar": listar, "leer": leer,
              "telemetria": telemetria, "journal": journal}


def ejecutar(nombre: str, args: dict[str, Any]) -> dict[str, Any]:
    """Corre una herramienta. Nunca lanza: un error es un resultado más."""
    fn = EJECUTORES.get(nombre)
    if fn is None:
        return {"error": f"no existe la herramienta {nombre!r}",
                "disponibles": list(EJECUTORES)}
    try:
        return fn(**{k: v for k, v in (args or {}).items()
                     if k in fn.__code__.co_varnames})
    except TypeError as e:
        return {"error": f"argumentos invalidos: {e}"}
    except Exception as e:  # noqa: BLE001
        log.warning("fallo la herramienta %s: %s", nombre, e)
        return {"error": str(e)[:300]}
