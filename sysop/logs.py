"""Extraer de un log lo que sirve, y sólo eso.

Un log de mil líneas donde novecientas setenta dicen que todo salió bien no es
sólo caro de mandarle a un modelo: el ruido **empeora el diagnóstico**. Este
módulo hace el filtro antes, y guarda el resultado.

Tres problemas concretos que resuelve, los tres vistos en los logs reales de la
plataforma:

**Los tracebacks no tienen prefijo.** La última línea de una excepción de Python
es `mysql.connector.errors.DatabaseError: 1129 (HY000): Host ... is blocked`, sin
fecha, sin nivel y sin componente. Un `grep ERROR` la agarra suelta y no se sabe
ni cuándo pasó. Acá una línea sin marca de tiempo se trata como **continuación
del evento anterior**, así que el traceback viaja junto a su encabezado.

**Conviven cuatro formatos.** El worker de Node escribe `[ISO] [WARN] [SHIFT]`,
los bots de Python `2026-08-20 20:05:34,093 [WARNING] [MainThread] nombre:`, y el
back otro más. El parser reconoce los que hay en vez de exigir uno.

**Cuarenta y siete errores casi iguales son un error.** Se agrupan por firma
—normalizando números, ids y direcciones— y se manda un ejemplo de cada grupo
con su cuenta. Eso es lo que convierte "muchas líneas" en "esto pasó 47 veces".

El cursor se guarda por archivo, así que entre corridas sólo se lee lo nuevo.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("sysop.logs")

# Una línea que arranca con marca de tiempo abre un evento nuevo. Cualquier otra
# es continuación de lo anterior: así el traceback queda pegado a su encabezado.
INICIO_EVENTO = re.compile(
    r"^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")

NIVEL = re.compile(
    r"\b(CRITICAL|CRIT|FATAL|ERROR|ERR|WARNING|WARN|NOTICE|INFO|DEBUG|TRACE)\b")

GRAVES = ("CRITICAL", "CRIT", "FATAL", "ERROR", "ERR")

# Señales de problema que no traen nivel: los tracebacks de Python y los errores
# de Node no dicen "ERROR" en ningún lado.
SENALES = re.compile(
    r"(Traceback \(most recent call last\)|^\w+Error:|^\w+Exception:|"
    r"\bUnhandledPromiseRejection\b|\bECONNREFUSED\b|\bETIMEDOUT\b|"
    r"\bsegfault\b|\bOOM\b|\bpanic:)", re.M)

# Para agrupar por firma: lo que cambia entre dos ocurrencias del mismo error.
VARIABLES = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    # Sin `\b` al final, y con la zona horaria adentro. Con `\d*\b`, sobre
    # `38.864Z` el limite de palabra entre `4` y `Z` no existe, asi que el motor
    # retrocede y consume solo hasta el punto: la firma quedaba `<ts>864Z` y dos
    # ocurrencias del MISMO error no agrupaban. Medido en el log del worker:
    # 218 eventos daban 200 grupos, o sea ninguna agrupacion.
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
                r"(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b\d+\b"), "<n>"),
]

MAX_EVENTO_LINEAS = 25
MAX_BYTES_POR_ARCHIVO = 200_000     # cuánto se lee de una pasada


@dataclass
class Grupo:
    firma: str
    nivel: str
    veces: int = 0
    primera: str = ""
    ultima: str = ""
    ejemplo: list[str] = field(default_factory=list)

    def como_dict(self) -> dict[str, Any]:
        return {"veces": self.veces, "nivel": self.nivel,
                "primera_vez": self.primera, "ultima_vez": self.ultima,
                "ejemplo": "\n".join(self.ejemplo[:MAX_EVENTO_LINEAS])}


def _umbral_para(texto: str, ignorar: Optional[list[dict[str, Any]]]) -> int:
    """Cuántas veces tiene que repetirse este evento para que se reporte.

    0 = se reporta siempre (el caso normal: no matchea ninguna regla).

    Un error que el propio flujo reintenta no es una novedad hasta que deja de
    ser el ruido de fondo de siempre. Los dos casos medidos en srv2 el 2026-09-08:
    `respuesta Gemini vacia` dice en el mismo mensaje "retry desde rango" y sale
    entre 1 y 17 veces por dia todos los dias; `HTTP 404` sobre una grabacion sale
    entre 5 y 21, salvo el 2026-09-07 que salio 155 — y ESE dia si habia algo que
    mirar. Reportar los dos siempre entierra el 155 entre los 17 de todos los
    dias.

    Por eso el umbral va por patron y no global: cada error tiene su piso de
    ruido, y el numero sale de medirlo, no de estimarlo.
    """
    if not ignorar:
        return 0
    t = texto.lower()
    for regla in ignorar:
        p = str(regla.get("patron", "")).lower()
        if p and p in t:
            return max(0, int(regla.get("minimo_veces", 0)))
    return 0


def firma_de(texto: str) -> str:
    """Qué tienen en común dos ocurrencias del mismo error."""
    t = texto.strip()
    for patron, marca in VARIABLES:
        t = patron.sub(marca, t)
    return t[:300]


def _marca_de_tiempo(linea: str) -> str:
    m = re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", linea)
    return m.group(0) if m else ""


def _nivel_de(linea: str) -> str:
    m = NIVEL.search(linea[:120])
    if m:
        n = m.group(1).upper()
        return {"ERR": "ERROR", "WARN": "WARNING", "CRIT": "CRITICAL"}.get(n, n)
    return ""


def eventos(lineas: Iterable[str]) -> list[list[str]]:
    """Agrupa líneas en eventos: una cabecera con marca de tiempo y su cola."""
    out: list[list[str]] = []
    actual: list[str] = []
    for linea in lineas:
        if INICIO_EVENTO.match(linea):
            if actual:
                out.append(actual)
            actual = [linea.rstrip()]
        elif actual:
            if len(actual) < MAX_EVENTO_LINEAS:
                actual.append(linea.rstrip())
        else:
            # Continuación sin cabecera: pasa cuando la ventana leída arranca en
            # medio de un traceback. Se conserva igual, marcada, porque suele ser
            # la línea que más dice.
            actual = [linea.rstrip()]
    if actual:
        out.append(actual)
    return out


def interesa(evento: list[str], patrones: Optional[list[str]] = None) -> bool:
    texto = "\n".join(evento)
    if patrones:
        return any(p.lower() in texto.lower() for p in patrones)
    return _nivel_de(evento[0]) in GRAVES or bool(SENALES.search(texto))


def extraer(ruta: str, desde_byte: int = 0,
            patrones: Optional[list[str]] = None,
            max_grupos: int = 12,
            ignorar: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """Lo que pasó en un archivo desde el último corte, agrupado por firma.

    `ignorar` son reglas `{patron, minimo_veces, motivo}` para errores que el
    propio flujo ya reintenta: se callan mientras se mantengan en su piso de
    ruido y se reportan cuando lo superan. Ver `_umbral_para`.

    Lo omitido NO desaparece: viaja en `omitidos`, con cuántos grupos y cuántos
    eventos fueron. Un filtro que borra sin dejar rastro es el que termina
    escondiendo justo lo que hacía falta ver.
    """
    p = Path(ruta)
    if not p.exists():
        return {"ruta": ruta, "error": "no existe", "cursor": desde_byte}
    try:
        tam = p.stat().st_size
    except OSError as e:
        return {"ruta": ruta, "error": str(e)[:120], "cursor": desde_byte}

    # Si el archivo encogió, rotó: se arranca de cero en vez de leer basura.
    if desde_byte > tam:
        log.info("%s roto (era %d bytes, ahora %d): se relee desde el principio",
                 ruta, desde_byte, tam)
        desde_byte = 0

    inicio = max(desde_byte, tam - MAX_BYTES_POR_ARCHIVO)
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(inicio)
            crudo = f.read()
    except (OSError, PermissionError) as e:
        return {"ruta": ruta, "error": str(e)[:120], "cursor": desde_byte}

    grupos: dict[str, Grupo] = {}
    total_interesantes = 0
    for ev in eventos(crudo.splitlines()):
        if not interesa(ev, patrones):
            continue
        total_interesantes += 1
        texto = "\n".join(ev)
        f = firma_de(texto)
        g = grupos.get(f)
        if g is None:
            g = Grupo(firma=f, nivel=_nivel_de(ev[0]) or "SIN_NIVEL",
                      primera=_marca_de_tiempo(ev[0]), ejemplo=ev)
            grupos[f] = g
        g.veces += 1
        g.ultima = _marca_de_tiempo(ev[0]) or g.ultima

    # El filtro va DESPUES de agrupar, no al leer: el umbral se compara contra
    # la cuenta del grupo entero, que es justo lo que no se sabe leyendo linea
    # por linea.
    vivos, om_grupos, om_eventos = [], 0, 0
    for g in grupos.values():
        umbral = _umbral_para("\n".join(g.ejemplo), ignorar)
        if umbral and g.veces < umbral:
            om_grupos += 1
            om_eventos += g.veces
            continue
        vivos.append(g)

    ordenados = sorted(vivos, key=lambda g: -g.veces)[:max_grupos]
    r: dict[str, Any] = {
        "ruta": ruta,
        "cursor": tam,
        "bytes_leidos": len(crudo),
        "eventos_relevantes": total_interesantes,
        "grupos_distintos": len(grupos),
        "grupos": [g.como_dict() for g in ordenados],
        "truncado_al_leer": inicio > desde_byte,
    }
    if om_grupos:
        r["omitidos"] = {"grupos": om_grupos, "eventos": om_eventos}
    return r


# ---------------------------------------------------------------------------
# Cursores
# ---------------------------------------------------------------------------

def _clave(ruta: str) -> str:
    return f"log:{ruta}"


def cursor(conn: Any, ruta: str) -> int:
    f = conn.execute("SELECT value FROM cursor WHERE key = ?",
                     (_clave(ruta),)).fetchone()
    try:
        return int(f["value"]) if f else 0
    except (TypeError, ValueError):
        return 0


def guardar_cursor(conn: Any, ruta: str, valor: int) -> None:
    conn.execute(
        "INSERT INTO cursor (key, value, updated_at) VALUES (?,?,datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=excluded.updated_at", (_clave(ruta), str(valor)))


def barrer(conn: Any, declarados: list[dict[str, Any]], avanzar: bool = True,
           desde_cero: bool = False) -> list[dict[str, Any]]:
    """Todos los logs declarados, desde donde quedó cada uno.

    Dos interruptores que hacen cosas distintas, y conviene no confundirlos:

    - `avanzar=False` no mueve el cursor, pero **igual arranca donde quedó**.
      Sirve para que dos consumidores lean lo mismo sin pisarse.
    - `desde_cero=True` ignora el cursor y relee la ventana entera. Es lo que
      hay que usar para mirar a mano: preguntar "qué errores hay" y que
      conteste "ninguno nuevo desde el último informe" no es lo que uno espera.
    """
    out = []
    for d in declarados or []:
        ruta = str(d.get("ruta", ""))
        if not ruta:
            continue
        for archivo in _expandir(ruta):
            desde = 0 if desde_cero else cursor(conn, archivo)
            r = extraer(archivo, desde, d.get("patrones"),
                        int(d.get("max_grupos", 12)), d.get("ignorar"))
            r["que_es"] = d.get("que_es", "")
            if avanzar and "cursor" in r and not r.get("error"):
                guardar_cursor(conn, archivo, int(r["cursor"]))
            # `eventos_relevantes` cuenta ANTES del filtro, asi que no sirve
            # como condicion: un archivo donde todo quedo bajo su umbral tiene
            # eventos pero ningun grupo que mostrar, y entraria como una entrada
            # vacia. Lo que decide es que haya quedado algo para reportar.
            if r.get("error") or r.get("grupos"):
                out.append(r)
    return out


def _expandir(ruta: str) -> list[str]:
    """Un archivo, o todos los .log de un directorio."""
    p = Path(ruta)
    if p.is_dir():
        try:
            return sorted(str(x) for x in p.rglob("*.log") if x.is_file())[:40]
        except OSError:
            return []
    if any(c in ruta for c in "*?["):
        from glob import glob                                # noqa: PLC0415
        return sorted(glob(ruta))[:40]
    return [ruta]
