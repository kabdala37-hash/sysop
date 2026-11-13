"""Las herramientas que necesitan el almacen del observador.

Las de sistema (listar, leer, telemetria, journal) ya viven en
`sysop/llm/herramientas.py` y sirven igual aca: el agente que conversa por
Telegram es el mismo que diagnostica, y mira el mismo servidor. Este modulo
suma las que necesitan la base local, que aquellas no pueden tener porque no
reciben la conexion.

La que justifica el modulo es `informes`. El pedido era poder leer el correo que
acaba de llegar y seguir la conversacion desde ahi, y resulta que el cuerpo ya
estaba guardado: `outbox.body_html` tiene el HTML de cada informe enviado, con
90 dias de retencion. No hizo falta guardar nada nuevo, solo darle la llave.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("sysop.bot.herramientas")

def _corte(**delta: float) -> str:
    """El umbral de una ventana temporal, en el MISMO formato que la columna.

    No se usa `datetime('now', '-6 hours')` de SQLite, y no es un capricho: las
    fechas se guardan en ISO con T y offset (2026-09-02T20:10:31+00:00) y esa
    funcion devuelve "2026-09-02 20:15:54", con un espacio. SQLite compara eso
    como TEXTO, y 'T' (0x54) es mayor que ' ' (0x20), asi que toda fila del
    mismo dia pasa el filtro sin importar la hora.

    Medido en srv3: una ventana de 2 minutos devolvia 85.655 filas donde habia
    185. No falla ruidosamente -- devuelve de mas y parece que funciona.
    """
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


# Cuanto texto de un informe se le pasa al modelo. Un informe de un dia malo son
# varias pantallas; mandar tres enteros llena la ventana de contexto con HTML
# viejo y deja afuera la pregunta.
MAX_INFORME = 6000


def informes(limite: int = 3, buscar: Optional[str] = None) -> dict[str, Any]:
    """Los ultimos informes que se mandaron por correo, en texto."""
    raise NotImplementedError   # se liga en `ligar()`


def estado() -> dict[str, Any]:
    """Que ve el observador ahora: incidentes abiertos."""
    raise NotImplementedError


def incidente(incident_id: str) -> dict[str, Any]:
    """El detalle de un incidente, con su diagnostico si lo tiene."""
    raise NotImplementedError


# ---------------------------------------------------------------------------
# Implementacion, ligada a una conexion
# ---------------------------------------------------------------------------

def ligar(conn: Any,
          definiciones: Optional[dict[str, Any]] = None,
          quien: Optional[str] = None,
          app: Any = None,
          cfg_llm: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Devuelve los ejecutores atados a esta conexion.

    Se hace asi y no con un global porque el bot es un proceso aparte con su
    propia conexion, y un global convierte un olvido en un `None` a mitad de una
    respuesta al usuario.

    `definiciones` son los controles DECLARADOS (titulo, cada cuanto, severidad,
    histeresis). Sin eso, el agente solo ve mediciones y tiene que deducir de los
    timestamps cada cuanto corre cada control -- que fue exactamente lo que hizo
    cuando se lo preguntaron, inventando frecuencias plausibles.
    """
    definiciones = definiciones or {}
    # De quien es la decision cuando se clasifica algo. Se guarda con la
    # clasificacion: un silencio sin autor es una decision que dentro de seis
    # meses nadie puede revisar.
    autor = "telegram:%s" % (quien or "?")

    def _informes(limite: int = 3, buscar: Optional[str] = None) -> dict[str, Any]:
        limite = max(1, min(int(limite or 3), 10))
        filas = conn.execute(
            "SELECT id, report_type, subject, sent_at, created_at, state, "
            "       body_html, recipients_json "
            "FROM outbox ORDER BY COALESCE(sent_at, created_at) DESC LIMIT ?",
            (limite,)).fetchall()
        out = []
        for f in filas:
            texto = _a_texto(f["body_html"] or "")
            if buscar and buscar.lower() not in texto.lower():
                continue
            out.append({
                "id": f["id"],
                "tipo": f["report_type"],
                "asunto": f["subject"],
                "cuando": f["sent_at"] or f["created_at"],
                "estado": f["state"],
                "destinatarios": _json(f["recipients_json"]),
                "texto": texto[:MAX_INFORME],
                "recortado": len(texto) > MAX_INFORME,
            })
        if not out:
            return {"informes": [],
                    "nota": "no hay informes guardados que coincidan"}
        return {"informes": out}

    def _estado() -> dict[str, Any]:
        filas = conn.execute(
            "SELECT i.incident_id, i.check_id, i.resource_id, i.severity, "
            "       i.state, i.opened_at, i.last_seen, i.cause_json, "
            "       d.causa, d.accion, d.es_real "
            "FROM incident i LEFT JOIN diagnostico d "
            "  ON d.incident_id = i.incident_id "
            "WHERE i.closed_at IS NULL "
            "ORDER BY CASE i.severity WHEN 'critical' THEN 0 WHEN 'major' "
            "         THEN 1 WHEN 'warning' THEN 2 ELSE 3 END, i.opened_at",
            ()).fetchall()
        abiertos = [{
            "id": f["incident_id"], "control": f["check_id"],
            "recurso": f["resource_id"], "severidad": f["severity"],
            "estado": f["state"], "desde": f["opened_at"],
            "visto_por_ultima_vez": f["last_seen"],
            "causa_del_modelo": f["causa"],
            "accion_sugerida": f["accion"],
            "diagnostico_de_cuando": "de cuando se abrio: puede estar viejo",
        } for f in filas]
        for a in abiertos[:25]:
            a.update(_revisiones(a["id"]))
        return {"abiertos": len(abiertos), "incidentes": abiertos[:25]}

    def _revisiones(incident_id: str) -> dict[str, Any]:
        """Lo que se reviso despues de abrir: la revalidacion y la marca."""
        from sysop import revalidacion as reval_mod           # noqa: PLC0415
        out: dict[str, Any] = {}
        r = reval_mod.de(conn, incident_id)
        if r:
            out["revalidacion"] = {
                "cuando": r["at"], "veredicto": r["veredicto"],
                "motivo": r["motivo"], "causa": r["causa"],
                "nota": r["nota"], "confirma_en": r["confirma_en"],
                "citas_verificadas": [c for c in r["evidencia"]
                                      if c.get("verificada")][:3]}
        m = reval_mod.marca(conn, incident_id)
        if m:
            out["marcado_arreglado"] = {
                "quien": m["quien"], "cuando": m["at"], "motivo": m["motivo"],
                "confirma_en": m["confirma_en"]}
        return out

    def _marcar_arreglado(incident_id: str, motivo: str,
                          horas: Optional[float] = None) -> dict[str, Any]:
        from sysop import revalidacion as reval_mod           # noqa: PLC0415
        return reval_mod.marcar(conn, app, str(incident_id), str(motivo or ""),
                                autor, horas=horas, cfg_llm=cfg_llm)

    def _desmarcar_arreglado(incident_id: str) -> dict[str, Any]:
        from sysop import revalidacion as reval_mod           # noqa: PLC0415
        ok = reval_mod.desmarcar(conn, str(incident_id), autor)
        return {"hecho": ok,
                "nota": ("vuelve a clasificarse como antes: lo decide la "
                         "revalidacion del proximo informe" if ok else
                         "ese incidente no tenia una marca vigente")}

    def _incidente(incident_id: str) -> dict[str, Any]:
        f = conn.execute(
            "SELECT i.*, d.causa, d.accion, d.es_real, d.confianza, d.descarte "
            "FROM incident i LEFT JOIN diagnostico d "
            "  ON d.incident_id = i.incident_id WHERE i.incident_id = ?",
            (incident_id,)).fetchone()
        if f is None:
            return {"error": f"no existe el incidente {incident_id!r}"}
        eventos = conn.execute(
            "SELECT at, kind, payload_json FROM incident_event "
            "WHERE incident_id = ? ORDER BY at DESC LIMIT 12",
            (incident_id,)).fetchall()
        return {
            "id": f["incident_id"], "control": f["check_id"],
            "recurso": f["resource_id"], "severidad": f["severity"],
            "estado": f["state"], "desde": f["opened_at"],
            "recuperado": f["recovered_at"], "cerrado": f["closed_at"],
            "evidencia": _json(f["cause_json"]),
            "diagnostico": {"causa": f["causa"], "accion": f["accion"],
                            "es_real": bool(f["es_real"]) if f["es_real"] is not None else None,
                            "confianza": f["confianza"], "descarte": f["descarte"],
                            "ojo": "es de cuando se abrio: puede estar viejo"},
            **_revisiones(f["incident_id"]),
            "eventos": [{"cuando": e["at"], "que": e["kind"],
                         "detalle": _json(e["payload_json"])} for e in eventos],
        }

    def _controles(check_id: Optional[str] = None,
                   horas: int = 6) -> dict[str, Any]:
        """Que esta midiendo el observador, y como le fue.

        Existe porque sin esto el agente no puede responder nada sobre SI MISMO.
        Preguntado por cuantos controles corren, se iba a mirar /etc/cron.d --
        que son los trabajos de la PLATAFORMA, no sus propios controles -- y
        contestaba con lo que hubiera en el ultimo informe.
        """
        horas = max(1, min(int(horas or 6), 168))
        desde = _corte(hours=horas)
        if check_id:
            filas = conn.execute(
                "SELECT resource_id, observed_at, state, severity, reason_code, "
                "       actual_json, expected_json, evidence_quality "
                "FROM check_result WHERE check_id = ? "
                "  AND observed_at >= ? "
                "ORDER BY observed_at DESC LIMIT 40",
                (check_id, desde)).fetchall()
            if not filas:
                return {"error": f"el control {check_id!r} no midio nada en las "
                                 f"ultimas {horas} horas. Puede no existir: "
                                 f"pedi la lista sin `check_id`."}
            return {"control": check_id,
                    "definicion": definiciones.get(check_id,
                                                   "no declarado en este host"),
                    "horas": horas,
                    "mediciones": [{"recurso": f["resource_id"],
                                    "cuando": f["observed_at"],
                                    "estado": f["state"],
                                    "severidad": f["severity"],
                                    "motivo": f["reason_code"],
                                    "medido": _json(f["actual_json"]),
                                    "esperado": _json(f["expected_json"]),
                                    "calidad": f["evidence_quality"]}
                                   for f in filas]}

        filas = conn.execute(
            "SELECT check_id, COUNT(DISTINCT resource_id) recursos, "
            "       SUM(state='OK') ok, SUM(state='PROBLEM') problema, "
            "       SUM(state='UNKNOWN') unknown, COUNT(*) mediciones, "
            "       MAX(observed_at) ultima "
            "FROM check_result WHERE observed_at >= ? "
            "GROUP BY check_id ORDER BY unknown DESC, problema DESC, check_id",
            (desde,)).fetchall()
        out = []
        for f in filas:
            d = definiciones.get(f["check_id"]) or {}
            out.append({"id": f["check_id"],
                        "que_mide": d.get("titulo"),
                        "cada_seg": d.get("cada_seg"),
                        "severidad_si_falla": d.get("severidad"),
                        "recursos": f["recursos"], "ok": f["ok"],
                        "problema": f["problema"],
                        "sin_poder_medir": f["unknown"],
                        "mediciones": f["mediciones"], "ultima": f["ultima"]})
        # Un control declarado que no midio NADA en la ventana no aparece en la
        # tabla de resultados, y es justo el caso que hay que ver: no es que
        # este sano, es que no corrio.
        sin_datos = sorted(set(definiciones) - {f["check_id"] for f in filas})
        # Dos numeros y no uno, porque no siempre coinciden y la diferencia
        # dice algo. Los controles por trabajo se planifican a partir de la
        # lista de trabajos, asi que si la base no respondia cuando el bot
        # arranco, quedan medidos pero sin definicion. Un solo numero
        # inventaria una precision que no hay.
        return {"horas": horas,
                "midieron_en_la_ventana": len(filas),
                "declarados_al_arrancar": len(definiciones),
                "nota_si_diferen": ("los controles por trabajo se declaran "
                                    "segun los trabajos que haya; si aparecen "
                                    "medidos sin `que_mide`, es que se "
                                    "declararon despues de arrancar el chat"),
                "controles": out,
                "declarados_sin_ninguna_medicion": sin_datos}

    def _gasto(dias: int = 1) -> dict[str, Any]:
        """Lo que costo el modelo. El dato exacto, no el del ultimo informe."""
        dias = max(1, min(int(dias or 1), 90))
        filas = conn.execute(
            "SELECT date(at) dia, "
            "       SUM(CASE WHEN incident_id LIKE 'telegram:%' THEN 0 ELSE 1 END) diagnosticos, "
            "       SUM(CASE WHEN incident_id LIKE 'telegram:%' THEN 1 ELSE 0 END) conversaciones, "
            "       ROUND(SUM(costo_usd), 4) usd, "
            "       SUM(tokens_in) tokens_in, SUM(tokens_out) tokens_out, "
            "       SUM(ok = 0) fallidas "
            "FROM llm_call WHERE at >= ? "
            "GROUP BY date(at) ORDER BY dia DESC",
            (_corte(days=dias),)).fetchall()
        return {"dias": dias,
                "nota": "el gasto del chat y el de los diagnosticos tienen "
                        "presupuestos separados",
                "por_dia": [dict(f) for f in filas]}

    def _clasificar(recurso: str, veredicto: str, motivo: str,
                    dias: int = 30) -> dict[str, Any]:
        from sysop import settings                            # noqa: PLC0415
        from sysop.core import inventory                      # noqa: PLC0415
        mapa = {"ignorar": "IGNORED", "ignorado": "IGNORED",
                "es_nuestro": "MANAGED", "nuestro": "MANAGED", "gestionado": "MANAGED",
                "externo": "EXTERNAL", "de_otro_sistema": "EXTERNAL"}
        estado = mapa.get(str(veredicto or "").strip().lower(),
                          str(veredicto or "").upper())
        try:
            hecho = inventory.clasificar(conn, str(recurso), settings.HOST_ID,
                                         estado, motivo, autor, dias)
        except inventory.ClasificacionInvalida as e:
            return {"hecho": False, "problema": str(e)}
        hecho["hecho"] = True
        hecho["nota"] = ("queda guardado hasta esa fecha. Solo deja de avisar lo "
                         "que es WARNING: un problema mayor o critico sobre este "
                         "recurso va a seguir avisando igual")
        return hecho

    def _clasificaciones(incluir_vencidas: bool = False) -> dict[str, Any]:
        from sysop import settings                            # noqa: PLC0415
        from sysop.core import inventory                      # noqa: PLC0415
        filas = inventory.clasificaciones(conn, settings.HOST_ID,
                                          bool(incluir_vencidas))
        return {"cuantas": len(filas), "clasificaciones": filas}

    def _desclasificar(recurso: str) -> dict[str, Any]:
        from sysop import settings                            # noqa: PLC0415
        from sysop.core import inventory                      # noqa: PLC0415
        ok = inventory.desclasificar(conn, str(recurso), settings.HOST_ID, autor)
        return {"hecho": ok,
                "nota": ("vuelve a la lista de sin clasificar y puede volver a "
                         "avisar" if ok else
                         "no habia ninguna clasificacion vigente para ese recurso")}

    def _recursos(buscar: Optional[str] = None, limite: int = 25) -> dict[str, Any]:
        """Para poder clasificar hace falta saber como se llama cada cosa."""
        from sysop import settings                            # noqa: PLC0415
        limite = max(1, min(int(limite or 25), 60))
        sql = ("SELECT resource_id, kind, state, first_seen FROM resource "
               "WHERE host_id = ?")
        params: list[Any] = [settings.HOST_ID]
        if buscar:
            sql += " AND resource_id LIKE ?"
            params.append("%%%s%%" % str(buscar))
        sql += " ORDER BY state, resource_id LIMIT ?"
        params.append(limite)
        filas = [dict(f) for f in conn.execute(sql, tuple(params)).fetchall()]
        return {"cuantos": len(filas), "recursos": filas}

    return {"informes": _informes, "estado": _estado, "incidente": _incidente,
            "clasificar": _clasificar, "clasificaciones": _clasificaciones,
            "desclasificar": _desclasificar, "recursos": _recursos,
            "controles": _controles, "gasto": _gasto,
            "marcar_arreglado": _marcar_arreglado,
            "desmarcar_arreglado": _desmarcar_arreglado}


# ---------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_ESPACIOS = re.compile(r"[ \t]+")
_SALTOS = re.compile(r"\n{3,}")


def _a_texto(html: str) -> str:
    """El informe es HTML para el correo; el modelo lo quiere leer como texto.

    No se usa un parser: el HTML lo genera `notify/render.py`, es conocido y no
    tiene entradas de terceros. Lo unico que importa es que `</tr>` y `</p>`
    dejen un salto, porque sin eso una tabla de incidentes se convierte en un
    renglon corrido donde no se distingue una fila de la siguiente.
    """
    t = re.sub(r"(?i)</(tr|p|div|h[1-6]|li)>", "\n", html)
    t = re.sub(r"(?i)</t[dh]>", " | ", t)
    t = _TAG.sub("", t)
    for ent, car in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                     ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        t = t.replace(ent, car)
    t = _ESPACIOS.sub(" ", t)
    t = "\n".join(l.strip(" |") for l in t.split("\n"))
    return _SALTOS.sub("\n\n", t).strip()


def _json(s: Any) -> Any:
    try:
        return json.loads(s) if s else {}
    except (TypeError, ValueError):
        return s


DECLARACIONES = [
    {
        "name": "controles",
        "description": ("QUE ESTA MIDIENDO EL OBSERVADOR y como le fue. Sin "
                        "`check_id` devuelve todos los controles con su cuenta "
                        "de ok / problema / sin-poder-medir; con `check_id` "
                        "devuelve las mediciones de ese control, con lo "
                        "esperado y lo medido. Usala para cualquier pregunta "
                        "sobre el propio observador: cuantos controles hay, "
                        "que mide cada uno, CADA CUANTO CORRE, con que "
                        "severidad abre y cual esta ciego. Los datos vienen "
                        "declarados: no deduzcas la frecuencia de los "
                        "timestamps. NO son los trabajos de la plataforma ni "
                        "estan en /etc/cron.d."),
        "parameters": {
            "type": "object",
            "properties": {
                "check_id": {"type": "string",
                             "description": "opcional, por ejemplo HST-09 o DB-01"},
                "horas": {"type": "integer",
                          "description": "ventana hacia atras, 1 a 168. Por defecto 6"},
            },
        },
    },
    {
        "name": "gasto",
        "description": ("Lo que costo el modelo, por dia, separando los "
                        "diagnosticos de las conversaciones. Es el dato exacto "
                        "de la base: para responder cuanto se gasto, usa esto "
                        "y NO lo que diga un informe, que es una foto vieja."),
        "parameters": {
            "type": "object",
            "properties": {"dias": {"type": "integer",
                                    "description": "cuantos dias hacia atras, 1 a 90"}},
        },
    },
    {
        "name": "informes",
        "description": ("Los ultimos informes que el observador mando por "
                        "correo, en texto. USALO PRIMERO cuando te pregunten "
                        "por algo que 'llego por mail' o 'dice el informe': es "
                        "el mismo contenido que recibio quien te escribe."),
        "parameters": {
            "type": "object",
            "properties": {
                "limite": {"type": "integer",
                           "description": "cuantos traer, 1 a 10. Por defecto 3"},
                "buscar": {"type": "string",
                           "description": "opcional: solo los que contengan este texto"},
            },
        },
    },
    {
        "name": "estado",
        "description": ("Los incidentes abiertos AHORA, con su severidad y el "
                        "diagnostico del modelo si ya lo tienen. Es la foto del "
                        "momento, no la del ultimo informe."),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "incidente",
        "description": ("El detalle de UN incidente: su evidencia medida, su "
                        "diagnostico y su linea temporal. El id sale de "
                        "`estado` o del propio informe."),
        "parameters": {
            "type": "object",
            "properties": {"incident_id": {"type": "string"}},
            "required": ["incident_id"],
        },
    },
]

# ---------------------------------------------------------------------------
# Las de ESCRIBIR. Las cuatro de arriba solo leen; estas cambian como se
# comporta el observador, asi que llevan sus propias reglas y todas dejan rastro
# de quien las uso.
# ---------------------------------------------------------------------------
DECLARACIONES += [
    {
        "name": "recursos",
        "description": ("QUE COSAS VE el observador en este servidor, con su "
                        "nombre exacto y si estan clasificadas o no. Usala "
                        "ANTES de clasificar algo, para saber como se llama de "
                        "verdad: los nombres son del estilo "
                        "`systemd:nginx.service` o `job:clientea:12`. `buscar` "
                        "filtra por texto."),
        "parameters": {
            "type": "object",
            "properties": {
                "buscar": {"type": "string",
                           "description": "opcional, parte del nombre"},
                "limite": {"type": "integer", "description": "hasta 60"},
            },
        },
    },
    {
        "name": "clasificar",
        "description": ("DECIR QUE ES una cosa para que el observador deje de "
                        "contarla como sospechosa. Usala cuando la persona diga "
                        "algo como 'ese job es nuestro' o 'ese servicio es de "
                        "otro sistema'.\n"
                        "REGLAS, decilas si hacen falta:\n"
                        "- El motivo es OBLIGATORIO y tiene que explicar por "
                        "que. Si no te lo dieron, preguntalo.\n"
                        "- Vence si o si, maximo 30 dias. Un silencio sin fecha "
                        "es un control borrado. Si no dijeron cuantos, usa 30.\n"
                        "- Solo silencia lo WARNING. Un problema MAYOR o CRITICO "
                        "sobre esa misma cosa va a seguir avisando, y eso NO se "
                        "puede cambiar por chat. Aclaraselo.\n"
                        "- Si el nombre del recurso no es exacto, buscalo antes "
                        "con `recursos`. No lo inventes."),
        "parameters": {
            "type": "object",
            "properties": {
                "recurso": {"type": "string",
                            "description": "el nombre exacto, ej systemd:cron.service"},
                "veredicto": {"type": "string",
                              "enum": ["ignorar", "es_nuestro", "externo"],
                              "description": ("ignorar = no me interesa; "
                                              "es_nuestro = lo esperamos aca; "
                                              "externo = es de otro sistema")},
                "motivo": {"type": "string",
                           "description": "por que. Obligatorio."},
                "dias": {"type": "integer",
                         "description": "de 1 a 30. Por defecto 30."},
            },
            "required": ["recurso", "veredicto", "motivo"],
        },
    },
    {
        "name": "clasificaciones",
        "description": ("QUE ESTA SILENCIADO a mano ahora mismo, con el motivo, "
                        "quien lo puso y hasta cuando. Es lo que hace revisable "
                        "cualquier silencio."),
        "parameters": {
            "type": "object",
            "properties": {
                "incluir_vencidas": {
                    "type": "boolean",
                    "description": "tambien las que ya vencieron"},
            },
        },
    },
    {
        "name": "marcar_arreglado",
        "description": ("DECIR QUE UN INCIDENTE YA SE ARREGLO y falta que el "
                        "control lo confirme (tipicamente: se corrigio algo y "
                        "el trabajo recien corre el domingo). Usala cuando la "
                        "persona diga 'eso ya lo arregle'.\n"
                        "REGLAS, decilas si hacen falta:\n"
                        "- El id sale de `estado`. No lo inventes.\n"
                        "- El motivo es OBLIGATORIO: que se arreglo y como.\n"
                        "- Sale de la lista principal del informe solo HASTA "
                        "que se pueda confirmar: si es un trabajo, su proxima "
                        "corrida; si no, lo que diga `horas` o un dia. Si para "
                        "entonces sigue abierto, vuelve a la lista.\n"
                        "- No cierra nada ni silencia nada nuevo. Un control "
                        "critico sigue en la lista igual.\n"
                        "- El agente lo comprueba igual en cada informe.\n"
                        "Decile la fecha de confirmacion que devuelve."),
        "parameters": {
            "type": "object",
            "properties": {
                "incident_id": {"type": "string",
                                "description": "el id exacto, ej INC-a1b2c3d4-20260913T0700"},
                "motivo": {"type": "string",
                           "description": "que se arreglo. Obligatorio."},
                "horas": {"type": "number",
                          "description": ("opcional: en cuantas horas se puede "
                                          "confirmar, 1 a 192. Sin esto se "
                                          "calcula solo")},
            },
            "required": ["incident_id", "motivo"],
        },
    },
    {
        "name": "desmarcar_arreglado",
        "description": ("DESHACER `marcar_arreglado`: el incidente vuelve a "
                        "clasificarse como antes."),
        "parameters": {
            "type": "object",
            "properties": {"incident_id": {"type": "string"}},
            "required": ["incident_id"],
        },
    },
    {
        "name": "desclasificar",
        "description": ("DESHACER una clasificacion: el recurso vuelve a la "
                        "lista de sin clasificar y puede volver a avisar. Usala "
                        "cuando digan que algo ya no corresponde o que fue un "
                        "error."),
        "parameters": {
            "type": "object",
            "properties": {
                "recurso": {"type": "string", "description": "el nombre exacto"},
            },
            "required": ["recurso"],
        },
    },
]
