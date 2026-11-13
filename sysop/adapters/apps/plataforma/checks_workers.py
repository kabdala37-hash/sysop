"""Controles de los workers de voz.

Un worker es un `node workerBot/index.js` por línea telefónica. Que falte uno
significa que esa línea no atiende: las llamadas entran y nadie las contesta, y
no hay ningún error en ningún log porque no hay nada corriendo que pueda
escribirlo. Es la clase de falla que sólo se detecta comparando contra lo que
debería existir.

**El conjunto esperado NO se deriva acá.** Se le pide al back, que usa la misma
función que el supervisor de líneas para decidir qué levantar. Copiar esa
derivación es garantizar que el día que alguien la toque, el observador empiece
a reportar workers faltantes que no faltan.

Y una trampa concreta, medida en producción: hay líneas activas SIN binding
activo (cuatro de Twilio en `acme`, dos de Telefonia en `clientea`) que
correctamente no generan worker. Comparar contra "líneas activas" en vez de
contra la derivación real produce seis faltantes falsos.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sysop.adapters.apps.plataforma import datos
from sysop.core.models import (CheckContext, CheckDefinition, CheckOutcome,
                               EvidenceQuality, Hysteresis, Resource,
                               ResourceState, Severity, State)

log = logging.getLogger("sysop.nac.workers")

# Las cuatro formas de invocar un worker, sacadas de su propio mensaje de uso
# (`workerBot/index.js:745`) y de como lo lanza el supervisor
# (`acmeBot/shooters/line_supervisor.py:134-140`):
#
#   --inbound  --media <id> --client <c>                     <-- SIN --agent
#   --shift    --agent <id> --client <c> --media <id>
#   --both     --agent <id> --client <c> --media <id>
#   --outbound --agent <id> --client <c> --lead <id> [--agenda <id>]   <-- SIN --media
#
# **Son flags con nombre, no posicionales.** Hasta el 2026-09-03 esto se parseaba
# como `--modo <agente> <tenant> <media>`, un formato que no usa nadie, y el
# resultado era que `client_id` se llevaba el nombre del AGENTE y `media_id`
# quedaba en None. Con la identidad rota (`AGENTE3:None` en vez de `clientea:15`) ningun
# esperado matcheaba con ningun corriendo: en srv2 daba a la vez WRK-02 "faltan 6",
# WRK-03 "sobran 6" y WRK-04 "duplicado", con los seis workers corriendo
# perfectamente. Esa contradiccion --seis corriendo y seis faltando-- es la huella
# del bug.
MODOS = ("--inbound", "--shift", "--both", "--outbound")

# `--outbound` es una llamada puntual a un lead, no una linea: no lleva `--media`
# y no tiene que contarse ni como faltante ni como sobrante.
MODOS_DE_LINEA = ("inbound", "shift", "both")

# El flag que trae cada dato. `_entero` sobre lo que no es numero da None.
_FLAGS = {"--agent": "agent_id", "--client": "client_id", "--media": "media_id"}


def _procesos() -> tuple[list[dict[str, Any]], bool]:
    """Los workerBot vivos, con su identidad sacada del argv.

    Devuelve además si se pudo leer: no poder mirar `/proc` tiene que dar
    UNKNOWN, no una lista vacía que se leería como "no hay ninguno".
    """
    from pathlib import Path                                # noqa: PLC0415
    out: list[dict[str, Any]] = []
    try:
        pids = [p for p in Path("/proc").iterdir() if p.name.isdigit()]
    except OSError:
        return [], False

    for p in pids:
        try:
            argv = (p / "cmdline").read_bytes().decode("utf-8", "replace").split("\x00")
        except (OSError, UnicodeDecodeError):
            continue
        argv = [a for a in argv if a]
        if not argv or "index.js" not in " ".join(argv[:3]):
            continue
        modo = next((a.lstrip("-") for a in argv if a in MODOS), None)
        if modo is None or modo not in MODOS_DE_LINEA:
            continue
        campos = _flags(argv)
        out.append({
            "pid": int(p.name), "mode": modo,
            # En `--inbound` no hay agente, y esta bien que quede None.
            "agent_id": campos.get("agent_id"),
            "client_id": campos.get("client_id"),
            "media_id": _entero(campos.get("media_id")),
        })
    return out, True


def _flags(argv: list[str]) -> dict[str, Any]:
    """`--agent X --client Y --media 7` -> {agent_id: X, client_id: Y, media_id: 7}.

    Un flag cuyo valor falta, o al que le sigue otro flag, queda sin cargar: es
    preferible un None explicito a quedarse con el `--client` de al lado como si
    fuera un nombre de agente, que es exactamente lo que hacia el parser viejo.
    """
    campos: dict[str, Any] = {}
    for i, a in enumerate(argv):
        clave = _FLAGS.get(a)
        if clave is None:
            continue
        valor = argv[i + 1] if i + 1 < len(argv) else None
        if valor is None or valor.startswith("--"):
            continue
        campos[clave] = valor
    return campos


def _entero(v: Any) -> Optional[int]:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def _identidad(w: dict[str, Any]) -> str:
    return f"{w.get('client_id')}:{w.get('media_id')}"


def _sin_procesos() -> CheckOutcome:
    return CheckOutcome(state=State.UNKNOWN, reason_code="PROC_ILEGIBLE",
                        actual={"motivo": "no se pudo leer /proc"},
                        evidence_quality=EvidenceQuality.NONE)


# ---------------------------------------------------------------------------
# WRK-01: no se puede saber que deberia haber
# ---------------------------------------------------------------------------

def _wrk01(ctx: CheckContext) -> CheckOutcome:
    """Sin el conjunto esperado, los otros cuatro controles no significan nada.

    Existe para que esa ceguera sea visible y tenga su propio incidente, en vez
    de que WRK-02 a WRK-05 devuelvan UNKNOWN cada uno por su lado y nadie
    entienda por qué.
    """
    r = datos.workers_esperados(ctx.deps)
    if not r.hay:
        return CheckOutcome(
            state=State.FAILED, reason_code="DERIVACION_NO_DISPONIBLE",
            expected={"fuente": "back o base"}, actual={"motivo": r.motivo},
            evidence_quality=EvidenceQuality.NONE,
            severity_override=Severity.MAJOR)
    return CheckOutcome(
        state=State.OK,
        actual={"esperados": len(r.datos), "origen": r.origen},
        evidence_quality=r.calidad)


# ---------------------------------------------------------------------------
# WRK-02 y 03: falta uno / sobra uno
# ---------------------------------------------------------------------------

SUPERVISOR_UNIT = "acme-line-supervisor.service"


def _supervisor_apagado_a_proposito(ctx: CheckContext) -> bool:
    """¿El host declara que el supervisor de líneas NO tiene que estar corriendo?

    Sin supervisor no hay quien levante los workers, así que esperarlos es
    esperar algo imposible. Pasa en desarrollo, donde el supervisor está
    declarado como esperado-detenido: sin esta pregunta, WRK-02 reporta tres
    workers faltantes todos los días, para siempre.
    """
    unidad = ctx.deps["cfg"].umbral("workers.unidad_supervisor", SUPERVISOR_UNIT)
    return unidad in (ctx.deps["cfg"].units_detenidas or ())


def _wrk02(ctx: CheckContext) -> CheckOutcome:
    if _supervisor_apagado_a_proposito(ctx):
        return CheckOutcome(
            state=State.DISABLED_EXPECTED, reason_code="SUPERVISOR_DETENIDO_A_PROPOSITO",
            actual={"unidad": SUPERVISOR_UNIT},
            evidence_quality=EvidenceQuality.FULL)
    r = datos.workers_esperados(ctx.deps)
    if not r.hay:
        return CheckOutcome(state=State.UNKNOWN, reason_code="SIN_DERIVACION",
                            evidence_quality=EvidenceQuality.NONE)
    vivos, pudo = _procesos()
    if not pudo:
        return _sin_procesos()

    corriendo = {_identidad(w) for w in vivos}
    faltan = [w for w in r.datos if _identidad(w) not in corriendo]
    detalle = {"esperados": len(r.datos), "corriendo": len(vivos),
               "origen": r.origen}
    if faltan:
        return CheckOutcome(
            state=State.MISSING, reason_code="WORKER_FALTANTE",
            expected={"todos_los_derivados": True},
            actual={**detalle,
                    "faltan": [{"tenant": w.get("client_id"),
                                "media": w.get("media_id"),
                                "agente": w.get("agent_id"),
                                "modo": w.get("mode")} for w in faltan[:8]]},
            evidence_quality=r.calidad, severity_override=Severity.CRITICAL)
    return CheckOutcome(state=State.OK, actual=detalle, evidence_quality=r.calidad)


def _wrk03(ctx: CheckContext) -> CheckOutcome:
    """Un worker corriendo que ya no corresponde a la configuración.

    Suele quedar de una línea que se desactivó: sigue registrado contra el ITSP
    y puede robarle el registro a otra, que es peor que no tener ninguno.
    """
    r = datos.workers_esperados(ctx.deps)
    if not r.hay:
        return CheckOutcome(state=State.UNKNOWN, reason_code="SIN_DERIVACION",
                            evidence_quality=EvidenceQuality.NONE)
    vivos, pudo = _procesos()
    if not pudo:
        return _sin_procesos()

    esperados = {_identidad(w) for w in r.datos}
    sobran = [w for w in vivos if _identidad(w) not in esperados]
    detalle = {"esperados": len(esperados), "corriendo": len(vivos)}
    if sobran:
        return CheckOutcome(
            state=State.DEGRADED, reason_code="WORKER_SOBRANTE",
            expected={"solo_los_derivados": True},
            actual={**detalle,
                    "sobran": [{"pid": w["pid"], "tenant": w.get("client_id"),
                                "media": w.get("media_id")} for w in sobran[:8]]},
            evidence_quality=r.calidad, severity_override=Severity.MAJOR,
        )
    return CheckOutcome(state=State.OK, actual=detalle, evidence_quality=r.calidad)


# ---------------------------------------------------------------------------
# WRK-04: dos procesos para la misma linea
# ---------------------------------------------------------------------------

def _wrk04(ctx: CheckContext) -> CheckOutcome:
    """Duplicados. Dos workers en la misma línea se pelean el registro SIP y las
    llamadas terminan en el buzón de voz sin que nadie vea un error."""
    vivos, pudo = _procesos()
    if not pudo:
        return _sin_procesos()

    cuenta: dict[str, list[int]] = {}
    for w in vivos:
        cuenta.setdefault(_identidad(w), []).append(w["pid"])
    dups = {k: v for k, v in cuenta.items() if len(v) > 1}
    if dups:
        return CheckOutcome(
            state=State.FAILED, reason_code="WORKER_DUPLICADO",
            expected={"uno_por_linea": True},
            actual={"duplicados": {k: v for k, v in list(dups.items())[:5]}},
            severity_override=Severity.CRITICAL)
    return CheckOutcome(state=State.OK, actual={"lineas": len(cuenta)})


# ---------------------------------------------------------------------------
# WRK-05: el proceso existe pero no esta conectado al supervisor
# ---------------------------------------------------------------------------

def _wrk05(ctx: CheckContext) -> CheckOutcome:
    """El sidecar ya publica cuántos workers tiene conectados, sin autenticación.

    Un worker vivo pero desconectado del supervisor atiende llamadas y no se
    puede intervenir: no se lo puede escuchar ni cortar desde el panel.
    """
    url = ctx.deps["cfg"].umbral("workers.supervisor_url",
                                 "http://127.0.0.1:11000/api/health")
    try:
        import httpx                                        # noqa: PLC0415
        with httpx.Client(timeout=ctx.budget.timeout_sec) as c:
            resp = c.get(url)
        if resp.status_code != 200:
            return CheckOutcome(
                state=State.UNKNOWN, reason_code="SUPERVISOR_NO_RESPONDE",
                actual={"status": resp.status_code},
                evidence_quality=EvidenceQuality.NONE)
        d = resp.json()
    except Exception as e:  # noqa: BLE001
        return CheckOutcome(state=State.UNKNOWN, reason_code="SUPERVISOR_INALCANZABLE",
                            actual={"error": str(e)[:160]},
                            evidence_quality=EvidenceQuality.NONE)

    vivos, pudo = _procesos()
    conectados = int(d.get("workers_connected") or 0)
    detalle = {"conectados": conectados, "conocidos": d.get("workers_known"),
               "llamadas_activas": d.get("calls_active")}
    if pudo:
        detalle["procesos"] = len(vivos)
        if len(vivos) > conectados:
            return CheckOutcome(
                state=State.DEGRADED, reason_code="WORKER_DESCONECTADO",
                expected={"conectados": len(vivos)}, actual=detalle,
                severity_override=Severity.MAJOR)
    return CheckOutcome(state=State.OK, actual=detalle)


CONTROLES = {
    "WRK-01": CheckDefinition(
        check_id="WRK-01", dependencies=("mysql",), version=1, titulo="Derivacion de workers disponible",
        selector="host", expectation_id="workers.derivacion", schedule_sec=60,
        evaluate=_wrk01, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=3, cierra_tras=2),
        runbook="Sin esto, WRK-02 a WRK-05 no pueden concluir nada. "
                "Revisar el back y la clave interna antes que los workers."),
    "WRK-02": CheckDefinition(
        check_id="WRK-02", dependencies=("mysql",), version=1, titulo="Worker esperado que falta",
        selector="host", expectation_id="workers.presentes", schedule_sec=30,
        evaluate=_wrk02, severity=Severity.CRITICAL,
        hysteresis=Hysteresis(abre_tras=3, cierra_tras=3),
        runbook="La linea no atiende. Mirar el line_supervisor: los workers "
                "heredan su entorno, asi que matar el worker solo no alcanza."),
    "WRK-03": CheckDefinition(
        check_id="WRK-03", dependencies=("mysql",), version=1, titulo="Worker que sobra",
        selector="host", expectation_id="workers.presentes", schedule_sec=60,
        evaluate=_wrk03, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=3, cierra_tras=2),
        runbook="Suele quedar de una linea desactivada. Puede robarle el "
                "registro a otra linea."),
    "WRK-04": CheckDefinition(
        check_id="WRK-04", version=1, titulo="Worker duplicado",
        selector="host", expectation_id="workers.unicos", schedule_sec=30,
        evaluate=_wrk04, severity=Severity.CRITICAL,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=2),
        runbook="Dos procesos en la misma linea se pelean el registro SIP y "
                "las llamadas caen al buzon."),
    "WRK-05": CheckDefinition(
        check_id="WRK-05", version=1, titulo="Worker desconectado del supervisor",
        selector="host", expectation_id="workers.supervision", schedule_sec=30,
        evaluate=_wrk05, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=4, cierra_tras=3),
        runbook="Atiende pero no se puede escuchar ni cortar desde el panel."),
}


def planificar(cfg: Any, host_id: str) -> list[tuple[CheckDefinition, Resource]]:
    r = Resource(resource_id=f"host:{host_id}", host_id=host_id, kind="host",
                 state=ResourceState.MANAGED)
    return [(cd, r) for cd in CONTROLES.values()]
