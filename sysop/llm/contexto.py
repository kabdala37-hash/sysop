"""El paquete de evidencia que ve el modelo.

Dos preocupaciones, y las dos importan por separado:

**Qué necesita para diagnosticar.** Un incidente solo no alcanza: un servicio
caído a la misma hora que un disco lleno es un síntoma, no una causa. Va el
incidente, lo que pasó alrededor, y las últimas muestras del mismo control para
distinguir un pico de una tendencia.

**Qué no puede salir de acá.** Una credencial, un teléfono, un correo de un
cliente final. La identidad interna sí va (D5): pseudonimizar el nombre de un
servicio hace el diagnóstico inútil, y esto no sale de la infraestructura de la
plataforma. Lo que no sale es lo que no hace falta para diagnosticar.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Claves que nunca viajan, sin importar en qué nivel del árbol aparezcan.
CLAVES_PROHIBIDAS = re.compile(
    r"(pass|passwd|password|secret|token|api[_-]?key|credential|authorization"
    r"|cookie|private[_-]?key|dsn|conn(ection)?[_-]?string)", re.I)

# Valores que parecen un dato de cliente final. El observador mira
# infraestructura: si un teléfono o un correo aparecen en la evidencia de un
# control, es de una fila de negocio que se coló, y no hace falta para
# diagnosticar que la cola está estancada.
TELEFONO = re.compile(r"\+?\d[\d\s().-]{7,}\d")

# UNA CLAVE PROHIBIDA TAMBIEN VIENE ADENTRO DE UN TEXTO, no solo como clave de
# un diccionario. Es el caso que importa de verdad: lo que se le manda al modelo
# cuando lee un archivo es UN STRING, y ahi `CLAVES_PROHIBIDAS` no se aplicaba.
# Probado contra el redactor real antes de esto:
#
#     in : JWT_SECRET = "s3cr3t-de-produccion"
#     out: JWT_SECRET = "s3cr3t-de-produccion"      <- intacto
#
# Se redacta el VALOR y se deja el nombre: que el diagnostico pueda decir "esta
# seteada la clave X" es util; su contenido no aporta nada para diagnosticar.
ASIGNACION_SECRETA = re.compile(
    r"([A-Za-z0-9_.\-]*"
    r"(?:pass|passwd|password|secret|token|api[_-]?key|credential|authorization"
    r"|cookie|private[_-]?key|dsn|conn(?:ection)?[_-]?string)"
    r"[A-Za-z0-9_.\-]*)"          # el nombre, con lo que lo rodee
    r"(\s*[:=]\s*\"?'?)"          # el separador y una comilla opcional
    # El esquema va ADENTRO del valor: sin esto, `Authorization: Bearer <token>`
    # redactaba la palabra "Bearer" y dejaba el token entero a la vista.
    r"(?:(?:Bearer|Basic|Token|Digest|JWT)\s+)?"
    r"[^\s\"',;]+",               # el valor, hasta el primer separador
    re.I)
CORREO = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Una marca de tiempo ISO es indistinguible de un teléfono para el patrón de
# arriba: `2026-08-28T15:02:33` son dígitos con separadores. Sin esta excepción
# el paquete llegaba con `[telefono]T15:02:[telefono]`, es decir SIN las horas,
# que son justo lo que deja ver si un valor viene subiendo hace rato o saltó de
# golpe. El modelo diagnosticaba a ciegas sobre la dimensión que más importa.
FECHA_ISO = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?")

MAX_TEXTO = 400


def redactar(valor: Any, _prof: int = 0, max_texto: int = MAX_TEXTO) -> Any:
    """Poda recursiva. Se aplica SIEMPRE, antes de armar el paquete.

    `max_texto` acota cada string. El valor por defecto sirve para la evidencia
    de un control, donde un campo largo es casi siempre un error. **No sirve
    para el contenido de un archivo**: truncar un log a cuatrocientos caracteres
    se ve igual que un log corto, y el agente diagnosticaria sobre un pedazo sin
    saber que le falta el resto. Las herramientas de lectura pasan su propio
    limite, que ya controlan ellas.
    """
    if _prof > 6:
        return "..."
    if isinstance(valor, dict):
        out = {}
        for k, v in valor.items():
            if CLAVES_PROHIBIDAS.search(str(k)):
                out[k] = "[redactado]"
            else:
                out[k] = redactar(v, _prof + 1, max_texto)
        return out
    if isinstance(valor, (list, tuple)):
        return [redactar(v, _prof + 1, max_texto) for v in valor[:20]]
    if isinstance(valor, str):
        return _texto(valor)[:max_texto]
    return valor


def _texto(v: str) -> str:
    """Redacta un string sin comerse las marcas de tiempo.

    Las fechas se apartan antes de buscar teléfonos y se reponen después. Hacer
    los patrones más finos no alcanzaba: cualquier patrón que reconozca un
    teléfono con separadores reconoce también una fecha ISO.
    """
    fechas: list[str] = []

    def apartar(m: "re.Match[str]") -> str:
        fechas.append(m.group(0))
        return f"\x00{len(fechas) - 1}\x00"

    v = FECHA_ISO.sub(apartar, v)
    v = ASIGNACION_SECRETA.sub(lambda m: f"{m.group(1)}{m.group(2)}[redactado]", v)
    v = CORREO.sub("[correo]", v)
    v = TELEFONO.sub("[telefono]", v)
    for i, f in enumerate(fechas):
        v = v.replace(f"\x00{i}\x00", f)
    return v


def armar(conn: Any, incidente: dict[str, Any], instalacion: str,
          adaptador: Any = None, mapa: Any = None,
          modo: str = "urgente") -> dict[str, Any]:
    """El paquete completo, ya redactado."""
    paquete = {
        "modo": modo,
        "instalacion": instalacion,
        # Donde esta parado y que hay en cada lado. No limita lo que puede
        # mirar: le dice que existe, para que no busque a ciegas.
        "mapa_del_servidor": mapa or {},
        "incidente": {
            "control": incidente["check_id"],
            "recurso": incidente["resource_id"],
            "severidad": incidente["severity"],
            "abierto_desde": incidente["opened_at"],
            "causa": incidente.get("cause") or {},
        },
        "muestras_recientes": _muestras(conn, incidente),
        "otros_incidentes_abiertos": _vecinos(conn, incidente),
        "eventos_del_incidente": _eventos(conn, incidente["incident_id"]),
    }

    # Última pasada del adaptador: sabe de su plataforma y puede sacar lo que
    # el filtro genérico no reconoce como sensible.
    if adaptador is not None and callable(getattr(adaptador, "redactar", None)):
        try:
            paquete = adaptador.redactar(paquete)
        except Exception:  # noqa: BLE001
            pass

    return redactar(paquete)


def _muestras(conn: Any, inc: dict[str, Any], n: int = 12) -> list[dict[str, Any]]:
    """Las últimas muestras del mismo control sobre el mismo recurso.

    Es lo que deja ver si el valor viene subiendo hace rato o saltó de golpe,
    que es la diferencia entre "se está llenando" y "alguien copió un archivo".
    """
    filas = conn.execute(
        "SELECT observed_at, state, reason_code, actual_json FROM check_result "
        "WHERE check_id = ? AND resource_id = ? "
        "ORDER BY observed_at DESC, id DESC LIMIT ?",
        (inc["check_id"], inc["resource_id"], n)).fetchall()
    out = []
    for f in reversed(filas):
        try:
            actual = json.loads(f["actual_json"]) if f["actual_json"] else None
        except json.JSONDecodeError:
            actual = None
        out.append({"cuando": f["observed_at"], "estado": f["state"],
                    "motivo": f["reason_code"] or "", "valor": actual})
    return out


def _vecinos(conn: Any, inc: dict[str, Any], n: int = 8) -> list[dict[str, Any]]:
    """Qué más estaba roto al mismo tiempo. Sin esto no hay correlación posible."""
    filas = conn.execute(
        "SELECT check_id, resource_id, severity, opened_at, state, cause_json "
        "FROM incident WHERE closed_at IS NULL AND incident_id != ? "
        "ORDER BY opened_at DESC LIMIT ?",
        (inc["incident_id"], n)).fetchall()
    out = []
    for f in filas:
        try:
            causa = json.loads(f["cause_json"] or "{}")
        except json.JSONDecodeError:
            causa = {}
        out.append({"control": f["check_id"], "recurso": f["resource_id"],
                    "severidad": f["severity"], "desde": f["opened_at"],
                    "estado": f["state"],
                    "motivo": causa.get("reason_code", "")})
    return out


def _eventos(conn: Any, incident_id: str, n: int = 10) -> list[dict[str, Any]]:
    filas = conn.execute(
        "SELECT at, kind, payload_json FROM incident_event "
        "WHERE incident_id = ? ORDER BY id DESC LIMIT ?",
        (incident_id, n)).fetchall()
    out = []
    for f in reversed(filas):
        try:
            payload = json.loads(f["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        out.append({"cuando": f["at"], "que": f["kind"], "detalle": payload})
    return out
