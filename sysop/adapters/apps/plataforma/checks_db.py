"""Controles de MySQL.

Dos de los cuatro (DB-04 y DB-05) necesitan `PROCESS` y `SELECT` sobre
`performance_schema`. Verificado el 2026-08-27 contra srv2 con un usuario de
sólo `SELECT`: `performance_schema` devuelve error 1142 e
`information_schema.processlist` muestra una fila de las 232 conexiones que
había. Sin esos permisos estos controles no ven nada y devuelven `UNKNOWN`, que
es lo correcto, pero deja la mitad de la observabilidad de la base sin cubrir.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Optional

from sysop.adapters.apps.plataforma import mysql as mysql_col
from sysop.core.models import (CheckContext, CheckDefinition, CheckOutcome,
                               EvidenceQuality, Hysteresis, Resource,
                               ResourceState, Severity, State)

log = logging.getLogger("sysop.nac.checks")


@contextmanager
def _conexion(ctx: CheckContext):
    """La conexion del ciclo si la hay; si no, una propia.

    Cuatro controles de base abriendo cada uno su conexion son cuatro
    handshakes TLS por ciclo, y era la mitad del costo de CPU del observador.
    DB-01 igual abre la suya: su trabajo ES medir si se puede conectar, asi que
    reusar una conexion ya abierta lo volveria un control que no prueba nada.
    """
    compartida = ctx.deps.get("mysql")
    if compartida is not None:
        yield compartida
        return
    with mysql_col.conexion(ctx.budget.timeout_sec) as c:
        yield c


def _tiene_process(conn: Any, timeout_sec: float) -> bool:
    """Si el usuario tiene el privilegio PROCESS.

    Hay que preguntarlo explícitamente, y esta es la razón: sin `PROCESS`,
    consultar `information_schema.processlist` **no da error**. Devuelve
    únicamente la sesión propia. O sea que un control que busque consultas
    largas encuentra cero y concluye que todo anda bien, cuando en realidad no
    está viendo nada. Un verde falso es peor que un rojo: el rojo se investiga.
    """
    try:
        filas = mysql_col.consultar(conn, "SHOW GRANTS FOR CURRENT_USER()",
                                    timeout_sec=timeout_sec)
    except Exception:  # noqa: BLE001
        return False
    for f in filas:
        for v in f.values():
            if isinstance(v, str) and "PROCESS" in v.upper() and "*.*" in v:
                return True
    return False


def _status(conn: Any, nombres: tuple[str, ...]) -> dict[str, int]:
    marcadores = ",".join(["%s"] * len(nombres))
    filas = mysql_col.consultar(
        conn,
        "SELECT VARIABLE_NAME n, VARIABLE_VALUE v FROM performance_schema.global_status "
        f"WHERE VARIABLE_NAME IN ({marcadores})",
        nombres,
    )
    out = {}
    for f in filas:
        try:
            out[f["n"]] = int(f["v"])
        except (TypeError, ValueError):
            pass
    return out


# ---------------------------------------------------------------------------
# DB-01: se puede conectar y consultar
# ---------------------------------------------------------------------------

def _db01(ctx: CheckContext) -> CheckOutcome:
    inicio = time.monotonic()
    with mysql_col.conexion(ctx.budget.timeout_sec) as conn:
        if conn is None:
            # Es el control raíz de toda la familia: si esto falla, los demás
            # controles de base van a dar UNKNOWN, y la correlación tiene que
            # poder colgarlos de acá en vez de mandar cinco alertas sueltas.
            return CheckOutcome(state=State.FAILED, reason_code="DB_INACCESIBLE",
                                expected={"conecta": True}, actual={"conecta": False},
                                severity_override=Severity.CRITICAL)
        try:
            mysql_col.consultar(conn, "SELECT 1 AS x", timeout_sec=ctx.budget.timeout_sec)
        except mysql_col.ConsultaTimeout:
            return CheckOutcome(state=State.DEGRADED, reason_code="DB_TIMEOUT",
                                expected={"responde_en_seg": ctx.budget.timeout_sec},
                                severity_override=Severity.CRITICAL)
        except Exception as e:  # noqa: BLE001
            return CheckOutcome(state=State.FAILED, reason_code="DB_ERROR_CONSULTA",
                                actual={"error": str(e)[:200]},
                                severity_override=Severity.CRITICAL)
    ms = (time.monotonic() - inicio) * 1000
    return CheckOutcome(state=State.OK, actual={"latencia_ms": round(ms, 1)})


# ---------------------------------------------------------------------------
# DB-02: conexiones contra el máximo
# ---------------------------------------------------------------------------

def _db02(ctx: CheckContext) -> CheckOutcome:
    umbrales = ctx.deps["cfg"].umbral("db.umbrales_conexiones_pct",
                                      {"warning": 70, "major": 85, "critical": 95})
    with _conexion(ctx) as conn:
        if conn is None:
            return CheckOutcome(state=State.UNKNOWN, reason_code="DB_INACCESIBLE",
                                evidence_quality=EvidenceQuality.NONE)
        st = _status(conn, ("Threads_connected", "Threads_running", "Max_used_connections"))
        filas = mysql_col.consultar(
            conn, "SELECT @@max_connections AS m", timeout_sec=ctx.budget.timeout_sec)

    maximo = int(filas[0]["m"]) if filas else 0
    conectadas = st.get("Threads_connected", 0)
    if not maximo:
        return CheckOutcome(state=State.UNKNOWN, reason_code="MAX_CONNECTIONS_ILEGIBLE",
                            evidence_quality=EvidenceQuality.NONE)

    pct = 100.0 * conectadas / maximo
    actual = {"conectadas": conectadas, "maximo": maximo, "pct": round(pct, 1),
              "corriendo": st.get("Threads_running"),
              "pico_historico": st.get("Max_used_connections")}

    for sev, clave in ((Severity.CRITICAL, "critical"), (Severity.MAJOR, "major"),
                       (Severity.WARNING, "warning")):
        if pct >= umbrales.get(clave, 101):
            return CheckOutcome(state=State.DEGRADED, reason_code="DB_CONEXIONES_ALTAS",
                                expected={"pct_max": umbrales.get("warning")},
                                actual=actual, severity_override=sev)
    return CheckOutcome(state=State.OK, actual=actual)


# ---------------------------------------------------------------------------
# DB-04: consultas o transacciones largas
# ---------------------------------------------------------------------------

def _db04(ctx: CheckContext) -> CheckOutcome:
    umbrales = ctx.deps["cfg"].umbral("db.umbrales_consulta_seg",
                                      {"warning": 30, "major": 120, "critical": 300})
    minimo = min(umbrales.values())

    with _conexion(ctx) as conn:
        if conn is None:
            return CheckOutcome(state=State.UNKNOWN, reason_code="DB_INACCESIBLE",
                                evidence_quality=EvidenceQuality.NONE)

        # Se pregunta ANTES de consultar. Sin PROCESS la consulta no falla:
        # devuelve sólo la sesión propia, encuentra cero consultas largas y
        # este control informaría OK sin haber visto nada.
        if not _tiene_process(conn, ctx.budget.timeout_sec):
            return CheckOutcome(
                state=State.UNKNOWN, reason_code="SIN_PERMISO_PROCESS",
                actual={"detalle": "el usuario de monitoreo no tiene PROCESS: "
                                   "solo ve su propia conexion"},
                evidence_quality=EvidenceQuality.NONE)
        try:
            # Se excluyen las de sistema y la propia: el hilo del event_scheduler
            # y los de replicación llevan días "corriendo" por diseño, y
            # contarlos daría una alerta permanente que no significa nada.
            filas = mysql_col.consultar(
                conn,
                "SELECT p.ID, p.USER, p.DB, p.TIME, LEFT(p.INFO, 200) AS q "
                "FROM information_schema.processlist p "
                "WHERE p.COMMAND NOT IN ('Sleep','Daemon','Binlog Dump') "
                "  AND p.ID <> CONNECTION_ID() AND p.TIME >= %s "
                "ORDER BY p.TIME DESC LIMIT 20",
                (minimo,), timeout_sec=ctx.budget.timeout_sec,
            )
        except Exception as e:  # noqa: BLE001
            # Sin PROCESS sólo se ve la sesión propia. Eso no es "todo bien":
            # es no poder mirar.
            return CheckOutcome(state=State.UNKNOWN, reason_code="SIN_PERMISO_PROCESS",
                                actual={"error": str(e)[:160]},
                                evidence_quality=EvidenceQuality.NONE)

    if not filas:
        return CheckOutcome(state=State.OK, actual={"consultas_largas": 0})

    peor = max(int(f["TIME"] or 0) for f in filas)
    sev = Severity.WARNING
    if peor >= umbrales.get("critical", 300):
        sev = Severity.CRITICAL
    elif peor >= umbrales.get("major", 120):
        sev = Severity.MAJOR

    return CheckOutcome(
        state=State.DEGRADED, reason_code="DB_CONSULTA_LARGA",
        expected={"duracion_max_seg": umbrales.get("warning")},
        actual={"consultas_largas": len(filas), "peor_seg": peor,
                # La consulta se recorta: puede traer literales con datos de
                # cliente, y la evidencia se guarda y puede ir al modelo.
                "muestra": [{"seg": f["TIME"], "usuario": f["USER"],
                             "sql": (f["q"] or "")[:120]} for f in filas[:3]]},
        severity_override=sev)


# ---------------------------------------------------------------------------
# DB-05: esperas de bloqueo
# ---------------------------------------------------------------------------

def _db05(ctx: CheckContext) -> CheckOutcome:
    with _conexion(ctx) as conn:
        if conn is None:
            return CheckOutcome(state=State.UNKNOWN, reason_code="DB_INACCESIBLE",
                                evidence_quality=EvidenceQuality.NONE)
        try:
            filas = mysql_col.consultar(
                conn,
                "SELECT r.trx_id AS esperando, b.trx_id AS bloqueante, "
                "       r.trx_mysql_thread_id AS hilo, "
                "       TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS seg "
                "FROM performance_schema.data_lock_waits w "
                "JOIN information_schema.innodb_trx r "
                "  ON r.trx_mysql_thread_id = ("
                "     SELECT PROCESSLIST_ID FROM performance_schema.threads "
                "     WHERE THREAD_ID = w.REQUESTING_THREAD_ID) "
                "JOIN information_schema.innodb_trx b "
                "  ON b.trx_mysql_thread_id = ("
                "     SELECT PROCESSLIST_ID FROM performance_schema.threads "
                "     WHERE THREAD_ID = w.BLOCKING_THREAD_ID) "
                "LIMIT 20",
                timeout_sec=ctx.budget.timeout_sec,
            )
        except Exception as e:  # noqa: BLE001
            return CheckOutcome(state=State.UNKNOWN, reason_code="SIN_PERMISO_PERFSCHEMA",
                                actual={"error": str(e)[:160]},
                                evidence_quality=EvidenceQuality.NONE)

    if not filas:
        return CheckOutcome(state=State.OK, actual={"esperas": 0})
    return CheckOutcome(
        state=State.DEGRADED, reason_code="DB_LOCK_WAIT",
        expected={"esperas": 0},
        actual={"esperas": len(filas),
                "peor_seg": max(int(f["seg"] or 0) for f in filas)},
        severity_override=Severity.MAJOR)


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

CONTROLES = {
    "DB-01": CheckDefinition(
        check_id="DB-01", version=1, titulo="La base responde",
        # Cada 30 s y no cada 15: este control abre su PROPIA conexion a
        # proposito (reusar la del ciclo lo volveria un control que no prueba
        # nada), y ese handshake TLS cuesta 78 ms medidos. A 15 segundos era
        # 0,52% de un core el solo, la mitad del presupuesto entero. Con la
        # histeresis de dos muestras una caida se detecta en 60 s en vez de 30,
        # que es barato al lado de duplicar el costo del ciclo.
        selector="db:negocio", expectation_id="db.conecta", schedule_sec=30,
        evaluate=_db01, severity=Severity.CRITICAL, timeout_sec=3.0,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=3),
        runbook="Si falla, los demas controles de base dan UNKNOWN: este es el "
                "incidente raiz y los otros son sintomas."),
    "DB-02": CheckDefinition(
        check_id="DB-02", dependencies=("mysql",), version=1, titulo="Conexiones contra el maximo",
        selector="db:negocio", expectation_id="db.conexiones", schedule_sec=30,
        evaluate=_db02, severity=Severity.WARNING,
        hysteresis=Hysteresis(abre_tras=3, cierra_tras=3),
        runbook="Mirar si crecen las Sleep: suele ser una app que no devuelve "
                "conexiones al pool, no carga real."),
    "DB-04": CheckDefinition(
        check_id="DB-04", dependencies=("mysql",), version=1, titulo="Consultas largas",
        selector="db:negocio", expectation_id="db.consultas", schedule_sec=30,
        evaluate=_db04, severity=Severity.WARNING,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=2)),
    "DB-05": CheckDefinition(
        check_id="DB-05", dependencies=("mysql",), version=1, titulo="Esperas de bloqueo",
        selector="db:negocio", expectation_id="db.locks", schedule_sec=30,
        evaluate=_db05, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=2)),
}


def planificar(cfg: Any, host_id: str) -> list[tuple[CheckDefinition, Resource]]:
    r = Resource(resource_id="db:negocio", host_id=host_id, kind="db",
                 scope={"base": "plataforma"}, state=ResourceState.MANAGED)
    return [(cd, r) for cd in CONTROLES.values()]
