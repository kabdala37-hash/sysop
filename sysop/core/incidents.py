"""Incidentes: agrupar, abrir, actualizar, recuperar y cerrar.

Un incidente es "este problema, en este recurso, desde este momento". Los
resultados de los controles llegan cada quince o treinta segundos; el incidente
es lo que hace que eso sea un aviso y no doscientos cuarenta por hora.

La pieza que lo hace posible es el fingerprint, más el índice único parcial del
esquema: sólo puede haber un incidente ABIERTO por fingerprint. Cerrado, el
mismo fingerprint puede volver a abrir, que es lo que distingue una recurrencia
de un duplicado.
"""
from __future__ import annotations

import hashlib
import json
import logging

from sysop.store import db, registro
from datetime import datetime, timezone
from typing import Any, Optional

from sysop.core.models import CheckResult, IncidentState, Severity

log = logging.getLogger("sysop.incidents")


def fingerprint(check_id: str, resource_id: str, reason_code: str) -> str:
    """Identidad del problema.

    Incluye el `reason_code` y no sólo el control: que un mount esté al 92% y
    que ese mismo mount esté montado de solo lectura son dos problemas
    distintos sobre el mismo recurso, y colapsarlos haría que el segundo quede
    tapado por el primero.

    NO incluye la severidad: un incidente que escala de major a critical es el
    mismo incidente empeorando, no uno nuevo.
    """
    crudo = f"{check_id}|{resource_id}|{reason_code}"
    return hashlib.sha256(crudo.encode()).hexdigest()[:16]


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def abierto(conn: Any, fp: str) -> Optional[dict[str, Any]]:
    fila = conn.execute(
        "SELECT * FROM incident WHERE fingerprint = ? AND closed_at IS NULL",
        (fp,),
    ).fetchone()
    return dict(fila) if fila else None


def abiertos_de(conn: Any, check_id: str, resource_id: str) -> list[dict[str, Any]]:
    """Incidentes vivos de un control sobre un recurso, sin mirar el motivo.

    Hace falta porque el fingerprint incluye el `reason_code`, y cuando el
    control se recupera ese código pasa a vacío. Buscar por fingerprint en ese
    momento no encuentra nada, y el incidente queda abierto para siempre: el
    problema se arregla y el correo lo sigue reportando todos los días.

    Puede devolver más de uno: un mismo mount puede tener abierto "casi lleno" y
    "montado de solo lectura" a la vez. Cuando el control vuelve a sano, se
    recuperan los dos.
    """
    filas = conn.execute(
        "SELECT * FROM incident WHERE check_id = ? AND resource_id = ? "
        "AND closed_at IS NULL ORDER BY opened_at",
        (check_id, resource_id),
    ).fetchall()
    return [dict(f) for f in filas]


def abrir(conn: Any, r: CheckResult) -> str:
    fp = fingerprint(r.check_id, r.resource_id, r.reason_code)
    ya = abierto(conn, fp)
    if ya:
        return str(ya["incident_id"])

    # Legible a propósito: el id aparece en el asunto del correo y en el panel,
    # y "INC-a1b2c3d4-20260827T2130" dice más que un uuid al que hay que ir a
    # buscarle el significado.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    incident_id = f"INC-{fp[:8]}-{ts}"

    conn.execute(
        "INSERT INTO incident (incident_id, fingerprint, resource_id, check_id, "
        "opened_at, state, severity, last_seen, cause_json) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (incident_id, fp, r.resource_id, r.check_id, r.observed_at.isoformat(),
         IncidentState.OPEN.value, r.severity.value, r.observed_at.isoformat(),
         json.dumps({"reason_code": r.reason_code,
                     "expected": r.expected, "actual": r.actual,
                     "evidence_quality": r.evidence_quality.value})),
    )
    _evento(conn, incident_id, "abierto", {
        "state": r.state.value, "severity": r.severity.value,
        "reason_code": r.reason_code})
    registro.anotar("incidente_abierto", incident_id=incident_id, fp=fp[:8],
                    check_id=r.check_id, resource_id=r.resource_id,
                    severity=r.severity.value, reason_code=r.reason_code,
                    actual=r.actual)
    log.info("incidente abierto %s (%s en %s)", incident_id, r.check_id, r.resource_id)
    return incident_id


def actualizar(conn: Any, incident_id: str, r: CheckResult) -> None:
    """Refresca un incidente vivo. Sólo emite evento si algo cambió de verdad.

    Sin ese filtro, un control cada quince segundos escribiría 5.760 eventos por
    día por incidente, y la línea temporal que sirve para entender qué pasó se
    volvería ilegible.
    """
    fila = conn.execute(
        "SELECT severity, cause_json FROM incident WHERE incident_id = ?",
        (incident_id,),
    ).fetchone()
    if fila is None:
        return

    sev_previa = fila["severity"]
    causa_previa = json.loads(fila["cause_json"] or "{}")
    causa_nueva = {"reason_code": r.reason_code, "expected": r.expected,
                   "actual": r.actual,
                   "evidence_quality": r.evidence_quality.value}

    conn.execute(
        "UPDATE incident SET last_seen = ?, severity = ?, cause_json = ? "
        "WHERE incident_id = ?",
        (r.observed_at.isoformat(), r.severity.value,
         json.dumps(causa_nueva), incident_id),
    )

    if r.severity.value != sev_previa:
        subio = Severity(r.severity.value).orden > Severity(sev_previa).orden
        _evento(conn, incident_id, "agravado" if subio else "atenuado",
                {"de": sev_previa, "a": r.severity.value})
    elif causa_previa.get("actual") != causa_nueva.get("actual"):
        _evento(conn, incident_id, "cambio", {"actual": r.actual})


def recuperar(conn: Any, incident_id: str, cuando: Optional[str] = None) -> bool:
    """El problema dejó de verse. Todavía NO se cierra.

    Entre recuperado y cerrado hay una ventana a propósito: si el síntoma vuelve
    dentro de ella, es el mismo incidente que intermitea y no uno nuevo. Cerrar
    de inmediato produciría una serie de incidentes cortos que parecen
    resolverse solos, escondiendo que en realidad nunca se arregló.

    **Es idempotente: sólo actúa sobre un incidente ABIERTO.** El motor la llama
    en cada ciclo mientras el control siga sano, y sin esa condición cada
    llamada reescribía `recovered_at`. Como la ventana de cierre se mide desde
    ese campo, se reiniciaba una vez por ciclo y el incidente no cerraba jamás.
    Verificado en srv3: 841 "incidente recuperado" en diez horas sobre el mismo
    incidente, que seguía figurando como activo en el correo.

    Devuelve si realmente cambió algo, para que el llamador no cuente como
    recuperación lo que ya estaba recuperado.
    """
    cur = conn.execute(
        "UPDATE incident SET state = ?, recovered_at = ? "
        "WHERE incident_id = ? AND closed_at IS NULL AND state = ?",
        (IncidentState.RECOVERED.value, cuando or _ahora(), incident_id,
         IncidentState.OPEN.value),
    )
    if cur.rowcount == 0:
        return False
    _evento(conn, incident_id, "recuperado", {"cuando": cuando or _ahora()})
    log.info("incidente recuperado %s", incident_id)
    return True


def reabrir(conn: Any, incident_id: str, r: CheckResult) -> None:
    conn.execute(
        "UPDATE incident SET state = ?, last_seen = ?, severity = ?, "
        "recovered_at = NULL WHERE incident_id = ?",
        (IncidentState.OPEN.value, r.observed_at.isoformat(),
         r.severity.value, incident_id),
    )
    _evento(conn, incident_id, "reabierto", {"reason_code": r.reason_code})
    log.info("incidente reabierto %s: volvio antes de cerrarse", incident_id)


def cerrar(conn: Any, incident_id: str, motivo: str = "estable") -> None:
    conn.execute(
        "UPDATE incident SET state = ?, closed_at = ? WHERE incident_id = ?",
        (IncidentState.CLOSED.value, _ahora(), incident_id),
    )
    _evento(conn, incident_id, "cerrado", {"motivo": motivo})


def cerrar_recuperados(conn: Any, ventana_seg: int = 900) -> list[str]:
    """Cierra los que estuvieron recuperados y estables por la ventana entera."""
    # Se cuenta desde `recovered_at`, no desde `last_seen`: la ventana mide
    # cuanto lleva SIN el problema, no cuanto hace que se vio por ultima vez.
    #
    # Y se compara con datetime() y no con aritmetica sobre julianday: esa
    # aritmetica es punto flotante y `now` tiene menos resolucion que un
    # timestamp con microsegundos, asi que una recuperacion del mismo segundo
    # daba una diferencia NEGATIVA y el incidente no cerraba nunca.
    filas = conn.execute(
        "SELECT incident_id FROM incident "
        "WHERE state = ? AND closed_at IS NULL AND recovered_at IS NOT NULL "
        "  AND datetime(recovered_at, '+' || ? || ' seconds') <= datetime('now')",
        (IncidentState.RECOVERED.value, int(ventana_seg)),
    ).fetchall()
    ids = [f["incident_id"] for f in filas]
    for i in ids:
        cerrar(conn, i, motivo=f"estable por {ventana_seg}s")
    return ids


def cerrar_sin_evidencia(conn: Any, horas: int = 24) -> list[str]:
    """Cierra los incidentes cuyo control lleva horas sin poder evaluarse.

    El agujero que tapa: `UNKNOWN` no mueve incidentes —y está bien que no los
    mueva, porque una fuente caída no resuelve un problema— pero eso deja al
    incidente abierto para siempre cuando su control deja de poder mirar. Pasó
    con un falso positivo de `HST-10`: el control pasó a `UNKNOWN` al corregirse
    el umbral y el incidente quedó colgado hasta que se lo cerró a mano.

    Cerrarlo NO es decir que se resolvió: es reconocer que no se puede sostener.
    Que el control no vea nada se sigue reportando, en la sección de ceguera del
    informe, que es donde corresponde.

    El criterio es doble a propósito: que lo último visto sea `UNKNOWN` **y** que
    no haya habido ninguna medición concluyente en la ventana. Con una sola de
    las dos condiciones, un control intermitente cerraría incidentes vivos.
    """
    filas = conn.execute("""
        SELECT i.incident_id, i.check_id, i.resource_id
          FROM incident i
         WHERE i.closed_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM check_result r
                  WHERE r.check_id = i.check_id AND r.resource_id = i.resource_id
                    AND r.state = 'UNKNOWN'
                    AND r.observed_at >= ?)
           AND NOT EXISTS (
                 SELECT 1 FROM check_result r
                  WHERE r.check_id = i.check_id AND r.resource_id = i.resource_id
                    AND r.state != 'UNKNOWN'
                    AND r.observed_at >= ?)
    """, (db.corte(hours=int(horas)), db.corte(hours=int(horas)))).fetchall()

    ids = []
    for f in filas:
        cerrar(conn, f["incident_id"],
               motivo=f"sin evidencia por {horas} h: el control no puede evaluarse")
        log.info("incidente %s cerrado sin evidencia: %s sobre %s lleva %d h "
                 "en UNKNOWN", f["incident_id"], f["check_id"], f["resource_id"],
                 horas)
        ids.append(f["incident_id"])
    return ids


def activos(conn: Any, severidad_minima: Severity = Severity.WARNING) -> list[dict[str, Any]]:
    """Los que tienen que aparecer en el digest.

    Incluye los RECOVERED todavía sin cerrar: el correo tiene que poder decir
    "esto se recuperó en la última hora", que es información distinta de que
    nunca haya pasado.
    """
    orden_min = severidad_minima.orden
    filas = conn.execute(
        "SELECT * FROM incident WHERE closed_at IS NULL ORDER BY opened_at"
    ).fetchall()
    out = []
    for f in filas:
        d = dict(f)
        if Severity(d["severity"]).orden >= orden_min:
            d["cause"] = json.loads(d.pop("cause_json") or "{}")
            out.append(d)
    out.sort(key=lambda d: (-Severity(d["severity"]).orden, d["opened_at"]))
    return out


# Los que tambien van al registro en archivo. `cambio` NO: sale cada vez que
# varia el valor medido (un disco que se llena da uno por minuto) y el registro
# es para reconstruir que estaba abierto, no la curva. `abierto` lo anota
# `abrir()`, que tiene el control y el recurso.
_AL_REGISTRO = frozenset({"recuperado", "reabierto", "cerrado", "agravado",
                          "atenuado", "descartado"})


def fp_corto(incident_id: str) -> str:
    """Los 8 caracteres de fingerprint que lleva el id (`INC-<fp8>-<fecha>`)."""
    partes = str(incident_id).split("-")
    return partes[1] if len(partes) >= 3 else ""


def _evento(conn: Any, incident_id: str, kind: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO incident_event (incident_id, at, kind, payload_json) "
        "VALUES (?,?,?,?)",
        (incident_id, _ahora(), kind, json.dumps(payload, default=str)),
    )
    # Despues del INSERT: el registro dice lo que paso, no lo que se intento.
    if kind in _AL_REGISTRO:
        registro.anotar(f"incidente_{kind}", incident_id=incident_id,
                        fp=fp_corto(incident_id), **payload)
