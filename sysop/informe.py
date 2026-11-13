"""El informe periódico. Lo dispara cron, no el daemon.

La razón es concreta: si el informe lo mandara el propio daemon y el daemon se
muriera, no llegaría nada, y no llegar nada es indistinguible de que esté todo
bien. Disparado por cron, el trabajo corre igual, mira el último estado
conocido y manda el correo **diciendo que el observador está caído**.

De paso, eso es lo que hace de latido: no hace falta ningún proceso vigilando
desde otro servidor.

La cadencia no está acá ni en la base: vive en `/etc/cron.d/sysop-agent`. Pasar
de diario a semanal es editar esa línea.
"""
from __future__ import annotations

import logging
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sysop import diagnostico as diag_mod, periodo as periodo_mod, settings
from sysop import revalidacion as reval_mod
from sysop.core import incidents, inventory
from sysop.core.models import Severity
from sysop.store import db, registro
from sysop.notify import outbox, render, smtp

log = logging.getLogger("sysop.informe")

TIPO = "informe"

# Cuánto puede pasar sin un solo resultado antes de dar por caído al observador.
# Tres veces el tick más holgura: un tick lento no es una caída.
SILENCIO_MAX_SEG = 300

# Sin base, cuánto registro se mira. Los incidentes abiertos con más ventana
# que los eventos: un crítico de hace tres días sigue siendo lo más importante.
DEGRADADO_ABIERTOS_H = 168
DEGRADADO_EVENTOS_H = 24
_EVENTOS_DEGRADADO = ("almacen_falla", "almacen_recupera", "almacen_aviso",
                      "modo_degradado_entra", "modo_degradado_sale",
                      "correo_enviado", "correo_fallido")


def generar(ctx: Any, forzar: bool = False,
            vista_previa: Optional[str] = None) -> dict[str, Any]:
    """Arma el informe, lo encola y despacha lo que haya pendiente.

    Con `vista_previa` (una ruta) lo arma entero, revalidacion incluida, y lo
    escribe en ese archivo SIN encolar ni mandar nada, y sin avanzar los
    cursores de los logs: el informe de verdad tiene que ver lo mismo.
    """
    conn = ctx.conn
    cfg = ctx.fuentes()
    if not cfg.puede_notificar and not vista_previa:
        log.error("no hay cuenta de correo o destinatarios: no se puede informar")
        return {"estado": "sin_destinatarios"}

    ahora_local = _ahora_local(cfg.timezone_name)
    periodo = ahora_local.strftime("%Y-%m-%dT%H")

    if vista_previa:
        pass
    elif forzar:
        # Con `--forzar` el periodo baja a minutos. No alcanza con saltear el
        # chequeo de abajo: la bandeja tiene clave unica (tipo, periodo) y
        # rechazaria el encolado igual, asi que el comando diria que mando algo
        # que nunca salio.
        periodo = ahora_local.strftime("%Y-%m-%dT%H:%M")
    elif outbox.ya_enviado(conn, TIPO, periodo) or registro.informe_enviado(periodo):
        # El registro tambien: con la base recreada en la misma hora, la bandeja
        # nueva no sabe que el informe ya salio (revision).
        log.info("el informe del periodo %s ya se envio", periodo)
        return {"estado": "ya_enviado", "periodo": periodo}

    activos = incidents.activos(conn, Severity.WARNING) + _solo_en_registro(conn)
    caido = _observador_caido(conn)

    # Que paso DESDE EL INFORME ANTERIOR. Si nunca hubo uno -- porque el cron no
    # esta instalado o porque es la primera vez -- se miran 24 horas. El informe
    # periodico puede no usarse nunca sin que nada mas dependa de el.
    desde = periodo_mod.desde_el_ultimo_informe(conn, TIPO)
    resumen_periodo = periodo_mod.resumir(
        conn, desde,
        ahora_local.astimezone(__import__("datetime").timezone.utc),
        logs_declarados=ctx.cfg.umbral("logs.extractos", []),
        avanzar_cursor=not vista_previa)

    # Lo que alguien silencio a mano sale de la lista principal, pero **NO del
    # informe**: va en su propia seccion, con el motivo, quien lo puso y hasta
    # cuando. Un silencio invisible es un control borrado; uno que se anuncia en
    # cada correo se puede discutir.
    #
    # `inventory.silenciado` es el que aplica la regla: solo lo WARNING. Un
    # problema mayor o critico sobre un recurso clasificado sigue en la lista
    # y avisa igual. Va ANTES de la revalidacion, sobre la severidad MEDIDA: la
    # revalidacion puede bajar un major sin validar a warning, y eso no puede
    # volverlo silenciable.
    silenciados = []
    quedan = []
    for i in activos:
        callado = None
        if i["state"] != "RECOVERED":
            callado = inventory.silenciado(conn, i.get("resource_id") or "",
                                           settings.HOST_ID, i.get("severity") or "")
        (silenciados if callado else quedan).append(
            dict(i, clasificacion=callado) if callado else i)

    # ANTES DE INFORMAR, SE VUELVE A MIRAR. Cada abierto se revalida contra el
    # servidor de ahora; lo que resulta falsa alarma o ya arreglado sale de la
    # lista principal, en su propia seccion (`revalidacion.py`).
    revalidado_desde = None
    try:
        revalidado_desde = reval_mod.revalidar_activos(
            ctx, cfg, ctx.cfg.llm,
            [i for i in quedan if not i.get("_solo_registro")])
    except Exception:  # noqa: BLE001
        log.exception("fallo la revalidacion; el informe sale sin ella")
    cl = reval_mod.clasificar(conn, quedan, ctx.cfg.llm, revalidado_desde)
    cuentan = cl.cuentan
    abiertos = [i for i in cuentan if i["state"] != "RECOVERED"]

    # El agente lee el periodo entero. Va despues de revalidar y antes del
    # render, para que su lectura entre en el correo y no contradiga lo que se
    # acaba de verificar.
    lectura = None
    try:
        lectura = diag_mod.del_periodo(
            ctx, cfg, ctx.cfg.llm, resumen_periodo,
            reval_mod.para_el_modelo(cl) if revalidado_desde else None)
    except Exception:  # noqa: BLE001
        log.exception("fallo el diagnostico del periodo; el informe sale igual")

    html = render.informe_periodico(
        instalacion=ctx.cfg.installation,
        fecha=ahora_local.strftime("%Y-%m-%d %H:%M"),
        incidentes=abiertos,
        resumen=resumen_controles(conn, len(ctx.planificar())),
        recuperados_24h=[i for i in cuentan if i["state"] == "RECOVERED"],
        sin_clasificar=len(inventory.sin_clasificar(conn, settings.HOST_ID)),
        huecos=huecos_24h(conn),
        origen_config=cfg.origen, desactualizados=cfg.desactualizados,
        fallidos=outbox.fallidos(conn),
        observador_caido=caido,
        descartados=cl.descartados,
        falsas=cl.falsas,
        pendientes=cl.pendientes,
        silenciados=silenciados,
        costo_modelo=costo_modelo(conn),
        periodo=resumen_periodo,
        lectura=lectura)

    asunto = render.asunto_informe(ctx.cfg.installation, abiertos,
                                   ahora_local.strftime("%Y-%m-%d"))
    if caido:
        asunto = asunto.replace("[OK]", "[CRITICAL]") + " — OBSERVADOR CAIDO"

    if vista_previa:
        from pathlib import Path                               # noqa: PLC0415
        Path(vista_previa).write_text(
            f"<!-- {asunto} -->\n{html}", encoding="utf-8")
        return {"estado": "vista_previa", "archivo": vista_previa,
                "asunto": asunto, "activos": len(abiertos),
                "revalidado": bool(revalidado_desde),
                "falsas": len(cl.falsas), "pendientes": len(cl.pendientes),
                "sin_validar": sum(1 for i in abiertos if i.get("sin_validar")),
                "clasificacion": cl}

    outbox.encolar(conn, TIPO, periodo, asunto, html, cfg.destinatarios)
    res = outbox.despachar(conn, cfg.cuenta)
    log.info("informe %s: %s", periodo, res)
    return {"estado": "encolado", "periodo": periodo, "envio": res,
            "observador_caido": bool(caido), "activos": len(abiertos),
            "descartados": len(cl.descartados),
            "falsas": len(cl.falsas), "pendientes": len(cl.pendientes),
            "ventana_horas": resumen_periodo["horas"],
            "transitorios": len(resumen_periodo["transitorios"]),
            "ceguera": len(resumen_periodo["ceguera"])}


def degradado(ctx: Any, falla: Any, forzar: bool = False) -> dict[str, Any]:
    """El informe sin almacén: sale igual, armado desde el registro en archivo.

    Medido el 2026-09-14: con la base corrupta, bloqueada o sin permiso el
    informe de cron moría en el primer SELECT y no llegaba ningún correo. Y un
    informe que no llega es exactamente lo que este canal existe para evitar.

    Va por SMTP directo, sin bandeja (la bandeja es una tabla de la base). La
    idempotencia sale del registro.
    """
    from sysop import logs as logs_mod                        # noqa: PLC0415

    cfg = ctx.fuentes()
    if not cfg.puede_notificar:
        log.error("sin almacen y sin cuenta de correo o destinatarios: no se "
                  "puede informar")
        return {"estado": "sin_destinatarios", "degradado": True}

    ahora_local = _ahora_local(cfg.timezone_name)
    periodo = ahora_local.strftime("%Y-%m-%dT%H:%M" if forzar else "%Y-%m-%dT%H")
    if not forzar and registro.informe_enviado(periodo):
        return {"estado": "ya_enviado", "periodo": periodo, "degradado": True}

    ahora = datetime.now(timezone.utc)
    abiertos = registro.abiertos_segun_registro(
        ahora - timedelta(hours=DEGRADADO_ABIERTOS_H))
    eventos = registro.leer(ahora - timedelta(hours=DEGRADADO_EVENTOS_H),
                            _EVENTOS_DEGRADADO)
    logs = []
    try:
        # `desde_cero` y sin avanzar: los cursores viven en la base.
        logs = logs_mod.barrer(None, ctx.cfg.umbral("logs.extractos", []),
                               avanzar=False, desde_cero=True)
    except Exception:  # noqa: BLE001
        log.exception("no se pudieron leer los logs para el informe degradado")

    fecha = ahora_local.strftime("%Y-%m-%d %H:%M")
    html = render.informe_degradado(
        ctx.cfg.installation, fecha, str(falla), _estado_daemon(), abiertos,
        eventos, logs, ventana_horas=DEGRADADO_ABIERTOS_H)
    asunto = (f"[Sysop IA][{ctx.cfg.installation}][CRITICAL] informe "
              f"{ahora_local.strftime('%Y-%m-%d')} — ALMACEN NO DISPONIBLE")

    msg = smtp.armar(cfg.cuenta, cfg.destinatarios, asunto, html)
    envio = None
    for intento in range(3):
        envio = smtp.enviar(cfg.cuenta, msg)
        if envio.ok or "autenticacion" in envio.error.lower():
            break
        log.warning("fallo el envio del informe degradado (intento %d): %s",
                    intento + 1, envio.error)
        time.sleep(20)
    if envio is None or not envio.ok:
        registro.anotar("correo_fallido", tipo=TIPO, periodo=periodo,
                        asunto=asunto, error=envio.error if envio else "")
        return {"estado": "fallido", "periodo": periodo, "degradado": True,
                "error": envio.error if envio else ""}
    outbox.anotar_envio(TIPO, periodo, asunto, "directo")
    log.info("informe degradado %s enviado", periodo)
    return {"estado": "enviado", "periodo": periodo, "degradado": True,
            "falla": str(falla), "activos": len(abiertos)}


def _estado_daemon() -> str:
    """Qué se sabe del daemon sin la base: el latido y lo que dice systemd."""
    partes = []
    edad = registro.edad_latido()
    if edad is None:
        partes.append("sin archivo de latido")
    elif edad <= SILENCIO_MAX_SEG:
        partes.append(f"vivo (ultimo latido hace {int(edad)} s)")
    else:
        partes.append(f"sin señales hace {int(edad // 60)} minutos")
    try:
        r = subprocess.run(["systemctl", "is-active", "sysop-agent"],
                           capture_output=True, text=True, timeout=5)
        partes.append(f"systemd: {(r.stdout or r.stderr).strip() or '?'}")
    except (OSError, subprocess.SubprocessError):
        pass
    return "; ".join(partes)


def _solo_en_registro(conn: Any) -> list[dict[str, Any]]:
    """Lo que el registro da por abierto y la base no conoce.

    Pasa cuando la base se recreo: el arnes del 2026-09-14 mando un informe
    `[OK]` con cero activos justo despues, con un critico caido, porque el
    incidente todavia no habia vuelto a juntar muestras en la base nueva. Con
    la base de siempre no agrega nada: todo lo que se abre y cierra se anota en
    los dos lados.
    """
    try:
        en_base = {incidents.fp_corto(f["incident_id"]) for f in conn.execute(
            "SELECT incident_id FROM incident WHERE closed_at IS NULL")}
    except Exception:  # noqa: BLE001
        return []
    desde = datetime.now(timezone.utc) - timedelta(hours=DEGRADADO_ABIERTOS_H)
    out = []
    for i in registro.abiertos_segun_registro(desde):
        # Un recuperado sin cierre en el registro es uno que se perdio con la
        # base en su ventana de quince minutos: no es un activo.
        if i.get("fp") and i["fp"] not in en_base and i.get("state") != "RECOVERED":
            causa = dict(i.get("cause") or {})
            causa["reason_code"] = (f"{causa.get('reason_code') or ''} "
                                    f"(segun el registro en archivo)").strip()
            # Marcado: no esta en la base, asi que no se revalida.
            out.append({**i, "cause": causa, "_solo_registro": True})
    return out


def _ahora_local(tz_nombre: str) -> datetime:
    try:
        from zoneinfo import ZoneInfo               # noqa: PLC0415
        return datetime.now(ZoneInfo(tz_nombre))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def _observador_caido(conn: Any) -> Optional[str]:
    """¿Hace cuánto que el daemon no da señales?

    Es la pregunta que sólo puede contestar alguien que no sea el daemon.

    Primero el LATIDO en archivo, que el daemon toca en cada tick aunque la base
    no ande. Deducirlo del último resultado guardado mentía con la base borrada:
    el informe veía una base vacía y decía "caído" con el daemon vivo. El último
    resultado queda solo para una instalación que todavía no tiene latido.
    """
    edad = registro.edad_latido()
    if edad is not None and edad > SILENCIO_MAX_SEG:
        return f"El daemon no da señales hace {int(edad // 60)} minutos."
    if conn is None:
        return None if edad is not None else "No hay latido del daemon ni almacen para mirar."
    fila = conn.execute(
        "SELECT MAX(observed_at) ultimo FROM check_result").fetchone()
    ultimo = fila["ultimo"] if fila else None
    if not ultimo:
        # Con latido fresco es una base recien creada, no un daemon caido.
        return None if edad is not None else "No hay ni un resultado registrado."
    try:
        t = datetime.fromisoformat(ultimo)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    seg = (datetime.now(timezone.utc) - t).total_seconds()
    if seg <= SILENCIO_MAX_SEG:
        return None
    if edad is not None:
        # Vivo pero sin guardar: el latido solo no alcanza para decir que anda
        # (revision: 5 ticks con error, cero resultados, y el informe callaba).
        return (f"El daemon esta vivo pero no guarda resultados hace "
                f"{int(seg // 60)} minutos ({ultimo[:19].replace('T', ' ')} UTC).")
    return (f"El ultimo control corrio hace {int(seg // 60)} minutos "
            f"({ultimo[:19].replace('T', ' ')} UTC).")


def resumen_controles(conn: Any, planificados: int) -> dict[str, int]:
    """Último resultado de cada control, agrupado por estado.

    Se toma el ULTIMO por par (control, recurso) y no todos los del día: si no,
    un control que estuvo mal una hora y se arregló seguiría contando como
    problema en el informe de la mañana.
    """
    filas = conn.execute(
        "SELECT state, COUNT(*) n FROM ("
        "  SELECT check_id, resource_id, state,"
        "         ROW_NUMBER() OVER (PARTITION BY check_id, resource_id"
        "                            ORDER BY observed_at DESC, id DESC) rn"
        "  FROM check_result WHERE observed_at >= ?"
        ") WHERE rn = 1 GROUP BY state", (db.corte(days=1),)).fetchall()
    cnt = {f["state"]: f["n"] for f in filas}
    problema = sum(v for k, v in cnt.items()
                   if k in ("FAILED", "DEGRADED", "MISSING", "LATE", "STUCK"))
    medidos = sum(cnt.values())
    return {
        "ok": cnt.get("OK", 0) + cnt.get("DISABLED_EXPECTED", 0),
        "unknown": cnt.get("UNKNOWN", 0),
        # Controles que existen pero no produjeron ni un resultado. No es lo
        # mismo que UNKNOWN: acá ni siquiera se intentó.
        "sin_datos": max(0, planificados - medidos),
        "problema": problema,
    }


def costo_modelo(conn: Any) -> dict[str, Any]:
    """Cuánto salió el diagnóstico en las últimas 24 horas.

    Va en el informe como una línea. Un gasto que no se ve es un gasto que
    nadie controla, y el observador no puede ser el que genera el problema.
    """
    f = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(costo_usd), 0) c FROM llm_call "
        "WHERE ok = 1 AND at >= ?", (db.corte(days=1),)).fetchone()
    return {"llamadas": int(f["n"] or 0), "costo_usd": float(f["c"] or 0.0)}


def huecos_24h(conn: Any) -> list[str]:
    filas = conn.execute(
        "SELECT observed_at FROM check_result "
        "WHERE check_id='HST-01' AND reason_code='AGENTE_INTERRUMPIDO' "
        "  AND observed_at >= ? "
        "ORDER BY observed_at", (db.corte(days=1),)).fetchall()
    return [f["observed_at"][:19].replace("T", " ") for f in filas]
