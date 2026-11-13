"""Armado de los correos.

Regla que atraviesa todo el módulo: **`OK`, `UNKNOWN` y "sin datos" son tres
cosas distintas y se muestran distinto.** Un informe que pinta de verde lo que
en realidad no se pudo medir es peor que no mandar nada, porque genera confianza
sin respaldo.

HTML simple y con estilos en línea a propósito: los clientes de correo ignoran
las hojas de estilo, y este correo se lee tanto en un escritorio como en un
teléfono a las tres de la mañana.
"""
from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Optional, Sequence

COLOR = {
    "critical": "#b3261e",
    "major": "#e8710a",
    "warning": "#f9ab00",
    "info": "#5f6368",
    "ok": "#188038",
    "unknown": "#7b1fa2",   # distinto del gris: UNKNOWN no es "sin importancia"
}

_ESTILO_TABLA = ("width:100%;border-collapse:collapse;"
                 "font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px")
_TD = "padding:8px 10px;border-bottom:1px solid #e0e0e0;vertical-align:top"


def _e(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def _edad(desde: str, hasta: Optional[datetime] = None) -> str:
    try:
        t0 = datetime.fromisoformat(desde)
    except (ValueError, TypeError):
        return "?"
    t1 = hasta or datetime.now(t0.tzinfo)
    seg = int((t1 - t0).total_seconds())
    if seg < 60:
        return f"{seg}s"
    if seg < 3600:
        return f"{seg // 60}m"
    if seg < 86400:
        return f"{seg // 3600}h {(seg % 3600) // 60}m"
    return f"{seg // 86400}d {(seg % 86400) // 3600}h"


def _badge(texto: str, color: str) -> str:
    return (f'<span style="background:{color};color:#fff;padding:2px 8px;'
            f'border-radius:3px;font-size:12px;font-weight:600">{_e(texto)}</span>')


# ---------------------------------------------------------------------------
# Asuntos
# ---------------------------------------------------------------------------

def asunto_informe(instalacion: str, incidentes: Sequence[dict[str, Any]],
                   fecha: str) -> str:
    if not incidentes:
        return f"[Sysop IA][{instalacion}][OK] informe {fecha}"
    from sysop.core.models import Severity
    peor = max(incidentes, key=lambda i: Severity(i["severity"]).orden)["severity"]
    return (f"[Sysop IA][{instalacion}][{peor.upper()}] informe {fecha} "
            f"— {len(incidentes)} activo{'s' if len(incidentes) != 1 else ''}")


def asunto_urgente(instalacion: str, inc: dict[str, Any]) -> str:
    return (f"[Sysop IA][{instalacion}][URGENTE {inc['severity'].upper()}] "
            f"{inc['check_id']} en {inc['resource_id']}")


# ---------------------------------------------------------------------------
# Bloques
# ---------------------------------------------------------------------------


def _tabla_silenciados(filas: Sequence[dict[str, Any]]) -> str:
    """Lo silenciado a mano, con su por que y su vencimiento.

    Lleva las cuatro cosas que hacen revisable una decision: que se callo, quien
    lo decidio, con que argumento y hasta cuando. Sin el autor y el motivo, en
    seis meses nadie puede discutirlo.
    """
    out = ['<table style="border-collapse:collapse;font-size:13px;width:100%">',
           '<tr style="background:#f1f3f4">'
           '<th style="text-align:left;padding:6px">Recurso</th>'
           '<th style="text-align:left;padding:6px">Que es</th>'
           '<th style="text-align:left;padding:6px">Motivo</th>'
           '<th style="text-align:left;padding:6px">Quien</th>'
           '<th style="text-align:left;padding:6px">Hasta</th></tr>']
    for f in filas:
        c = f.get("clasificacion") or {}
        vence = str(c.get("vence_at") or "")[:10]
        out.append(
            '<tr>'
            f'<td style="padding:6px;border-top:1px solid #e8eaed">{_e(f.get("resource_id"))}</td>'
            f'<td style="padding:6px;border-top:1px solid #e8eaed">{_e(c.get("estado"))}</td>'
            f'<td style="padding:6px;border-top:1px solid #e8eaed">{_e(c.get("motivo"))}</td>'
            f'<td style="padding:6px;border-top:1px solid #e8eaed">{_e(c.get("quien"))}</td>'
            f'<td style="padding:6px;border-top:1px solid #e8eaed">{_e(vence)}</td>'
            '</tr>')
    out.append("</table>")
    return "".join(out)

def _tabla_incidentes(incidentes: Sequence[dict[str, Any]],
                      etiqueta_fija: Optional[str] = None) -> str:
    if not incidentes:
        return '<p style="color:#188038;margin:8px 0">Sin incidentes activos.</p>'

    filas = []
    for i in incidentes:
        c = i.get("cause") or {}
        sev = i["severity"]
        recuperado = i.get("state") == "RECOVERED"
        etiqueta = "RECUPERADO" if recuperado else sev.upper()
        color = COLOR["ok"] if recuperado else COLOR.get(sev, COLOR["info"])
        if etiqueta_fija:
            etiqueta, color = etiqueta_fija, COLOR["info"]
        elif i.get("sin_validar") and not recuperado:
            etiqueta = "SIN VALIDAR"

        detalle = []
        if i.get("severidad_medida"):
            # El asunto no lo cuenta como major, pero lo medido no se esconde.
            detalle.append(f"medido como {_e(str(i['severidad_medida']).upper())}")
        if c.get("expected"):
            detalle.append(f"esperado {_e(c['expected'])}")
        if c.get("actual"):
            detalle.append(f"real {_e(c['actual'])}")
        # La calidad de evidencia se muestra sólo cuando NO es completa: si
        # apareciera siempre, se volvería ruido y dejaría de leerse justo cuando
        # importa.
        eq = c.get("evidence_quality")
        if eq and eq != "FULL":
            detalle.append(f'<span style="color:{COLOR["unknown"]}">'
                           f'evidencia {_e(eq)}</span>')

        filas.append(
            f'<tr>'
            f'<td style="{_TD};white-space:nowrap">{_badge(etiqueta, color)}</td>'
            f'<td style="{_TD}"><b>{_e(i["resource_id"])}</b><br>'
            f'<span style="color:#5f6368">{_e(c.get("reason_code", ""))} '
            f'· {_e(i["check_id"])}</span></td>'
            f'<td style="{_TD};white-space:nowrap">{_e(_edad(i["opened_at"]))}</td>'
            f'<td style="{_TD};color:#3c4043">{" · ".join(detalle)}</td>'
            f'</tr>')

        diag = _fila_diagnostico(i)
        if diag:
            filas.append(f'<tr><td style="{_TD}"></td>'
                         f'<td style="{_TD}" colspan="3">{diag}</td></tr>')

    return (f'<table style="{_ESTILO_TABLA}">'
            f'<tr style="text-align:left;color:#5f6368;font-size:12px">'
            f'<th style="{_TD}">Estado</th><th style="{_TD}">Recurso</th>'
            f'<th style="{_TD}">Desde</th><th style="{_TD}">Detalle</th></tr>'
            + "".join(filas) + '</table>')


_VEREDICTO = {"real": "sigue pasando",
             "falsa_alarma": "falsa alarma",
             "arreglado_pendiente": "arreglado, falta confirmar",
             "no_se_pudo_validar": "no se pudo validar"}


def _fila_diagnostico(i: dict[str, Any]) -> str:
    """El diagnóstico del modelo, si lo hay, debajo de la fila del incidente.

    Si hubo revalidacion, manda ella: es de ahora. El diagnostico de apertura
    puede tener dias y ya no se muestra, que era justamente el problema.
    """
    r = i.get("revalidacion")
    if r:
        return _fila_revalidacion(r, i.get("nota"))
    if i.get("nota"):
        return (f'<div style="font-size:12px;color:#5f6368;padding:4px 0 0 2px">'
                f'<i>{_e(i["nota"])}</i></div>')
    d = i.get("diagnostico")
    if not d or not d.get("causa"):
        return ""
    txt = f"{_e(d['causa'])}"
    if d.get("accion"):
        txt += f"<br><b>Sugerido:</b> {_e(d['accion'])}"
    if not d.get("es_real"):
        txt += (f"<br><i>El modelo lo da por falso positivo "
                f"({_e(d.get('confianza', ''))} confianza): "
                f"{_e(d.get('descarte', ''))}</i>")
    return (f'<div style="font-size:12px;color:#5f6368;padding:4px 0 0 2px">'
            f'<b>Diagnostico:</b> {txt}</div>')


def _fila_revalidacion(r: dict[str, Any], nota: Optional[str] = None) -> str:
    v = r.get("veredicto") or ""
    cab = f"<b>Revisado para este informe:</b> {_e(_VEREDICTO.get(v, v))}"
    if v != "no_se_pudo_validar" and r.get("confianza"):
        cab += f" ({_e(r['confianza'])} confianza)"
    partes = [cab]
    if r.get("causa"):
        partes.append(_e(r["causa"]))
    if r.get("motivo") and r.get("motivo") != r.get("causa"):
        partes.append(f"<b>Por que:</b> {_e(r['motivo'])}")
    if r.get("accion"):
        partes.append(f"<b>Sugerido:</b> {_e(r['accion'])}")
    # Solo las citas que el codigo encontro en lo que el agente miro. Van en el
    # correo para que cualquiera pueda comprobar de donde sale la conclusion.
    for c in [c for c in (r.get("evidencia") or []) if c.get("verificada")][:2]:
        partes.append(f'<span style="font-family:monospace;font-size:11px">'
                      f'{_e(c.get("fuente", ""))}: {_e(str(c.get("cita", ""))[:240])}'
                      f'</span>')
    for n in (r.get("nota"), nota):
        if n:
            partes.append(f"<i>{_e(n)}</i>")
    return (f'<div style="font-size:12px;color:#5f6368;padding:4px 0 0 2px">'
            + "<br>".join(partes) + '</div>')


def _tabla_pendientes(filas: Sequence[dict[str, Any]]) -> str:
    """Lo que se dio por arreglado y todavia no se pudo confirmar."""
    out = []
    for i in filas:
        p = i.get("pendiente") or {}
        c = i.get("cause") or {}
        quien = ("el agente" if p.get("origen") == "agente"
                 else str(p.get("origen") or "?"))
        detalle = _e(p.get("motivo") or "")
        r = i.get("revalidacion")
        if r and p.get("origen") != "agente":
            # Lo marco una persona: se muestra tambien que vio el agente.
            detalle += "<br>" + _fila_revalidacion(r)
        elif r:
            for cita in [x for x in (r.get("evidencia") or [])
                         if x.get("verificada")][:2]:
                detalle += (f'<br><span style="font-family:monospace;font-size:11px;'
                            f'color:#5f6368">{_e(cita.get("fuente", ""))}: '
                            f'{_e(str(cita.get("cita", ""))[:240])}</span>')
        out.append(
            f'<tr><td style="{_TD}"><b>{_e(i["resource_id"])}</b><br>'
            f'<span style="color:#5f6368">{_e(c.get("reason_code", ""))} '
            f'· {_e(i["check_id"])}</span></td>'
            f'<td style="{_TD}">{detalle}</td>'
            f'<td style="{_TD};white-space:nowrap">{_e(quien)}</td>'
            f'<td style="{_TD};white-space:nowrap">'
            f'{_e(_utc(p.get("confirma_en")))}</td></tr>')
    return (f'<table style="{_ESTILO_TABLA}">'
            f'<tr style="text-align:left;color:#5f6368;font-size:12px">'
            f'<th style="{_TD}">Recurso</th><th style="{_TD}">Que se arreglo</th>'
            f'<th style="{_TD}">Quien</th><th style="{_TD}">Se confirma despues de</th>'
            f'</tr>' + "".join(out) + '</table>')


def _utc(v: Any) -> str:
    try:
        d = datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return str(v or "?")
    return d.strftime("%Y-%m-%d %H:%M UTC")


def _tabla_resumen(resumen: dict[str, int]) -> str:
    """OK, UNKNOWN y sin datos, cada uno con su color."""
    orden = [("ok", "Correctos", COLOR["ok"]),
             ("unknown", "Sin poder medir", COLOR["unknown"]),
             ("sin_datos", "Sin datos todavia", "#9aa0a6"),
             ("problema", "Con problema", COLOR["major"])]
    celdas = []
    for k, etiqueta, color in orden:
        v = resumen.get(k, 0)
        celdas.append(
            f'<td style="padding:12px;text-align:center;border:1px solid #e0e0e0">'
            f'<div style="font-size:26px;font-weight:600;color:{color}">{v}</div>'
            f'<div style="font-size:12px;color:#5f6368">{etiqueta}</div></td>')
    return (f'<table style="{_ESTILO_TABLA};margin:12px 0"><tr>'
            + "".join(celdas) + '</tr></table>')


def _aviso_config(origen: str, desactualizados: Sequence[str]) -> str:
    """Dos avisos distintos, y no hay que confundirlos.

    El primero es una degradación EN CURSO: la base no respondía y este correo
    salió con lo que decía el archivo de respaldo. El segundo es preventivo: la
    base respondió bien, pero el archivo que la reemplazaría quedó viejo. Ese
    hay que arreglarlo ahora, que no cuesta nada, y no el día que haga falta.
    """
    partes = []

    if origen == "respaldo":
        partes.append(
            f'<p style="background:#fef7e0;border-left:4px solid {COLOR["warning"]};'
            f'padding:10px;margin:12px 0;font-size:13px">'
            f'La configuracion de notificacion se leyo del <b>archivo de '
            f'respaldo</b>: la base de negocio no respondia. Los destinatarios '
            f'pueden estar desactualizados.</p>')

    if desactualizados:
        items = "".join(f"<li>{_e(d)}</li>" for d in desactualizados)
        partes.append(
            f'<p style="background:#e8f0fe;border-left:4px solid #1a73e8;'
            f'padding:10px;margin:12px 0;font-size:13px">'
            f'El <b>archivo de respaldo</b> (<code>config/base.yaml</code>) no '
            f'coincide con lo que dice la base. Hoy no molesta, porque manda la '
            f'base; el dia que la base no responda se va a usar el archivo tal '
            f'como esta:<ul style="margin:6px 0 0 18px;padding:0">{items}</ul></p>')

    return "".join(partes)


def _aviso_fallidos(fallidos: Sequence[dict[str, Any]]) -> str:
    if not fallidos:
        return ""
    items = "".join(f'<li>{_e(f["report_type"])} del {_e(f["period_start"])} '
                    f'({_e(f["attempts"])} intentos): {_e(f["last_error"])[:120]}</li>'
                    for f in fallidos)
    return (f'<p style="background:#fce8e6;border-left:4px solid {COLOR["critical"]};'
            f'padding:10px;margin:12px 0;font-size:13px">'
            f'<b>Correos que no se pudieron entregar:</b><ul>{items}</ul></p>')


# ---------------------------------------------------------------------------
# Correos completos
# ---------------------------------------------------------------------------

def _bloque_periodo(p: dict[str, Any]) -> str:
    """Qué pasó desde el informe anterior.

    Va ANTES del estado actual a propósito: lo que pasó explica lo que hay, y no
    al revés. Un informe que sólo muestra el ahora deja invisible el servicio
    que se cayó a las tres de la mañana y volvió solo.
    """
    inc = p.get("incidentes") or {}
    act = p.get("actividad") or {}
    trans = p.get("transitorios") or []
    ceg = p.get("ceguera") or []

    abiertos = len(inc.get("abiertos_en_el_periodo") or [])
    cerrados = len(inc.get("cerrados_en_el_periodo") or [])
    vivos = len(inc.get("siguen_abiertos") or [])
    dur = inc.get("duracion_minutos") or []

    horas = p.get("horas", 0)
    partes = [f'<h3>Que paso en las ultimas {_e(horas)} horas</h3>']

    linea = (f"{abiertos} incidente{'s' if abiertos != 1 else ''} nuevo"
             f"{'s' if abiertos != 1 else ''}, {cerrados} resuelto"
             f"{'s' if cerrados != 1 else ''}, {vivos} sin resolver.")
    if dur:
        linea += (f" El mas largo de los resueltos duro "
                  f"{_e(round(max(dur)))} minutos.")
    partes.append(f'<p style="font-size:13px;margin:4px 0 12px">{linea}</p>')

    if trans:
        total = sum(int(t.get("muestras") or 0) for t in trans)
        partes.append(
            f'<p style="font-size:13px;margin:12px 0 4px"><b>{len(trans)} '
            f'problema{"s" if len(trans) != 1 else ""} transitorio'
            f'{"s" if len(trans) != 1 else ""}</b> ({total} muestra'
            f'{"s" if total != 1 else ""}): fallaron y se recuperaron sin llegar '
            f'a abrir incidente. No generaron aviso; estan aca porque repetirse '
            f'es un dato.</p>')
        filas = "".join(
            f'<tr><td style="{_TD}">{_e(t["check_id"])}</td>'
            f'<td style="{_TD}">{_e(t["resource_id"])}</td>'
            f'<td style="{_TD}">{_e(t.get("reason_code") or "")}</td>'
            f'<td style="{_TD};text-align:right">{_e(t["muestras"])}</td>'
            f'<td style="{_TD};white-space:nowrap">{_e(str(t.get("ultima") or "")[:16].replace("T", " "))}</td></tr>'
            for t in trans[:12])
        partes.append(
            f'<table style="{_ESTILO_TABLA}">'
            f'<tr style="text-align:left;color:#5f6368;font-size:12px">'
            f'<th style="{_TD}">Control</th><th style="{_TD}">Recurso</th>'
            f'<th style="{_TD}">Motivo</th><th style="{_TD}">Veces</th>'
            f'<th style="{_TD}">Ultima</th></tr>{filas}</table>')

    if ceg:
        partes.append(
            f'<p style="font-size:13px;margin:12px 0 4px"><b>Lo que no se pudo '
            f'mirar.</b> Estos controles quedaron sin evidencia durante el '
            f'periodo. No es que esten bien: es que no se supo.</p>')
        filas = "".join(
            f'<tr><td style="{_TD}">{_e(c["check_id"])}</td>'
            f'<td style="{_TD}">{_e(c.get("reason_code") or "")}</td>'
            f'<td style="{_TD};text-align:right">{_e(c["muestras"])}</td></tr>'
            for c in ceg[:8])
        partes.append(
            f'<table style="{_ESTILO_TABLA}">'
            f'<tr style="text-align:left;color:#5f6368;font-size:12px">'
            f'<th style="{_TD}">Control</th><th style="{_TD}">Motivo</th>'
            f'<th style="{_TD}">Muestras</th></tr>{filas}</table>')

    for l in (p.get("logs") or [])[:6]:
        if l.get("error"):
            continue
        partes.append(
            f'<p style="font-size:13px;margin:12px 0 4px"><b>Errores en '
            f'{_e(l["ruta"])}</b>: {_e(l["eventos_relevantes"])} evento'
            f'{"s" if l["eventos_relevantes"] != 1 else ""}, '
            f'{_e(l["grupos_distintos"])} distinto'
            f'{"s" if l["grupos_distintos"] != 1 else ""}.</p>')
        for g in l.get("grupos", [])[:3]:
            partes.append(
                f'<div style="font-family:monospace;font-size:11px;'
                f'background:#f8f9fa;border-left:3px solid {COLOR["major"]};'
                f'padding:8px;margin:4px 0;white-space:pre-wrap;'
                f'overflow-x:auto">'
                f'<b>x{_e(g["veces"])}</b>  {_e(g["nivel"])}  '
                f'{_e(g.get("ultima_vez") or "")}\n{_e(g["ejemplo"][:600])}</div>')
        # Lo que el umbral por patron dejo afuera. Va SIEMPRE que haya algo: un
        # filtro invisible es el que termina escondiendo lo que hacia falta ver.
        om = l.get("omitidos")
        if om:
            partes.append(
                f'<p style="font-size:12px;color:#5f6368;margin:2px 0 8px">'
                f'{_e(om["eventos"])} evento{"s" if om["eventos"] != 1 else ""} '
                f'en {_e(om["grupos"])} grupo{"s" if om["grupos"] != 1 else ""} '
                f'no se listan: son errores que el propio flujo reintenta y se '
                f'mantuvieron por debajo de su umbral. Se reportan cuando lo '
                f'superan.</p>')

    if act.get("resultados"):
        partes.append(
            f'<p style="font-size:12px;color:#5f6368;margin-top:8px">'
            f'Se hicieron {_e(act["resultados"])} mediciones sobre '
            f'{_e(act.get("controles", 0))} controles y '
            f'{_e(act.get("recursos", 0))} recursos.</p>')
    return "".join(partes)


def _envoltorio(titulo: str, subtitulo: str, cuerpo: str) -> str:
    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;'
        f'max-width:820px;margin:0 auto;color:#202124">'
        f'<h2 style="margin:0 0 4px">{_e(titulo)}</h2>'
        f'<p style="color:#5f6368;margin:0 0 16px;font-size:13px">{_e(subtitulo)}</p>'
        f'{cuerpo}'
        f'<p style="color:#9aa0a6;font-size:11px;margin-top:24px;'
        f'border-top:1px solid #e0e0e0;padding-top:8px">'
        f'Sysop IA. Este correo se genera solo; no hace falta responderlo.</p>'
        f'</div>')


def informe_periodico(instalacion: str, fecha: str,
                   incidentes: Sequence[dict[str, Any]],
                   resumen: dict[str, int],
                   recuperados_24h: Sequence[dict[str, Any]] = (),
                   sin_clasificar: int = 0,
                   huecos: Sequence[str] = (),
                   origen_config: str = "vivo",
                   desactualizados: Sequence[str] = (),
                   fallidos: Sequence[dict[str, Any]] = (),
                   observador_caido: Optional[str] = None,
                   descartados: Sequence[dict[str, Any]] = (),
                   falsas: Sequence[dict[str, Any]] = (),
                   pendientes: Sequence[dict[str, Any]] = (),
                   silenciados: Sequence[dict[str, Any]] = (),
                   costo_modelo: Optional[dict[str, Any]] = None,
                   periodo: Optional[dict[str, Any]] = None,
                   lectura: Optional[Any] = None) -> str:
    partes = [_aviso_config(origen_config, desactualizados),
              _aviso_fallidos(fallidos)]

    if observador_caido:
        # Este informe lo dispara cron, no el daemon. Esa es exactamente la
        # razon: si el daemon esta muerto, el correo sale igual y lo dice. Un
        # informe que dijera "todo OK" con el observador caido seria mentira.
        partes.append(
            f'<p style="background:#fce8e6;border-left:4px solid {COLOR["critical"]};'
            f'padding:10px;font-size:13px"><b>El observador no esta corriendo.</b> '
            f'{_e(observador_caido)} Lo que sigue es el ultimo estado conocido, '
            f'no el estado actual.</p>')

    if huecos:
        # El observador estuvo caído: hay que decirlo, porque en ese rato no vio
        # nada y el "todo OK" de este informe no cubre esas horas.
        partes.append(
            f'<p style="background:#fef7e0;border-left:4px solid {COLOR["warning"]};'
            f'padding:10px;font-size:13px"><b>El observador estuvo interrumpido:</b> '
            f'{_e(", ".join(huecos))}. No hay observaciones de ese periodo.</p>')

    if lectura is not None:
        # Arriba de todo: es la lectura del conjunto, y lo que sigue es el
        # detalle que la sostiene.
        cuerpo = _e(lectura.resumen or lectura.causa)
        if lectura.accion:
            cuerpo += f'<br><br><b>Que conviene hacer:</b> {_e(lectura.accion)}'
        partes.append(
            f'<div style="background:#e8f0fe;border-left:4px solid #1a73e8;'
            f'padding:12px;margin:8px 0 16px;font-size:14px;line-height:1.5">'
            f'{cuerpo}</div>')

    if periodo:
        partes.append(_bloque_periodo(periodo))

    partes.append("<h3>Estado de los controles, ahora</h3>")
    partes.append(_tabla_resumen(resumen))

    partes.append("<h3>Incidentes activos</h3>")
    partes.append(_tabla_incidentes(incidentes))
    if any(i.get("sin_validar") for i in incidentes):
        partes.append(
            '<p style="font-size:12px;color:#5f6368">SIN VALIDAR: el control lo '
            'marco pero no se pudo revisar para este informe. Se muestra igual; '
            'no sube la severidad del asunto por encima de WARNING.</p>')

    if recuperados_24h:
        partes.append("<h3>Recuperados en las ultimas 24 horas</h3>")
        partes.append(_tabla_incidentes(recuperados_24h))

    if pendientes:
        partes.append("<h3>Arreglados, falta confirmar</h3>")
        partes.append(
            '<p style="font-size:13px;color:#5f6368">La causa ya no esta, pero '
            'el control todavia no lo pudo comprobar: falta que el trabajo '
            'vuelva a correr. Siguen abiertos y se siguen midiendo. Si despues '
            'de la fecha siguen abiertos, vuelven a la lista de arriba.</p>')
        partes.append(_tabla_pendientes(pendientes))

    if falsas:
        partes.append("<h3>Falsas alarmas</h3>")
        partes.append(
            '<p style="font-size:13px;color:#5f6368">El control las marco, pero '
            'revisadas para este informe no son un problema. Siguen abiertas y '
            'se siguen midiendo; abajo de cada una, por que y de donde sale.</p>')
        partes.append(_tabla_incidentes(falsas, etiqueta_fija="FALSA ALARMA"))

    if descartados:
        # Se muestran, no se esconden. El modelo opina; que se haya equivocado
        # tiene que poder verse sin ir a buscar al log.
        partes.append("<h3>Descartados por el diagnostico</h3>")
        partes.append(
            '<p style="font-size:13px;color:#5f6368">Siguen abiertos y se siguen '
            'midiendo. No cuentan como problema activo porque el modelo los dio '
            'por falso positivo con confianza alta.</p>')
        partes.append(_tabla_incidentes(descartados))

    if silenciados:
        # Un silencio que no se ve es un control borrado. Este bloque es la
        # contrapartida de poder silenciar por chat: cada correo dice que esta
        # callado, por que, quien lo decidio y hasta cuando.
        partes.append("<h3>Silenciados a mano</h3>")
        partes.append(
            '<p style="font-size:13px;color:#5f6368">Siguen abiertos y se siguen '
            'midiendo. No cuentan como problema activo porque alguien los '
            'clasifico. El silencio vence solo, y solo alcanza a lo WARNING: un '
            'problema mayor o critico sobre estos mismos recursos avisa igual.</p>')
        partes.append(_tabla_silenciados(silenciados))

    if sin_clasificar:
        partes.append(
            f'<p style="font-size:13px;color:#5f6368">'
            f'{sin_clasificar} recurso{"s" if sin_clasificar != 1 else ""} '
            f'sin clasificar. No generan alerta; esperan que alguien defina '
            f'que son.</p>')

    if costo_modelo and costo_modelo.get("llamadas"):
        partes.append(
            f'<p style="font-size:12px;color:#5f6368;margin-top:16px">'
            f'Diagnostico: {costo_modelo["llamadas"]} llamada'
            f'{"s" if costo_modelo["llamadas"] != 1 else ""} al modelo en 24 h, '
            f'{costo_modelo["costo_usd"]:.4f} USD.</p>')

    sub = "Ultimas 24 horas"
    return _envoltorio(f"Informe de {instalacion} — {fecha}", sub,
                       "".join(partes))


def alerta_urgente(instalacion: str, inc: dict[str, Any],
                   contexto: Sequence[dict[str, Any]] = (),
                   diagnostico: Optional[str] = None) -> str:
    """Un problema critico, ahora. Un incidente por correo, sin agrupar.

    Agrupar seria lo correcto para el informe y lo incorrecto para esto: dos
    caidas distintas a la misma hora son dos cosas que atender, y meterlas en
    un solo correo hace que la segunda se lea como detalle de la primera.
    """
    partes = [_tabla_incidentes([inc])]

    if diagnostico:
        partes.append(
            '<h3>Diagnostico</h3>'
            f'<div style="background:#f1f3f4;padding:12px;font-size:13px;'
            f'white-space:pre-wrap">{_e(diagnostico)}</div>')

    if contexto:
        partes.append("<h3>Que mas estaba pasando al mismo tiempo</h3>")
        partes.append(_tabla_incidentes(contexto))

    return _envoltorio(
        f"{inc['check_id']} en {instalacion}",
        f"Abierto {_edad(inc['opened_at'])} atras. Severidad {inc['severity']}.",
        "".join(partes))


def aviso_almacen(instalacion: str, titulo: str, texto: str,
                  abiertos: Sequence[dict[str, Any]] = ()) -> str:
    """El propio almacén del observador: falló, sigue roto, volvió, se apartó.

    Sale por el camino de emergencia, sin bandeja: tiene que poder armarse sin
    leer una sola fila de la base.
    """
    partes = [f'<p style="background:#fce8e6;border-left:4px solid {COLOR["critical"]};'
              f'padding:10px;font-size:13px">{_e(texto)}</p>']
    if abiertos:
        partes.append("<h3>Criticos avisados mientras tanto</h3>")
        partes.append(_tabla_incidentes(abiertos))
    return _envoltorio(f"Almacen del observador — {instalacion}", titulo,
                       "".join(partes))


def informe_degradado(instalacion: str, fecha: str, falla: str,
                      daemon: str, abiertos: Sequence[dict[str, Any]],
                      eventos: Sequence[dict[str, Any]],
                      logs: Sequence[dict[str, Any]] = (),
                      ventana_horas: float = 24.0) -> str:
    """El informe cuando el almacén no abre. Sale igual, con lo que hay.

    Antes, con la base corrupta, bloqueada o sin permiso, el informe de cron
    moría en el primer SELECT y no llegaba nada, que es indistinguible de un
    "todo bien". Lo que sigue sale del registro en archivo, no de la base: por
    eso lo dice arriba de todo.
    """
    partes = [
        f'<p style="background:#fce8e6;border-left:4px solid {COLOR["critical"]};'
        f'padding:10px;font-size:13px"><b>El almacen del observador no esta '
        f'disponible:</b> {_e(falla)}<br>Este informe sale del registro en '
        f'archivo, no de la base: no hay estado de los controles ni '
        f'diagnostico del modelo.</p>',
        f'<p style="font-size:13px"><b>Daemon:</b> {_e(daemon)}</p>',
        f"<h3>Incidentes abiertos segun el registro (ultimas "
        f"{ventana_horas:.0f} h)</h3>",
        _tabla_incidentes(abiertos),
    ]
    if eventos:
        filas = "".join(
            f'<tr><td style="{_TD};white-space:nowrap">'
            f'{_e(str(e.get("at", ""))[:19].replace("T", " "))}</td>'
            f'<td style="{_TD}">{_e(e.get("evento", ""))}</td>'
            f'<td style="{_TD}">{_e(e.get("clase") or e.get("asunto") or e.get("mensaje") or "")}</td></tr>'
            for e in eventos[-30:])
        partes.append("<h3>Lo que paso con el almacen y los avisos</h3>")
        partes.append(f'<table style="{_ESTILO_TABLA}">{filas}</table>')
    for l in logs:
        if l.get("error"):
            partes.append(f'<p style="font-size:13px">{_e(l["ruta"])}: {_e(l["error"])}</p>')
            continue
        partes.append(f'<h3>Errores en {_e(l["ruta"])}: {_e(l.get("eventos_relevantes", 0))} '
                      f'evento(s)</h3>')
        for g in (l.get("grupos") or [])[:4]:
            partes.append(f'<pre style="background:#f1f3f4;padding:8px;font-size:12px;'
                          f'white-space:pre-wrap">x{_e(g.get("veces"))} '
                          f'{_e(g.get("nivel"))}\n{_e(str(g.get("ejemplo", ""))[:1500])}</pre>')
    return _envoltorio(f"Informe de {instalacion} — {fecha}",
                       "Informe degradado: almacen no disponible", "".join(partes))

