"""Controles de servicios, sockets, endpoints y TLS.

Las unidades esperadas NO están en el código: vienen de
`config/expectations.d/<host>.yaml`. Mudar la plataforma a otro servidor es
escribir un YAML.
"""
from __future__ import annotations

import logging
from typing import Any

from sysop.adapters.so_linux.collectors import net, systemd
from sysop.core.models import (CheckContext, CheckDefinition, CheckOutcome,
                               EvidenceQuality, Hysteresis, Resource,
                               ResourceState, Severity, State)

log = logging.getLogger("sysop.so.checks")


def _unidad(ctx: CheckContext, nombre: str):
    """La unidad, del cache del ciclo si existe.

    El motor lee todas las unidades de una vez al empezar el ciclo y las deja
    en `deps`. Sin eso, SVC-01 y SVC-03 sobre siete unidades son catorce
    procesos `systemctl` por ciclo.
    """
    cache = ctx.deps.get("systemd_cache")
    if cache is not None and nombre in cache:
        return cache[nombre]
    return systemd.mostrar(nombre, ctx.budget.timeout_sec)


# ---------------------------------------------------------------------------
# SVC-01: la unidad esperada está activa
# ---------------------------------------------------------------------------

def _svc01(ctx: CheckContext) -> CheckOutcome:
    nombre = ctx.resource.scope["unidad"]
    esperada_activa = ctx.resource.attrs.get("esperada_activa", True)

    u = _unidad(ctx, nombre)
    if u is None:
        return CheckOutcome(state=State.UNKNOWN, reason_code="SYSTEMCTL_SIN_RESPUESTA",
                            evidence_quality=EvidenceQuality.NONE)

    actual = {"active_state": u.active_state, "sub_state": u.sub_state,
              "unit_file_state": u.unit_file_state, "pid": u.main_pid}

    if not u.existe:
        # Distinto de "detenida": la unidad no está instalada. Suele ser un
        # despliegue a medias o un rename que no se propagó.
        return CheckOutcome(state=State.MISSING, reason_code="UNIDAD_NO_INSTALADA",
                            expected={"unidad": nombre}, actual=actual,
                            severity_override=Severity.CRITICAL)

    if esperada_activa and not u.activa:
        # `activating/auto-restart` se reporta aparte: es un bucle, no una
        # caída limpia, y el runbook es distinto.
        if u.en_bucle:
            return CheckOutcome(state=State.FAILED, reason_code="UNIDAD_EN_BUCLE",
                                expected={"active_state": "active"}, actual=actual,
                                severity_override=Severity.MAJOR)
        return CheckOutcome(state=State.FAILED, reason_code="UNIDAD_CAIDA",
                            expected={"active_state": "active"}, actual=actual,
                            severity_override=Severity.CRITICAL)

    if not esperada_activa and u.activa:
        # Arrancó sola algo que tenía que estar apagado. En srv3 pasa con los
        # supervisores viejos, y no es inocuo: dos supervisores de línea
        # compitiendo levantan workers duplicados.
        return CheckOutcome(state=State.DEGRADED, reason_code="UNIDAD_NO_ESPERADA_ACTIVA",
                            expected={"active_state": "inactive"}, actual=actual,
                            severity_override=Severity.MAJOR)

    estado = State.OK if esperada_activa else State.DISABLED_EXPECTED
    return CheckOutcome(state=estado, actual=actual)


# ---------------------------------------------------------------------------
# SVC-03: bucle de reinicios
# ---------------------------------------------------------------------------

def _svc03(ctx: CheckContext) -> CheckOutcome:
    nombre = ctx.resource.scope["unidad"]
    u = _unidad(ctx, nombre)
    if u is None or not u.existe:
        return CheckOutcome(state=State.UNKNOWN, reason_code="UNIDAD_ILEGIBLE",
                            evidence_quality=EvidenceQuality.NONE)

    if u.en_bucle:
        return CheckOutcome(state=State.FAILED, reason_code="BUCLE_DE_REINICIOS",
                            expected={"reiniciando": False},
                            actual={"active_state": u.active_state,
                                    "sub_state": u.sub_state,
                                    "n_restarts": u.n_restarts},
                            severity_override=Severity.MAJOR)

    # NRestarts es acumulado desde que se cargó la unidad, así que un número
    # alto en un servicio con meses de uptime no dice nada por sí solo. Lo que
    # importa es el DELTA entre muestras, y eso lo resuelve la línea base en la
    # fase 4. Por ahora sólo se reporta el valor, sin juzgarlo.
    return CheckOutcome(state=State.OK,
                        actual={"n_restarts": u.n_restarts, "pid": u.main_pid},
                        evidence_quality=EvidenceQuality.PARTIAL)


# ---------------------------------------------------------------------------
# SVC-05: el socket esperado escucha donde debe
# ---------------------------------------------------------------------------

def _svc05(ctx: CheckContext) -> CheckOutcome:
    puerto = int(ctx.resource.scope["puerto"])
    solo_loopback = ctx.resource.attrs.get("solo_loopback", True)

    encontrados = net.escucha_en(puerto)
    if not encontrados:
        return CheckOutcome(state=State.MISSING, reason_code="SOCKET_NO_ESCUCHA",
                            expected={"puerto": puerto},
                            actual=None, severity_override=Severity.CRITICAL)

    ips = sorted({l.ip for l in encontrados})
    publicos = [l for l in encontrados if l.publico]
    if solo_loopback and publicos:
        # Cambio de exposición. El back y el supervisor escuchan en loopback a
        # propósito y salen por nginx; que aparezcan en 0.0.0.0 significa que
        # quedaron accesibles sin pasar por el proxy.
        return CheckOutcome(state=State.DEGRADED, reason_code="SOCKET_EXPUESTO",
                            expected={"puerto": puerto, "solo_loopback": True},
                            actual={"ips": ips},
                            severity_override=Severity.MAJOR)
    return CheckOutcome(state=State.OK, actual={"ips": ips})


# ---------------------------------------------------------------------------
# SVC-07: el endpoint responde dentro del SLO
# ---------------------------------------------------------------------------

def _svc07(ctx: CheckContext) -> CheckOutcome:
    url = ctx.resource.attrs["url"]
    espera = int(ctx.resource.attrs.get("espera_codigo", 200))
    slo_ms = float(ctx.resource.attrs.get("slo_ms", 1500))

    s = net.probar_http(url, ctx.budget.timeout_sec, espera)
    actual = {"codigo": s.codigo, "latencia_ms": round(s.latencia_ms, 1)}

    if s.codigo is None:
        # No hubo respuesta. Es distinto de un código inesperado: acá el
        # proceso puede estar vivo pero no atendiendo, que es justamente lo que
        # un control de "está el proceso" no distingue.
        return CheckOutcome(state=State.FAILED, reason_code="ENDPOINT_SIN_RESPUESTA",
                            expected={"codigo": espera}, actual={"error": s.error},
                            severity_override=Severity.CRITICAL)
    if s.codigo != espera:
        return CheckOutcome(state=State.FAILED, reason_code="ENDPOINT_CODIGO_INESPERADO",
                            expected={"codigo": espera}, actual=actual,
                            severity_override=Severity.CRITICAL)
    if s.latencia_ms > slo_ms:
        return CheckOutcome(state=State.DEGRADED, reason_code="ENDPOINT_LENTO",
                            expected={"latencia_ms_max": slo_ms}, actual=actual,
                            severity_override=Severity.MAJOR)
    return CheckOutcome(state=State.OK, actual=actual)


# ---------------------------------------------------------------------------
# SVC-09: vencimiento del certificado
# ---------------------------------------------------------------------------

def _svc09(ctx: CheckContext) -> CheckOutcome:
    host = ctx.resource.scope["host"]
    puerto = int(ctx.resource.scope.get("puerto", 443))

    c = net.vencimiento_tls(host, puerto, ctx.budget.timeout_sec)
    if c.dias_restantes is None:
        # Incluye cadena inválida y handshake fallido. No es que no se sepa la
        # fecha: es que el certificado no sirve.
        return CheckOutcome(state=State.FAILED, reason_code="TLS_INVALIDO",
                            expected={"cadena_valida": True},
                            actual={"error": c.error},
                            severity_override=Severity.CRITICAL)

    actual = {"dias_restantes": c.dias_restantes, "subject": c.subject}
    if c.dias_restantes < 3:
        sev = Severity.CRITICAL
    elif c.dias_restantes < 14:
        sev = Severity.MAJOR
    elif c.dias_restantes < 30:
        sev = Severity.WARNING
    else:
        return CheckOutcome(state=State.OK, actual=actual)
    return CheckOutcome(state=State.DEGRADED, reason_code="TLS_POR_VENCER",
                        expected={"dias_restantes_min": 30}, actual=actual,
                        severity_override=sev)


# ---------------------------------------------------------------------------
# Registro
# ---------------------------------------------------------------------------

POR_UNIDAD = {
    "SVC-01": CheckDefinition(
        check_id="SVC-01", version=1, titulo="Unidad en el estado esperado",
        selector="systemd:*", expectation_id="svc.estado", schedule_sec=15,
        evaluate=_svc01, severity=Severity.CRITICAL,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=3),
        runbook="Mirar journalctl -u <unidad> -n 100 antes de reiniciar. "
                "Si es el line_supervisor, los workers heredan su entorno."),
    "SVC-03": CheckDefinition(
        check_id="SVC-03", version=1, titulo="Bucle de reinicios",
        selector="systemd:*", expectation_id="svc.estable", schedule_sec=30,
        evaluate=_svc03, severity=Severity.MAJOR,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=4),
        runbook="Un servicio que reinicia en bucle no aparece como failed: "
                "systemd lo esta reintentando."),
}

POR_PUERTO = {
    "SVC-05": CheckDefinition(
        check_id="SVC-05", version=1, titulo="Socket en escucha",
        selector="socket:*", expectation_id="svc.socket", schedule_sec=30,
        evaluate=_svc05, severity=Severity.CRITICAL,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=3)),
}

POR_ENDPOINT = {
    "SVC-07": CheckDefinition(
        check_id="SVC-07", version=1, titulo="Endpoint dentro del SLO",
        selector="endpoint:*", expectation_id="svc.endpoint", schedule_sec=30,
        evaluate=_svc07, severity=Severity.CRITICAL,
        hysteresis=Hysteresis(abre_tras=2, cierra_tras=3),
        runbook="Un proceso vivo que no atiende suele ser el pool de conexiones "
                "agotado o un deadlock, no el servicio caido."),
}

POR_TLS = {
    "SVC-09": CheckDefinition(
        check_id="SVC-09", version=1, titulo="Vencimiento del certificado",
        selector="tls:*", expectation_id="svc.tls", schedule_sec=21600,
        evaluate=_svc09, severity=Severity.WARNING,
        hysteresis=Hysteresis(abre_tras=1, cierra_tras=1)),
}


def descubrir_recursos(cfg: Any, host_id: str) -> list[Resource]:
    """Unidades, puertos, endpoints y certificados de esta máquina.

    Las unidades DECLARADAS se controlan. Las que aparecen en el sistema y no
    están declaradas quedan como recurso UNCLASSIFIED: se ven en el panel, no
    generan alerta, y esperan que alguien diga qué son.
    """
    out: list[Resource] = []

    for unidad in cfg.units_activas:
        out.append(Resource(resource_id=f"systemd:{unidad}", host_id=host_id,
                            kind="systemd", scope={"unidad": unidad},
                            state=ResourceState.MANAGED,
                            attrs={"esperada_activa": True}))
    for unidad in cfg.units_detenidas:
        out.append(Resource(resource_id=f"systemd:{unidad}", host_id=host_id,
                            kind="systemd", scope={"unidad": unidad},
                            state=ResourceState.MANAGED,
                            attrs={"esperada_activa": False}))

    for ep in cfg.endpoints_habilitados:
        out.append(Resource(resource_id=f"endpoint:{ep['id']}", host_id=host_id,
                            kind="endpoint", scope={"id": ep["id"]},
                            state=ResourceState.MANAGED, attrs=dict(ep)))

    for s in (cfg.umbral("sockets", []) or []):
        out.append(Resource(resource_id=f"socket:{s['puerto']}", host_id=host_id,
                            kind="socket", scope={"puerto": s["puerto"]},
                            state=ResourceState.MANAGED, attrs=dict(s)))

    for t in (cfg.umbral("tls", []) or []):
        out.append(Resource(resource_id=f"tls:{t['host']}", host_id=host_id,
                            kind="tls",
                            scope={"host": t["host"], "puerto": t.get("puerto", 443)},
                            state=ResourceState.MANAGED, attrs=dict(t)))
    return out


def descubrir_no_declaradas(cfg: Any, host_id: str) -> list[Resource]:
    """Unidades que existen y nadie declaró. No alertan: se inventarían."""
    declaradas = set(cfg.units_activas) | set(cfg.units_detenidas)
    ignoradas = {i.unidad for i in cfg.units_ignoradas if i.vigente}
    out = []
    for u in systemd.listar_servicios():
        if u in declaradas or u in ignoradas:
            continue
        out.append(Resource(resource_id=f"systemd:{u}", host_id=host_id,
                            kind="systemd", scope={"unidad": u},
                            state=ResourceState.UNCLASSIFIED))
    return out


def planificar(cfg: Any, host_id: str) -> list[tuple[CheckDefinition, Resource]]:
    pares: list[tuple[CheckDefinition, Resource]] = []
    for r in descubrir_recursos(cfg, host_id):
        catalogo = {"systemd": POR_UNIDAD, "socket": POR_PUERTO,
                    "endpoint": POR_ENDPOINT, "tls": POR_TLS}.get(r.kind, {})
        for cd in catalogo.values():
            # Una unidad que se espera DETENIDA no necesita el control de bucle
            # de reinicios: no debería estar corriendo en absoluto.
            if cd.check_id == "SVC-03" and not r.attrs.get("esperada_activa", True):
                continue
            pares.append((cd, r))
    return pares
