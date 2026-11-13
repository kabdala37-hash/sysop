"""El canal de Telegram: quien puede hablarle, que lee y como contesta.

Lo que se verifica, en orden de importancia:

  1. LA LISTA BLANCA. Es el unico control que separa un agente con acceso de
     lectura a produccion de cualquiera que sepa el nombre del bot. Si esto
     falla, no importa nada de lo demas.
  2. Que el agente pueda leer el informe que se mando por correo, que es lo que
     hace que la conversacion tenga contexto en vez de empezar de cero.
  3. Que el historial se pode sin romper el hilo de herramientas.
  4. Que el presupuesto del chat no se coma el de los diagnosticos.

No se toca la API de Telegram ni el modelo: los dos se reemplazan por dobles.
"""
import logging
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sysop.bot import ajustes as ajustes_mod
from sysop.bot import conversacion as conv_mod
from sysop.bot import daemon as bot_daemon
from sysop.bot import herramientas as herr_bot
from sysop.bot import telegram as tg
from sysop.llm import gateway
from sysop.store import db


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    # bool() y no `c`: varias comprobaciones son expresiones con `and`, que
    # devuelven el operando y no True/False. Sin esto, el conteo final suma un
    # str con un int y el archivo entero falla despues de pasar todo.
    return bool(c)


def almacen():
    """Un almacen de prueba. Devuelve tambien la RUTA, que el bot necesita.

    El bot abre una conexion por hilo, asi que no alcanza con pasarle una
    conexion: hay que decirle donde esta la base. Sin esto, los hilos abren la
    real y el test escribe en el almacen del servidor.
    """
    d = Path(tempfile.mkdtemp())
    ruta = d / "t.db"
    c = db.connect(ruta); db.init_schema(c)
    return c, d, ruta


AHORA = datetime.now(timezone.utc).isoformat()

INFORME_HTML = """<html><body>
<h2>Informe de srv2</h2>
<p>La configuracion se leyo del archivo de respaldo.</p>
<table><tr><td>HST-09</td><td>disco</td><td>critical</td></tr>
<tr><td>DB-01</td><td>mysql</td><td>major</td></tr></table>
</body></html>"""


class TelegramFalso:
    """Un doble de la API: registra lo que se mando, no manda nada."""

    def __init__(self, updates=None):
        self.updates = updates or []
        self.enviados = []      # (chat_id, texto)
        self.escribiendo_a = []

    def recibir(self, offset, espera_seg=30):
        u, self.updates = self.updates, []
        return u

    def mandar(self, chat_id, texto):
        self.enviados.append((chat_id, texto))

    def escribiendo(self, chat_id):
        self.escribiendo_a.append(chat_id)

    def quien_soy(self):
        return {"username": "falso_bot"}


def mensaje(chat_id, texto, uid=1):
    return {"update_id": uid,
            "message": {"text": texto, "chat": {"id": chat_id},
                        "from": {"username": "alguien"}}}


class CtxFalso:
    def __init__(self, conn):
        self.conn = conn
        self.cfg = type("C", (), {"llm": {}})()

    def fuentes(self):
        return type("F", (), {"agente": None})()


def main():
    r = []
    tmp = []

    # ------------------------------------------------------------------
    # 1. La lista blanca
    # ------------------------------------------------------------------
    conn, d, ruta = almacen(); tmp.append(d)
    aj = ajustes_mod.Ajustes(habilitado=True, token="x",
                             autorizados=frozenset({111}),
                             nombres={111: "duenio"}, prompt="sos el bot")
    # Construir el Bot no puede tirar NADA al log de errores. Hasta el
    # 2026-09-08 el `__init__` traia una copia del bloque que carga los
    # controles declarados, que usaba un `ctx` inexistente en ese scope: cada
    # arranque escribia un `NameError: name 'ctx' is not defined` que el
    # `except` se tragaba. El bloque bueno es el de `arrancar()`, donde `ctx` si
    # existe; el del `__init__` era una copia muerta.
    _errores: list = []
    _h = logging.Handler()
    _h.setLevel(logging.ERROR)
    _h.emit = lambda rec: _errores.append(rec.getMessage())  # type: ignore[method-assign]
    logging.getLogger("sysop.bot").addHandler(_h)
    bot = bot_daemon.Bot(ruta_db=ruta)
    logging.getLogger("sysop.bot").removeHandler(_h)
    r.append(ok("construir el Bot no escribe ningun error", _errores == [], _errores))
    r.append(ok("y arranca con los controles declarados vacios, listos para arrancar()",
                bot._defs == {}, bot._defs))

    api = TelegramFalso([mensaje(999, "hola", uid=1)])
    bot._una_vuelta(api, aj, CtxFalso(conn)); bot.despachador.esperar(10)
    r.append(ok("a un chat NO autorizado no se le contesta NADA",
                api.enviados == [], api.enviados))
    r.append(ok("y el offset avanza igual, para no reprocesarlo eternamente",
                bot._offset == 2, bot._offset))

    api = TelegramFalso([mensaje(111, "/ayuda", uid=5)])
    bot._una_vuelta(api, aj, CtxFalso(conn)); bot.despachador.esperar(10)
    r.append(ok("a un chat autorizado si se le contesta", len(api.enviados) == 1))
    r.append(ok("la ayuda nombra la instalacion",
                api.enviados and "observador" in api.enviados[0][1].lower()))

    # Un comando que no gasta modelo tiene que andar aunque no haya agente.
    api = TelegramFalso([mensaje(111, "/estado", uid=6)])
    bot._una_vuelta(api, aj, CtxFalso(conn)); bot.despachador.esperar(10)
    r.append(ok("/estado contesta sin incidentes y sin gastar modelo",
                api.enviados and "No hay incidentes" in api.enviados[0][1],
                api.enviados))

    # ------------------------------------------------------------------
    # 2. Leer el informe que se mando por correo
    # ------------------------------------------------------------------
    conn.execute(
        "INSERT INTO outbox (report_type, period_start, state, recipients_json, "
        "subject, body_html, created_at, sent_at) VALUES (?,?,?,?,?,?,?,?)",
        ("informe", AHORA, "SENT", '["a@b.com"]', "Informe srv2",
         INFORME_HTML, AHORA, AHORA))
    ej = herr_bot.ligar(conn)

    res = ej["informes"](limite=3)
    r.append(ok("encuentra el informe que se mando", len(res["informes"]) == 1))
    txt = res["informes"][0]["texto"]
    r.append(ok("el HTML llega al modelo como texto, sin etiquetas",
                "<" not in txt and "respaldo" in txt, txt[:80]))
    r.append(ok("las filas de la tabla no quedan pegadas en un renglon",
                "HST-09" in txt and "DB-01" in txt
                and txt.index("HST-09") < txt.index("DB-01")))
    r.append(ok("se puede filtrar por texto",
                len(ej["informes"](buscar="HST-09")["informes"]) == 1))
    r.append(ok("y un filtro que no matchea no devuelve nada",
                ej["informes"](buscar="no-existe-esto")["informes"] == []))

    api = TelegramFalso([mensaje(111, "/informe", uid=7)])
    bot._una_vuelta(api, aj, CtxFalso(conn)); bot.despachador.esperar(10)
    r.append(ok("/informe manda el ultimo, con su asunto",
                api.enviados and "Informe srv2" in api.enviados[0][1]))

    # ------------------------------------------------------------------
    # 3. Estado e incidentes
    # ------------------------------------------------------------------
    conn.execute(
        "INSERT INTO incident (incident_id, fingerprint, resource_id, check_id, "
        "opened_at, state, severity, cause_json) VALUES (?,?,?,?,?,?,?,?)",
        ("inc-1", "fp1", "host:srv2", "HST-09", AHORA, "OPEN", "critical",
         '{"libre_pct": 3}'))
    est = ej["estado"]()
    r.append(ok("estado ve el incidente abierto", est["abiertos"] == 1, est))
    det = ej["incidente"]("inc-1")
    r.append(ok("el detalle trae la evidencia medida",
                det["evidencia"] == {"libre_pct": 3}, det.get("evidencia")))
    r.append(ok("un incidente que no existe da error, no explota",
                "error" in ej["incidente"]("no-existe")))

    # ------------------------------------------------------------------
    # 4. El historial
    # ------------------------------------------------------------------
    convs = conv_mod.Conversaciones()
    h, _ = convs.de(42)
    for i in range(conv_mod.MAX_TURNOS + 5):
        h.append({"role": "user", "parts": [{"text": f"p{i}"}]})
        h.append({"role": "model", "parts": [{"text": f"r{i}"}]})
    convs.podar(42)
    h2, _ = convs.de(42)
    turnos = [c for c in h2 if c.get("role") == "user"]
    r.append(ok("el historial se poda al tope de turnos",
                len(turnos) <= conv_mod.MAX_TURNOS, len(turnos)))
    r.append(ok("y empieza en un turno del usuario, no en medio de uno",
                h2[0].get("role") == "user"))

    # La poda no puede partir un intercambio de herramientas: un
    # functionResponse sin su functionCall hace que Gemini devuelva 400.
    convs2 = conv_mod.Conversaciones()
    h3, _ = convs2.de(43)
    for i in range(conv_mod.MAX_TURNOS + 3):
        h3.append({"role": "user", "parts": [{"text": f"p{i}"}]})
        h3.append({"role": "model", "parts": [{"functionCall": {"name": "estado"}}]})
        h3.append({"role": "user", "parts": [{"functionResponse": {"name": "estado"}}]})
        h3.append({"role": "model", "parts": [{"text": f"r{i}"}]})
    convs2.podar(43)
    h4, _ = convs2.de(43)
    llamadas = sum(1 for c in h4 for p in c.get("parts", []) if "functionCall" in p)
    respuestas = sum(1 for c in h4 for p in c.get("parts", []) if "functionResponse" in p)
    r.append(ok("la poda nunca deja una respuesta de herramienta huerfana",
                llamadas == respuestas, f"{llamadas} llamadas, {respuestas} respuestas"))

    convs.olvidar(42)
    r.append(ok("/reset deja la conversacion vacia", convs.de(42)[0] == []))

    # ------------------------------------------------------------------
    # 5. Presupuestos separados
    # ------------------------------------------------------------------
    conn2, d2, _ = almacen(); tmp.append(d2)
    for inc, costo in (("inc-1", 1.50), (f"{gateway.PREFIJO_CHAT}111", 0.75)):
        conn2.execute(
            "INSERT INTO llm_call (at, incident_id, modelo, costo_usd, ok) "
            "VALUES (?,?,?,?,1)", (AHORA, inc, "m", costo))
    r.append(ok("el total sigue siendo el total",
                abs(gateway.gastado_hoy(conn2) - 2.25) < 1e-9))
    r.append(ok("el diagnostico NO cuenta lo que gasto el chat",
                abs(gateway.gastado_hoy(conn2, "diagnostico") - 1.50) < 1e-9,
                gateway.gastado_hoy(conn2, "diagnostico")))
    r.append(ok("y el chat NO cuenta lo que gastaron los diagnosticos",
                abs(gateway.gastado_hoy(conn2, "chat") - 0.75) < 1e-9))

    # Con el tope alcanzado contesta igual, diciendo por que.
    dicho = conv_mod.responder(
        conn=conn2, agente=type("A", (), {"utilizable": True, "vendor": "gemini",
                                          "modelo": "m", "prompt": "p",
                                          "api_key": "k"})(),
        cfg_llm={}, historial=[], pregunta="hola", ejecutores={},
        declaraciones=[], chat_id=111, tope_usd=0.5)
    r.append(ok("con el presupuesto agotado avisa en vez de quedarse mudo",
                "tope de gasto" in dicho, dicho[:80]))
    r.append(ok("y aclara que los diagnosticos siguen andando",
                "presupuesto aparte" in dicho, dicho[:120]))

    # ------------------------------------------------------------------
    # 6. Partir mensajes largos
    # ------------------------------------------------------------------
    partes = tg._partir("x" * 10000)
    r.append(ok("un mensaje largo se parte", len(partes) > 1))
    r.append(ok("y ninguna parte pasa el limite de Telegram",
                all(len(p) <= 4096 for p in partes),
                max(len(p) for p in partes)))
    r.append(ok("un mensaje corto no se toca", tg._partir("hola") == ["hola"]))
    lineas = tg._partir("\n".join(f"linea {i}" for i in range(1200)))
    r.append(ok("al partir por lineas no se pierde ningun renglon",
                sum(p.count("linea ") for p in lineas) == 1200))

    # ------------------------------------------------------------------
    # 7. Los ajustes
    # ------------------------------------------------------------------
    import os
    dcfg = Path(tempfile.mkdtemp()); tmp.append(dcfg)
    (dcfg / "prompts").mkdir()
    (dcfg / "prompts" / "t.md").write_text("prompt del chat", encoding="utf-8")

    def escribir(txt):
        (dcfg / "telegram.yaml").write_text(txt, encoding="utf-8")

    os.environ.pop("TEST_TG_TOKEN", None)
    escribir("habilitado: true\ntoken_env: TEST_TG_TOKEN\nautorizados: [111]\n")
    a = ajustes_mod.cargar(dcfg)
    r.append(ok("sin el token en el entorno, dice exactamente que falta",
                a.problema and "TEST_TG_TOKEN" in a.problema, a.problema))

    os.environ["TEST_TG_TOKEN"] = "un-token"
    escribir("habilitado: true\ntoken_env: TEST_TG_TOKEN\n"
             "prompt: prompts/t.md\n"
             "autorizados:\n  - id: 0\n    nombre: ejemplo\n  - id: 222\n")
    a = ajustes_mod.cargar(dcfg)
    r.append(ok("el id 0 del archivo de ejemplo se ignora",
                a.autorizados == frozenset({222}), a.autorizados))
    r.append(ok("el prompt se lee del archivo relativo",
                a.prompt == "prompt del chat", a.prompt))
    r.append(ok("con todo bien, queda utilizable", a.utilizable))

    escribir("habilitado: false\ntoken_env: TEST_TG_TOKEN\n")
    r.append(ok("deshabilitado no es utilizable", not ajustes_mod.cargar(dcfg).utilizable))

    escribir("habilitado: true\ntoken_env: TEST_TG_TOKEN\n"
             "prompt: prompts/no-esta.md\n")
    a = ajustes_mod.cargar(dcfg)
    r.append(ok("un prompt declarado que no existe se reporta, no se ignora",
                a.problema and "no existe" in a.problema, a.problema))

    a = ajustes_mod.cargar(dcfg / "no-hay-nada")
    r.append(ok("sin archivo de telegram no rompe: queda apagado",
                not a.utilizable and a.problema))


    # ------------------------------------------------------------------
    # 8. El peso del historial
    # ------------------------------------------------------------------
    # Con un tope propio y chico: asi la prueba mide el MECANISMO y no el
    # valor por defecto, que es configurable y va a cambiar.
    convs3 = conv_mod.Conversaciones(max_chars=20000)
    r.append(ok("los topes se pueden configurar por conversacion",
                convs3.max_chars == 20000 and convs3.max_turnos == conv_mod.MAX_TURNOS))
    h5, _ = convs3.de(50)
    # Un turno donde el agente leyo algo grande: es lo que de verdad pesa.
    for i in range(6):
        h5.append({"role": "user", "parts": [{"text": f"pregunta {i}"}]})
        h5.append({"role": "model",
                   "parts": [{"functionCall": {"name": "informes", "args": {}}}]})
        h5.append({"role": "user",
                   "parts": [{"functionResponse":
                              {"name": "informes",
                               "response": {"texto": "x" * 8000}}}]})
        h5.append({"role": "model", "parts": [{"text": f"respuesta {i}"}]})
    antes = conv_mod._peso(h5)
    convs3.podar(50)
    h6, _ = convs3.de(50)
    despues = conv_mod._peso(h6)
    r.append(ok("un historial pesado se poda aunque no pase el tope de turnos",
                despues < antes, f"{antes} -> {despues}"))
    r.append(ok("y queda abajo del tope de peso",
                despues <= convs3.max_chars, despues))

    llamadas = sum(1 for c in h6 for p in c.get("parts", []) if "functionCall" in p)
    respuestas = sum(1 for c in h6 for p in c.get("parts", []) if "functionResponse" in p)
    r.append(ok("podar por peso tampoco deja herramientas huerfanas",
                llamadas == respuestas, f"{llamadas} vs {respuestas}"))
    r.append(ok("el ultimo turno conserva su resultado, que es sobre lo que se habla",
                any(p.get("functionResponse", {}).get("response", {}).get("texto")
                    for c in h6 for p in c.get("parts", [])
                    if isinstance(p, dict) and "functionResponse" in p)))
    r.append(ok("y las preguntas del usuario NO se descartan por peso",
                any("pregunta" in p.get("text", "")
                    for c in h6 for p in c.get("parts", [])
                    if isinstance(p, dict) and "text" in p)))


    # El techo por ventana del modelo: gana el mas chico de los dos.
    c_chico = conv_mod.Conversaciones(max_chars=500000, ventana_tokens=8000)
    r.append(ok("con un modelo de ventana chica, el techo baja el tope",
                c_chico.max_chars == int(8000 * 0.9 * 4), c_chico.max_chars))
    c_grande = conv_mod.Conversaciones(max_chars=80000, ventana_tokens=1_000_000)
    r.append(ok("con ventana grande manda el tope economico, no el techo",
                c_grande.max_chars == 80000, c_grande.max_chars))
    r.append(ok("y el techo queda informado igual, para poder verlo",
                c_grande.techo_ventana == 3_600_000, c_grande.techo_ventana))

    # ------------------------------------------------------------------
    # 9. La renovacion por inactividad
    # ------------------------------------------------------------------
    convs4 = conv_mod.Conversaciones(ttl_seg=conv_mod.TTL_SEG)
    h7, nuevo = convs4.de(60)
    h7.append({"role": "user", "parts": [{"text": "hola"}]})
    r.append(ok("la primera vez no cuenta como renovacion", not nuevo))
    _, nuevo2 = convs4.de(60)
    r.append(ok("y volver enseguida tampoco", not nuevo2))
    convs4._visto[60] = time.time() - convs4.ttl_seg - 10
    h8, nuevo3 = convs4.de(60)
    r.append(ok("pasado el TTL se renueva y se avisa", nuevo3))
    r.append(ok("y el historial arranca vacio", h8 == [], h8))

    # ------------------------------------------------------------------
    # 10. Paralelo entre chats, orden adentro de cada uno
    # ------------------------------------------------------------------
    hechos = []
    trabas = {}

    def trabajo(chat_id, texto):
        if texto == "lento":
            trabas.setdefault(chat_id, threading.Event()).wait(5)
        hechos.append((chat_id, texto))

    desp = bot_daemon.Despachador(trabajo)
    desp.encolar(1, "lento")          # el chat 1 queda trabado
    time.sleep(0.2)
    desp.encolar(2, "rapido")         # el chat 2 NO tiene que esperarlo
    t0 = time.time()
    while (2, "rapido") not in hechos and time.time() - t0 < 3:
        time.sleep(0.02)
    r.append(ok("un chat trabado NO bloquea a otro",
                (2, "rapido") in hechos and (1, "lento") not in hechos, hechos))
    trabas[1].set()
    desp.esperar(10)

    hechos.clear()
    for i in range(8):
        desp.encolar(3, f"m{i}")
    desp.esperar(10)
    delc3 = [x[1] for x in hechos if x[0] == 3]
    r.append(ok("adentro de un chat se atiende EN ORDEN",
                delc3 == [f"m{i}" for i in range(8)], delc3))

    # Un trabajo que revienta no puede matar el hilo del chat.
    def revienta(chat_id, texto):
        if texto == "boom":
            raise RuntimeError("explosion de prueba")
        hechos.append((chat_id, texto))

    desp2 = bot_daemon.Despachador(revienta)
    desp2.encolar(4, "boom")
    desp2.encolar(4, "sigo-vivo")
    desp2.esperar(10)
    r.append(ok("un error en un mensaje no mata el hilo del chat",
                (4, "sigo-vivo") in hechos, hechos))
    desp.parar(2); desp2.parar(2)

    bot.despachador.parar(2)
    for x in tmp:
        shutil.rmtree(x, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
