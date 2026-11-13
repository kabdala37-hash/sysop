"""Controles de las colas: mensajería, pipeline de llamadas y agenda.

Una cola estancada no produce errores. Los mensajes entran, nadie los toma, y
todo el resto del sistema se ve perfectamente sano: el daemon corre, la base
responde, no hay una sola excepción en ningún log. Lo único que pasa es que
nadie contesta.

Por eso estos controles miran la EDAD de lo pendiente, no la cantidad. Mil
mensajes procesándose al día son normales; uno solo esperando hace media hora
no lo es.
"""
from __future__ import annotations

import logging
from typing import Any

from sysop.adapters.apps.plataforma import datos
from sysop.adapters.apps.plataforma import mysql as mysql_col
from sysop.core.models import (CheckContext, CheckDefinition, CheckOutcome,
                               EvidenceQuality, Hysteresis, Resource,
                               ResourceState, Severity, State)

log = logging.getLogger("sysop.nac.colas")


def _sin_base() -> CheckOutcome:
    return CheckOutcome(state=State.UNKNOWN, reason_code="BASE_INACCESIBLE",
                        evidence_quality=EvidenceQuality.NONE)


def _consulta(ctx: CheckContext, sql: str, params: tuple = ()) -> Any:
    conn = ctx.deps.get("mysql")
    if conn is None:
        return None
    try:
        return mysql_col.consultar(conn, sql, params,
                                   timeout_sec=ctx.budget.timeout_sec)
    except Exception as e:  # noqa: BLE001
        log.debug("fallo la consulta de cola: %s", e)
        return None


# ---------------------------------------------------------------------------
# QUE-01: bandeja de entrada estancada
# ---------------------------------------------------------------------------

def _que01(ctx: CheckContext) -> CheckOutcome:
    """Mensajes que entraron y nadie tomó.

    Se cuentan sólo los que ya deberían haberse tomado: un `RETRY` con
    `next_attempt_at` en el futuro está esperando a propósito y no es un
    estancamiento.
    """
    tope = int(ctx.deps["cfg"].umbral("colas.inbox_edad_max_seg", 300))
    filas = _consulta(ctx, """
        SELECT client_id, COUNT(*) AS n,
               MAX(TIMESTAMPDIFF(SECOND, received_at, NOW())) AS edad_seg
          FROM channel_inbox
         WHERE status IN ('PENDING', 'RETRY')
           AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
           AND received_at >= NOW() - INTERVAL 2 DAY
         GROUP BY client_id
    """)
    if filas is None:
        return _sin_base()
    viejos = [f for f in filas if int(f.get("edad_seg") or 0) > tope]
    detalle = {"tenants_con_pendientes": len(filas), "maximo_seg": tope}
    if viejos:
        peor = max(viejos, key=lambda f: int(f["edad_seg"]))
        return CheckOutcome(
            state=State.STUCK, reason_code="INBOX_ESTANCADO",
            expected={"edad_maxima_seg": tope},
            actual={**detalle, "tenant": peor["client_id"],
                    "pendientes": int(peor["n"]),
                    "el_mas_viejo_seg": int(peor["edad_seg"])})
    return CheckOutcome(state=State.OK, actual=detalle)


# ---------------------------------------------------------------------------
# QUE-02: tomado y nunca soltado
# ---------------------------------------------------------------------------

def _que02(ctx: CheckContext) -> CheckOutcome:
    """Un mensaje en PROCESSING más allá de su arriendo.

    Significa que el proceso que lo tomó murió sin devolverlo. Nadie lo va a
    reintentar: queda tomado para siempre y el usuario nunca recibe respuesta.
    """
    lease = int(ctx.deps["cfg"].umbral("colas.processing_lease_seg", 600))
    filas = _consulta(ctx, """
        SELECT client_id, COUNT(*) AS n,
               MAX(TIMESTAMPDIFF(SECOND, received_at, NOW())) AS edad_seg
          FROM channel_inbox
         WHERE status = 'PROCESSING'
           AND received_at >= NOW() - INTERVAL 7 DAY
         GROUP BY client_id
    """)
    if filas is None:
        return _sin_base()
    colgados = [f for f in filas if int(f.get("edad_seg") or 0) > lease]
    if colgados:
        peor = max(colgados, key=lambda f: int(f["edad_seg"]))
        return CheckOutcome(
            state=State.STUCK, reason_code="MENSAJE_TOMADO_Y_ABANDONADO",
            expected={"lease_seg": lease},
            actual={"tenant": peor["client_id"], "tomados": int(peor["n"]),
                    "el_mas_viejo_seg": int(peor["edad_seg"])})
    return CheckOutcome(state=State.OK,
                        actual={"en_proceso": sum(int(f["n"]) for f in filas)})


# ---------------------------------------------------------------------------
# QUE-04: salida vencida
# ---------------------------------------------------------------------------

def _que04(ctx: CheckContext) -> CheckOutcome:
    tope = int(ctx.deps["cfg"].umbral("colas.outbox_vencida_seg", 300))
    filas = _consulta(ctx, """
        SELECT client_id, COUNT(*) AS n,
               MAX(TIMESTAMPDIFF(SECOND, COALESCE(scheduled_at, created_at), NOW()))
                   AS edad_seg
          FROM channel_outbox
         WHERE status IN ('scheduled', 'sending')
           AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
           AND COALESCE(scheduled_at, created_at) >= NOW() - INTERVAL 2 DAY
           AND COALESCE(scheduled_at, created_at) <= NOW()
         GROUP BY client_id
    """)
    if filas is None:
        return _sin_base()
    viejos = [f for f in filas if int(f.get("edad_seg") or 0) > tope]
    if viejos:
        peor = max(viejos, key=lambda f: int(f["edad_seg"]))
        return CheckOutcome(
            state=State.STUCK, reason_code="SALIDA_SIN_ENVIAR",
            expected={"edad_maxima_seg": tope},
            actual={"tenant": peor["client_id"], "sin_enviar": int(peor["n"]),
                    "el_mas_viejo_seg": int(peor["edad_seg"])})
    return CheckOutcome(state=State.OK,
                        actual={"en_cola": sum(int(f["n"]) for f in filas)})


# ---------------------------------------------------------------------------
# QUE-09: el pipeline de llamadas acumula
# ---------------------------------------------------------------------------

def _que09(ctx: CheckContext) -> CheckOutcome:
    """Backlog de la cohorte que ya tuvo tiempo de procesarse.

    Se mira la ventana de 24 a 48 horas atrás y no la última hora: las llamadas
    recientes están pendientes porque todavía se están bajando, y contarlas
    daría backlog siempre.
    """
    r = datos.backlog(ctx.deps)
    if not r.hay:
        return CheckOutcome(state=State.UNKNOWN, reason_code="SIN_DATOS_DE_COHORTE",
                            actual={"motivo": r.motivo},
                            evidence_quality=EvidenceQuality.NONE)
    tope = int(ctx.deps["cfg"].umbral("colas.pipeline_pendientes_max", 50))
    peor = None
    for f in (r.datos.get("cohorte") or []):
        total = int(f.get("sin_bajar") or 0) + int(f.get("sin_transcribir") or 0)
        if peor is None or total > peor[1]:
            peor = (f, total)
    if peor is None:
        return CheckOutcome(state=State.OK, reason_code="COHORTE_VACIA",
                            actual={"ventana": r.datos.get("ventana")},
                            evidence_quality=r.calidad)
    f, total = peor
    detalle = {"tenant": f.get("client_id"), "sin_bajar": int(f.get("sin_bajar") or 0),
               "sin_transcribir": int(f.get("sin_transcribir") or 0),
               "maximo": tope, "origen": r.origen}
    if total > tope:
        return CheckOutcome(
            state=State.DEGRADED, reason_code="PIPELINE_ACUMULANDO",
            expected={"pendientes_maximo": tope}, actual=detalle,
            evidence_quality=r.calidad)
    return CheckOutcome(state=State.OK, actual=detalle, evidence_quality=r.calidad)


# ---------------------------------------------------------------------------
# QUE-10: agenda vencida
# ---------------------------------------------------------------------------

def _que10(ctx: CheckContext) -> CheckOutcome:
    """Tareas de llamado cuya ventana se venció sin un solo intento.

    Tiene su caso de prueba esperando en producción: dos tareas de clienteb
    del 29 de julio, con la ventana vencida hace un mes y cero intentos.
    """
    gracia = int(ctx.deps["cfg"].umbral("colas.agenda_vencida_seg", 3600))
    filas = _consulta(ctx, """
        SELECT client_id, COUNT(*) AS n, SUM(attempts = 0) AS sin_intentos,
               MAX(TIMESTAMPDIFF(SECOND, available_until, NOW())) AS vencida_hace_seg
          FROM call_agenda
         WHERE status = 'scheduled'
           AND available_until IS NOT NULL
           AND available_until < NOW() - INTERVAL %s SECOND
           AND available_until >= NOW() - INTERVAL 90 DAY
         GROUP BY client_id
    """, (gracia,))
    if filas is None:
        return _sin_base()
    if not filas:
        return CheckOutcome(state=State.OK, actual={"vencidas": 0})
    peor = max(filas, key=lambda f: int(f.get("vencida_hace_seg") or 0))
    return CheckOutcome(
        state=State.STUCK, reason_code="AGENDA_VENCIDA",
        expected={"vencidas": 0},
        actual={"tenant": peor["client_id"], "vencidas": int(peor["n"]),
                "sin_un_solo_intento": int(peor.get("sin_intentos") or 0),
                "vencida_hace_horas": round(int(peor["vencida_hace_seg"] or 0) / 3600, 1)},
        severity_override=Severity.MAJOR)


CONTROLES = {
    "QUE-01": CheckDefinition(
        check_id="QUE-01", dependencies=("mysql",), version=1, titulo="Bandeja de entrada estancada",
        selector="host", expectation_id="colas.inbox", schedule_sec=30,
        evaluate=_que01, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=3),
        runbook="Mirar si el daemon del canal esta corriendo antes que la cola."),
    "QUE-02": CheckDefinition(
        check_id="QUE-02", dependencies=("mysql",), version=1, titulo="Mensaje tomado y abandonado",
        selector="host", expectation_id="colas.lease", schedule_sec=60,
        evaluate=_que02, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=2),
        runbook="El proceso que lo tomo murio sin devolverlo. Nadie lo "
                "reintenta solo."),
    "QUE-04": CheckDefinition(
        check_id="QUE-04", dependencies=("mysql",), version=1, titulo="Salida sin enviar",
        selector="host", expectation_id="colas.outbox", schedule_sec=60,
        evaluate=_que04, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=3)),
    "QUE-09": CheckDefinition(
        check_id="QUE-09", dependencies=("mysql",), version=1, titulo="Pipeline de llamadas acumulando",
        selector="host", expectation_id="colas.pipeline", schedule_sec=300,
        evaluate=_que09, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=2),
        runbook="Ver primero JOB-05/07 de procesarLlamadas: si el trabajo no "
                "corre, la cola es el sintoma y no la causa."),
    "QUE-10": CheckDefinition(
        check_id="QUE-10", dependencies=("mysql",), version=1, titulo="Agenda vencida",
        selector="host", expectation_id="colas.agenda", schedule_sec=60,
        evaluate=_que10, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=2),
        runbook="Una tarea vencida con cero intentos nunca se llamo y ya no se "
                "va a llamar sola."),
}


def planificar(cfg: Any, host_id: str) -> list[tuple[CheckDefinition, Resource]]:
    r = Resource(resource_id=f"host:{host_id}", host_id=host_id, kind="host",
                 state=ResourceState.MANAGED)
    return [(cd, r) for cd in CONTROLES.values()]
