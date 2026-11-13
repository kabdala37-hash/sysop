"""Logs, costo de modelos, y el observador mirándose a sí mismo.

Los tres son de la familia que se olvida hasta que duele: un log sin rotar llena
el disco que todo lo demás necesita, un costo de modelo desbocado no rompe nada
hasta que llega la factura, y un observador que perdió eventos sigue mandando
correos que dicen "todo bien".
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from sysop.adapters.apps.plataforma import datos
from sysop.adapters.apps.plataforma import mysql as mysql_col
from sysop.store import db
from sysop.core.models import (CheckContext, CheckDefinition, CheckOutcome,
                               EvidenceQuality, Hysteresis, Resource,
                               ResourceState, Severity, State)

log = logging.getLogger("sysop.nac.ops")


# ---------------------------------------------------------------------------
# OPS-01: logs sin rotar
# ---------------------------------------------------------------------------

def _ops01(ctx: CheckContext) -> CheckOutcome:
    """Un log administrado que crece sin rotación.

    Arranca con un hallazgo confirmado: no existe logrotate para
    `/var/log/plataforma` ni para `/var/log/clientea`. Hay para nginx, para
    MySQL y para el resto del sistema; para los de la plataforma, no.
    """
    declarados = ctx.deps["cfg"].umbral("logs.administrados", []) or []
    if not declarados:
        return CheckOutcome(state=State.UNKNOWN, reason_code="SIN_LOGS_DECLARADOS",
                            actual={"nota": "declararlos en el yaml del host"},
                            evidence_quality=EvidenceQuality.NONE)

    problemas = []
    revisados = 0
    for item in declarados:
        ruta = Path(str(item.get("ruta", "")))
        if not ruta.exists():
            continue
        revisados += 1
        mib = _tamano_mib(ruta)
        tope = float(item.get("max_mib", 512))
        rota = _tiene_rotacion(ruta)
        if mib > tope or (item.get("rotacion_esperada") and not rota):
            problemas.append({"ruta": str(ruta), "mib": round(mib, 1),
                              "tope_mib": tope, "rotacion": rota})

    if not revisados:
        return CheckOutcome(state=State.UNKNOWN, reason_code="LOGS_NO_ENCONTRADOS",
                            actual={"declarados": len(declarados)},
                            evidence_quality=EvidenceQuality.NONE)
    if problemas:
        peor = max(problemas, key=lambda p: p["mib"])
        return CheckOutcome(
            state=State.DEGRADED, reason_code="LOG_SIN_ROTAR",
            expected={"rotacion": True}, actual={"revisados": revisados, **peor},
            severity_override=Severity.WARNING)
    return CheckOutcome(state=State.OK, actual={"revisados": revisados})


def _tamano_mib(ruta: Path) -> float:
    try:
        if ruta.is_file():
            return ruta.stat().st_size / 1048576.0
        total = 0
        for f in ruta.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
        return total / 1048576.0
    except OSError:
        return 0.0


def _tiene_rotacion(ruta: Path) -> bool:
    """Busca una regla de logrotate que mencione la ruta.

    Es una heurística y se declara como tal: encontrar la regla no garantiza que
    esté funcionando, pero NO encontrarla sí garantiza que no hay rotación.
    """
    base = str(ruta)
    for d in ("/etc/logrotate.d", "/etc/logrotate.conf"):
        p = Path(d)
        try:
            archivos = [p] if p.is_file() else list(p.iterdir()) if p.is_dir() else []
            for f in archivos:
                if f.is_file() and base in f.read_text(errors="replace"):
                    return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------------------
# OPS-08: costo de modelos
# ---------------------------------------------------------------------------

def _ops08(ctx: CheckContext) -> CheckOutcome:
    """El gasto de LLM del día contra el presupuesto.

    Referencia medida en producción: entre 16 y 405 dólares por día según el día
    de la semana. Esa dispersión es de día de semana y no de anomalía, y es la
    razón por la que el umbral tiene que ser un presupuesto declarado y no una
    desviación sobre el promedio.
    """
    conn = ctx.deps.get("mysql")
    if conn is None:
        return CheckOutcome(state=State.UNKNOWN, reason_code="BASE_INACCESIBLE",
                            evidence_quality=EvidenceQuality.NONE)
    diario = float(ctx.deps["cfg"].umbral("costos.presupuesto_diario_usd", 600))
    warn = float(ctx.deps["cfg"].umbral("costos.umbral_warning_pct", 80))
    major = float(ctx.deps["cfg"].umbral("costos.umbral_major_pct", 100))
    # Una sola consulta, y con los tenants EXPLICITOS. Filtrar solo por fecha
    # no usa `idx_client_fecha`, que empieza por `client_id`: medido en srv3,
    # recorria las 291.523 filas de la tabla y costaba 701 ms, el pico que hacia
    # que un ciclo se pasara del tope de CPU. Con los tenants enumerados el plan
    # pasa a `range` sobre el indice y toca tres filas.
    t = datos.tenants(ctx.deps)
    if not t.hay or not t.datos:
        return CheckOutcome(state=State.UNKNOWN, reason_code="SIN_LISTA_DE_TENANTS",
                            actual={"motivo": t.motivo},
                            evidence_quality=EvidenceQuality.NONE)
    marcas = ", ".join(["%s"] * len(t.datos))
    try:
        por_tenant = mysql_col.consultar(conn, f"""
            SELECT client_id, ROUND(SUM(costo_total_usd), 2) AS usd
              FROM rc_token_usage
             WHERE client_id IN ({marcas}) AND fecha_registro >= CURDATE()
             GROUP BY client_id
        """, tuple(t.datos), timeout_sec=ctx.budget.timeout_sec)
    except Exception as e:  # noqa: BLE001
        return CheckOutcome(state=State.UNKNOWN, reason_code="CONSULTA_FALLIDA",
                            actual={"error": str(e)[:160]},
                            evidence_quality=EvidenceQuality.NONE)

    hoy = sum(float(f["usd"] or 0) for f in (por_tenant or []))
    pct = (hoy / diario * 100.0) if diario else 0.0
    detalle = {"gastado_usd": round(hoy, 2), "presupuesto_usd": diario,
               "porcentaje": round(pct, 1),
               "por_tenant": {f["client_id"]: float(f["usd"] or 0)
                              for f in sorted(por_tenant or [],
                                              key=lambda x: -float(x["usd"] or 0))[:4]}}
    if pct >= major:
        return CheckOutcome(state=State.DEGRADED, reason_code="PRESUPUESTO_EXCEDIDO",
                            expected={"maximo_usd": diario}, actual=detalle,
                            severity_override=Severity.MAJOR)
    if pct >= warn:
        return CheckOutcome(state=State.DEGRADED, reason_code="PRESUPUESTO_CERCA",
                            expected={"maximo_usd": diario}, actual=detalle,
                            severity_override=Severity.WARNING)
    return CheckOutcome(state=State.OK, actual=detalle)


# ---------------------------------------------------------------------------
# OPS-10: el propio observador
# ---------------------------------------------------------------------------

def _ops10(ctx: CheckContext) -> CheckOutcome:
    """Que el observador no sea el que está fallando.

    Mira tres cosas: que el almacén local no crezca sin control, que los
    controles no estén quedando sin correr por falta de presupuesto de ciclo, y
    que el correo esté saliendo. Sin esto, un observador roto informa "todo
    bien" con total convicción.
    """
    conn = ctx.deps["conn"]
    tope_abs = float(ctx.deps["cfg"].umbral("agente.max_almacen_mib", 2048))
    fraccion = float(ctx.deps["cfg"].umbral("agente.max_almacen_frac_libre", 0.25))

    from sysop import settings                              # noqa: PLC0415
    try:
        mib = settings.DB_PATH.stat().st_size / 1048576.0
    except OSError:
        mib = 0.0

    # UN TOPE ABSOLUTO NO ALCANZA, y la instalacion de srv3 es la prueba: el
    # umbral estaba en 2048 MiB sobre un disco con 1.884 MiB LIBRES. O sea que
    # el disco se llenaba del todo antes de que este control dijera nada — un
    # umbral que no se puede alcanzar es un umbral apagado.
    #
    # Por eso el techo efectivo es el menor entre el absoluto y una fraccion de
    # lo que queda libre. Con el disco holgado manda el absoluto; con el disco
    # ajustado, avisa mientras todavia se puede hacer algo.
    libre_mib = None
    try:
        st = os.statvfs(settings.DB_PATH.parent)
        libre_mib = st.f_bavail * st.f_frsize / 1048576.0
    except OSError:
        pass
    tope_mib = (min(tope_abs, libre_mib * fraccion)
                if libre_mib is not None else tope_abs)

    # `db.corte` y no `datetime('now', ...)`: ver el porque en su docstring.
    # Este control mide la ceguera del propio observador, y con la ventana rota
    # la media sobre el dia entero en vez de sobre la ultima hora -- justo el
    # control que tenia que avisar que algo no se estaba viendo.
    una_hora = db.corte(hours=1)
    # No hay indice por `observed_at` solo, asi que contar la ultima hora
    # recorria `check_result` entera: en srv2 (2026-10, ~6 M filas) eran 5-10 s
    # por minuto, la mayor parte del presupuesto del ciclo. Las filas entran en
    # orden de `observed_at`, entonces todo lo que esta antes de la ultima
    # medicion de OPS-10 previa a la ventana es mas viejo que la ventana: ese
    # `id` es el piso, y la busqueda va por clave primaria. El filtro por fecha
    # se mantiene, asi que la cuenta es exacta.
    piso = conn.execute(
        "SELECT MAX(id) m FROM check_result "
        "WHERE check_id = 'OPS-10' AND observed_at < ?", (una_hora,)).fetchone()["m"] or 0
    unk = conn.execute(
        "SELECT COUNT(*) n FROM check_result "
        "WHERE id > ? AND state = 'UNKNOWN' AND observed_at >= ?",
        (piso, una_hora)).fetchone()["n"]
    total = conn.execute(
        "SELECT COUNT(*) n FROM check_result "
        "WHERE id > ? AND observed_at >= ?", (piso, una_hora)).fetchone()["n"]
    fallidos = conn.execute(
        "SELECT COUNT(*) n FROM outbox WHERE state = 'FAILED' "
        "AND created_at >= ?", (db.corte(days=1),)).fetchone()["n"]

    pct_unk = (unk / total * 100.0) if total else 0.0
    detalle = {"almacen_mib": round(mib, 1), "tope_mib": round(tope_mib, 1),
               "tope_absoluto_mib": tope_abs,
               "libre_mib": round(libre_mib, 1) if libre_mib is not None else None,
               "resultados_1h": total, "unknown_pct": round(pct_unk, 1),
               "correos_fallidos_24h": fallidos}

    if mib > tope_mib:
        return CheckOutcome(state=State.DEGRADED, reason_code="ALMACEN_GRANDE",
                            expected={"maximo_mib": round(tope_mib, 1)}, actual=detalle,
                            severity_override=Severity.MAJOR)
    if fallidos:
        return CheckOutcome(state=State.DEGRADED, reason_code="CORREO_FALLIDO",
                            expected={"fallidos": 0}, actual=detalle,
                            severity_override=Severity.CRITICAL)
    # Mucho UNKNOWN no es una falla en si, pero significa que el observador esta
    # mirando poco. Se reporta bajo, para que se vea sin despertar a nadie.
    if total and pct_unk > 50:
        return CheckOutcome(state=State.DEGRADED, reason_code="DEMASIADO_UNKNOWN",
                            expected={"unknown_pct_max": 50}, actual=detalle,
                            severity_override=Severity.WARNING)
    return CheckOutcome(state=State.OK, actual=detalle)


CONTROLES = {
    "OPS-01": CheckDefinition(
        check_id="OPS-01", version=1, titulo="Logs administrados",
        selector="host", expectation_id="logs.rotacion", schedule_sec=300,
        evaluate=_ops01, severity=Severity.WARNING,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=2),
        runbook="No hay logrotate para /var/log/plataforma ni /var/log/clientea."),
    "OPS-08": CheckDefinition(
        check_id="OPS-08", dependencies=("mysql",), version=1, titulo="Costo de modelos",
        selector="host", expectation_id="costos.diario", schedule_sec=900,
        evaluate=_ops08, severity=Severity.WARNING,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=3),
        runbook="El gasto varia mucho por dia de semana: comparar contra el "
                "mismo dia, no contra ayer."),
    "OPS-10": CheckDefinition(
        check_id="OPS-10", version=1, titulo="Salud del propio observador",
        selector="host", expectation_id="agente.salud", schedule_sec=60,
        evaluate=_ops10, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=3, cierra_tras=2),
        runbook="Si el correo esta fallando, este control es lo unico que lo "
                "dice, y solo se ve entrando al servidor."),
}


def planificar(cfg: Any, host_id: str) -> list[tuple[CheckDefinition, Resource]]:
    r = Resource(resource_id=f"host:{host_id}", host_id=host_id, kind="host",
                 state=ResourceState.MANAGED)
    return [(cd, r) for cd in CONTROLES.values()]
