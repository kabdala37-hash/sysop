"""El motor: corre un control, persiste el resultado y mueve el incidente.

El orden importa y no es arbitrario:

    1. correr el control, con timeout
    2. persistir el resultado (SIEMPRE, incluso UNKNOWN)
    3. recién entonces decidir sobre el incidente

Persistir antes de decidir es lo que hace que la histéresis sobreviva a un
reinicio: el conteo sale de la historia, no de memoria. Y persistir el UNKNOWN
es lo que permite después distinguir "estuvo sano" de "no lo pudimos mirar",
que en un informe son cosas muy distintas.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sysop.core import hysteresis, incidents
from sysop.core.models import (CheckContext, CheckDefinition, CheckOutcome,
                               CheckResult, EvidenceQuality, Resource,
                               Severity, State)

log = logging.getLogger("sysop.engine")


class ResultadoCiclo:
    def __init__(self) -> None:
        self.corridos = 0
        self.abiertos: list[str] = []
        self.recuperados: list[str] = []
        self.desconocidos = 0
        self.errores = 0
        # Los errores que vinieron del almacen, aparte de los bugs de un
        # control. Una pagina rota puede dejar escribir la sonda y fallar al
        # guardar un incidente: sin contarlos, el daemon quedaba vivo y mudo.
        self.errores_almacen = 0
        self.ultimo_error_almacen: Optional[BaseException] = None
        # (control, recurso, resultado) de lo que corrio. Lo usa el camino de
        # emergencia para tener la racha al dia antes de necesitarla.
        self.resultados: list = []


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def ejecutar_control(conn: Any, cd: CheckDefinition, recurso: Resource,
                     deps: Optional[dict[str, Any]] = None
                     ) -> tuple[CheckResult, dict[str, Any]]:
    """Corre un control y devuelve `(resultado, evidencia)`.

    La evidencia va aparte del resultado a propósito: el resultado es el
    veredicto y se guarda siempre; la evidencia es el material que lo sostiene,
    tiene retención más corta y pasa por la política de redacción antes de
    persistirse. Sin ella, una conclusión no se puede auditar ni explicar, que
    es media razón de ser del producto.

    Nunca lanza: cualquier excepción de la función de evaluación se traduce a
    UNKNOWN. Un control con un bug no puede tumbar el ciclo entero y dejar sin
    mirar a los otros treinta y nueve.
    """
    inicio = time.monotonic()
    ctx = CheckContext(resource=recurso, expectation=None, now=_ahora(),
                       budget=cd.budget, deps=deps or {})
    try:
        out = cd.evaluate(ctx)
    except Exception as e:  # noqa: BLE001
        log.warning("el control %s fallo sobre %s: %s",
                    cd.check_id, recurso.resource_id, e)
        out = CheckOutcome(
            state=State.UNKNOWN,
            reason_code="CHECK_ERROR",
            evidence={"error": f"{type(e).__name__}: {e}"},
            evidence_quality=EvidenceQuality.NONE,
        )
    dur_ms = int((time.monotonic() - inicio) * 1000)

    if dur_ms > cd.timeout_sec * 1000:
        log.warning("el control %s tardo %dms (presupuesto %.0fms)",
                    cd.check_id, dur_ms, cd.timeout_sec * 1000)

    severidad = out.severity_override or cd.severity
    # Un estado sano no arrastra la severidad del control: informar un OK como
    # "critical" haría que el asunto del correo grite por algo que anda bien.
    if not out.state.es_problema:
        severidad = Severity.INFO

    r = CheckResult(
        check_id=cd.check_id,
        resource_id=recurso.resource_id,
        observed_at=ctx.now,
        state=out.state,
        severity=severidad,
        expected=out.expected,
        actual=out.actual,
        reason_code=out.reason_code,
        evidence_refs=(),
        evidence_quality=out.evidence_quality,
        rule_version=cd.version,
    )
    # Sólo se guarda evidencia cuando hay algo que explicar. Persistirla en cada
    # OK llenaría el almacén con miles de filas que dicen "todo bien", y la
    # retención de siete días dejaría de alcanzar justo para los incidentes,
    # que son las que hay que poder mirar después.
    evidencia = out.evidence if (out.evidence and out.state is not State.OK) else {}
    if evidencia and cd.evidence_policy.campos_permitidos:
        # Lista POSITIVA: lo que no está declarado no se guarda. Una lista de
        # prohibidos deja pasar todo lo que nadie previó.
        evidencia = {k: v for k, v in evidencia.items()
                     if k in cd.evidence_policy.campos_permitidos}
    return r, evidencia


def persistir(conn: Any, r: CheckResult, evidencia: dict[str, Any],
              host_id: str) -> None:
    ev_id = None
    if evidencia:
        cur = conn.execute(
            "INSERT INTO observation (host_id, source, observed_at, payload_json) "
            "VALUES (?,?,?,?)",
            (host_id, r.check_id, r.observed_at.isoformat(),
             json.dumps(evidencia, default=str)),
        )
        ev_id = cur.lastrowid

    conn.execute(
        "INSERT INTO check_result (check_id, resource_id, observed_at, state, "
        "severity, expected_json, actual_json, reason_code, evidence_id, "
        "evidence_quality, rule_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (r.check_id, r.resource_id, r.observed_at.isoformat(), r.state.value,
         r.severity.value,
         json.dumps(r.expected, default=str) if r.expected else None,
         json.dumps(r.actual, default=str) if r.actual else None,
         r.reason_code, ev_id, r.evidence_quality.value, r.rule_version),
    )


def procesar(conn: Any, cd: CheckDefinition, recurso: Resource,
             out_evidencia: dict[str, Any], r: CheckResult,
             host_id: str, acc: ResultadoCiclo) -> None:
    """Persiste el resultado y aplica la histéresis sobre el incidente."""
    persistir(conn, r, out_evidencia, host_id)
    acc.corridos += 1
    if r.state is State.UNKNOWN:
        acc.desconocidos += 1

    # Dos búsquedas distintas a propósito:
    #
    #   * por fingerprint EXACTO para abrir y actualizar, porque "casi lleno" y
    #     "montado de solo lectura" son problemas distintos del mismo mount y
    #     cada uno tiene que llevar su propio incidente;
    #   * por (control, recurso) para recuperar, porque cuando el control vuelve
    #     a sano el reason_code queda vacío y el fingerprint ya no coincide con
    #     ninguno. Buscando por fingerprint ahí, el incidente no se cerraría
    #     nunca: el problema se arregla y el correo lo sigue reportando.
    fp = incidents.fingerprint(cd.check_id, recurso.resource_id, r.reason_code)
    inc = incidents.abierto(conn, fp)
    vivos = incidents.abiertos_de(conn, cd.check_id, recurso.resource_id)

    d = hysteresis.evaluar(conn, cd.check_id, recurso.resource_id,
                           r.state, cd.hysteresis,
                           hay_incidente_mismo_motivo=inc is not None,
                           hay_incidentes_vivos=bool(vivos),
                           reason_code=r.reason_code)

    if r.state.es_problema:
        if inc is not None:
            # Si estaba recuperado y el problema volvió antes del cierre, es el
            # mismo incidente intermitente, no uno nuevo.
            if inc["state"] == incidents.IncidentState.RECOVERED.value:
                incidents.reabrir(conn, inc["incident_id"], r)
            else:
                incidents.actualizar(conn, inc["incident_id"], r)
        elif d.abrir:
            acc.abiertos.append(incidents.abrir(conn, r))

        # EL SINTOMA VIEJO DEJO DE VERSE.
        #
        # Un control da UN resultado por ciclo, asi que dos motivos distintos no
        # pueden estar activos a la vez sobre el mismo recurso. Si ahora reporta
        # otra cosa, lo que decia antes ya no se observa y su incidente tiene que
        # recuperarse. Recuperar y no cerrar: quedan los quince minutos de
        # ventana, y si el motivo viejo vuelve se reabre como el mismo incidente.
        #
        # Sin esto el incidente del motivo anterior queda vivo PARA SIEMPRE: la
        # unica via de recuperacion es la rama de abajo, que pide que el control
        # vuelva a estar sano, y un control que pasa de un problema a otro nunca
        # pasa por ahi. `cerrar_sin_evidencia` tampoco lo alcanza, porque cubre
        # solo los que quedan en UNKNOWN.
        #
        # Medido en srv2 el 2026-09-03: al mejorar JOB-05, trece incidentes
        # `NUNCA_SE_LANZO` quedaron huerfanos de golpe mientras el control abria
        # trece nuevos con el motivo correcto. El informe mostraba los dos juegos.
        for v in vivos:
            if v["incident_id"] == (inc or {}).get("incident_id"):
                continue
            if incidents.recuperar(conn, v["incident_id"]):
                acc.recuperados.append(v["incident_id"])
    elif d.cerrar:
        for v in vivos:
            # Sólo cuenta como recuperación la primera vez. `recuperar` es
            # idempotente y devuelve False si ya estaba recuperado; sin ese
            # filtro el ciclo reportaría una recuperación por vuelta durante
            # los quince minutos que el incidente espera antes de cerrarse.
            if incidents.recuperar(conn, v["incident_id"]):
                acc.recuperados.append(v["incident_id"])


def correr_ciclo(conn: Any, controles: list[tuple[CheckDefinition, Resource]],
                 host_id: str, deps: Optional[dict[str, Any]] = None,
                 presupuesto_seg: float = 20.0,
                 proveedores: Sequence[Any] = ()) -> ResultadoCiclo:
    """Un ciclo completo. Respeta el presupuesto total.

    Si se agota el tiempo, los controles que no llegaron a correr NO se marcan
    como OK ni se saltean en silencio: quedan sin resultado y OPS-10 lo levanta
    como problema del propio observador. Un ciclo que no alcanza a mirar todo
    tiene que ser visible, no cómodo.

    `proveedores` prepara los recursos compartidos del ciclo: una sola conexión
    MySQL, una sola lectura de systemd para todas las unidades. Medido en srv3,
    no compartirlos costaba la mitad del presupuesto de CPU (cuatro handshakes
    TLS y catorce procesos `systemctl` por vuelta).

    Los aporta quien arma el ciclo, no el motor. El motor no puede importar de
    un adaptador sin volver a atarse a la plataforma de la que justamente se
    separó: los recursos compartidos son de quien los sabe abrir.
    """
    acc = ResultadoCiclo()
    inicio = time.monotonic()
    deps = dict(deps or {})

    cierres: list[Any] = []
    try:
        for proveer in proveedores:
            try:
                cerrar = proveer(controles, deps)
                if cerrar is not None:
                    cierres.append(cerrar)
            except Exception:  # noqa: BLE001
                # Un proveedor que falla deja su dependencia sin poner, y los
                # controles que la necesitan darán UNKNOWN. No se cancela el
                # ciclo: los del otro adaptador no tienen la culpa.
                log.exception("fallo un proveedor de recursos del ciclo")

        acc = _correr(conn, controles, host_id, deps, presupuesto_seg,
                      acc, inicio)
    finally:
        for cerrar in reversed(cierres):
            try:
                cerrar()
            except Exception:  # noqa: BLE001
                pass
    return acc


def _correr(conn: Any, controles: list[tuple[CheckDefinition, Resource]],
            host_id: str, deps: dict[str, Any], presupuesto_seg: float,
            acc: ResultadoCiclo, inicio: float) -> ResultadoCiclo:
    for cd, recurso in controles:
        if time.monotonic() - inicio > presupuesto_seg:
            faltan = len(controles) - acc.corridos
            log.error("presupuesto de ciclo agotado: %d controles sin correr", faltan)
            acc.errores += faltan
            break
        try:
            r, evidencia = ejecutar_control(conn, cd, recurso, deps)
            acc.resultados.append((cd, recurso, r))
            procesar(conn, cd, recurso, evidencia, r, host_id, acc)
        except Exception as e:  # noqa: BLE001
            log.exception("fallo procesando %s sobre %s: %s",
                          cd.check_id, recurso.resource_id, e)
            acc.errores += 1
            if isinstance(e, sqlite3.Error):
                acc.errores_almacen += 1
                acc.ultimo_error_almacen = e

    return acc
