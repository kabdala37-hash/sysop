"""La alerta urgente: enterarse ahora, no en el próximo informe.

Es el otro canal de salida. Sale sola, en el momento, y sólo para lo crítico:
si todo mandara alerta, no habría diferencia entre urgente y no urgente, y la
primera vez que suene por algo que podía esperar deja de ser urgente para
siempre.

La idempotencia sale gratis de la bandeja de salida: la clave única es
`(tipo, periodo)`, y acá el periodo es el id del incidente. Un incidente crítico
genera una alerta y nada más, por más ciclos que siga abierto.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sysop import diagnostico
from sysop.core import incidents
from sysop.core.models import Severity
from sysop.notify import outbox, render
from sysop.store import registro

log = logging.getLogger("sysop.alertas")

TIPO = "urgente"

# Cuanto hacia atras se mira el registro para no repetir una alerta (D4 del
# plan de resiliencia, 2026-09-14).
VENTANA_DEDUPE_H = 24

# Los que ya se saltearon por el registro, para decirlo una vez y no por tick.
_salteados: set[str] = set()


def habilitadas(cfg_alertas: dict[str, Any]) -> bool:
    """Por defecto SI. Es el canal que no puede faltar.

    El informe periódico depende del cron y puede no usarse nunca; la alerta
    urgente existe mientras el servicio corra. El interruptor está para poder
    apagarla en una ventana de mantenimiento, no para el uso normal: apagarla
    deja al observador sin forma de avisar en el momento, y con el periódico
    desactivado también, sin forma de avisar nunca.
    """
    return bool((cfg_alertas or {}).get("urgentes", True))


def revisar(ctx: Any, cfg: Any, cfg_alertas: Optional[dict[str, Any]] = None) -> int:
    """Manda una alerta por cada incidente crítico que todavía no la tenga."""
    if not habilitadas(cfg_alertas or {}):
        return 0
    if not cfg.puede_notificar:
        return 0

    activos = incidents.activos(ctx.conn, Severity.CRITICAL)
    criticos = [i for i in activos
                if i["state"] == "OPEN" and i["severity"] == Severity.CRITICAL.value]
    if not criticos:
        return 0

    # Un critico NUNCA se silencia por diagnostico: `sin_filtro` lo protege y
    # `descartado()` devuelve False para el. Esto es defensa en profundidad, no
    # redundancia: si alguien agrega mañana una via de silenciado, que no pase
    # por encima de lo critico sin darse cuenta.
    criticos = [i for i in criticos
                if not diagnostico.descartado(ctx.conn, i["incident_id"])]

    mandadas = 0
    for inc in criticos:
        if outbox.ya_enviado(ctx.conn, TIPO, inc["incident_id"]):
            continue
        # La bandeja es de ESTA base. Si la base se recreo (o el aviso salio por
        # el camino de emergencia, sin bandeja), el mismo problema trae otro id
        # y la bandeja nueva no lo conoce: sin esto la alerta salia dos veces.
        previa = registro.alerta_vigente(incidents.fp_corto(inc["incident_id"]),
                                         VENTANA_DEDUPE_H)
        if previa is not None:
            if inc["incident_id"] not in _salteados:
                _salteados.add(inc["incident_id"])
                log.warning("alerta de %s no se repite: ya salio %s (%s) y no "
                            "consta que se haya cerrado", inc["incident_id"],
                            previa.get("at", "")[:19], previa.get("periodo"))
            continue
        # Lo que estaba pasando al mismo tiempo, para no mandar el sintoma
        # desnudo: un disco lleno y tres servicios caidos son un solo problema,
        # y verlos juntos es la diferencia entre diagnosticar y adivinar.
        contexto = [i for i in activos
                    if i["incident_id"] != inc["incident_id"]][:5]
        outbox.encolar(
            ctx.conn, TIPO, inc["incident_id"],
            render.asunto_urgente(ctx.cfg.installation, inc),
            render.alerta_urgente(ctx.cfg.installation, inc, contexto,
                                  diagnostico.texto(ctx.conn, inc["incident_id"])),
            cfg.destinatarios)
        log.warning("alerta urgente encolada por %s (%s en %s)",
                    inc["incident_id"], inc["check_id"], inc["resource_id"])
        mandadas += 1
    return mandadas
