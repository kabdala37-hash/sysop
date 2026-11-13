"""La revalidacion antes del informe: que se acepta, que se verifica, donde va.

Nacio del informe de srv2 del 2026-09-17, que traia tres cosas mal: dos trabajos
ya arreglados con la causa del dia en que fallaron, y un "401 de Telefonia"
que el modelo saco de `quedan 2401s`. Lo que esta bateria protege, en orden:

  1. Que nada salga de la lista principal sin una cita VERIFICADA de algo que
     el agente miro ahora, y con confianza alta.
  2. Que lo critico no salga nunca, diga lo que diga el agente o una persona.
  3. Que "arreglado, falta confirmar" tenga fecha, y que esa fecha no se corra.
  4. Que un major sin validar se muestre, pero no suba el asunto.

No se toca el modelo ni la base de la plataforma: los dos se reemplazan.
"""
import json
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sysop import informe as informe_mod
from sysop import revalidacion as rv
from sysop import settings
from sysop.adapters.apps import plataforma as nac
from sysop.bot import herramientas as herr_bot
from sysop.core import incidents
from sysop.core.fuentes import Agente, Config, CuentaSMTP
from sysop.core.models import CheckResult, EvidenceQuality, Severity, State
from sysop.llm import esquema, gateway
from sysop.llm import herramientas as herr_sis
from sysop.store import db, registro


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return bool(c)


def nuevo():
    d = Path(tempfile.mkdtemp())
    c = db.connect(d / "t.db"); db.init_schema(c)
    return c, d


def abrir(conn, check_id, resource_id, sev, motivo="ROTO", actual=None):
    r = CheckResult(check_id=check_id, resource_id=resource_id,
                    observed_at=datetime.now(timezone.utc), state=State.FAILED,
                    severity=sev, reason_code=motivo, expected={"x": 1},
                    actual=actual or {"x": 2},
                    evidence_quality=EvidenceQuality.FULL,
                    evidence_refs=[], rule_version=1)
    return incidents.abrir(conn, r)


def rev(veredicto, confianza="alta", citas=(), causa="c", motivo="m"):
    return esquema.Revalidacion(
        veredicto=veredicto, confianza=confianza, causa=causa, accion="a",
        motivo=motivo, evidencia=tuple({"fuente": "f", "cita": c} for c in citas))


AGENTE = Agente(prompt="p", modelo="m", api_key="k", vendor="gemini")
CUENTA = CuentaSMTP(host="127.0.0.1", port=1, secure=False, user="u",
                    password="p", from_addr="f@n.com")

# Lo que devolvio de verdad `leer` en srv2 el 2026-09-17 buscando "401".
LOG_SYNC = {"contenido": (
    "12: 2026-09-17T05:50:03 INFO plataforma/syncLastCalls tenant=clienteb | "
    "[rcex] sesion restaurada del cache para clienteb (quedan 2401s), sin nuevo login")}
# Lo que habria devuelto `consultar` sobre las corridas del sync.
CORRIDAS = {"columnas": ["job_id", "status", "cuantas"],
            "filas": "job_id=5 | status=success | cuantas=80"}


class AppFalsa:
    """Adaptador de prueba: sabe cuando corre el trabajo y tiene `consultar`."""

    def __init__(self):
        self.proxima = datetime.now(timezone.utc) + timedelta(days=3)

    def confirmacion(self, resource_id, ahora=None):
        return self.proxima if resource_id.startswith("job:") else None

    def herramientas(self):
        return ([{"name": "consultar", "description": "d",
                  "parameters": {"type": "object", "properties": {}}}],
                {"consultar": lambda sql="", limite=50: CORRIDAS})


class CfgFalsa:
    installation = "test"
    mapa = {}
    llm = {"presupuesto_diario_usd": 100.0}

    def umbral(self, k, d=None):
        return d


class CtxFalso:
    def __init__(self, conn, agente=AGENTE):
        self.conn = conn
        self.cfg = CfgFalsa()
        self.app = AppFalsa()
        self._agente = agente

    def fuentes(self):
        return Config(cuenta=CUENTA, destinatarios=("a@b.c",), agente=self._agente)

    def planificar(self):
        return []


@contextmanager
def modelo_falso(respuestas):
    """Reemplaza la llamada al modelo. `respuestas`: {texto_en_el_paquete: (json, vistos)}."""
    real = gateway._llamar
    llamadas = []

    def falso(agente, paquete, timeout, schema=None, extra=None, vistos=None):
        llamadas.append(paquete)
        # Por el recurso DEL incidente: el paquete trae tambien los vecinos.
        recurso = (paquete.get("incidente") or {}).get("recurso", "")
        if paquete.get("modo") == "periodico":
            return (json.dumps({"es_real": True, "confianza": "alta",
                                "causa_probable": "periodo", "accion_sugerida": "",
                                "resumen": "todo leido"}),
                    {"entrada": 10, "salida": 1})
        for clave, (resp, vio) in respuestas.items():
            if clave == recurso:
                if vistos is not None:
                    vistos.extend(vio)
                return resp, {"entrada": 10, "salida": 1}
        raise RuntimeError("el modelo no contesto")
    gateway._llamar = falso
    try:
        yield llamadas
    finally:
        gateway._llamar = real


def main():
    r = []
    base = Path(tempfile.mkdtemp())
    settings.STATE_DIR = base / "state"
    settings.STATE_DIR.mkdir()
    registro.configurar("test", settings.STATE_DIR)

    # --- 1. el esquema es estricto ---
    bien = {"veredicto": "falsa_alarma", "confianza": "alta",
            "causa_probable": "c", "accion_sugerida": "", "motivo": "m",
            "evidencia": [{"fuente": "f", "cita": "x"}]}
    r.append(ok("una respuesta completa se acepta",
                esquema.validar_revalidacion(bien) is not None))
    r.append(ok("un veredicto inventado se rechaza",
                esquema.validar_revalidacion({**bien, "veredicto": "todo_ok"}) is None))
    r.append(ok("sin evidencia (ni una lista vacia) se rechaza",
                esquema.validar_revalidacion(
                    {k: v for k, v in bien.items() if k != "evidencia"}) is None))
    r.append(ok("sin motivo se rechaza",
                esquema.validar_revalidacion({**bien, "motivo": " "}) is None))
    r.append(ok("una fecha de confirmacion invalida queda vacia, no se inventa",
                esquema.validar_revalidacion(
                    {**bien, "confirmar_despues_de": "el domingo"})
                .confirmar_despues_de == ""))

    # --- 2. la verificacion de las citas ---
    vistos = [esquema.textos(LOG_SYNC), esquema.textos(CORRIDAS)]
    paquete = esquema.textos({"stderr": "no encuentro /opt/x/acmeBot/acmeBot/r.py"})

    v = esquema.verificar(rev("real", citas=["401 Unauthorized de Telefonia"],
                              causa="la API de Telefonia devuelve 401"),
                          paquete, vistos)
    r.append(ok("EL CASO DEL 17/09: un 401 que no esta en lo que miro no se publica",
                v.veredicto == "real" and v.causa == "" and v.accion == ""
                and "no se publica" in v.nota, v))
    v = esquema.verificar(rev("real", citas=["401"]), paquete,
                          [esquema.textos({"c": "HTTP 401 en /x"})])
    r.append(ok("un numero suelto no es evidencia aunque aparezca",
                not v.evidencia[0]["verificada"]))

    v = esquema.verificar(rev("falsa_alarma", citas=["job_id=5 | status=success"]),
                          paquete, vistos)
    r.append(ok("una falsa alarma con cita fresca y confianza alta se sostiene",
                v.veredicto == "falsa_alarma" and v.evidencia[0]["fresca"], v))
    v = esquema.verificar(rev("falsa_alarma", citas=['"job_id": 5, "status": "success"']),
                          paquete, [esquema.textos({"job_id": 5, "status": "success"})])
    r.append(ok("la cita se reconoce aunque venga con otra puntuacion que el JSON",
                v.veredicto == "falsa_alarma", v))
    v = esquema.verificar(rev("falsa_alarma", confianza="media",
                              citas=["job_id=5 | status=success"]), paquete, vistos)
    r.append(ok("con confianza media pasa a sin validar, y lo dice",
                v.veredicto == "no_se_pudo_validar" and "media" in v.nota, v))
    v = esquema.verificar(rev("falsa_alarma", citas=["el sync anda perfecto hoy"]),
                          paquete, vistos)
    r.append(ok("con una cita inventada pasa a sin validar",
                v.veredicto == "no_se_pudo_validar"))
    v = esquema.verificar(rev("arreglado_pendiente",
                              citas=["no encuentro /opt/x/acmeBot/acmeBot/r.py"]),
                          paquete, vistos)
    r.append(ok("arreglado citando SOLO el paquete (la medicion vieja) no alcanza",
                v.veredicto == "no_se_pudo_validar" and v.evidencia[0]["verificada"]
                and not v.evidencia[0]["fresca"], v))
    v = esquema.verificar(rev("real", citas=["no encuentro /opt/x/acmeBot/acmeBot/r.py"]),
                          paquete, [])
    r.append(ok("un real puede citar el paquete y conserva su causa",
                v.veredicto == "real" and v.causa == "c" and not v.nota))

    # Lo que paso en srv3 el 17/09: la telemetria citada con las claves en
    # otro orden que el JSON.
    tele = {"memoria": {"total_kb": 3926208, "disponible_kb": 2011368},
            "discos": [{"mount": "/", "total_bytes": 14483001344,
                        "libre_bytes": 3419815936},
                       {"mount": "/boot", "total_bytes": 923156480,
                        "libre_bytes": 667754496}]}
    v = esquema.verificar(rev("falsa_alarma",
                              citas=["memoria:{disponible_kb:2011368,total_kb:3926208}"]),
                          paquete, [tele])
    r.append(ok("una cita de un resultado estructurado vale con otro orden de claves",
                v.veredicto == "falsa_alarma", v))
    v = esquema.verificar(rev("falsa_alarma",
                              citas=['libre_bytes:3419815936,mount:"/",total_bytes:14483001344']),
                          paquete, [tele])
    r.append(ok("y la de un disco de la lista, con comillas",
                v.veredicto == "falsa_alarma", v))
    v = esquema.verificar(rev("falsa_alarma",
                              citas=["disponible_kb:9999999, total_kb:3926208"]),
                          paquete, [tele])
    r.append(ok("pero un valor cambiado no pasa",
                v.veredicto == "no_se_pudo_validar"))
    v = esquema.verificar(rev("falsa_alarma",
                              citas=['mount:"/boot", libre_bytes:3419815936']),
                          paquete, [tele])
    r.append(ok("ni una cita que mezcla dos discos",
                v.veredicto == "no_se_pudo_validar"))
    v = esquema.verificar(rev("falsa_alarma", citas=["status=401, error=unauthorized"]),
                          paquete, [LOG_SYNC, CORRIDAS])
    r.append(ok("ni pares inventados con la forma correcta",
                v.veredicto == "no_se_pudo_validar"))
    v = esquema.verificar(rev("falsa_alarma", citas=["status: success, job_id: 5"]),
                          paquete, [CORRIDAS])
    r.append(ok("una fila de consultar se reconoce por pares, en otro orden",
                v.veredicto == "falsa_alarma", v))

    # --- 3. `leer`: un numero se busca como palabra entera ---
    d = Path(tempfile.mkdtemp())
    (d / "sync.log").write_text(
        "sesion restaurada (quedan 2401s)\nOK 0.35MB en 401ms\n"
        "HTTP 401 Unauthorized\nerror=401\n", encoding="utf-8")
    res = herr_sis.leer(str(d / "sync.log"), buscar="401")
    r.append(ok("buscar 401 no trae 2401s ni 401ms, si 'HTTP 401' y 'error=401'",
                res["coincidencias"] == 2 and "2401s" not in res["contenido"],
                res))
    r.append(ok("un texto que no es numero se sigue buscando como antes",
                herr_sis.leer(str(d / "sync.log"), buscar="401m")["coincidencias"] == 1))
    shutil.rmtree(d, ignore_errors=True)

    # --- 4. `consultar` del adaptador: solo lectura, sin base ---
    r.append(ok("consultar rechaza lo que no es lectura",
                "error" in nac.consultar("UPDATE agents SET x=1")
                and "error" in nac.consultar("DROP TABLE x")))
    r.append(ok("y dos sentencias en una",
                "error" in nac.consultar("SELECT 1; DELETE FROM x")))
    r.append(ok("y escribir a un archivo desde un SELECT",
                "error" in nac.consultar("SELECT * FROM a INTO OUTFILE '/tmp/x'")))
    r.append(ok("pero un `;` al final se tolera",
                nac._SQL_LECTURA.match("select 1") is not None))
    r.append(ok("confirmacion no inventa fecha para algo que no es un trabajo",
                nac.confirmacion("systemd:nginx.service") is None))

    real_con, real_jobs = nac.mysql_col.conexion, nac.datos.contratos_jobs

    @contextmanager
    def sin_base(timeout_sec=5.0):
        yield None
    nac.mysql_col.conexion = sin_base
    nac.datos.contratos_jobs = lambda deps: nac.datos.Respuesta(
        [{"job_id": 22, "schedule": "0 7 * * 0", "timezone": "UTC"}],
        EvidenceQuality.FULL, "sql")
    jueves = datetime(2026, 9, 17, 11, 0, tzinfo=timezone.utc)
    c22 = nac.confirmacion("job:clientea:22", jueves)
    nac.mysql_col.conexion, nac.datos.contratos_jobs = real_con, real_jobs
    r.append(ok("un semanal se confirma el domingo a las 07:00 mas 12 h de margen",
                c22 == datetime(2026, 9, 20, 19, 0, tzinfo=timezone.utc), c22))

    # --- 5. guardar: la fecha de confirmacion se ancla ---
    conn, dd = nuevo()
    app = AppFalsa()
    i22 = abrir(conn, "JOB-07", "job:clientea:22", Severity.MAJOR, "CORRIDA_FALLIDA")
    inc22 = dict(conn.execute("SELECT * FROM incident WHERE incident_id=?", (i22,)).fetchone())
    primera = app.proxima
    rv.guardar(conn, inc22, rev("arreglado_pendiente", citas=["x" * 20]), app, "m")
    app.proxima = primera + timedelta(days=2)
    rv.guardar(conn, inc22, rev("arreglado_pendiente", citas=["x" * 20]), app, "m")
    g = rv.de(conn, i22)
    r.append(ok("la segunda vez NO corre la fecha a la proxima corrida",
                g["confirma_en"] == primera.isoformat(), (g["confirma_en"], primera)))
    r.append(ok("y deja un solo evento de revalidacion (no cambio el veredicto)",
                conn.execute("SELECT COUNT(*) FROM incident_event WHERE "
                             "incident_id=? AND kind='revalidado'",
                             (i22,)).fetchone()[0] == 1))
    rv.guardar(conn, inc22, rev("real"), app, "m")
    rv.guardar(conn, inc22, rev("arreglado_pendiente"), app, "m")
    r.append(ok("si en el medio volvio a real, la fecha se recalcula",
                rv.de(conn, i22)["confirma_en"] == app.proxima.isoformat()))
    r.append(ok("una fecha lejana se acota a 8 dias",
                rv.plazo(None, "x", horas=1000)
                <= datetime.now(timezone.utc) + timedelta(days=8, seconds=5)))

    # --- 6. clasificar: donde va cada uno ---
    conn, dd = nuevo()
    desde = datetime.now(timezone.utc).isoformat()
    casos = {}
    for nombre, check, recurso, sev in (
            ("real", "JOB-07", "job:a:1", Severity.MAJOR),
            ("falsa", "JOB-08", "job:a:2", Severity.MAJOR),
            ("falsa_critica", "JOB-08", "job:a:3", Severity.CRITICAL),
            ("falsa_svc", "SVC-01", "systemd:back", Severity.MAJOR),
            ("arreglado", "JOB-07", "job:a:4", Severity.MAJOR),
            ("arreglado_vencido", "JOB-07", "job:a:5", Severity.MAJOR),
            ("sin_validar", "JOB-05", "job:a:6", Severity.MAJOR),
            ("sin_validar_critico", "DB-02", "db:x", Severity.CRITICAL),
            ("viejo", "JOB-05", "job:a:7", Severity.MAJOR)):
        casos[nombre] = abrir(conn, check, recurso, sev)

    def poner(nombre, veredicto, confirma_en=None, at=None):
        conn.execute(
            "INSERT OR REPLACE INTO revalidacion (incident_id, at, veredicto, "
            "confianza, causa, motivo, evidencia_json, pendiente_desde, "
            "confirma_en) VALUES (?,?,?,?,?,?,?,?,?)",
            (casos[nombre], at or datetime.now(timezone.utc).isoformat(),
             veredicto, "alta", "c", "m", "[]", desde, confirma_en))
    ahora = datetime.now(timezone.utc)
    poner("real", "real")
    poner("falsa", "falsa_alarma")
    poner("falsa_critica", "falsa_alarma")
    poner("falsa_svc", "falsa_alarma")
    poner("arreglado", "arreglado_pendiente", (ahora + timedelta(days=2)).isoformat())
    poner("arreglado_vencido", "arreglado_pendiente", (ahora - timedelta(hours=1)).isoformat())
    poner("sin_validar", "no_se_pudo_validar")
    poner("sin_validar_critico", "no_se_pudo_validar")
    poner("viejo", "falsa_alarma", at=(ahora - timedelta(days=1)).isoformat())

    cl = rv.clasificar(conn, incidents.activos(conn, Severity.WARNING), {}, desde)
    donde = {}
    for lista, nombre in ((cl.cuentan, "cuentan"), (cl.falsas, "falsas"),
                          (cl.pendientes, "pendientes")):
        for i in lista:
            donde[i["incident_id"]] = (nombre, i)
    lugar = lambda n: donde[casos[n]][0]            # noqa: E731
    fila = lambda n: donde[casos[n]][1]             # noqa: E731

    r.append(ok("real -> lista principal", lugar("real") == "cuentan"))
    r.append(ok("falsa alarma -> su seccion", lugar("falsa") == "falsas"))
    r.append(ok("UNA FALSA ALARMA CRITICA SIGUE EN LA LISTA PRINCIPAL",
                lugar("falsa_critica") == "cuentan"
                and "critico" in fila("falsa_critica").get("nota", "")))
    r.append(ok("un control de la lista dura (SVC-01) tambien, aunque sea major",
                lugar("falsa_svc") == "cuentan"))
    r.append(ok("arreglado con fecha por delante -> su seccion, con la fecha",
                lugar("arreglado") == "pendientes"
                and fila("arreglado")["pendiente"]["origen"] == "agente"))
    r.append(ok("arreglado con la fecha vencida -> vuelve a la principal y lo dice",
                lugar("arreglado_vencido") == "cuentan"
                and "sigue abierto" in fila("arreglado_vencido").get("nota", "")))
    r.append(ok("un major sin validar se muestra, pero como warning",
                lugar("sin_validar") == "cuentan"
                and fila("sin_validar")["severity"] == "warning"
                and fila("sin_validar")["severidad_medida"] == "major"
                and fila("sin_validar")["sin_validar"]))
    r.append(ok("un critico sin validar conserva su severidad",
                fila("sin_validar_critico")["severity"] == "critical"))
    r.append(ok("un veredicto de un informe anterior no cuenta para este",
                lugar("viejo") == "cuentan" and "revalidacion" not in fila("viejo")
                and fila("viejo")["severity"] == "major"))
    asunto = informe_mod.render.asunto_informe(
        "t", [i for i in cl.cuentan if i["state"] != "RECOVERED"], "hoy")
    r.append(ok("el asunto toma la severidad efectiva",
                "[CRITICAL]" in asunto, asunto))

    # sin revalidacion: el criterio de antes
    cl0 = rv.clasificar(conn, incidents.activos(conn, Severity.WARNING), {}, None)
    r.append(ok("sin revalidar no se toca ninguna severidad ni se saca nada",
                len(cl0.falsas) == 0 and len(cl0.pendientes) == 0
                and all(i["severity"] != "warning" for i in cl0.cuentan)))

    # un recuperado pasa derecho
    incidents.recuperar(conn, casos["real"])
    cl1 = rv.clasificar(conn, incidents.activos(conn, Severity.WARNING), {}, desde)
    r.append(ok("un recuperado queda en su lugar de siempre",
                any(i["incident_id"] == casos["real"] and i["state"] == "RECOVERED"
                    for i in cl1.cuentan)))

    # --- 7. la marca de una persona ---
    m_mal = rv.marcar(conn, app, "INC-no-existe", "lo arregle yo", "telegram:e")
    r.append(ok("marcar un id inexistente no hace nada y lo dice",
                not m_mal["hecho"] and "no existe" in m_mal["problema"]))
    r.append(ok("sin motivo no se marca",
                not rv.marcar(conn, app, casos["viejo"], "listo", "telegram:e")["hecho"]))
    m = rv.marcar(conn, app, casos["viejo"], "corregi la ruta del script",
                  "telegram:Operador")
    cl2 = rv.clasificar(conn, incidents.activos(conn, Severity.WARNING), {}, desde)
    pend = {i["incident_id"]: i for i in cl2.pendientes}
    r.append(ok("marcado por una persona -> arreglados, con su nombre",
                m["hecho"] and casos["viejo"] in pend
                and pend[casos["viejo"]]["pendiente"]["origen"] == "Operador", m))
    r.append(ok("la fecha de una marca sobre un trabajo sale de su proxima corrida",
                m["confirma_en"] == app.proxima.isoformat()))
    mc = rv.marcar(conn, app, casos["sin_validar_critico"], "reinicie la base",
                   "telegram:Operador")
    cl3 = rv.clasificar(conn, incidents.activos(conn, Severity.WARNING), {}, desde)
    r.append(ok("UNA MARCA SOBRE UN CRITICO NO LO SACA DE LA LISTA",
                any(i["incident_id"] == casos["sin_validar_critico"] for i in cl3.cuentan)
                and "critico" in mc["nota"]))
    conn.execute("UPDATE marca_arreglado SET confirma_en = ? WHERE incident_id = ?",
                 ((ahora - timedelta(minutes=1)).isoformat(), casos["viejo"]))
    cl4 = rv.clasificar(conn, incidents.activos(conn, Severity.WARNING), {}, desde)
    vuelto = [i for i in cl4.cuentan if i["incident_id"] == casos["viejo"]]
    r.append(ok("vencida la marca y todavia abierto -> vuelve, con la marca a la vista",
                vuelto and "Operador lo marco arreglado" in vuelto[0]["nota"]))
    r.append(ok("desmarcar la saca", rv.desmarcar(conn, casos["viejo"], "telegram:e")
                and rv.marca(conn, casos["viejo"]) is None))

    # --- 8. revalidar_activos, con el modelo reemplazado ---
    conn, dd = nuevo()
    ctx = CtxFalso(conn)
    i_sync = abrir(conn, "JOB-08", "job:clienteb:5", Severity.MAJOR,
                   "INGESTA_CONGELADA", {"atraso_min": 313})
    i_sem = abrir(conn, "JOB-07", "job:clientea:22", Severity.MAJOR, "CORRIDA_FALLIDA",
                  {"stderr": "no encuentro /opt/x/acmeBot/acmeBot/r.py"})
    i_mudo = abrir(conn, "JOB-05", "job:clienteb:21", Severity.MAJOR,
                   "NUNCA_SE_LANZO")
    respuestas = {
        # el modelo repite el error del 17/09: dice 401 citando algo que no vio
        "job:clienteb:5": (json.dumps({
            "veredicto": "real", "confianza": "alta",
            "causa_probable": "la API de Telefonia devuelve 401",
            "accion_sugerida": "renovar credenciales", "motivo": "vi el 401",
            "evidencia": [{"fuente": "leer", "cita": "401 Unauthorized"}]}),
            [LOG_SYNC]),
        "job:clientea:22": (json.dumps({
            "veredicto": "arreglado_pendiente", "confianza": "alta",
            "causa_probable": "ruta duplicada", "accion_sugerida": "",
            "motivo": "el script ya arma bien la ruta",
            "evidencia": [{"fuente": "leer reconcile_semanal.sh",
                           "cita": 'RECONCILE="${BOT}/reconcile/reconcileDay.py"'}]}),
            [{"contenido": '61: RECONCILE="${BOT}/reconcile/reconcileDay.py"'}]),
    }
    with modelo_falso(respuestas) as llamadas:
        desde8 = rv.revalidar_activos(ctx, ctx.fuentes(), ctx.cfg.llm,
                                      incidents.activos(conn, Severity.WARNING))
    r.append(ok("con agente, se revalida y devuelve desde cuando vale",
                desde8 is not None and len(llamadas) == 3, len(llamadas)))
    r.append(ok("el paquete dice que es modo revalidar y trae la confirmacion calculada",
                all(p["modo"] == "revalidar" for p in llamadas)
                and all(p.get("confirmacion_calculada") for p in llamadas)))
    s = rv.de(conn, i_sync)
    r.append(ok("el 401 inventado queda real pero SIN causa publicada",
                s["veredicto"] == "real" and s["causa"] == "" and s["nota"], s))
    r.append(ok("el arreglado con cita fresca queda arreglado, con fecha",
                rv.de(conn, i_sem)["veredicto"] == "arreglado_pendiente"
                and rv.de(conn, i_sem)["confirma_en"]))
    m8 = rv.de(conn, i_mudo)
    r.append(ok("si el modelo no contesta, queda escrito como sin validar",
                m8["veredicto"] == "no_se_pudo_validar" and "no respondio" in m8["motivo"]))
    etiquetas = [f["incident_id"] for f in conn.execute("SELECT incident_id FROM llm_call")]
    r.append(ok("las llamadas se registran con prefijo: no gastan los intentos "
                "del diagnostico de apertura",
                etiquetas and all(e.startswith(gateway.PREFIJO_REVALIDAR) for e in etiquetas),
                etiquetas))

    ctx.cfg.llm = {"presupuesto_diario_usd": 100.0, "revalidar": {"max_por_informe": 1}}
    with modelo_falso(respuestas) as llamadas:
        rv.revalidar_activos(ctx, ctx.fuentes(), ctx.cfg.llm,
                             incidents.activos(conn, Severity.WARNING))
    r.append(ok("pasado el tope por informe, el resto queda sin validar y sin llamar",
                len(llamadas) == 1
                and "no alcanzo" in (rv.de(conn, i_mudo)["motivo"] or "")))
    ctx.cfg.llm = {"presupuesto_diario_usd": 100.0}

    ctx_sin = CtxFalso(conn, agente=None)
    r.append(ok("sin agente no se revalida nada (el informe sale como antes)",
                rv.revalidar_activos(ctx_sin, ctx_sin.fuentes(), {}, []) is None))
    ctx.cfg.llm = {"presupuesto_diario_usd": 100.0, "revalidar": {"habilitada": False}}
    r.append(ok("y se puede apagar desde el yaml",
                rv.revalidar_activos(ctx, ctx.fuentes(), ctx.cfg.llm, []) is None))
    ctx.cfg.llm = {"presupuesto_diario_usd": 100.0}

    # --- 9. el informe entero, en vista previa ---
    # Registro propio y con latido: el de arriba tiene los incidentes de las
    # otras secciones, que el informe tomaria como "solo en el registro".
    settings.STATE_DIR = base / "state9"
    settings.STATE_DIR.mkdir()
    registro.configurar("test", settings.STATE_DIR)
    registro.latir()
    conn, dd = nuevo()
    ctx = CtxFalso(conn)
    i_sync = abrir(conn, "JOB-08", "job:clienteb:5", Severity.MAJOR,
                   "INGESTA_CONGELADA", {"atraso_min": 313})
    i_sem = abrir(conn, "JOB-07", "job:clientea:22", Severity.MAJOR, "CORRIDA_FALLIDA")
    i_mudo = abrir(conn, "JOB-05", "job:clienteb:21", Severity.MAJOR,
                   "NUNCA_SE_LANZO")
    respuestas_ok = dict(respuestas)
    respuestas_ok["job:clienteb:5"] = (json.dumps({
        "veredicto": "falsa_alarma", "confianza": "alta",
        "causa_probable": "hueco nocturno", "accion_sugerida": "",
        "motivo": "el sync corrio bien las 80 veces de hoy",
        "evidencia": [{"fuente": "consultar rc_cron_runs",
                       "cita": "job_id=5 | status=success | cuantas=80"}]}),
        [CORRIDAS])
    archivo = str(dd / "vista.html")
    with modelo_falso(respuestas_ok) as llamadas:
        res = informe_mod.generar(ctx, vista_previa=archivo)
    html = Path(archivo).read_text(encoding="utf-8")
    r.append(ok("la vista previa no encola nada",
                conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0))
    r.append(ok("el asunto cuenta solo lo que sigue siendo problema, y no sube a MAJOR "
                "por uno sin validar",
                res["asunto"].startswith("[Sysop IA][test][WARNING]")
                and "1 activo" in res["asunto"], res["asunto"]))
    r.append(ok("el correo tiene las dos secciones nuevas",
                "Falsas alarmas" in html and "Arreglados, falta confirmar" in html))
    r.append(ok("y la cita que sostiene la falsa alarma, para poder comprobarla",
                "status=success" in html))
    r.append(ok("el sin validar se ve como tal, con lo medido",
                "SIN VALIDAR" in html and "medido como MAJOR" in html))
    r.append(ok("la lectura del periodo recibio lo que se reviso",
                any(p.get("modo") == "periodico"
                    and p.get("revalidacion_de_los_activos") for p in llamadas)))

    # silenciar va por la severidad MEDIDA, no por la bajada
    from sysop.core import inventory
    hoy = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO resource (resource_id, host_id, kind, first_seen, "
                 "last_seen) VALUES (?,?,?,?,?)",
                 ("job:clienteb:21", settings.HOST_ID, "job", hoy, hoy))
    inventory.clasificar(conn, "job:clienteb:21", settings.HOST_ID, "MANAGED",
                         "prueba de silencio", "test", 5)
    with modelo_falso(respuestas_ok):
        res2 = informe_mod.generar(ctx, vista_previa=archivo)
    r.append(ok("UN MAJOR SIN VALIDAR NO SE VUELVE SILENCIABLE",
                any(i["incident_id"] == i_mudo for i in res2["clasificacion"].cuentan)))

    # --- 10. el bot ---
    ej = herr_bot.ligar(conn, quien="Operador", app=AppFalsa(), cfg_llm={})
    hecho = ej["marcar_arreglado"](incident_id=i_sem, motivo="corregi la ruta del script")
    est = {i["id"]: i for i in ej["estado"]()["incidentes"]}
    r.append(ok("el bot marca, y `estado` muestra la marca y la revalidacion",
                hecho["hecho"] and est[i_sem].get("marcado_arreglado")
                and est[i_sem].get("revalidacion"), est.get(i_sem)))
    r.append(ok("la marca queda a nombre de quien habla",
                rv.marca(conn, i_sem)["quien"] == "telegram:Operador"))
    r.append(ok("y se deshace", ej["desmarcar_arreglado"](incident_id=i_sem)["hecho"]))
    r.append(ok("las herramientas nuevas estan declaradas para el modelo",
                {"marcar_arreglado", "desmarcar_arreglado"}
                <= {x["name"] for x in herr_bot.DECLARACIONES}))

    shutil.rmtree(base, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
