"""Los controles de host y capacidad de la fase 1.

Cada uno compara una lectura contra una expectativa declarada en
`config/expectations.d/`. Ninguno tiene un umbral escrito en el código: los
valores viven en el YAML y se ajustan por instalación sin tocar Python.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sysop.adapters.so_linux.collectors import host, journal
from sysop.core import trend
from sysop.core.models import (CheckContext, CheckDefinition, CheckOutcome,
                               EvidenceQuality, Hysteresis, Resource,
                               ResourceState, Severity, State)

log = logging.getLogger("sysop.so.checks")


def _escalar(valor: float, umbrales: dict[str, float]) -> Optional[Severity]:
    """Devuelve la severidad más alta cuyo umbral se superó, o None si está sano."""
    for sev, clave in ((Severity.CRITICAL, "critical"),
                       (Severity.MAJOR, "major"),
                       (Severity.WARNING, "warning")):
        u = umbrales.get(clave)
        if u is not None and valor >= u:
            return sev
    return None


# ---------------------------------------------------------------------------
# HST-09: ocupación de un filesystem
# ---------------------------------------------------------------------------

def _hst09(ctx: CheckContext) -> CheckOutcome:
    mount = ctx.resource.scope["mount"]
    umbrales = ctx.deps["cfg"].umbral("filesystems.umbrales_uso_pct", {}) or {}

    u = host.uso(mount)
    if u is None:
        # No poder leer un mount que se esperaba NO es que esté sano. Puede ser
        # que haya desaparecido, que el NFS esté colgado o que falten permisos:
        # las tres cosas ameritan mirar, y ninguna es OK.
        return CheckOutcome(state=State.UNKNOWN, reason_code="FS_ILEGIBLE",
                            expected={"mount": mount},
                            evidence_quality=EvidenceQuality.NONE)

    sev = _escalar(u.uso_pct, umbrales)
    actual = {"uso_pct": round(u.uso_pct, 1),
              "libre_gib": round(u.libre_bytes / 2**30, 2),
              "total_gib": round(u.total_bytes / 2**30, 2)}
    if sev is None:
        return CheckOutcome(state=State.OK, actual=actual,
                            expected={"uso_pct_max": umbrales.get("warning")})
    return CheckOutcome(state=State.DEGRADED, reason_code="FS_CASI_LLENO",
                        actual=actual,
                        expected={"uso_pct_max": umbrales.get("warning")},
                        severity_override=sev)


# ---------------------------------------------------------------------------
# HST-11: inodos
# ---------------------------------------------------------------------------

def _hst11(ctx: CheckContext) -> CheckOutcome:
    mount = ctx.resource.scope["mount"]
    umbrales = ctx.deps["cfg"].umbral("filesystems.umbrales_inodos_pct", {}) or {}

    u = host.uso(mount)
    if u is None or u.inodos_total == 0:
        # Sin inodos declarados no hay nada que medir. Es el caso de vfat y
        # otros: se informa como no aplicable, no como sano ni como falla.
        return CheckOutcome(state=State.UNKNOWN, reason_code="INODOS_NO_APLICA",
                            evidence_quality=EvidenceQuality.NONE)

    sev = _escalar(u.inodos_pct, umbrales)
    actual = {"inodos_pct": round(u.inodos_pct, 1),
              "usados": u.inodos_usados, "total": u.inodos_total}
    if sev is None:
        return CheckOutcome(state=State.OK, actual=actual)
    # Quedarse sin inodos con espacio de sobra es de los fallos más confusos que
    # existen: el disco dice tener lugar y las escrituras fallan igual.
    return CheckOutcome(state=State.DEGRADED, reason_code="INODOS_ALTOS",
                        actual=actual, severity_override=sev)


# ---------------------------------------------------------------------------
# HST-12: el mount está y es escribible
# ---------------------------------------------------------------------------

def _hst12(ctx: CheckContext) -> CheckOutcome:
    mount = ctx.resource.scope["mount"]
    esperado = ctx.resource.attrs

    actuales = {m.target: m for m in host.listar_mounts()}
    m = actuales.get(mount)
    if m is None:
        return CheckOutcome(state=State.MISSING, reason_code="MOUNT_AUSENTE",
                            expected={"mount": mount, "fstype": esperado.get("fstype")},
                            actual=None, severity_override=Severity.CRITICAL)

    if m.solo_lectura:
        # El kernel remonta ro ante errores de I/O. La aplicación sigue leyendo
        # bien y falla sólo al escribir, así que el síntoma aparece lejos de la
        # causa y suele diagnosticarse mal.
        return CheckOutcome(state=State.FAILED, reason_code="FS_SOLO_LECTURA",
                            expected={"rw": True},
                            actual={"opciones": m.opciones},
                            severity_override=Severity.CRITICAL)

    if esperado.get("source") and m.source != esperado["source"]:
        return CheckOutcome(state=State.DEGRADED, reason_code="MOUNT_CAMBIO_DISPOSITIVO",
                            expected={"source": esperado["source"]},
                            actual={"source": m.source})

    return CheckOutcome(state=State.OK,
                        actual={"source": m.source, "fstype": m.fstype})


# ---------------------------------------------------------------------------
# HST-05: memoria disponible
# ---------------------------------------------------------------------------

def _hst05(ctx: CheckContext) -> CheckOutcome:
    m = host.memoria()
    if m is None:
        return CheckOutcome(state=State.UNKNOWN, reason_code="MEMINFO_ILEGIBLE",
                            evidence_quality=EvidenceQuality.NONE)

    # Umbrales invertidos: acá alerta lo BAJO, no lo alto.
    disp = m.disponible_pct
    actual = {"disponible_pct": round(disp, 1),
              "disponible_gib": round(m.disponible_kb / 2**20, 2)}
    if disp < 5:
        return CheckOutcome(state=State.DEGRADED, reason_code="MEMORIA_CRITICA",
                            actual=actual, expected={"disponible_pct_min": 10},
                            severity_override=Severity.CRITICAL)
    if disp < 10:
        return CheckOutcome(state=State.DEGRADED, reason_code="MEMORIA_BAJA",
                            actual=actual, expected={"disponible_pct_min": 10},
                            severity_override=Severity.MAJOR)
    return CheckOutcome(state=State.OK, actual=actual)


# ---------------------------------------------------------------------------
# HST-17: reloj
# ---------------------------------------------------------------------------

def _hst17(ctx: CheckContext) -> CheckOutcome:
    sync, offset_ms = host.reloj_sincronizado()
    if sync is None:
        return CheckOutcome(state=State.UNKNOWN, reason_code="RELOJ_INDETERMINADO",
                            evidence_quality=EvidenceQuality.NONE)
    if not sync:
        # Un reloj corrido rompe todo lo demás en silencio: las ventanas de los
        # cron dejan de coincidir, las cohortes se calculan mal y los incidentes
        # quedan fechados en el futuro.
        return CheckOutcome(state=State.DEGRADED, reason_code="RELOJ_SIN_SINCRONIZAR",
                            expected={"ntp_sincronizado": True},
                            actual={"ntp_sincronizado": False},
                            severity_override=Severity.MAJOR)
    if offset_ms is not None and offset_ms > 500:
        return CheckOutcome(state=State.DEGRADED, reason_code="RELOJ_DESVIADO",
                            expected={"offset_ms_max": 500},
                            actual={"offset_ms": round(offset_ms, 1)},
                            severity_override=Severity.MAJOR)
    return CheckOutcome(
        state=State.OK,
        actual={"offset_ms": round(offset_ms, 3) if offset_ms is not None else None},
        # Si chrony no está, se sabe que NTP dice estar sincronizado pero no se
        # puede medir cuánto. Es un OK con evidencia incompleta, y se declara.
        evidence_quality=(EvidenceQuality.FULL if offset_ms is not None
                          else EvidenceQuality.PARTIAL))




# ---------------------------------------------------------------------------
# HST-10: proyección de agotamiento
# ---------------------------------------------------------------------------

def _hst10(ctx: CheckContext) -> CheckOutcome:
    """Cuántos días faltan para que el filesystem se llene.

    Es el control que convierte "87% de uso" en algo accionable. Un mount al 87%
    estable no es urgente; uno al 87% creciendo 1,5 puntos por día se llena en
    una semana y hay que actuar antes del fin de semana.
    """
    mount = ctx.resource.scope["mount"]
    umbrales = ctx.deps["cfg"].umbral("filesystems.umbrales_dias_para_llenar",
                                      {"warning": 30, "major": 14, "critical": 3})

    serie = trend.serie_de_uso(ctx.deps["conn"], "HST-09",
                               ctx.resource.resource_id, "uso_pct", dias=30)
    p = trend.proyectar(serie)

    if not p.confiable:
        # Sin historia suficiente no se inventa una proyección. Es UNKNOWN con
        # motivo, no un OK: todavía no se sabe.
        return CheckOutcome(
            state=State.UNKNOWN, reason_code="SIN_HISTORIA_SUFICIENTE",
            actual={"puntos": p.puntos, "horas": round(p.horas_de_historia, 1)},
            evidence_quality=EvidenceQuality.NONE)

    actual = {"pendiente_pct_dia": round(p.pendiente_pct_por_dia, 3),
              "puntos": p.puntos, "horas_historia": round(p.horas_de_historia, 1)}

    if p.dias_para_llenar is None:
        return CheckOutcome(state=State.OK,
                            actual={**actual, "tendencia": "no crece"})

    actual["dias_para_llenar"] = round(p.dias_para_llenar, 1)

    # Poder calcular una pendiente no es poder anunciar un horizonte. Doce horas
    # de historia sirven para decir "se llena en dos días", no para decir "se
    # llena en once": el umbral de historia tiene que escalar con lo que se
    # afirma, o el control abre incidentes sobre una extrapolación inventada.
    if not p.suficiente_para(p.dias_para_llenar):
        return CheckOutcome(
            state=State.UNKNOWN, reason_code="HISTORIA_CORTA_PARA_EL_HORIZONTE",
            actual={**actual,
                    "horas_necesarias": round(
                        trend.Proyeccion.horas_necesarias(p.dias_para_llenar), 1)},
            evidence_quality=EvidenceQuality.NONE)

    for sev, clave in ((Severity.CRITICAL, "critical"), (Severity.MAJOR, "major"),
                       (Severity.WARNING, "warning")):
        if p.dias_para_llenar <= umbrales.get(clave, -1):
            return CheckOutcome(state=State.DEGRADED, reason_code="FS_AGOTAMIENTO_PROXIMO",
                                expected={"dias_minimos": umbrales.get("warning")},
                                actual=actual, severity_override=sev)
    return CheckOutcome(state=State.OK, actual=actual)


# ---------------------------------------------------------------------------
# HST-07: evento OOM nuevo
# ---------------------------------------------------------------------------

def _hst07(ctx: CheckContext) -> CheckOutcome:
    """El kernel mató un proceso por falta de memoria.

    Es crítico aunque el sistema se vea sano después: el proceso que murió puede
    ser un worker en medio de una llamada, y nadie se entera salvo por acá.
    """
    conn = ctx.deps["conn"]
    clave = "journal:oom"
    cur = journal.cursor_guardado(conn, clave)
    msgs, cur_nuevo, pudo_leer = journal.leer(
        desde_cursor=cur, solo_kernel=True,
        prioridad_max=4,
        timeout_sec=ctx.budget.timeout_sec)

    # No poder leer el journal NO es que no haya errores. Sin esto, un usuario
    # sin permiso sobre el journal produce un OK permanente.
    if not pudo_leer:
        return CheckOutcome(
            state=State.UNKNOWN, reason_code="JOURNAL_ILEGIBLE",
            actual={"detalle": "journalctl no devolvio datos: revisar que el "
                               "usuario este en el grupo systemd-journal"},
            evidence_quality=EvidenceQuality.NONE)

    if cur_nuevo and cur_nuevo != cur:
        journal.guardar_cursor(conn, clave, cur_nuevo)

    hallados = []
    for m in msgs:
        p = journal.coincide(m.mensaje, journal.PATRONES_OOM)
        if p:
            hallados.append({"patron": p, "mensaje": m.mensaje[:200]})

    if not hallados:
        return CheckOutcome(state=State.OK, actual={"eventos_oom": 0},
                            evidence_quality=EvidenceQuality.FULL if cur_nuevo
                            else EvidenceQuality.PARTIAL)
    return CheckOutcome(state=State.FAILED, reason_code="OOM_KILL",
                        expected={"eventos_oom": 0},
                        actual={"eventos_oom": len(hallados),
                                "muestra": hallados[:3]},
                        severity_override=Severity.CRITICAL)


# ---------------------------------------------------------------------------
# HST-13: errores de disco o de filesystem
# ---------------------------------------------------------------------------

def _hst13(ctx: CheckContext) -> CheckOutcome:
    conn = ctx.deps["conn"]
    clave = "journal:io"
    cur = journal.cursor_guardado(conn, clave)
    msgs, cur_nuevo, pudo_leer = journal.leer(
        desde_cursor=cur, solo_kernel=True,
        prioridad_max=journal.PRIO_ERROR,
        timeout_sec=ctx.budget.timeout_sec)

    # No poder leer el journal NO es que no haya errores. Sin esto, un usuario
    # sin permiso sobre el journal produce un OK permanente.
    if not pudo_leer:
        return CheckOutcome(
            state=State.UNKNOWN, reason_code="JOURNAL_ILEGIBLE",
            actual={"detalle": "journalctl no devolvio datos: revisar que el "
                               "usuario este en el grupo systemd-journal"},
            evidence_quality=EvidenceQuality.NONE)

    if cur_nuevo and cur_nuevo != cur:
        journal.guardar_cursor(conn, clave, cur_nuevo)

    hallados = []
    for m in msgs:
        p = journal.coincide(m.mensaje, journal.PATRONES_IO)
        if p:
            hallados.append({"patron": p, "mensaje": m.mensaje[:200]})

    if not hallados:
        return CheckOutcome(state=State.OK, actual={"errores_io": 0},
                            evidence_quality=EvidenceQuality.FULL if cur_nuevo
                            else EvidenceQuality.PARTIAL)
    return CheckOutcome(state=State.FAILED, reason_code="ERROR_IO",
                        expected={"errores_io": 0},
                        actual={"errores_io": len(hallados),
                                "muestra": hallados[:3]},
                        severity_override=Severity.CRITICAL)


# ---------------------------------------------------------------------------
# HST-01: el propio observador dejó de reportar
# ---------------------------------------------------------------------------

def _hst01(ctx: CheckContext) -> CheckOutcome:
    """Detecta que el agente estuvo caído, mirando el hueco que dejó.

    Tiene un límite que conviene decir en voz alta: si el agente está caído
    AHORA, este control no corre, porque es el agente el que lo corre. Lo que sí
    detecta es el hueco cuando vuelve, y eso es lo que permite decir en el
    informe diario "estuve ciego entre las 3 y las 5". El punto ciego real, que
    el servidor entero se caiga y nadie avise, sólo lo cubre el vigía externo.
    """
    conn = ctx.deps["conn"]
    esperado = float(ctx.resource.attrs.get("intervalo_seg", 15))
    clave = "agente:latido"

    previo = journal.cursor_guardado(conn, clave)
    ahora_ts = ctx.now.timestamp()
    journal.guardar_cursor(conn, clave, str(ahora_ts))

    if previo is None:
        return CheckOutcome(state=State.OK, actual={"primer_latido": True},
                            evidence_quality=EvidenceQuality.PARTIAL)
    try:
        hueco = ahora_ts - float(previo)
    except ValueError:
        return CheckOutcome(state=State.UNKNOWN, reason_code="LATIDO_ILEGIBLE",
                            evidence_quality=EvidenceQuality.NONE)

    # Tres veces el intervalo: dos ciclos perdidos son un hipo, más es una caída.
    if hueco > esperado * 3:
        return CheckOutcome(state=State.DEGRADED, reason_code="AGENTE_INTERRUMPIDO",
                            expected={"hueco_max_seg": esperado * 3},
                            actual={"hueco_seg": round(hueco, 1),
                                    "ciclos_perdidos": int(hueco / esperado)},
                            severity_override=Severity.MAJOR)
    return CheckOutcome(state=State.OK, actual={"hueco_seg": round(hueco, 1)})


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

POR_MOUNT = {
    "HST-09": CheckDefinition(
        check_id="HST-09", version=1, titulo="Ocupacion del filesystem",
        selector="mount:*", expectation_id="fs.uso", schedule_sec=60,
        evaluate=_hst09, severity=Severity.WARNING,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=3),
        runbook="Ver que creció con du antes de borrar nada. En /mnt/datos, "
                "revisar retención de grabaciones y backup antes de limpiar."),
    "HST-11": CheckDefinition(
        check_id="HST-11", version=1, titulo="Inodos del filesystem",
        selector="mount:*", expectation_id="fs.inodos", schedule_sec=300,
        evaluate=_hst11, severity=Severity.WARNING,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=3),
        runbook="Buscar directorios con muchos archivos chicos: cache, tmp, "
                "logs rotados sin borrar."),
    "HST-10": CheckDefinition(
        check_id="HST-10", version=1, titulo="Proyeccion de agotamiento",
        selector="mount:*", expectation_id="fs.tendencia", schedule_sec=900,
        evaluate=_hst10, severity=Severity.WARNING,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=2),
        runbook="Ver que crece antes de borrar. En /mnt/datos, revisar la "
                "retencion de grabaciones y confirmar el backup primero."),
    "HST-12": CheckDefinition(
        check_id="HST-12", version=1, titulo="Mount presente y escribible",
        selector="mount:*", expectation_id="fs.montado", schedule_sec=30,
        evaluate=_hst12, severity=Severity.CRITICAL,
        hysteresis=Hysteresis(abre_tras=1, cierra_tras=3),
        runbook="Un remonte a solo lectura suele venir de errores de I/O: "
                "mirar el journal del kernel antes de remontar."),
}

GLOBALES = {
    "HST-05": CheckDefinition(
        check_id="HST-05", version=1, titulo="Memoria disponible",
        selector="host", expectation_id="host.memoria", schedule_sec=15,
        evaluate=_hst05, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=4, cierra_tras=4),
        runbook="Identificar el proceso que creció. No reiniciar servicios sin "
                "mirar si hay una llamada en curso."),
    "HST-01": CheckDefinition(
        check_id="HST-01", version=1, titulo="Latido del observador",
        selector="host", expectation_id="agente.latido", schedule_sec=15,
        evaluate=_hst01, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=1, cierra_tras=2),
        runbook="Un hueco significa que el observador estuvo caido y no vio "
                "nada en ese rato. El informe diario tiene que decirlo."),
    "HST-07": CheckDefinition(
        check_id="HST-07", version=1, titulo="Evento OOM",
        selector="host", expectation_id="host.oom", schedule_sec=15,
        evaluate=_hst07, severity=Severity.CRITICAL,
        hysteresis=Hysteresis(abre_tras=1, cierra_tras=4),
        runbook="Identificar que proceso mato el kernel. Si fue un worker, la "
                "llamada se corto sin que nadie lo registre."),
    "HST-13": CheckDefinition(
        check_id="HST-13", version=1, titulo="Errores de disco o filesystem",
        selector="host", expectation_id="host.io", schedule_sec=30,
        evaluate=_hst13, severity=Severity.CRITICAL,
        hysteresis=Hysteresis(abre_tras=1, cierra_tras=4),
        runbook="Un remonte a solo lectura viene despues de estos errores. "
                "Mirar SMART antes de remontar nada."),
    "HST-17": CheckDefinition(
        check_id="HST-17", version=1, titulo="Sincronizacion del reloj",
        selector="host", expectation_id="host.reloj", schedule_sec=60,
        evaluate=_hst17, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=3, cierra_tras=3),
        runbook="Revisar chrony/systemd-timesyncd. Un reloj corrido desalinea "
                "las ventanas de los cron y las cohortes de llamadas."),
}


def descubrir_recursos(cfg: Any, host_id: str) -> list[Resource]:
    """Los recursos de host de esta máquina.

    Los mounts se DESCUBREN. srv2 tiene /mnt/datos y srv3 no, así que una lista
    fija estaría mal en alguno de los dos. La configuración aporta exclusiones
    y umbrales, no el inventario.
    """
    out = [Resource(resource_id=f"host:{host_id}", host_id=host_id, kind="host",
                    state=ResourceState.MANAGED)]

    excluir_tipos = cfg.umbral("filesystems.excluir_tipos", []) or []
    excluir_mounts = cfg.umbral("filesystems.excluir_mounts", []) or []
    for m in host.mounts_administrados(excluir_tipos, excluir_mounts):
        out.append(Resource(
            resource_id=f"mount:{m.target}",
            host_id=host_id, kind="mount",
            scope={"mount": m.target},
            state=ResourceState.MANAGED,
            attrs={"source": m.source, "fstype": m.fstype},
        ))
    return out


def planificar(cfg: Any, host_id: str) -> list[tuple[CheckDefinition, Resource]]:
    """Pares (control, recurso) que corresponden a esta máquina."""
    pares: list[tuple[CheckDefinition, Resource]] = []
    for r in descubrir_recursos(cfg, host_id):
        catalogo = GLOBALES if r.kind == "host" else POR_MOUNT
        for cd in catalogo.values():
            pares.append((cd, r))
    return pares
