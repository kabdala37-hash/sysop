"""Controles de los trabajos programados.

La pregunta que contesta esta familia no es "¿el cron está andando?" sino
**"¿el trabajo hizo lo que tenía que hacer?"**, que son cosas distintas: un
trabajo puede lanzarse puntual, salir con código 0 y no haber producido nada.

Cinco formas de fallar, y cada una necesita su control:

    no existe la definicion   JOB-03
    no se lanzo               JOB-05
    se lanzo y no volvio      JOB-06
    volvio con error          JOB-07
    volvio bien y no hizo     JOB-08   <- el mas valioso y el mas caro

Más dos de infraestructura: el planificador caído (JOB-14) y el bloqueo
abandonado que impide que el trabajo vuelva a arrancar (JOB-10).
"""
from __future__ import annotations

import fnmatch
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from sysop.adapters.apps.plataforma import contratos, cron, datos, locks
# El adaptador de la aplicación puede leer del sistema operativo, pero no al
# revés: `cron.service` es infraestructura del SO de la que esta plataforma
# depende. La dependencia va en un solo sentido a propósito.
from sysop.adapters.so_linux.collectors import journal, systemd
from sysop.core.models import (CheckContext, CheckDefinition, CheckOutcome,
                               EvidenceQuality, Hysteresis, Resource,
                               ResourceState, Severity, State)

log = logging.getLogger("sysop.nac.jobs")

PIDFILE_DIR = "/tmp"
PIDFILE_PATRON = "plataforma_*.pid"
# Locks que la plataforma NO borra al soltarlos, a proposito: sin bloqueo vivo
# son el estado normal, no un proceso muerto. El de sbcSync lo toma y suelta
# `line_supervisor._reconciliar_sbc` en cada cambio de lineas y nunca le
# escribe PID (acmeBot/shooters/line_supervisor.py).
PIDFILES_PERSISTENTES = ("plataforma_sbcSync_*.pid",)


def _jobs(ctx: CheckContext) -> tuple[list[dict[str, Any]], EvidenceQuality, str]:
    r = datos.contratos_jobs(ctx.deps)
    return (r.datos or []), r.calidad, r.motivo


def _job(ctx: CheckContext) -> tuple[Optional[dict[str, Any]], EvidenceQuality, str]:
    """El trabajo al que apunta este recurso."""
    jobs, calidad, motivo = _jobs(ctx)
    jid = ctx.resource.scope.get("job_id")
    for j in jobs:
        if j.get("job_id") == jid:
            return j, calidad, motivo
    return None, calidad, motivo


def _sin_datos(motivo: str, extra: Optional[dict] = None,
               codigo: str = "SIN_DATOS_DEL_JOB") -> CheckOutcome:
    """Ceguera declarada. El CODIGO distingue de que ceguera se trata.

    Antes todas salian con `SIN_DATOS_DEL_JOB` y el motivo real quedaba
    enterrado en `actual`. Medido en srv3 sobre 3 horas: 2.014 mediciones eran
    "la base no responde" y 3.789 "no hay corridas en el ledger". Son dos
    problemas distintos -- uno se arregla cuando vuelve la base, el otro cuando
    alguien escriba el ledger -- y con el mismo codigo el informe los mezcla en
    una sola linea.
    """
    return CheckOutcome(state=State.UNKNOWN, reason_code=codigo,
                        actual={"motivo": motivo, **(extra or {})},
                        evidence_quality=EvidenceQuality.NONE)


def _apagado(j: Optional[dict[str, Any]],
             calidad: EvidenceQuality) -> Optional[CheckOutcome]:
    """Un trabajo DESACTIVADO no es un punto ciego: no hay nada que medir.

    JOB-08 ya lo hacia y JOB-06/07 no, aunque miran el mismo recurso. El efecto
    en srv3, donde los 6 trabajos estan en `is_active = 0`: 12.711 mediciones de
    JOB-07 reportadas como ceguera, el 100% de las suyas, cuando en realidad no
    habia nada que ver. Un control que grita todos los dias por algo que esta
    apagado a proposito entrena a ignorar los controles.

    En srv2, donde 13 de 17 trabajos estan activos, esto NO tapa nada: esos 13
    siguen dando UNKNOWN mientras el ledger este vacio, que es ceguera de la
    buena.
    """
    if j is not None and not j.get("is_active"):
        return CheckOutcome(state=State.DISABLED_EXPECTED,
                            actual={"activo": False,
                                    "detalle": "el trabajo esta desactivado: "
                                               "no hay corridas que mirar"},
                            evidence_quality=calidad)
    return None


# ---------------------------------------------------------------------------
# JOB-03: el trabajo esta activo pero no se puede ejecutar
# ---------------------------------------------------------------------------

def _job03(ctx: CheckContext) -> CheckOutcome:
    """Un trabajo activo cuyo script o intérprete no existe.

    Falla en silencio absoluto: cron lo lanza, el shell no encuentra el archivo,
    y como siete de trece trabajos tiran su salida, no queda rastro en ningún
    lado. Se detecta mirando el disco, no esperando a que falle.
    """
    j, calidad, motivo = _job(ctx)
    if j is None:
        return _sin_datos(motivo or "el job no esta en el inventario")
    if not j.get("is_active"):
        return CheckOutcome(state=State.DISABLED_EXPECTED,
                            actual={"activo": False}, evidence_quality=calidad)

    crudo = j.get("comando") or j.get("comando_crudo") or ""
    rutas = [p for p in crudo.split()[:3]
             if p.startswith("/") and (p.endswith(".py") or "/bin/" in p)]
    if not rutas:
        # No haber encontrado ninguna ruta que verificar NO es haber verificado
        # que estan todas. Es el mismo patron de siempre: una fuente que no se
        # puede leer devuelve lo mismo que una que esta vacia.
        return CheckOutcome(
            state=State.UNKNOWN, reason_code="COMANDO_SIN_RUTAS",
            actual={"comando": crudo[:120] or "(vacio)"},
            evidence_quality=EvidenceQuality.NONE)

    faltan = [p for p in rutas if not os.path.exists(p)]
    if faltan:
        return CheckOutcome(
            state=State.FAILED, reason_code="EJECUTABLE_AUSENTE",
            expected={"existe": True}, actual={"faltan": faltan},
            evidence_quality=calidad, severity_override=Severity.CRITICAL)
    return CheckOutcome(state=State.OK,
                        actual={"verificadas": rutas},
                        evidence_quality=calidad)


# ---------------------------------------------------------------------------
# JOB-05: no se lanzo cuando tenia que lanzarse
# ---------------------------------------------------------------------------

def _job05(ctx: CheckContext) -> CheckOutcome:
    j, calidad, motivo = _job(ctx)
    if j is None:
        return _sin_datos(motivo or "el job no esta en el inventario")
    if not j.get("is_active"):
        return CheckOutcome(state=State.DISABLED_EXPECTED, actual={"activo": False},
                            evidence_quality=calidad)

    expr = j.get("schedule") or ""
    tzn = j.get("timezone") or "UTC"
    ahora = datetime.now(timezone.utc)
    try:
        esperado = cron.ultima_esperada(expr, ahora, tzn)
    except cron.CronInvalido as e:
        return CheckOutcome(
            state=State.FAILED, reason_code="EXPRESION_CRON_INVALIDA",
            actual={"schedule": expr, "error": str(e)[:120]},
            evidence_quality=calidad, severity_override=Severity.MAJOR)
    if esperado is None:
        return _sin_datos(f"la expresion {expr!r} no tiene un lanzamiento reciente")

    ultimo, fuente = _ultimo_lanzamiento(ctx, j)
    if ultimo is None:
        # Nunca corrió: puede ser un trabajo recién creado. Se compara contra
        # cuándo se creó o actualizó, no contra el principio de los tiempos.
        desde = _fecha(j.get("updated_at"))
        if desde and (ahora - desde).total_seconds() < 2 * 3600:
            return CheckOutcome(state=State.UNKNOWN, reason_code="SIN_CORRIDAS_TODAVIA",
                                actual={"actualizado": str(desde)},
                                evidence_quality=EvidenceQuality.PARTIAL)

        # ANTES DE DECIR QUE NO CORRIO, PREGUNTARLE A CRON.
        #
        # La aplicacion puede estar callada por dos razones muy distintas: que el
        # trabajo no corra, o que corra y nadie lo anote. El journal lo escribe
        # cron y no la aplicacion, asi que es lo unico que las separa. Sin esto,
        # en srv2 salieron trece `NUNCA_SE_LANZO` sobre trabajos que corrian.
        visto, pude_mirar = _lo_vio_cron(j, esperado)
        if visto is not None:
            return CheckOutcome(
                state=State.DEGRADED, reason_code="LEDGER_NO_SE_ESCRIBE",
                expected={"corridas_registradas": ">0"},
                actual={"corridas": 0, "cron_lo_lanzo": visto.isoformat(),
                        "fuente": "journal",
                        "nota": "el trabajo SI corre; lo que falta es el "
                                "registro en rc_cron_runs. Suele ser un cron.d "
                                "renderizado antes de que existiera "
                                "scripts/job_runner.py: se arregla "
                                "re-renderizando el trabajo"},
                evidence_quality=EvidenceQuality.PARTIAL,
                severity_override=Severity.WARNING)
        if not pude_mirar:
            # No se pudo leer el journal: no se puede afirmar que no corrio.
            return _sin_datos(
                "sin corridas en el ledger y no se pudo leer el journal de cron",
                {"corridas": 0}, "SIN_FUENTE_PARA_CORRIDAS")

        # Las dos fuentes coinciden en que no se lanzo. Eso ya es un hallazgo.
        return CheckOutcome(
            state=State.MISSING, reason_code="NUNCA_SE_LANZO",
            expected={"desde": esperado.isoformat()},
            actual={"corridas": 0, "cron_tampoco_lo_lanzo": True,
                    "ventana_journal_h": round(_ventana_journal(esperado))},
            evidence_quality=calidad, severity_override=Severity.MAJOR)

    gracia = _gracia(ctx, expr, tzn)
    atraso = (esperado - ultimo).total_seconds()
    detalle = {"ultimo": ultimo.isoformat(), "esperado": esperado.isoformat(),
               "atraso_seg": int(max(0, atraso)), "gracia_seg": gracia,
               "fuente": fuente}
    # Cron lanza unos segundos despues del minuto y `corridas` se cachea 25 s:
    # medido justo en el minuto del lanzamiento, el ledger todavia muestra la
    # corrida anterior. Sin este margen cada lanzamiento podia dar una muestra
    # LANZAMIENTO_FALTANTE falsa (srv2, 2026-10: los diarios, todos los dias).
    margen = int(ctx.deps["cfg"].umbral("jobs.gracia_lanzamiento_seg", 120))
    if atraso > gracia and (ahora - esperado).total_seconds() > margen:
        return CheckOutcome(
            state=State.LATE, reason_code="LANZAMIENTO_FALTANTE",
            expected={"a_mas_tardar": esperado.isoformat()}, actual=detalle,
            evidence_quality=calidad)
    return CheckOutcome(state=State.OK, actual=detalle, evidence_quality=calidad)


def _gracia(ctx: CheckContext, expr: str, tzn: str) -> int:
    """Tolerancia proporcional al período del trabajo.

    Uno que corre cada cinco minutos y se atrasa diez tiene un problema; uno
    semanal atrasado diez minutos, no. Un número fijo estaría mal para alguno
    de los dos.
    """
    base = int(ctx.deps["cfg"].umbral("jobs.gracia_lanzamiento_seg", 120))
    try:
        periodo = cron.periodo_seg(expr, tzn)
    except cron.CronInvalido:
        periodo = None
    if not periodo:
        return base
    return max(base, int(periodo * 0.5))


# Cuanto para atras se le pregunta al journal.
#
# NO puede ser fijo. Con 48 h alcanzaba para un trabajo diario, pero un semanal
# --`0 11 * * 1`, los lunes-- no aparece si se lo mira un jueves, y el control
# concluia que cron nunca lo lanzo. Paso en srv2 con `job:clientea:17` (el reporte
# TCPA), que salio como falso `NUNCA_SE_LANZO` mientras el de al lado, que corre
# lunes y jueves, se veia bien.
#
# La ventana se deriva de cuando se lo esperaba: hay que llegar hasta antes de
# esa ejecucion. El piso evita lecturas inutiles para los que corren cada cinco
# minutos, y el techo evita leer un journal de semanas en cada ciclo.
HORAS_JOURNAL_MIN = 48
HORAS_JOURNAL_MAX = 24 * 16


def _ultimo_lanzamiento(ctx: CheckContext,
                        j: dict[str, Any]) -> tuple[Optional[datetime], str]:
    """Cuando se lanzo por ultima vez, y **de que fuente salio el dato**.

    `rc_cron_runs` es la fuente buena: dice cuando arranco de verdad y como
    termino. `last_run_at` solo se escribe cuando el trabajo se corre desde la
    API. Las dos las escribe la aplicacion.

    La fuente devuelta importa tanto como la fecha, y por eso esta funcion ya no
    devuelve un `datetime` pelado: sirve para distinguir "el trabajo no corrio"
    de "el trabajo corrio y nadie lo anoto", que es lo que el observador estuvo
    trece veces sin poder distinguir en srv2.
    """
    r = datos.corridas(ctx.deps)
    if r.hay:
        fila = (r.datos or {}).get(j["job_id"]) or {}
        if fila.get("ultimo_inicio"):
            return _fecha(fila["ultimo_inicio"]), "ledger"
    d = _fecha(j.get("last_run_at"))
    if d:
        return d, "last_run_at"
    return None, "nada"


def _ventana_journal(esperado: Optional[datetime],
                     ahora: Optional[datetime] = None) -> float:
    """Cuantas horas de journal hay que mirar para este trabajo.

    Tiene que llegar hasta antes de la ultima ejecucion que le tocaba: mirar
    menos tiempo del que hay entre dos corridas garantiza no encontrar nada.
    """
    if esperado is None:
        return float(HORAS_JOURNAL_MIN)
    ahora = ahora or datetime.now(timezone.utc)
    falta = (ahora - esperado).total_seconds() / 3600.0 + 1
    return float(max(HORAS_JOURNAL_MIN, min(falta, HORAS_JOURNAL_MAX)))


def _lo_vio_cron(j: dict[str, Any],
                 esperado: Optional[datetime] = None) -> tuple[Optional[datetime], bool]:
    """Lo que dice el journal: (cuando lo lanzo cron, se pudo mirar).

    Es la fuente que NO depende de la aplicacion. Se consulta solo cuando las de
    la aplicacion no tienen nada, que es cuando aporta algo.

    `esperado` es la ultima ejecucion que le tocaba. La ventana llega hasta una
    hora antes de esa fecha: preguntar por menos tiempo del que hay entre dos
    corridas garantiza no encontrar nada y concluir mal.
    """
    from datetime import timedelta                            # noqa: PLC0415
    ahora = datetime.now(timezone.utc)
    horas = _ventana_journal(esperado, ahora)
    disparos, ok = journal.disparos_de_cron(ahora - timedelta(hours=horas))
    if not ok:
        return None, False
    return journal.ultimo_disparo(disparos, j.get("job_id"),
                                  j.get("comando")), True


def _fecha(v: Any) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# JOB-06: se lanzo y no volvio
# ---------------------------------------------------------------------------

def _job06(ctx: CheckContext) -> CheckOutcome:
    j, calidad, _ = _job(ctx)
    apagado = _apagado(j, calidad)
    if apagado is not None:
        return apagado
    r = datos.corridas(ctx.deps)
    if not r.hay:
        return _sin_datos(r.motivo, codigo="SIN_BASE_PARA_LEER_CORRIDAS")
    tope = int(ctx.deps["cfg"].umbral("jobs.max_runtime_seg", 3600))

    # UNA CONSULTA QUE VUELVE VACIA NO ES LO MISMO QUE UN TRABAJO SANO.
    #
    # `rc_cron_runs` esta VACIA en las dos instalaciones: 0 filas, y los 17
    # trabajos de produccion con `last_run_at` en NULL. Con el ledger asi, este
    # control encontraba `en_curso = []` y devolvia OK — o sea, afirmaba que no
    # hay ninguna corrida colgada sobre una fuente que nadie escribe. Es
    # exactamente el patron que este proyecto dice haber erradicado: confundir
    # "no hay nada" con "no pude ver nada".
    #
    # Sin entrada en el ledger no se puede afirmar nada de este trabajo, asi que
    # se dice UNKNOWN. Va a ser ruidoso hasta que el ledger se escriba, y eso es
    # correcto: la ceguera declarada se ve en el informe diario, un OK falso no.
    entrada = (r.datos or {}).get(ctx.resource.scope.get("job_id"))
    if entrada is None:
        # Se distinguen los dos motivos porque llevan a acciones distintas: uno
        # se arregla desplegando el ledger, el otro no es un problema.
        if j and j.get("last_run_at"):
            return CheckOutcome(
                state=State.UNKNOWN, reason_code="LEDGER_SIN_ESTE_JOB",
                actual={"last_run_at": j.get("last_run_at"),
                        "detalle": "el trabajo corrio pero no dejo fila en rc_cron_runs"},
                evidence_quality=EvidenceQuality.NONE)
        return CheckOutcome(
            state=State.UNKNOWN, reason_code="SIN_CORRIDAS_REGISTRADAS",
            actual={"detalle": "nunca corrio, o nadie escribe el ledger"},
            evidence_quality=EvidenceQuality.NONE)

    en_curso = entrada.get("en_curso") or []

    colgadas = [f for f in en_curso if f["corriendo_seg"] > tope]
    if colgadas:
        peor = max(colgadas, key=lambda f: f["corriendo_seg"])
        return CheckOutcome(
            state=State.STUCK, reason_code="CORRIDA_SIN_TERMINAR",
            expected={"max_seg": tope},
            actual={"run_id": peor["run_id"], "corriendo_seg": peor["corriendo_seg"],
                    "desde": str(peor["started_at"])},
            evidence_quality=calidad)
    return CheckOutcome(state=State.OK, actual={"en_curso": len(en_curso)},
                        evidence_quality=calidad)


# ---------------------------------------------------------------------------
# JOB-07: volvio con codigo distinto de cero
# ---------------------------------------------------------------------------

def _job07(ctx: CheckContext) -> CheckOutcome:
    j, calidad, motivo = _job(ctx)
    apagado = _apagado(j, calidad)
    if apagado is not None:
        return apagado
    r = datos.corridas(ctx.deps)
    if not r.hay:
        # Sin ledger queda el último estado que guardó la API. Es evidencia
        # inferida: sólo refleja las corridas manuales.
        if j and j.get("last_status") in ("failed", "timeout"):
            return CheckOutcome(
                state=State.FAILED, reason_code="ULTIMA_CORRIDA_FALLIDA",
                actual={"estado": j["last_status"], "exit": j.get("last_exit_code")},
                evidence_quality=EvidenceQuality.INFERRED)
        return _sin_datos(r.motivo, codigo="SIN_BASE_PARA_LEER_CORRIDAS")

    f = (r.datos or {}).get(ctx.resource.scope.get("job_id")) or {}
    if not f.get("estado"):
        return _sin_datos("todavia no hay corridas registradas en el ledger",
                          codigo="LEDGER_SIN_CORRIDAS")
    if f["estado"] in ("failed", "timeout"):
        return CheckOutcome(
            state=State.FAILED, reason_code="CORRIDA_FALLIDA",
            expected={"exit_code": 0},
            actual={"estado": f["estado"], "exit_code": f.get("exit_code"),
                    "cuando": str(f.get("finished_at")),
                    # El stderr es la unica pista de por que fallo, y hoy siete
                    # de trece jobs lo tiran a la nada.
                    "stderr": (f.get("stderr") or "").strip()[:400]},
            evidence_quality=calidad)
    return CheckOutcome(state=State.OK,
                        actual={"ultima": str(f.get("finished_at")),
                                "exit_code": f.get("exit_code")},
                        evidence_quality=calidad)


# ---------------------------------------------------------------------------
# JOB-08: volvio bien y no produjo nada
# ---------------------------------------------------------------------------

def _job08(ctx: CheckContext) -> CheckOutcome:
    """El control más valioso: verifica el RESULTADO, no la ejecución."""
    j, calidad, motivo = _job(ctx)
    if j is None:
        return _sin_datos(motivo or "el job no esta en el inventario")
    if not j.get("is_active"):
        return CheckOutcome(state=State.DISABLED_EXPECTED, actual={"activo": False},
                            evidence_quality=calidad)
    return contratos.verificar(ctx, j, calidad)


# ---------------------------------------------------------------------------
# JOB-10: bloqueo abandonado
# ---------------------------------------------------------------------------

def _job10(ctx: CheckContext) -> CheckOutcome:
    """Un `.pid` sin bloqueo vivo.

    NUNCA se intenta tomar el bloqueo para averiguarlo: si en ese instante no
    hay nadie corriendo, el observador se lo queda y el trabajo que arranque
    después muere con "ya hay una instancia". Se lee `/proc/locks` por inodo,
    que es lectura pura.

    `PidFileLock.release()` borra el archivo al salir, así que un `.pid` que
    sobrevive sin bloqueo significa muerte sin limpieza: SIGKILL, OOM o corte.
    Eso lo convierte en señal, no en ruido. Salvo los de
    `PIDFILES_PERSISTENTES`, que no se borran nunca y sin bloqueo están bien.
    """
    directorio = ctx.deps["cfg"].umbral("jobs.pidfiles.directorio", PIDFILE_DIR)
    patron = ctx.deps["cfg"].umbral("jobs.pidfiles.patron", PIDFILE_PATRON)
    persistentes = ctx.deps["cfg"].umbral("jobs.pidfiles.persistentes",
                                          PIDFILES_PERSISTENTES) or ()
    try:
        estados = locks.relevar(directorio, patron)
    except Exception as e:  # noqa: BLE001
        return _sin_datos(f"no se pudo leer /proc/locks: {e}")

    huerfanos = [e for e in estados if e.huerfano
                 and not any(fnmatch.fnmatch(os.path.basename(e.archivo), p)
                             for p in persistentes)]
    detalle = {"pidfiles": len(estados),
               "corriendo": sum(1 for e in estados if e.corriendo)}
    if huerfanos:
        return CheckOutcome(
            state=State.DEGRADED, reason_code="BLOQUEO_ABANDONADO",
            expected={"huerfanos": 0},
            actual={**detalle,
                    "huerfanos": [{"archivo": os.path.basename(e.archivo),
                                   "pid_escrito": e.pid_escrito}
                                  for e in huerfanos[:6]]},
            severity_override=Severity.WARNING)
    return CheckOutcome(state=State.OK, actual=detalle)


# ---------------------------------------------------------------------------
# JOB-14: el planificador
# ---------------------------------------------------------------------------

def _job14(ctx: CheckContext) -> CheckOutcome:
    """Sin cron, ningún trabajo corre y ningún otro control lo diría.

    Los demás controles de esta familia verían "no se lanzó" uno por uno, y el
    correo tendría trece incidentes en vez de uno solo con la causa.
    """
    cache = ctx.deps.get("systemd_cache") or {}
    u = cache.get("cron.service") or systemd.mostrar("cron.service",
                                                    ctx.budget.timeout_sec)
    if u is None:
        return CheckOutcome(state=State.UNKNOWN, reason_code="UNIDAD_ILEGIBLE",
                            evidence_quality=EvidenceQuality.NONE)
    if u.active_state != "active":
        return CheckOutcome(
            state=State.FAILED, reason_code="PLANIFICADOR_CAIDO",
            expected={"active_state": "active"},
            actual={"active_state": u.active_state, "sub_state": u.sub_state,
                    "existe": u.existe},
            severity_override=Severity.CRITICAL)
    return CheckOutcome(state=State.OK, actual={"active_state": "active"})


# ---------------------------------------------------------------------------
# JOB-01: aparecio o desaparecio un trabajo
# ---------------------------------------------------------------------------

def _job01(ctx: CheckContext) -> CheckOutcome:
    """Cuántos trabajos hay, contra cuántos había.

    El inventario ya reporta el alta y la baja de cada recurso; este control
    existe para que ese cambio tenga un estado consultable y entre en el
    informe con su propio motivo.
    """
    jobs, calidad, motivo = _jobs(ctx)
    if not jobs and calidad is EvidenceQuality.NONE:
        return _sin_datos(motivo or "no se pudo leer el inventario de trabajos")

    conn = ctx.deps["conn"]
    conocidos = {f["resource_id"] for f in conn.execute(
        "SELECT resource_id FROM resource WHERE kind = 'job' "
        "AND state != 'RETIRED'").fetchall()}
    actuales = {f"job:{j['client_id']}:{j['job_id']}" for j in jobs}

    nuevos = sorted(actuales - conocidos)
    idos = sorted(conocidos - actuales)
    detalle = {"total": len(actuales), "activos": sum(1 for j in jobs if j.get("is_active"))}
    if nuevos or idos:
        return CheckOutcome(
            state=State.DEGRADED, reason_code="INVENTARIO_DE_JOBS_CAMBIO",
            expected={"sin_cambios": True},
            actual={**detalle, "nuevos": nuevos[:8], "desaparecidos": idos[:8]},
            evidence_quality=calidad, severity_override=Severity.INFO)
    return CheckOutcome(state=State.OK, actual=detalle, evidence_quality=calidad)


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

POR_JOB = {
    "JOB-03": CheckDefinition(
        check_id="JOB-03", dependencies=("mysql",), version=1, titulo="Trabajo activo sin ejecutable",
        selector="job:*", expectation_id="jobs.ejecutable", schedule_sec=300,
        evaluate=_job03, severity=Severity.CRITICAL,
        hysteresis=Hysteresis(abre_tras=1, cierra_tras=2),
        runbook="El comando apunta a una ruta que no existe. Suele pasar tras "
                "renombrar el directorio de instalacion."),
    "JOB-05": CheckDefinition(
        check_id="JOB-05", dependencies=("mysql",), version=1, titulo="Lanzamiento faltante",
        selector="job:*", expectation_id="jobs.lanzamiento", schedule_sec=60,
        evaluate=_job05, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=2),
        runbook="Mirar primero JOB-14: si cron esta caido, todos los trabajos "
                "van a reportar lo mismo y la causa es una sola."),
    "JOB-06": CheckDefinition(
        check_id="JOB-06", dependencies=("mysql",), version=1, titulo="Corrida sin terminar",
        selector="job:*", expectation_id="jobs.duracion", schedule_sec=60,
        evaluate=_job06, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=2),
        runbook="Antes de matarlo, ver JOB-10: puede estar esperando un bloqueo."),
    "JOB-07": CheckDefinition(
        check_id="JOB-07", dependencies=("mysql",), version=1, titulo="Corrida con error",
        selector="job:*", expectation_id="jobs.salida", schedule_sec=60,
        evaluate=_job07, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=1, cierra_tras=2),
        runbook="El stderr de la corrida viene en la evidencia."),
    "JOB-08": CheckDefinition(
        check_id="JOB-08", dependencies=("mysql",), version=1, titulo="Termino bien y no produjo nada",
        selector="job:*", expectation_id="jobs.contrato", schedule_sec=300,
        evaluate=_job08, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=2),
        runbook="Salida cero no es exito. Ver que dice el contrato del trabajo "
                "en la evidencia: dice que resultado se esperaba."),
}

GLOBALES = {
    "JOB-01": CheckDefinition(
        check_id="JOB-01", dependencies=("mysql",), version=1, titulo="Inventario de trabajos",
        selector="host", expectation_id="jobs.inventario", schedule_sec=300,
        evaluate=_job01, severity=Severity.INFO,
        hysteresis=Hysteresis(abre_tras=1, cierra_tras=1)),
    "JOB-10": CheckDefinition(
        check_id="JOB-10", version=1, titulo="Bloqueo abandonado",
        selector="host", expectation_id="jobs.bloqueos", schedule_sec=60,
        evaluate=_job10, severity=Severity.WARNING,
        hysteresis=Hysteresis(abre_tras=3, cierra_tras=2),
        runbook="Un .pid sin bloqueo es un proceso que murio sin limpiar. "
                "Verificar que no haya nada corriendo ANTES de borrarlo."),
    "JOB-14": CheckDefinition(
        check_id="JOB-14", version=1, titulo="Planificador de tareas",
        selector="host", expectation_id="jobs.planificador", schedule_sec=60,
        evaluate=_job14, severity=Severity.CRITICAL,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=2)),
}


def descubrir(deps: dict[str, Any], host_id: str) -> list[Resource]:
    r = datos.contratos_jobs(deps)
    if not r.hay:
        return []
    return [Resource(
        resource_id=f"job:{j['client_id']}:{j['job_id']}", host_id=host_id,
        kind="job",
        scope={"job_id": j["job_id"], "client_id": j["client_id"],
               "name": j.get("name"), "schedule": j.get("schedule")},
        state=ResourceState.MANAGED if j.get("is_active") else ResourceState.IGNORED,
    ) for j in (r.datos or [])]


def planificar(cfg: Any, host_id: str,
               deps: dict[str, Any]) -> list[tuple[CheckDefinition, Resource]]:
    pares: list[tuple[CheckDefinition, Resource]] = []
    for recurso in descubrir(deps, host_id):
        # Los trabajos inactivos igual se planifican: JOB-05 y JOB-08 tienen que
        # poder decir DISABLED_EXPECTED, que es distinto de no mirarlos. Un
        # trabajo que se desactivo sin querer no se detecta si se lo ignora.
        pares.extend((cd, recurso) for cd in POR_JOB.values())
    host = Resource(resource_id=f"host:{host_id}", host_id=host_id, kind="host",
                    state=ResourceState.MANAGED)
    pares.extend((cd, host) for cd in GLOBALES.values())
    return pares
