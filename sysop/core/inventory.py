"""Inventario de recursos: qué hay, desde cuándo, y qué cambió.

Resuelve un problema concreto del descubrimiento. En un servidor cualquiera hay
más de doscientas unidades de systemd; reportarlas todas como "sin clasificar"
en cada ciclo produce una lista que nadie lee.

Lo que importa no es la lista completa: es **el cambio**. Un recurso que apareció
o desapareció desde la última vez es un hallazgo. Los otros doscientos siguen
estando, se pueden consultar en el panel, y no generan nada.

Esto es también lo que hace cumplible el principio "descubrir no es aprobar":
lo nuevo entra como `UNCLASSIFIED` y ahí se queda hasta que una regla o una
persona lo mueva. El inventario nunca lo promueve solo.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from sysop.core.models import Resource, ResourceState

log = logging.getLogger("sysop.inventory")


@dataclass(frozen=True)
class Cambios:
    nuevos: tuple[Resource, ...] = ()
    desaparecidos: tuple[str, ...] = ()
    total: int = 0
    primera_vez: bool = False

    @property
    def hay_novedades(self) -> bool:
        # En el primer arranque TODO es nuevo. Reportarlo sería una alerta de
        # doscientas líneas que no dice nada: la primera corrida establece la
        # línea de base, no la juzga.
        return not self.primera_vez and bool(self.nuevos or self.desaparecidos)


def sincronizar(conn: Any, recursos: Iterable[Resource],
                host_id: str, kinds: Iterable[str]) -> Cambios:
    """Persiste el inventario y devuelve qué cambió.

    `kinds` acota la comparación: sincronizar sólo las unidades de systemd no
    tiene que hacer desaparecer los mounts. Sin ese filtro, cada collector
    borraría los recursos de los demás.
    """
    ahora = datetime.now(timezone.utc).isoformat()
    kinds = tuple(kinds)
    marcadores = ",".join("?" * len(kinds))

    previos = {
        f["resource_id"]: f
        for f in conn.execute(
            f"SELECT resource_id, state FROM resource "
            f"WHERE host_id = ? AND kind IN ({marcadores})",
            (host_id, *kinds),
        ).fetchall()
    }
    primera_vez = not previos

    nuevos: list[Resource] = []
    vistos: set[str] = set()

    for r in recursos:
        vistos.add(r.resource_id)
        if r.resource_id not in previos:
            nuevos.append(r)
            conn.execute(
                "INSERT INTO resource (resource_id, host_id, kind, scope_json, "
                "state, first_seen, last_seen, attrs_json) VALUES (?,?,?,?,?,?,?,?)",
                (r.resource_id, host_id, r.kind, json.dumps(r.scope, default=str),
                 r.state.value, ahora, ahora, json.dumps(r.attrs, default=str)),
            )
        elif previos[r.resource_id]["state"] == ResourceState.RETIRED.value:
            # VOLVIO. RETIRED lo puso el sistema al no verlo, no una persona
            # clasificandolo, asi que verlo de nuevo lo revierte: un recurso que
            # esta ahi no puede quedar marcado como que se fue. Sin esto se
            # queda en RETIRED para siempre y el dia que se vaya EN SERIO nadie
            # se entera, porque el filtro de mas abajo lo saltea por su estado.
            conn.execute(
                "UPDATE resource SET last_seen = ?, attrs_json = ?, state = ? "
                "WHERE resource_id = ?",
                (ahora, json.dumps(r.attrs, default=str), r.state.value,
                 r.resource_id),
            )
        else:
            # Fuera de ese caso NO se pisa el `state`: si alguien clasificó este
            # recurso como IGNORED o MANAGED, el descubrimiento no puede
            # devolverlo a UNCLASSIFIED en el próximo ciclo.
            conn.execute(
                "UPDATE resource SET last_seen = ?, attrs_json = ? WHERE resource_id = ?",
                (ahora, json.dumps(r.attrs, default=str), r.resource_id),
            )

    # Los que YA estaban RETIRED no vuelven a "desaparecer": se fueron una vez y
    # eso ya se informo. Sin este filtro, cada recurso que alguna vez se fue se
    # cuenta de nuevo en cada pasada y para siempre — medido en srv3: 1.544 de
    # las 1.614 lineas del log (el 95,7%) eran la misma linea
    # `inventario: 0 nuevos, 1 desaparecidos` repetida 772 veces, cada 5
    # minutos, sin converger nunca. Un log que repite algo que no cambia entrena
    # a ignorarlo.
    desaparecidos = tuple(
        rid for rid, fila in previos.items()
        if rid not in vistos and fila["state"] != ResourceState.RETIRED.value
    )
    for rid in desaparecidos:
        # RETIRED y no borrado: la historia de un recurso que se fue sigue
        # siendo evidencia, y si vuelve hay que poder ver que ya había estado.
        conn.execute("UPDATE resource SET state = ? WHERE resource_id = ?",
                     (ResourceState.RETIRED.value, rid))

    if primera_vez:
        log.info("inventario inicial: %d recursos de tipo %s", len(nuevos), kinds)
    elif nuevos or desaparecidos:
        log.info("inventario: %d nuevos, %d desaparecidos",
                 len(nuevos), len(desaparecidos))

    return Cambios(nuevos=tuple(nuevos), desaparecidos=desaparecidos,
                   total=len(vistos), primera_vez=primera_vez)


def sin_clasificar(conn: Any, host_id: str) -> list[dict[str, Any]]:
    """Los que están esperando que alguien diga qué son."""
    return [dict(f) for f in conn.execute(
        "SELECT resource_id, kind, first_seen FROM resource "
        "WHERE host_id = ? AND state = ? ORDER BY first_seen DESC",
        (host_id, ResourceState.UNCLASSIFIED.value),
    ).fetchall()]


# ---------------------------------------------------------------------------
# Clasificación a mano: "esto es nuestro, no avises"
# ---------------------------------------------------------------------------
# Lo pidió Operador el 2026-09-03: poder contestarle al bot "este job X es
# nuestro" y que el observador deje de contarlo como sospechoso.
#
# Las tres reglas NO son nuevas, ya estaban decididas en el código:
#
#   1. Todo silencio vence. `core/config.py`: "Un silencio sin fecha es un
#      control borrado. Vencido, vuelve a sonar." Tope: 30 días.
#   2. Sólo se silencia lo WARNING. Lo MAJOR y lo CRITICAL avisan igual, y
#      `alertas.py` ya lo dice: "si alguien agrega mañana una via de silenciado,
#      que no pase por encima de lo critico sin darse cuenta". Esta es esa vía.
#   3. Descubrir no es aprobar: el aprendizaje nunca clasifica solo. Esto lo
#      mueve una persona, y queda registrado quién.
# ---------------------------------------------------------------------------

DIAS_MAX = 30

# Lo que una persona puede declarar. `MANAGED` es "es nuestro y lo esperamos",
# `EXTERNAL` "es de otro sistema", `IGNORED` "no me interesa". Los tres sacan al
# recurso de la lista de sin clasificar; sólo IGNORED y EXTERNAL silencian.
ESTADOS = (ResourceState.IGNORED, ResourceState.MANAGED, ResourceState.EXTERNAL)
SILENCIAN = (ResourceState.IGNORED, ResourceState.EXTERNAL)


class ClasificacionInvalida(ValueError):
    """Lo que pidieron no se puede hacer, con el motivo en el mensaje."""


def clasificar(conn: Any, resource_id: str, host_id: str, estado: str,
               motivo: str, quien: str, dias: int) -> dict[str, Any]:
    """Declara qué es un recurso. Devuelve la clasificación que quedó.

    Valida acá y no en la tool para que la regla valga venga de donde venga: si
    mañana esto se llama desde el CLI o desde un endpoint, las condiciones son
    las mismas.
    """
    from datetime import timedelta                            # noqa: PLC0415

    est = str(estado).upper()
    if est not in {e.value for e in ESTADOS}:
        raise ClasificacionInvalida(
            "estado invalido: %s (esperado %s)"
            % (estado, " | ".join(e.value for e in ESTADOS)))

    motivo = (motivo or "").strip()
    if len(motivo) < 3:
        raise ClasificacionInvalida(
            "hace falta un motivo: sin el, dentro de seis meses nadie sabe por "
            "que esto esta en silencio")

    quien = (quien or "").strip()
    if not quien:
        raise ClasificacionInvalida("hace falta saber quien lo clasifico")

    try:
        dias = int(dias)
    except (TypeError, ValueError):
        raise ClasificacionInvalida("los dias tienen que ser un numero") from None
    if dias < 1 or dias > DIAS_MAX:
        raise ClasificacionInvalida(
            "el silencio va de 1 a %d dias. Para algo permanente, declaralo en "
            "el YAML del host, que va a git y queda versionado" % DIAS_MAX)

    fila = conn.execute("SELECT resource_id FROM resource WHERE resource_id = ? "
                        "AND host_id = ?", (resource_id, host_id)).fetchone()
    if not fila:
        raise ClasificacionInvalida(
            "no hay ningun recurso %r en %s. Mira como se llama con la "
            "herramienta de recursos antes de clasificarlo" % (resource_id, host_id))

    ahora = datetime.now(timezone.utc)
    vence = ahora + timedelta(days=dias)

    # Una clasificacion nueva revoca la anterior: se guarda la vieja para tener
    # la historia, pero vigente hay una sola.
    conn.execute(
        "UPDATE resource_clasificacion SET revocada_at = ?, revocada_por = ? "
        "WHERE resource_id = ? AND host_id = ? AND revocada_at IS NULL",
        (ahora.isoformat(), quien, resource_id, host_id))
    conn.execute(
        "INSERT INTO resource_clasificacion "
        "(resource_id, host_id, estado, motivo, quien, at, vence_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (resource_id, host_id, est, motivo, quien, ahora.isoformat(),
         vence.isoformat()))
    conn.execute("UPDATE resource SET state = ? WHERE resource_id = ?",
                 (est, resource_id))
    conn.commit()
    log.info("clasificado %s como %s por %s hasta %s (%s)",
             resource_id, est, quien, vence.date(), motivo)
    return {"resource_id": resource_id, "estado": est, "motivo": motivo,
            "quien": quien, "vence_at": vence.isoformat(), "dias": dias}


def desclasificar(conn: Any, resource_id: str, host_id: str,
                  quien: str) -> bool:
    """Revoca la clasificación vigente y devuelve el recurso a la lista."""
    ahora = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE resource_clasificacion SET revocada_at = ?, revocada_por = ? "
        "WHERE resource_id = ? AND host_id = ? AND revocada_at IS NULL",
        (ahora, quien or "?", resource_id, host_id))
    if not cur.rowcount:
        return False
    conn.execute("UPDATE resource SET state = ? WHERE resource_id = ?",
                 (ResourceState.UNCLASSIFIED.value, resource_id))
    conn.commit()
    log.info("desclasificado %s por %s", resource_id, quien)
    return True


def clasificacion_vigente(conn: Any, resource_id: str,
                          host_id: str) -> Optional[dict[str, Any]]:
    """La clasificación que rige ahora, o None. Una vencida no rige."""
    fila = conn.execute(
        "SELECT resource_id, estado, motivo, quien, at, vence_at "
        "FROM resource_clasificacion "
        "WHERE resource_id = ? AND host_id = ? AND revocada_at IS NULL "
        "  AND vence_at > ? ORDER BY id DESC LIMIT 1",
        (resource_id, host_id, datetime.now(timezone.utc).isoformat()),
    ).fetchone()
    return dict(fila) if fila else None


def clasificaciones(conn: Any, host_id: str,
                    incluir_vencidas: bool = False) -> list[dict[str, Any]]:
    """Lo que hay clasificado a mano. Es la lista que hace esto auditable."""
    ahora = datetime.now(timezone.utc).isoformat()
    extra = "" if incluir_vencidas else " AND vence_at > ?"
    params: tuple[Any, ...] = ((host_id,) if incluir_vencidas
                               else (host_id, ahora))
    return [dict(f) for f in conn.execute(
        "SELECT resource_id, estado, motivo, quien, at, vence_at "
        "FROM resource_clasificacion "
        "WHERE host_id = ? AND revocada_at IS NULL" + extra +
        " ORDER BY vence_at", params).fetchall()]


def silenciado(conn: Any, resource_id: str, host_id: str,
               severidad: str) -> Optional[dict[str, Any]]:
    """Si este incidente no tiene que sonar, y por que.

    **Sólo lo WARNING se silencia.** Un MAJOR o un CRITICAL avisan igual, tenga
    el recurso la clasificación que tenga: silenciar por chat algo que puede
    tirar el servicio abajo es exactamente lo que `alertas.py` pide que no pase.
    """
    if str(severidad).lower() != "warning":
        return None
    c = clasificacion_vigente(conn, resource_id, host_id)
    if not c:
        return None
    if c["estado"] not in {e.value for e in SILENCIAN}:
        return None
    return c
