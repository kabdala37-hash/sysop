"""Qué pasó entre un informe y el siguiente.

El informe contaba el estado de AHORA. Eso deja afuera casi todo lo que importa:
un servicio que se cayó a las tres de la mañana y volvió solo no aparece por
ningún lado, y es exactamente la clase de cosa que hay que ver antes de que pase
en serio.

Cuatro bloques, y los dos del medio son los que hoy no existían:

    incidentes    lo que abrió, lo que cerró, lo que sigue abierto
    transitorios  fallas que NO llegaron a incidente: los picos
    ceguera       lo que quedó en UNKNOWN sostenido, y por qué
    actividad     cuánto se miró, para dimensionar el resto
    logs          los errores de los archivos declarados, agrupados por firma

La ventana no es un número fijo: va desde el último informe enviado hasta ahora.
Por eso cambiar la cadencia del cron a semanal cambia el período solo, sin tocar
código ni configuración.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("sysop.periodo")

# Si no hay informe anterior, cuánto se mira hacia atrás. Es el arranque
# limpio: el primer informe de una instalación nueva.
VENTANA_INICIAL_H = 24

# Un problema que aparece en pocas muestras y se va es un transitorio. Por
# encima de esto ya no es un pico: o abrió incidente, o hay algo mal en la
# histéresis del control y también hay que decirlo.
MUESTRAS_TRANSITORIO = 12


def desde_el_ultimo_informe(conn: Any, tipo: str = "informe") -> datetime:
    """Cuándo se mandó el informe anterior. Es el borde del período."""
    fila = conn.execute(
        "SELECT MAX(sent_at) AS ultimo FROM outbox "
        "WHERE report_type = ? AND state = 'SENT'", (tipo,)).fetchone()
    if fila and fila["ultimo"]:
        d = _fecha(fila["ultimo"])
        if d is not None:
            return d
    return datetime.now(timezone.utc) - timedelta(hours=VENTANA_INICIAL_H)


def _fecha(v: Any) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def resumir(conn: Any, desde: datetime, hasta: Optional[datetime] = None,
            logs_declarados: Optional[list[dict[str, Any]]] = None,
            avanzar_cursor: bool = True) -> dict[str, Any]:
    """El período entero, listo para el correo y para el modelo."""
    hasta = hasta or datetime.now(timezone.utc)
    d, h = desde.isoformat(), hasta.isoformat()
    horas = max(0.1, (hasta - desde).total_seconds() / 3600.0)

    out = {
        "desde": d, "hasta": h, "horas": round(horas, 1),
        "incidentes": _incidentes(conn, d, h),
        "transitorios": _transitorios(conn, d, h),
        "ceguera": _ceguera(conn, d, h),
        "actividad": _actividad(conn, d, h),
    }
    if logs_declarados:
        # El extracto NO se filtra por la ventana del período: se filtra por el
        # cursor de cada archivo, que es más exacto. Un log no garantiza estar
        # ordenado por fecha ni tener fechas parseables en todas sus líneas.
        try:
            from sysop import logs as logs_mod                # noqa: PLC0415
            out["logs"] = logs_mod.barrer(conn, logs_declarados, avanzar_cursor)
        except Exception:  # noqa: BLE001
            log.exception("fallo el extracto de logs; el resumen sale igual")
            out["logs"] = []
    return out


# ---------------------------------------------------------------------------

def _incidentes(conn: Any, d: str, h: str) -> dict[str, Any]:
    def filas(sql: str) -> list[dict[str, Any]]:
        out = []
        for f in conn.execute(sql, (d, h)).fetchall():
            r = dict(f)
            try:
                r["cause"] = json.loads(r.pop("cause_json", "") or "{}")
            except json.JSONDecodeError:
                r["cause"] = {}
            out.append(r)
        return out

    abiertos = filas(
        "SELECT incident_id, check_id, resource_id, severity, opened_at, "
        "       closed_at, state, cause_json FROM incident "
        " WHERE opened_at >= ? AND opened_at < ? ORDER BY opened_at")
    cerrados = filas(
        "SELECT incident_id, check_id, resource_id, severity, opened_at, "
        "       closed_at, state, cause_json FROM incident "
        " WHERE closed_at >= ? AND closed_at < ? ORDER BY closed_at")
    vivos = filas(
        "SELECT incident_id, check_id, resource_id, severity, opened_at, "
        "       closed_at, state, cause_json FROM incident "
        " WHERE closed_at IS NULL AND ? IS NOT NULL AND ? IS NOT NULL "
        " ORDER BY opened_at")

    # Cuánto duró cada uno de los que cerraron: un incidente de dos minutos y
    # uno de nueve horas son problemas distintos y el conteo solo no lo dice.
    duraciones = []
    for i in cerrados:
        a, b = _fecha(i["opened_at"]), _fecha(i["closed_at"])
        if a and b:
            duraciones.append(round((b - a).total_seconds() / 60.0, 1))

    return {
        "abiertos_en_el_periodo": abiertos,
        "cerrados_en_el_periodo": cerrados,
        "siguen_abiertos": vivos,
        "duracion_minutos": duraciones,
    }


def _transitorios(conn: Any, d: str, h: str) -> list[dict[str, Any]]:
    """Fallas que no llegaron a incidente. Los picos.

    Es el bloque que justifica todo el módulo. Un control que falla una o dos
    muestras y se recupera no abre incidente —y está bien que no lo abra, para
    eso está la histéresis— pero que haya pasado diecisiete veces en la semana
    es un dato, y hasta ahora no se veía en ningún lado.
    """
    filas = conn.execute("""
        SELECT r.check_id, r.resource_id, r.reason_code,
               COUNT(*) AS muestras,
               MIN(r.observed_at) AS primera,
               MAX(r.observed_at) AS ultima,
               COUNT(DISTINCT date(r.observed_at)) AS dias
          FROM check_result r
         WHERE r.observed_at >= ? AND r.observed_at < ?
           AND r.state NOT IN ('OK', 'UNKNOWN', 'DISABLED_EXPECTED')
           AND NOT EXISTS (
                 SELECT 1 FROM incident i
                  WHERE i.check_id = r.check_id
                    AND i.resource_id = r.resource_id
                    AND i.opened_at >= ? )
         GROUP BY r.check_id, r.resource_id, r.reason_code
         HAVING muestras <= ?
         ORDER BY muestras DESC, r.check_id
         LIMIT 30
    """, (d, h, d, MUESTRAS_TRANSITORIO)).fetchall()
    return [dict(f) for f in filas]


def _ceguera(conn: Any, d: str, h: str) -> list[dict[str, Any]]:
    """Lo que no se pudo mirar, agrupado por motivo.

    Un `UNKNOWN` aislado es normal. Doscientas muestras del mismo control con el
    mismo motivo significan que ese control lleva todo el período sin ver nada,
    y el informe no puede presentar eso como salud.
    """
    filas = conn.execute("""
        SELECT check_id, reason_code, COUNT(*) AS muestras,
               COUNT(DISTINCT resource_id) AS recursos,
               MAX(observed_at) AS ultima
          FROM check_result
         WHERE observed_at >= ? AND observed_at < ? AND state = 'UNKNOWN'
         GROUP BY check_id, reason_code
         ORDER BY muestras DESC
         LIMIT 15
    """, (d, h)).fetchall()
    return [dict(f) for f in filas]


def _actividad(conn: Any, d: str, h: str) -> dict[str, Any]:
    """Cuánto se miró. Sin esto, "tres problemas" no se puede dimensionar."""
    f = conn.execute("""
        SELECT COUNT(*) AS resultados,
               COUNT(DISTINCT check_id) AS controles,
               COUNT(DISTINCT resource_id) AS recursos,
               SUM(state = 'OK') AS ok,
               SUM(state = 'UNKNOWN') AS unknown,
               SUM(state NOT IN ('OK','UNKNOWN','DISABLED_EXPECTED')) AS problema
          FROM check_result WHERE observed_at >= ? AND observed_at < ?
    """, (d, h)).fetchone()
    out = {k: int(f[k] or 0) for k in f.keys()}
    llm = conn.execute("""
        SELECT COUNT(*) AS llamadas, COALESCE(SUM(costo_usd), 0) AS usd
          FROM llm_call WHERE at >= ? AND at < ? AND ok = 1
    """, (d, h)).fetchone()
    out["llamadas_al_modelo"] = int(llm["llamadas"] or 0)
    out["costo_modelo_usd"] = round(float(llm["usd"] or 0), 4)
    return out
