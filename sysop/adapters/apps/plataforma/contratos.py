"""Qué tiene que haber PRODUCIDO cada trabajo, no si corrió.

La diferencia es todo el punto de JOB-08. Un trabajo puede lanzarse puntual,
salir con código cero y no haber hecho nada: `syncLastCalls` que no le saca
una respuesta a Telefonia es exactamente eso, y los controles anteriores
pueden darlo por sano.

Cada contrato mira el efecto en los datos, no el proceso.

Un trabajo sin contrato declarado responde `UNKNOWN`, nunca `OK`. Decir "está
bien" de algo que no se verificó es la forma más cara de mentir que tiene un
observador.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from sysop.adapters.apps.plataforma import cron, datos
from sysop.adapters.apps.plataforma import mysql as mysql_col
from sysop.core.models import (CheckContext, CheckOutcome, EvidenceQuality,
                               State)

log = logging.getLogger("sysop.nac.contratos")


def verificar(ctx: CheckContext, job: dict[str, Any],
              calidad: EvidenceQuality) -> CheckOutcome:
    """Aplica el contrato del trabajo, si tiene uno."""
    script = job.get("script")
    if not script and not job.get("parseable"):
        return CheckOutcome(
            state=State.UNKNOWN, reason_code="COMANDO_NO_INTERPRETADO",
            actual={"motivo": "el back no respondio y el comando no se parseo"},
            evidence_quality=EvidenceQuality.NONE)

    # Un trabajo en modo prueba no tiene que cumplir su contrato. `--solo` acota
    # la corrida a un solo registro a propósito, así que exigirle el resultado
    # completo produce una falla por día, para siempre, hasta que alguien
    # aprende a ignorar el correo.
    args = job.get("args") or {}
    if "solo" in args:
        return CheckOutcome(
            state=State.DISABLED_EXPECTED, reason_code="JOB_EN_MODO_PRUEBA",
            actual={"solo": str(args["solo"])[:40]},
            evidence_quality=calidad)

    fn = CONTRATOS.get(script or "")
    if fn is None:
        return CheckOutcome(
            state=State.UNKNOWN, reason_code="SIN_CONTRATO_DECLARADO",
            actual={"script": script},
            evidence_quality=EvidenceQuality.NONE)

    conn = ctx.deps.get("mysql")
    if conn is None:
        return CheckOutcome(
            state=State.UNKNOWN, reason_code="SIN_BASE_PARA_VERIFICAR",
            evidence_quality=EvidenceQuality.NONE)
    try:
        return fn(ctx, job, conn, calidad)
    except Exception as e:  # noqa: BLE001
        log.warning("fallo el contrato de %s: %s", script, e)
        return CheckOutcome(state=State.UNKNOWN, reason_code="CONTRATO_NO_VERIFICABLE",
                            actual={"error": str(e)[:200]},
                            evidence_quality=EvidenceQuality.NONE)


def _tenant(job: dict[str, Any]) -> Optional[str]:
    return (job.get("args") or {}).get("client_id") or job.get("client_id")


# ---------------------------------------------------------------------------
# syncLastCalls: Telefonia responde
# ---------------------------------------------------------------------------
#
# LO QUE SE MIRA ES SI TELEFONIA CONTESTO, NO SI ENTRARON LLAMADAS.
#
# Hasta el 2026-09-17 esto comparaba `MAX(start_time)` contra un tope fijo de
# 180 minutos. En srv2, clienteb no tuvo llamadas de 05:36 a 11:00 UTC y salio
# como "ingesta congelada" con el sync respondiendo bien cada diez minutos: 86
# respuestas ese dia, ni una falla. Que no haya llamadas nuevas no es un error;
# que Telefonia no conteste, si. Y un horario de trabajo por cliente tampoco
# era la salida: deja sin mirar todo lo que pase fuera de ese horario.
#
# La prueba de que contesto la deja el propio sync en su log, en CADA corrida,
# tambien cuando no hay nada nuevo:
#
#   ringex  [rcex] get_records_telefonia: N records en P pagina(s) (...)
#   ringcx  [rcx] N llamadas obtenidas en total
#
# ESOS DOS MENSAJES SON EL CONTRATO (acmeBot/services/sync_telefonia_service.py
# y sync_ringcx_service.py). Si se cambian alla, se cambian aca.

FUENTES_SYNC = {
    "ringex": ("syncLastCalls",
               re.compile(r"get_records_telefonia: (\d+) records")),
    "ringcx": ("syncLastCallsCX",
               re.compile(r"\[rcx\] (\d+) llamadas obtenidas en total")),
}
# `LOG_DIR/<componente>/client_<tenant>.log`, como lo arma
# acmeBot/helpers/logging_setup.py. Se puede cambiar en app.yaml.
SYNC_LOG = "/var/log/plataforma/{componente}/client_{tenant}.log"
# Cuanto del final del log se lee. Un dia de un cliente grande son ~270 KB y la
# ventana mas larga que se mira es de media hora larga.
SYNC_COLA_BYTES = 512 * 1024
# Lo que se le da a la ultima corrida para terminar, encima de las corridas.
SYNC_GRACIA_SEG = 120

_LINEA = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
                    r"(?:Z|[+-]\d{2}:?\d{2})?)\s+([A-Z]+)\s")


def _sync_last_calls(ctx: CheckContext, job: dict[str, Any], conn: Any,
                     calidad: EvidenceQuality) -> CheckOutcome:
    """En las ultimas corridas, Telefonia respondio al menos una vez.

    Se da por falla cuando en `N` periodos del cron (3 por defecto: media hora
    para el sync de cada diez minutos) no hay una sola respuesta:

        TELEFONIA_SIN_RESPUESTA   el sync escribio (intentos, errores) pero
                                    Telefonia no contesto
        SYNC_SIN_ACTIVIDAD          el sync no escribio nada: no corre, o
                                    quedo colgado

    Sin el archivo del log no se puede afirmar nada: UNKNOWN, y queda en la
    ceguera del informe. Leer la ruta equivocada y gritar "no responde" es el
    error que ya se cometio con el ledger vacio (2026-09-03).
    """
    tenant = _tenant(job) or ""
    cfg = ctx.deps["cfg"]
    corridas = int(cfg.umbral("jobs.contratos.sync_corridas_sin_respuesta", 3))
    plantilla = str(cfg.umbral("jobs.contratos.sync_log", SYNC_LOG))
    periodo = 600
    try:
        periodo = cron.periodo_seg(job.get("schedule") or "",
                                   job.get("timezone") or "UTC") or 600
    except cron.CronInvalido:
        pass
    ventana = timedelta(seconds=corridas * periodo + SYNC_GRACIA_SEG)
    desde = ctx.now - ventana

    source = str((job.get("args") or {}).get("source") or "both")
    fuentes = ["ringex", "ringcx"] if source == "both" else [source]
    vistas = [_respuestas_de(plantilla, tenant, f, desde, ctx.now)
              for f in fuentes if f in FUENTES_SYNC]
    if not vistas:
        return CheckOutcome(state=State.UNKNOWN, reason_code="FUENTE_DE_SYNC_DESCONOCIDA",
                            actual={"source": source},
                            evidence_quality=EvidenceQuality.NONE)

    detalle = {"tenant": tenant, "ventana_min": round(ventana.total_seconds() / 60),
               "corridas_sin_respuesta_max": corridas,
               "fuentes": [{k: v for k, v in x.items() if k != "estado"} for x in vistas]}
    estados = {x["estado"] for x in vistas}
    for estado, codigo in (("sin_respuesta", "TELEFONIA_SIN_RESPUESTA"),
                           ("sin_actividad", "SYNC_SIN_ACTIVIDAD")):
        if estado in estados:
            return CheckOutcome(
                state=State.FAILED, reason_code=codigo,
                expected={"respuestas_de_telefonia": ">= 1 en la ventana"},
                actual=detalle, evidence_quality=EvidenceQuality.FULL)
    if "sin_log" in estados:
        return CheckOutcome(state=State.UNKNOWN, reason_code="LOG_DEL_SYNC_NO_ENCONTRADO",
                            actual=detalle, evidence_quality=EvidenceQuality.NONE)
    return CheckOutcome(state=State.OK, actual=detalle,
                        evidence_quality=EvidenceQuality.FULL)


def _respuestas_de(plantilla: str, tenant: str, fuente: str,
                   desde: datetime, hasta: datetime) -> dict[str, Any]:
    """Que dice el log de una fuente entre `desde` y `hasta`."""
    componente, patron = FUENTES_SYNC[fuente]
    ruta = Path(plantilla.format(componente=componente, tenant=tenant))
    out: dict[str, Any] = {"fuente": fuente, "log": str(ruta)}
    try:
        lineas = _cola(ruta)
    except OSError as e:
        out.update(estado="sin_log", error=str(e)[:160])
        return out
    # Recien rotado, el archivo nuevo puede no llegar a cubrir la ventana: se
    # suma el final del anterior.
    primera = next((t for t in map(_fecha_de, lineas) if t is not None), None)
    anterior = ruta.with_name(ruta.name + ".1")
    if (primera is None or primera > desde) and anterior.exists():
        try:
            lineas = _cola(anterior) + lineas
        except OSError:
            pass

    respuestas = intentos = 0
    ultima_respuesta = registros = ultima_linea = ultimo_error = None
    for linea in lineas:
        m = _LINEA.match(linea)
        if not m:
            continue
        t = _fecha_de(linea)
        if t is not None and t > hasta:
            break
        r = patron.search(linea)
        if r:
            ultima_respuesta, registros = t, int(r.group(1))
        if t is None or t < desde:
            continue
        ultima_linea = t
        if r:
            respuestas += 1
        if "sync cliente" in linea:
            intentos += 1
        if m.group(2) in ("ERROR", "CRITICAL"):
            ultimo_error = linea.split(" | ", 1)[-1][:300]

    out.update(respuestas_en_ventana=respuestas, intentos_en_ventana=intentos,
               ultima_respuesta=ultima_respuesta.isoformat() if ultima_respuesta else None,
               registros_ultima_respuesta=registros,
               ultimo_error=ultimo_error)
    if respuestas:
        out["estado"] = "ok"
    elif ultima_linea is None:
        out["estado"] = "sin_actividad"
    else:
        out["estado"] = "sin_respuesta"
    return out


def _cola(ruta: Path) -> list[str]:
    with ruta.open("rb") as f:
        f.seek(0, 2)
        tam = f.tell()
        f.seek(max(0, tam - SYNC_COLA_BYTES))
        crudo = f.read()
    lineas = crudo.decode("utf-8", "replace").splitlines()
    # Saltando al medio del archivo, la primera linea casi seguro esta cortada.
    return lineas[1:] if tam > SYNC_COLA_BYTES else lineas


def _fecha_de(linea: str) -> Optional[datetime]:
    m = _LINEA.match(linea)
    if not m:
        return None
    try:
        t = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# procesarLlamadas: la cohorte elegible baja
# ---------------------------------------------------------------------------

def _procesar_llamadas(ctx: CheckContext, job: dict[str, Any], conn: Any,
                       calidad: EvidenceQuality) -> CheckOutcome:
    """Las llamadas elegibles de hace un día tienen que estar procesadas.

    La elegibilidad NO se inventa: la duración mínima es el primer argumento
    posicional del comando en el cron, no un valor de la base. Usar otro número
    hace que el control cuente como pendientes llamadas que el trabajo nunca va
    a tomar, y reporte un backlog que no existe.
    """
    tenant = _tenant(job)
    dur = _duracion_minima(job)
    if dur is None:
        return CheckOutcome(
            state=State.UNKNOWN, reason_code="ELEGIBILIDAD_DESCONOCIDA",
            actual={"motivo": "no se pudo leer la duracion minima del comando"},
            evidence_quality=EvidenceQuality.NONE)

    r = datos.backlog(ctx.deps, duracion_minima=dur)
    if not r.hay:
        return CheckOutcome(state=State.UNKNOWN, reason_code="SIN_DATOS_DE_COHORTE",
                            actual={"motivo": r.motivo},
                            evidence_quality=EvidenceQuality.NONE)

    tope = int(ctx.deps["cfg"].umbral("jobs.contratos.pendientes_max", 20))
    fila = None
    for f in (r.datos.get("cohorte") or []):
        if not tenant or f.get("client_id") == tenant:
            fila = f
            break
    if fila is None:
        # Cohorte vacía: no hubo llamadas elegibles hace un día. No es falla ni
        # es éxito, es que no había nada que hacer.
        return CheckOutcome(state=State.OK, reason_code="COHORTE_VACIA",
                            actual={"tenant": tenant, "duracion_minima": dur},
                            evidence_quality=r.calidad)

    sin_transcribir = int(fila.get("sin_transcribir") or 0)
    detalle = {"tenant": fila.get("client_id"), "duracion_minima": dur,
               "llamadas_cohorte": int(fila.get("llamadas") or 0),
               "sin_transcribir": sin_transcribir,
               "sin_bajar": int(fila.get("sin_bajar") or 0), "maximo": tope}
    if sin_transcribir > tope:
        return CheckOutcome(
            state=State.FAILED, reason_code="COHORTE_SIN_PROCESAR",
            expected={"pendientes_maximo": tope}, actual=detalle,
            evidence_quality=r.calidad)
    return CheckOutcome(state=State.OK, actual=detalle, evidence_quality=r.calidad)


def _duracion_minima(job: dict[str, Any]) -> Optional[int]:
    """El primer posicional del comando: `procesarLlamadas.py 10 --today`."""
    args = job.get("args") or {}
    if "min_duracion" in args:
        try:
            return int(args["min_duracion"])
        except (TypeError, ValueError):
            return None
    for p in (job.get("posicionales") or []):
        try:
            return int(p)
        except (TypeError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# dept_email_report: el informe salio
# ---------------------------------------------------------------------------

def _dept_email_report(ctx: CheckContext, job: dict[str, Any], conn: Any,
                       calidad: EvidenceQuality) -> CheckOutcome:
    """Que la corrida del día haya terminado bien.

    Es un contrato PARCIAL y hay que decirlo: verifica que la corrida existió y
    salió con código cero, no que el correo haya llegado. La plataforma no
    registra los envíos de este reporte en ninguna tabla, así que la aceptación
    del servidor SMTP no es verificable desde acá.

    Para que sea FULL haría falta que el trabajo deje rastro de lo que mandó:
    destinatarios y `Message-ID`, como hace el outbox del propio observador.
    """
    filas = mysql_col.consultar(
        conn, "SELECT status, exit_code, finished_at FROM rc_cron_runs "
              "WHERE job_id = %s AND started_at >= NOW() - INTERVAL 1 DAY "
              "ORDER BY started_at DESC LIMIT 1",
        (job["job_id"],), timeout_sec=ctx.budget.timeout_sec)
    if not filas:
        return CheckOutcome(
            state=State.UNKNOWN, reason_code="SIN_CORRIDA_EN_24H",
            actual={"nota": "el ledger no tiene corridas de las ultimas 24 h"},
            evidence_quality=EvidenceQuality.NONE)
    f = filas[0]
    detalle = {"estado": f["status"], "exit_code": f.get("exit_code"),
               "cuando": str(f.get("finished_at")),
               "alcance": "solo la ejecucion: el envio no queda registrado"}
    if f["status"] != "success":
        return CheckOutcome(
            state=State.FAILED, reason_code="INFORME_NO_GENERADO",
            expected={"estado": "success"}, actual=detalle,
            evidence_quality=EvidenceQuality.PARTIAL)
    return CheckOutcome(state=State.OK, actual=detalle,
                        evidence_quality=EvidenceQuality.PARTIAL)


CONTRATOS: dict[str, Callable[..., CheckOutcome]] = {
    "syncLastCalls.py": _sync_last_calls,
    "procesarLlamadas.py": _procesar_llamadas,
    "dept_email_report.py": _dept_email_report,
}
