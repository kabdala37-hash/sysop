"""Antes de informar, se vuelve a mirar.

El diagnostico se hace una sola vez, al abrir el incidente, y el informe lo
repetia todos los dias. Medido en srv2 el 2026-09-17: un trabajo arreglado el 16
seguia saliendo con la causa del 13, y un falso "401 de Telefonia" salia
como hecho. El correo decia cosas que ya no eran ciertas, o que nunca lo
fueron.

Ahora, antes de armar el correo, el agente mira el servidor OTRA VEZ por cada
incidente abierto y contesta una de cuatro cosas. El correo se arma con eso:

    real                  lista principal, con la causa solo si la pudo citar
    no_se_pudo_validar    lista principal, pero no sube el asunto por encima
                          de WARNING (decision del usuario, 2026-09-17)
    falsa_alarma          seccion aparte, con el motivo y la cita
    arreglado_pendiente   seccion aparte HASTA la fecha de confirmacion; si
                          para entonces sigue abierto, vuelve a la principal

Una persona tambien puede decir "ya lo arregle" por Telegram (`marcar`): cuenta
como un arreglado_pendiente, con su nombre y su motivo, y el agente lo comprueba
igual.

Lo que NO cambia:

- Lo critico y los controles de `diagnostico.SIN_FILTRO` quedan en la lista
  principal diga lo que diga el agente o la persona.
- Ningun veredicto cierra un incidente. Eso lo sigue haciendo el plano
  deterministico, cuando el control vuelve a dar bien.
- Las alertas urgentes no pasan por aca: salen en el momento, como siempre.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sysop import diagnostico as diag_mod
from sysop.core import incidents
from sysop.core.models import Severity
from sysop.llm import contexto as ctx_mod
from sysop.llm import esquema, gateway

log = logging.getLogger("sysop.revalidacion")

# Cuantos incidentes se revalidan por informe, y cuanto tiempo en total. Cada
# uno es una conversacion con herramientas (10 a 60 s). Los que no entran salen
# como `no_se_pudo_validar`, que es lo que son.
MAX_POR_INFORME = 15
MAX_SEG = 900

# Plazo de confirmacion cuando nadie sabe cuando corre de nuevo lo que se
# arreglo (no es un trabajo programado y no se dio fecha).
PLAZO_SIN_CALENDARIO_H = 24

TEXTO = {"real": "real", "falsa_alarma": "falsa alarma",
         "arreglado_pendiente": "arreglado",
         "no_se_pudo_validar": "sin validar"}


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _fecha(v: Any) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def habilitada(cfg_llm: dict[str, Any]) -> bool:
    return bool((cfg_llm.get("revalidar") or {}).get("habilitada", True))


# ---------------------------------------------------------------------------
# Revalidar
# ---------------------------------------------------------------------------

def revalidar_activos(ctx: Any, cfg: Any, cfg_llm: dict[str, Any],
                      activos: list[dict[str, Any]]) -> Optional[str]:
    """Revalida los abiertos. Devuelve desde cuando valen los veredictos.

    None significa que NO se revalido nada (no hay agente, o esta apagada), y
    el informe se arma como antes. Distinto de un agente que no contesto: ese
    caso deja `no_se_pudo_validar` escrito, incidente por incidente.
    """
    if cfg is None or not cfg.puede_diagnosticar or not habilitada(cfg_llm):
        return None
    conf = cfg_llm.get("revalidar") or {}
    maximo = int(conf.get("max_por_informe", MAX_POR_INFORME))
    tope_seg = float(conf.get("max_seg", MAX_SEG))

    desde = _ahora().isoformat()
    extra = diag_mod.herramientas_de(ctx.app)
    t0 = time.monotonic()
    hechos = 0
    for inc in activos:
        if inc.get("state") == "RECOVERED":
            continue
        if hechos >= maximo or time.monotonic() - t0 > tope_seg:
            guardar(ctx.conn, inc, esquema.sin_validar(
                "no alcanzo el tiempo del informe para revisarlo"), ctx.app, "")
            continue
        hechos += 1
        rev = gateway.revalidar(ctx.conn, cfg.agente, armar(ctx, inc),
                                inc["incident_id"], cfg_llm, extra)
        if rev is None:
            rev = esquema.sin_validar("el agente no respondio, o respondio algo "
                                      "que no se puede usar")
        guardar(ctx.conn, inc, rev, ctx.app, cfg.agente.modelo)
        log.info("%s (%s en %s) revalidado: %s, %d cita(s) verificada(s)%s",
                 inc["incident_id"], inc["check_id"], inc["resource_id"],
                 rev.veredicto, len(rev.citas_verificadas),
                 f" -- {rev.nota}" if rev.nota else "")
    return desde


def armar(ctx: Any, inc: dict[str, Any]) -> dict[str, Any]:
    """El paquete del modo `revalidar`: el de siempre, mas lo que ya se dijo.

    Va lo que se dijo antes a proposito, marcado como tal: el diagnostico de
    apertura puede estar viejo o equivocado, y el agente tiene que comprobarlo
    en vez de repetirlo.
    """
    conn = ctx.conn
    paquete = ctx_mod.armar(conn, inc, ctx.cfg.installation, ctx.app,
                            mapa=ctx.cfg.mapa, modo="revalidar")
    iid = inc["incident_id"]
    d = diag_mod.de(conn, iid)
    previa = de(conn, iid)
    m = marca(conn, iid)
    calc = confirmacion_de(ctx.app, inc["resource_id"])
    paquete.update(ctx_mod.redactar({
        "ahora": _ahora().isoformat(),
        "diagnostico_al_abrir": ({
            "cuando": d["at"], "es_real": bool(d["es_real"]),
            "confianza": d["confianza"], "causa": d["causa"],
            "accion": d["accion"],
            "ojo": "puede estar viejo o equivocado: comprobalo, no lo repitas",
        } if d else None),
        "revalidacion_anterior": ({
            "cuando": previa["at"], "veredicto": previa["veredicto"],
            "motivo": previa["motivo"],
            "pendiente_desde": previa["pendiente_desde"],
            "confirma_en": previa["confirma_en"],
        } if previa else None),
        "marca_de_una_persona": ({
            "quien": m["quien"], "cuando": m["at"], "motivo": m["motivo"],
            "confirma_en": m["confirma_en"],
            "ojo": "comprobalo igual: si la causa sigue, es real",
        } if m else None),
        "confirmacion_calculada": calc.isoformat() if calc else None,
    }))
    return paquete


def guardar(conn: Any, inc: dict[str, Any], rev: esquema.Revalidacion,
            app: Any, modelo: str) -> None:
    iid = inc["incident_id"]
    previa = de(conn, iid)
    pendiente_desde = confirma_en = None
    if rev.veredicto == "arreglado_pendiente":
        if previa and previa["veredicto"] == "arreglado_pendiente" \
                and previa["confirma_en"]:
            # Anclado a la primera vez. Recalcularlo en cada informe lo
            # correria a "la proxima corrida" para siempre.
            pendiente_desde = previa["pendiente_desde"]
            confirma_en = previa["confirma_en"]
        else:
            pendiente_desde = _ahora().isoformat()
            confirma_en = plazo(app, inc["resource_id"],
                                sugerido=rev.confirmar_despues_de).isoformat()

    conn.execute(
        "INSERT INTO revalidacion (incident_id, at, veredicto, confianza, causa, "
        "accion, motivo, nota, evidencia_json, pendiente_desde, confirma_en, "
        "modelo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(incident_id) DO UPDATE SET at=excluded.at, "
        "veredicto=excluded.veredicto, confianza=excluded.confianza, "
        "causa=excluded.causa, accion=excluded.accion, motivo=excluded.motivo, "
        "nota=excluded.nota, evidencia_json=excluded.evidencia_json, "
        "pendiente_desde=excluded.pendiente_desde, "
        "confirma_en=excluded.confirma_en, modelo=excluded.modelo",
        (iid, _ahora().isoformat(), rev.veredicto, rev.confianza, rev.causa,
         rev.accion, rev.motivo, rev.nota,
         json.dumps(list(rev.evidencia), ensure_ascii=False),
         pendiente_desde, confirma_en, modelo))

    if previa is None or previa["veredicto"] != rev.veredicto:
        try:
            incidents._evento(conn, iid, "revalidado", {
                "veredicto": rev.veredicto, "motivo": rev.motivo[:300],
                "nota": rev.nota[:200]})
        except Exception:  # noqa: BLE001
            # Un incidente que solo esta en el registro no tiene fila: el
            # evento no entra, el veredicto si.
            log.debug("sin evento de revalidacion para %s", iid)


def de(conn: Any, incident_id: str) -> Optional[dict[str, Any]]:
    f = conn.execute("SELECT * FROM revalidacion WHERE incident_id = ?",
                     (incident_id,)).fetchone()
    if f is None:
        return None
    d = dict(f)
    try:
        d["evidencia"] = json.loads(d.pop("evidencia_json") or "[]")
    except json.JSONDecodeError:
        d["evidencia"] = []
    return d


def confirmacion_de(app: Any, resource_id: str) -> Optional[datetime]:
    """Lo que el adaptador sabe de cuando se puede confirmar. Nunca lanza."""
    fn = getattr(app, "confirmacion", None)
    if not callable(fn):
        return None
    try:
        return fn(resource_id)
    except Exception:  # noqa: BLE001
        log.exception("fallo el calculo de confirmacion de %s", resource_id)
        return None


def plazo(app: Any, resource_id: str, sugerido: str = "",
          horas: Optional[float] = None) -> datetime:
    """Hasta cuando algo puede quedar como "arreglado, falta confirmar".

    Manda, en este orden: las horas que dio una persona, lo que calcula el
    adaptador (la proxima corrida del trabajo), la fecha que propuso el agente,
    y si no hay nada, un dia. Nunca mas de `MAX_DIAS_CONFIRMAR`.
    """
    ahora = _ahora()
    d = None
    if horas:
        d = ahora + timedelta(hours=max(1.0, float(horas)))
    if d is None:
        d = confirmacion_de(app, resource_id)
    if d is None and sugerido:
        s = _fecha(sugerido)
        d = s if s and s > ahora else None
    if d is None:
        d = ahora + timedelta(hours=PLAZO_SIN_CALENDARIO_H)
    return min(d, ahora + timedelta(days=esquema.MAX_DIAS_CONFIRMAR))


# ---------------------------------------------------------------------------
# "Ya lo arregle", por Telegram
# ---------------------------------------------------------------------------

def marcar(conn: Any, app: Any, incident_id: str, motivo: str, quien: str,
           horas: Optional[float] = None,
           cfg_llm: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    inc = conn.execute(
        "SELECT incident_id, resource_id, check_id, severity, closed_at "
        "FROM incident WHERE incident_id = ?", (incident_id,)).fetchone()
    if inc is None:
        return {"hecho": False,
                "problema": f"no existe el incidente {incident_id!r}: "
                            f"buscalo con `estado`"}
    if inc["closed_at"]:
        return {"hecho": False, "problema": "ese incidente ya esta cerrado"}
    motivo = (motivo or "").strip()
    if len(motivo) < 8:
        return {"hecho": False,
                "problema": "falta el motivo: que se arreglo y como. Sin eso "
                            "nadie puede revisar la decision despues"}

    ahora = _ahora()
    hasta = plazo(app, inc["resource_id"], horas=horas)
    conn.execute(
        "UPDATE marca_arreglado SET revocada_at = ?, revocada_por = ? "
        "WHERE incident_id = ? AND revocada_at IS NULL",
        (ahora.isoformat(), f"{quien} (la reemplazo)", incident_id))
    conn.execute(
        "INSERT INTO marca_arreglado (incident_id, motivo, quien, at, "
        "confirma_en) VALUES (?,?,?,?,?)",
        (incident_id, motivo[:600], quien, ahora.isoformat(), hasta.isoformat()))
    incidents._evento(conn, incident_id, "marcado_arreglado",
                      {"quien": quien, "motivo": motivo[:300],
                       "confirma_en": hasta.isoformat()})
    protegido = diag_mod.sin_filtro(inc["check_id"], inc["severity"],
                                    cfg_llm or {})
    return {
        "hecho": True, "incidente": incident_id, "recurso": inc["resource_id"],
        "confirma_en": hasta.isoformat(),
        "nota": ("es un control critico: sigue en la lista principal del "
                 "informe igual, con la marca a la vista" if protegido else
                 "sale de la lista principal hasta esa fecha. Si para "
                 "entonces sigue abierto, vuelve, con la marca a la vista. "
                 "El agente lo va a comprobar igual en cada informe"),
    }


def desmarcar(conn: Any, incident_id: str, quien: str) -> bool:
    cur = conn.execute(
        "UPDATE marca_arreglado SET revocada_at = ?, revocada_por = ? "
        "WHERE incident_id = ? AND revocada_at IS NULL",
        (_ahora().isoformat(), quien, incident_id))
    hecho = bool(cur.rowcount and cur.rowcount > 0)
    if hecho:
        incidents._evento(conn, incident_id, "desmarcado_arreglado",
                          {"quien": quien})
    return hecho


def marca(conn: Any, incident_id: str) -> Optional[dict[str, Any]]:
    f = conn.execute(
        "SELECT * FROM marca_arreglado WHERE incident_id = ? "
        "AND revocada_at IS NULL ORDER BY id DESC LIMIT 1",
        (incident_id,)).fetchone()
    return dict(f) if f else None


# ---------------------------------------------------------------------------
# En que lista va cada uno
# ---------------------------------------------------------------------------

@dataclass
class Clasificacion:
    cuentan: list = field(default_factory=list)       # lista principal (y los RECOVERED)
    falsas: list = field(default_factory=list)
    pendientes: list = field(default_factory=list)    # arreglados, falta confirmar
    descartados: list = field(default_factory=list)   # sin revalidar: el criterio viejo


def clasificar(conn: Any, activos: list[dict[str, Any]],
               cfg_llm: dict[str, Any], desde: Optional[str],
               ahora: Optional[datetime] = None) -> Clasificacion:
    """Parte los activos en las listas del informe.

    `desde` es lo que devolvio `revalidar_activos`: solo cuentan los veredictos
    de esta corrida. Con None no hubo revalidacion y se usa el criterio de
    antes (el descarte del diagnostico de apertura); las marcas de una persona
    valen igual.
    """
    ahora = ahora or _ahora()
    out = Clasificacion()
    for i in activos:
        if i.get("state") == "RECOVERED":
            out.cuentan.append(i)
            continue
        i = dict(i)
        iid = i["incident_id"]
        d = diag_mod.de(conn, iid)
        if d:
            i["diagnostico"] = d
        protegido = diag_mod.sin_filtro(i["check_id"], i["severity"], cfg_llm)
        r = de(conn, iid) if desde else None
        if r is not None and r["at"] < desde:
            r = None
        if r is not None:
            i["revalidacion"] = r
        m = marca(conn, iid)
        if m is not None:
            i["marca"] = m

        if m is not None:
            vence = _fecha(m["confirma_en"])
            quien = str(m["quien"]).replace("telegram:", "")
            if vence is not None and vence >= ahora and not protegido:
                i["pendiente"] = {"origen": quien, "motivo": m["motivo"],
                                  "desde": m["at"], "confirma_en": m["confirma_en"]}
                out.pendientes.append(i)
                continue
            if vence is not None and vence >= ahora:
                i["nota"] = (f"{quien} lo marco arreglado ({m['motivo']}), pero "
                             f"es un control critico: se sigue mostrando")
            else:
                i["nota"] = (f"{quien} lo marco arreglado el {_corta(m['at'])} "
                             f"({m['motivo']}), pero despues de "
                             f"{_corta(m['confirma_en'])} sigue abierto")
            out.cuentan.append(i)
            continue

        if desde is None:
            (out.descartados if diag_mod.descartado(conn, iid)
             else out.cuentan).append(i)
            continue
        if r is None:
            # No se reviso en esta corrida (solo esta en el registro en
            # archivo): va como antes, sin tocarle la severidad.
            out.cuentan.append(i)
            continue

        v = r["veredicto"]
        if v == "no_se_pudo_validar":
            i["sin_validar"] = True
            if not protegido and \
                    Severity(i["severity"]).orden > Severity.WARNING.orden:
                i["severidad_medida"] = i["severity"]
                i["severity"] = Severity.WARNING.value
            out.cuentan.append(i)
        elif v == "real":
            out.cuentan.append(i)
        elif protegido:
            i["nota"] = (f"el agente lo da por {TEXTO[v]}, pero es un control "
                         f"critico: se sigue mostrando")
            out.cuentan.append(i)
        elif v == "falsa_alarma":
            out.falsas.append(i)
        elif v == "arreglado_pendiente":
            vence = _fecha(r["confirma_en"])
            if vence is not None and vence >= ahora:
                i["pendiente"] = {"origen": "agente", "motivo": r["motivo"],
                                  "desde": r["pendiente_desde"],
                                  "confirma_en": r["confirma_en"]}
                out.pendientes.append(i)
            else:
                i["nota"] = (f"el agente lo da por arreglado desde "
                             f"{_corta(r['pendiente_desde'])}, pero ya paso "
                             f"{_corta(r['confirma_en'])} y sigue abierto")
                out.cuentan.append(i)
        else:
            out.cuentan.append(i)
    return out


def _corta(v: Any) -> str:
    d = _fecha(v)
    return d.strftime("%Y-%m-%d %H:%M UTC") if d else str(v or "?")


def para_el_modelo(cl: Clasificacion) -> list[dict[str, Any]]:
    """Lo que la lectura del periodo tiene que saber de la revalidacion.

    Sin esto, el resumen de arriba del correo repetia la causa vieja de un
    incidente que la revalidacion acababa de dar por falso.
    """
    out = []
    for lista, grupo in ((cl.cuentan, "sigue_en_la_lista"),
                         (cl.falsas, "falsa_alarma"),
                         (cl.pendientes, "arreglado_falta_confirmar"),
                         (cl.descartados, "descartado_al_abrir")):
        for i in lista:
            if i.get("state") == "RECOVERED":
                continue
            r = i.get("revalidacion") or {}
            out.append({
                "control": i["check_id"], "recurso": i["resource_id"],
                "en_el_informe": grupo,
                "veredicto": r.get("veredicto"),
                "motivo": r.get("motivo"),
                "causa_verificada": r.get("causa") or None,
                "nota": i.get("nota") or r.get("nota") or None,
                "marca_de_una_persona": (i.get("marca") or {}).get("motivo"),
            })
    return out
