"""Cuándo se le pregunta al modelo, y qué se hace con lo que contesta.

El modelo hace dos cosas y ninguna más:

    1. confirma que el problema es real (filtra el falso positivo)
    2. dice la causa probable y qué conviene hacer

**Lo crítico no se filtra.** Un servicio caído, un disco lleno o una base que no
responde mandan aviso igual, y el diagnóstico va encima como texto. Un modelo
que se equivoca no puede ser la razón por la que nadie se enteró de que se cayó
producción.

**Descartar no es cerrar.** Un incidente descartado sigue abierto y sigue en el
informe, en su propia sección: deja de contar como problema activo, no
desaparece. El estado lo fija el plano determinístico; el modelo opina.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sysop.core import incidents
from sysop.core.models import Severity
from sysop.llm import contexto as ctx_mod
from sysop.llm import gateway
from sysop.llm.esquema import Diagnostico
from sysop.store import registro

log = logging.getLogger("sysop.diagnostico")

# Controles que nunca se silencian, aunque el modelo los descarte. Se puede
# ampliar desde `app.yaml` (`llm.sin_filtro`), nunca reducir por debajo de esto.
SIN_FILTRO = frozenset({
    "SVC-01",   # una unidad esperada que no esta activa
    "SVC-05",   # un socket que dejo de escuchar
    "DB-01",    # la base no responde
    "HST-07",   # el kernel mato un proceso por memoria
    "HST-09",   # filesystem lleno
    "HST-12",   # mount ausente o de solo lectura
    "HST-13",   # errores de disco
})

# Cuántos incidentes se diagnostican por tick. Uno: la llamada es HTTP y el tick
# dura quince segundos. Los incidentes nuevos son raros; hacer cola de a uno no
# retrasa nada y evita que un tick se vaya en llamadas al modelo.
POR_TICK = 1


def sin_filtro(check_id: str, severidad: str, cfg_llm: dict[str, Any]) -> bool:
    """¿Este incidente avisa aunque el modelo lo descarte?"""
    if severidad == Severity.CRITICAL.value:
        return True
    extra = {str(x).upper() for x in (cfg_llm.get("sin_filtro") or [])}
    return check_id in SIN_FILTRO or check_id in extra


# Cuántas veces se le pregunta al modelo por el mismo incidente antes de
# dejarlo sin diagnóstico. Si la respuesta no se puede usar, reintentar en cada
# tick gasta tokens para siempre y, peor, bloquea la cola: `pendientes` ordena
# por antigüedad, así que el que falla tapa a todos los que vienen atrás.
MAX_INTENTOS = 2


def pendientes(conn: Any, limite: int = POR_TICK) -> list[dict[str, Any]]:
    """Incidentes abiertos, sin diagnóstico y sin agotar los intentos."""
    filas = conn.execute(
        "SELECT i.* FROM incident i "
        "LEFT JOIN diagnostico d ON d.incident_id = i.incident_id "
        "WHERE i.closed_at IS NULL AND i.state = 'OPEN' AND d.incident_id IS NULL "
        "  AND (SELECT COUNT(*) FROM llm_call l "
        "         WHERE l.incident_id = i.incident_id) < ? "
        "ORDER BY i.opened_at", (MAX_INTENTOS,)).fetchall()
    import json                                            # noqa: PLC0415
    # Los intentos se cuentan tambien en el registro: el conteo del SQL sale de
    # `llm_call`, y si esa tabla no escribe el mismo incidente se le volvia a
    # mandar al modelo en cada tick, pagando cada vez. Y el tope va DESPUES de
    # filtrar: con el LIMIT en el SQL, un incidente agotado en la punta de la
    # cola tapaba a todos los de atras (revision).
    intentos = registro.intentos_hoy() if filas else {}
    out = []
    for f in filas:
        if len(out) >= limite:
            break
        if intentos.get(f["incident_id"], 0) >= MAX_INTENTOS:
            continue
        d = dict(f)
        try:
            d["cause"] = json.loads(d.pop("cause_json") or "{}")
        except json.JSONDecodeError:
            d["cause"] = {}
        out.append(d)
    return out


def revisar(ctx: Any, cfg: Any, cfg_llm: dict[str, Any]) -> int:
    """Diagnostica lo que haya pendiente. Devuelve cuántos se resolvieron."""
    if cfg is None or not cfg.puede_diagnosticar:
        return 0

    hechos = 0
    for inc in pendientes(ctx.conn):
        paquete = ctx_mod.armar(ctx.conn, inc, ctx.cfg.installation, ctx.app,
                                mapa=ctx.cfg.mapa, modo="urgente")
        diag = gateway.diagnosticar(ctx.conn, cfg.agente, paquete,
                                    inc["incident_id"], cfg_llm,
                                    herramientas_de(ctx.app))
        if diag is None:
            continue

        protegido = sin_filtro(inc["check_id"], inc["severity"], cfg_llm)
        guardar(ctx.conn, inc["incident_id"], diag, filtrado=not protegido,
                modelo=cfg.agente.modelo)
        hechos += 1

        if diag.descartable and not protegido:
            log.info("%s (%s en %s): el modelo lo da por falso positivo -- %s",
                     inc["incident_id"], inc["check_id"], inc["resource_id"],
                     diag.descarte[:120])
            incidents._evento(ctx.conn, inc["incident_id"], "descartado",
                              {"confianza": diag.confianza,
                               "motivo": diag.descarte[:400]})
        else:
            log.info("%s diagnosticado: %s", inc["incident_id"], diag.causa[:120])
    return hechos


def del_periodo(ctx: Any, cfg: Any, cfg_llm: dict[str, Any],
                resumen: dict[str, Any],
                revalidaciones: Optional[list[dict[str, Any]]] = None
                ) -> Optional[Diagnostico]:
    """El mismo agente, mirando el período entero en vez de un incidente.

    Un solo prompt para los dos casos: lo que cambia es el paquete, no quién
    responde. En modo periódico lo que importa no es un problema puntual sino el
    patrón — qué se repite, qué se viene degradando, qué transitorio apareció
    diecisiete veces sin llegar nunca a generar un aviso.

    Se pide **siempre**, incluso en un período tranquilo: un "no pasó nada
    grave, pero estos tres se repiten" es justamente lo que no se ve de ninguna
    otra forma.
    """
    if cfg is None or not cfg.puede_diagnosticar:
        return None

    paquete = {
        "modo": "periodico",
        "instalacion": ctx.cfg.installation,
        "mapa_del_servidor": ctx.cfg.mapa,
        "periodo": ctx_mod.redactar(resumen),
    }
    if revalidaciones is not None:
        # Lo que se acaba de verificar incidente por incidente. Sin esto el
        # resumen repetia la causa vieja de un incidente recien descartado.
        paquete["revalidacion_de_los_activos"] = ctx_mod.redactar(revalidaciones)
    return gateway.diagnosticar(ctx.conn, cfg.agente, paquete,
                                f"periodo:{resumen.get('desde', '')[:19]}",
                                cfg_llm, herramientas_de(ctx.app))


def herramientas_de(app: Any) -> Optional[tuple[list, dict[str, Any]]]:
    """Las herramientas propias de la plataforma, si el adaptador las tiene."""
    fn = getattr(app, "herramientas", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:  # noqa: BLE001
        log.exception("el adaptador no pudo dar sus herramientas")
        return None


def guardar(conn: Any, incident_id: str, d: Diagnostico, filtrado: bool,
            modelo: str) -> None:
    conn.execute(
        "INSERT INTO diagnostico (incident_id, at, es_real, confianza, causa, "
        "accion, descarte, modelo, filtrado) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(incident_id) DO UPDATE SET at=excluded.at, "
        "es_real=excluded.es_real, confianza=excluded.confianza, "
        "causa=excluded.causa, accion=excluded.accion, "
        "descarte=excluded.descarte, modelo=excluded.modelo",
        (incident_id, datetime.now(timezone.utc).isoformat(),
         1 if d.es_real else 0, d.confianza, d.causa, d.accion, d.descarte,
         modelo, 1 if filtrado else 0))


def de(conn: Any, incident_id: str) -> Optional[dict[str, Any]]:
    f = conn.execute("SELECT * FROM diagnostico WHERE incident_id = ?",
                     (incident_id,)).fetchone()
    return dict(f) if f else None


def descartado(conn: Any, incident_id: str) -> bool:
    """¿El modelo lo descartó, y era un incidente que se puede descartar?"""
    f = conn.execute(
        "SELECT es_real, confianza, filtrado FROM diagnostico "
        "WHERE incident_id = ?", (incident_id,)).fetchone()
    if f is None:
        return False
    return (not f["es_real"]) and f["confianza"] == "alta" and bool(f["filtrado"])


def separar(conn: Any, activos: list[dict[str, Any]]) -> tuple[list, list]:
    """Parte la lista en (los que cuentan, los descartados por el modelo)."""
    cuentan, descartados = [], []
    for i in activos:
        d = de(conn, i["incident_id"])
        if d:
            i = {**i, "diagnostico": d}
        (descartados if descartado(conn, i["incident_id"]) else cuentan).append(i)
    return cuentan, descartados


def texto(conn: Any, incident_id: str) -> Optional[str]:
    """El diagnóstico en prosa, para meter en el correo."""
    d = de(conn, incident_id)
    if not d:
        return None
    partes = [f"Causa probable ({d['confianza']} confianza): {d['causa']}"]
    if d["accion"]:
        partes.append(f"Accion sugerida: {d['accion']}")
    if not d["es_real"]:
        partes.append(f"El modelo lo considera un falso positivo: {d['descarte']}")
        if not d["filtrado"]:
            partes.append("Se avisa igual: es un control critico, que no se "
                          "silencia por diagnostico.")
    return "\n".join(partes)
