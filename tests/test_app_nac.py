"""El adaptador de la aplicacion: jobs, workers, colas y contratos.

Lo que protege esta bateria es sobre todo una cosa: que ningun control diga OK
sobre algo que no miro. Cada caso de aca corresponde a una forma de mentir que
ya aparecio corriendo el observador de verdad.
"""
import shutil, tempfile
from datetime import datetime, timezone
from pathlib import Path

from sysop.adapters.apps.plataforma import (checks_jobs, checks_workers,
                                                contratos, datos)
from sysop.core.models import (CheckContext, EvidenceQuality, QueryBudget,
                               Resource, ResourceState, Severity, State)
from sysop.store import db


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


class CfgFalsa:
    """Config minima: umbrales por defecto y ninguna unidad detenida."""
    units_detenidas = ()

    def umbral(self, ruta, default=None):
        return default


class CfgSupervisorApagado(CfgFalsa):
    units_detenidas = ("acme-line-supervisor.service",)


def ctx_de(conn, cfg=None, deps_extra=None, recurso=None):
    deps = {"cfg": cfg or CfgFalsa(), "conn": conn}
    deps.update(deps_extra or {})
    return CheckContext(
        resource=recurso or Resource(resource_id="host:t", host_id="t", kind="host",
                                     state=ResourceState.MANAGED),
        expectation=None, now=datetime.now(timezone.utc),
        budget=QueryBudget(), deps=deps)


def main():
    r = []
    d = Path(tempfile.mkdtemp())
    conn = db.connect(d / "t.db"); db.init_schema(conn)

    # --- JOB-03: no encontrar rutas NO es haber verificado que estan ---
    job_sin_ruta = {"job_id": 9, "client_id": "t", "is_active": True,
                    "comando": "", "parseable": True, "script": "x.py"}
    c = ctx_de(conn, recurso=Resource(resource_id="job:t:9", host_id="t", kind="job",
                                      scope={"job_id": 9}))
    c.deps["_nac_cache"] = {"jobs": datos.Respuesta([job_sin_ruta],
                                                    EvidenceQuality.FULL, "back")}
    out = checks_jobs._job03(c)
    r.append(ok("JOB-03 sin rutas que verificar da UNKNOWN, no OK",
                out.state is State.UNKNOWN and out.reason_code == "COMANDO_SIN_RUTAS",
                out))

    job_falta = dict(job_sin_ruta, comando="/no/existe/python /no/existe/x.py")
    c.deps["_nac_cache"] = {"jobs": datos.Respuesta([job_falta],
                                                    EvidenceQuality.FULL, "back")}
    out = checks_jobs._job03(c)
    r.append(ok("JOB-03 detecta el ejecutable ausente",
                out.state is State.FAILED and out.reason_code == "EJECUTABLE_AUSENTE",
                out))

    job_ok = dict(job_sin_ruta, comando="/usr/bin/env /usr/bin/env")
    c.deps["_nac_cache"] = {"jobs": datos.Respuesta([job_ok],
                                                    EvidenceQuality.FULL, "back")}
    out = checks_jobs._job03(c)
    r.append(ok("JOB-03 con rutas que existen da OK", out.state is State.OK, out))

    job_off = dict(job_ok, is_active=False)
    c.deps["_nac_cache"] = {"jobs": datos.Respuesta([job_off],
                                                    EvidenceQuality.FULL, "back")}
    r.append(ok("un job desactivado es DISABLED_EXPECTED, no una falla",
                checks_jobs._job03(c).state is State.DISABLED_EXPECTED))

    # ------------------------------------------------------------------
    # JOB-05: "no corrio" y "corrio y nadie lo anoto" NO son lo mismo.
    #
    # Hasta el 2026-09-03 la unica fuente era `rc_cron_runs`, una tabla que
    # escribe la aplicacion observada. En srv2 esa tabla estaba vacia desde
    # siempre --los cron.d se habian renderizado dos semanas antes de que
    # existiera el envoltorio que la escribe-- y el observador informo TRECE
    # veces `NUNCA_SE_LANZO` sobre trabajos que corrian perfectamente. Un
    # observador de una sola fuente, cuando el observado se calla, no dice "no
    # se": dice "esta roto".
    # ------------------------------------------------------------------
    from datetime import timedelta as _td
    job05 = {"job_id": 12, "client_id": "clientea", "is_active": True,
             "schedule": "*/5 * * * *", "timezone": "UTC",
             "comando": "/opt/x/venv/bin/python3 "
                              "/opt/x/acmeBot/shooters/mail_dispatch.py "
                              "--client_id clientea --workers 8",
             "last_run_at": None, "updated_at": "2026-01-01T00:00:00+00:00"}
    c5 = ctx_de(conn, recurso=Resource(resource_id="job:clientea:12", host_id="t",
                                       kind="job", scope={"job_id": 12}))
    c5.deps["_nac_cache"] = {"jobs": datos.Respuesta([job05], EvidenceQuality.FULL, "back"),
                             "corridas": datos.Respuesta({}, EvidenceQuality.FULL, "back")}

    hace_un_rato = datetime.now(timezone.utc) - _td(minutes=3)
    original = checks_jobs._lo_vio_cron
    try:
        # a) el ledger no tiene nada PERO cron dice que lo lanzo
        checks_jobs._lo_vio_cron = lambda j, esp=None: (hace_un_rato, True)
        out = checks_jobs._job05(c5)
        r.append(ok("con el ledger vacio y cron diciendo que lo lanzo, NO dice "
                    "'nunca se lanzo'",
                    out.reason_code == "LEDGER_NO_SE_ESCRIBE", out))
        r.append(ok("y lo reporta como lo que es: falta el registro, no el trabajo",
                    out.state is State.DEGRADED
                    and out.severity_override is Severity.WARNING, out))
        r.append(ok("y deja dicho como se arregla",
                    "job_runner" in str(out.actual.get("nota", "")), out.actual))

        # b) ninguna de las dos fuentes lo vio: ahi si es un hallazgo
        checks_jobs._lo_vio_cron = lambda j, esp=None: (None, True)
        out = checks_jobs._job05(c5)
        r.append(ok("si cron TAMPOCO lo lanzo, sigue siendo NUNCA_SE_LANZO",
                    out.reason_code == "NUNCA_SE_LANZO"
                    and out.state is State.MISSING, out))
        r.append(ok("y con severidad MAJOR, porque ahi el trabajo si esta caido",
                    out.severity_override is Severity.MAJOR, out))
        r.append(ok("dejando constancia de que se consulto a cron",
                    out.actual.get("cron_tampoco_lo_lanzo") is True, out.actual))

        # c) no se pudo leer el journal: no se puede afirmar nada
        checks_jobs._lo_vio_cron = lambda j, esp=None: (None, False)
        out = checks_jobs._job05(c5)
        r.append(ok("sin poder leer el journal NO se afirma que no corrio",
                    out.reason_code == "SIN_FUENTE_PARA_CORRIDAS"
                    and out.state is State.UNKNOWN, out))
    finally:
        checks_jobs._lo_vio_cron = original

    # d) con el ledger lleno ni se consulta al journal: la fuente buena manda
    c5.deps["_nac_cache"]["corridas"] = datos.Respuesta(
        {12: {"ultimo_inicio": (datetime.now(timezone.utc) - _td(minutes=2)).isoformat()}},
        EvidenceQuality.FULL, "back")
    def _no_deberia_llamarse(j, esp=None):
        raise AssertionError("se consulto el journal teniendo el ledger")
    original = checks_jobs._lo_vio_cron
    try:
        checks_jobs._lo_vio_cron = _no_deberia_llamarse
        out = checks_jobs._job05(c5)
        r.append(ok("con el ledger al dia da OK sin preguntarle al journal",
                    out.state is State.OK, out))
        r.append(ok("y deja anotado de que fuente salio el dato",
                    out.actual.get("fuente") == "ledger", out.actual))
    finally:
        checks_jobs._lo_vio_cron = original

    # e) un diario medido en el minuto de su lanzamiento: el ledger todavia
    # tiene la corrida de ayer. Daba LANZAMIENTO_FALTANTE todos los dias en srv2.
    _n = datetime.now(timezone.utc)
    ayer = (_n - _td(days=1)).isoformat()
    c5.deps["_nac_cache"]["corridas"] = datos.Respuesta(
        {12: {"ultimo_inicio": ayer}}, EvidenceQuality.FULL, "back")
    job05["schedule"] = f"{_n.minute} {_n.hour} * * *"
    out = checks_jobs._job05(c5)
    r.append(ok("un diario en el minuto de su lanzamiento NO es lanzamiento faltante",
                out.state is State.OK, out))
    _t = _n - _td(minutes=5)
    job05["schedule"] = f"{_t.minute} {_t.hour} * * *"
    out = checks_jobs._job05(c5)
    r.append(ok("pero cinco minutos despues sin corrida nueva si lo es",
                out.reason_code == "LANZAMIENTO_FALTANTE", out))
    job05["schedule"] = "*/5 * * * *"

    # El matcheo del comando distingue TENANTS, que es el riesgo real: dos
    # trabajos con el mismo script y distinto --client_id son trabajos distintos.
    from sysop.adapters.so_linux.collectors import journal as _j
    disparos = [{"at": hace_un_rato, "usuario": "deploy",
                 "cmd": "/bin/bash -lc '/opt/x/venv/bin/python3 "
                        "/opt/x/acmeBot/shooters/mail_dispatch.py "
                        "--client_id clientea --workers 8'"}]
    r.append(ok("reconoce el disparo de SU tenant",
                _j.ultimo_disparo(disparos, 12, job05["comando"]) == hace_un_rato))
    otro = job05["comando"].replace("--client_id clientea", "--client_id clienteb")
    r.append(ok("y NO se queda con el del tenant de al lado",
                _j.ultimo_disparo(disparos, 13, otro) is None))
    # La ventana del journal NO puede ser fija. Un trabajo semanal --`0 11 * * 1`,
    # los lunes-- mirado un jueves no aparece en 48 h, y el control concluia que
    # cron nunca lo lanzo. Paso en srv2 con el reporte TCPA de clientea.
    from datetime import timedelta as _td2
    _ahora = datetime.now(timezone.utc)
    r.append(ok("sin saber cuando le tocaba, se usa el piso",
                checks_jobs._ventana_journal(None) == checks_jobs.HORAS_JOURNAL_MIN))
    r.append(ok("un trabajo cada 5 minutos no hace leer mas que el piso",
                checks_jobs._ventana_journal(_ahora - _td2(minutes=5), _ahora)
                == checks_jobs.HORAS_JOURNAL_MIN))
    r.append(ok("uno SEMANAL mirado tres dias despues pide mas de 48 h",
                checks_jobs._ventana_journal(_ahora - _td2(days=3), _ahora) > 48))
    r.append(ok("y uno abandonado hace meses no hace leer el journal entero",
                checks_jobs._ventana_journal(_ahora - _td2(days=400), _ahora)
                == checks_jobs.HORAS_JOURNAL_MAX))

    r.append(ok("con el envoltorio, matchea por --job-id sin mirar el comando",
                _j.ultimo_disparo(
                    [{"at": hace_un_rato, "cmd": "python job_runner.py --job-id 12 -- x"}],
                    12, None) == hace_un_rato))

    # --- contratos ---
    ctx = ctx_de(conn)
    out = contratos.verificar(ctx, {"script": "otro.py", "args": {},
                                    "parseable": True}, EvidenceQuality.FULL)
    r.append(ok("un job sin contrato declarado da UNKNOWN, jamas OK",
                out.state is State.UNKNOWN
                and out.reason_code == "SIN_CONTRATO_DECLARADO", out))

    out = contratos.verificar(ctx, {"script": "procesarLlamadas.py",
                                    "args": {"solo": "5491100000000"},
                                    "parseable": True}, EvidenceQuality.FULL)
    r.append(ok("un job en modo prueba (--solo) no tiene que cumplir su contrato",
                out.state is State.DISABLED_EXPECTED
                and out.reason_code == "JOB_EN_MODO_PRUEBA", out))

    out = contratos.verificar(ctx, {"script": None, "args": {},
                                    "parseable": False}, EvidenceQuality.PARTIAL)
    r.append(ok("sin el comando interpretado, el contrato no concluye",
                out.state is State.UNKNOWN, out))

    # --- syncLastCalls: Telefonia responde (2026-09-17) ---
    from datetime import timedelta as _td3
    logs = d / "logs"

    class CfgSync(CfgFalsa):
        def umbral(self, ruta, default=None):
            if ruta == "jobs.contratos.sync_log":
                return str(logs / "{componente}" / "client_{tenant}.log")
            return default

    def escribir(componente, tenant, lineas, sufijo=""):
        f = logs / componente / f"client_{tenant}.log{sufijo}"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("\n".join(lineas) + "\n", encoding="utf-8")

    def hace(minutos):
        return (datetime.now(timezone.utc) - _td3(minutes=minutos)).isoformat(
            timespec="milliseconds")

    def corrida(min_, n, tenant="bc"):
        return [f"{hace(min_)} INFO plataforma/syncLastCalls tenant={tenant} | "
                f"[ringex] sync cliente '{tenant}' desde X hasta Y (UTC). min_duracion=10s",
                f"{hace(min_)} INFO plataforma/syncLastCalls.sync_telefonia_service "
                f"tenant={tenant} | [rcex] get_records_telefonia: {n} records en 1 "
                f"pagina(s) (X -> Y)"]

    def job_sync(tenant, source="ringex"):
        return {"job_id": 5, "client_id": tenant, "script": "syncLastCalls.py",
                "parseable": True, "schedule": "*/10 * * * *", "timezone": "UTC",
                "args": {"client_id": tenant, "source": source}}

    ctx_s = ctx_de(conn, cfg=CfgSync(), deps_extra={"mysql": object()})

    # El caso de srv2: horas sin llamadas nuevas, Telefonia respondiendo igual.
    escribir("syncLastCalls", "bc", corrida(55, 1) + corrida(45, 1) + corrida(25, 0)
             + corrida(15, 0) + corrida(5, 1))
    out = contratos.verificar(ctx_s, job_sync("bc"), EvidenceQuality.FULL)
    r.append(ok("SIN LLAMADAS NUEVAS PERO CON RESPUESTA DE TELEFONIA: OK",
                out.state is State.OK
                and out.actual["fuentes"][0]["respuestas_en_ventana"] == 3, out))
    r.append(ok("la ventana son 3 corridas del cron mas 2 min (32 min cada 10)",
                out.actual["ventana_min"] == 32, out.actual))

    escribir("syncLastCalls", "caido", corrida(50, 3, "caido") + [
        f"{hace(20)} INFO plataforma/syncLastCalls tenant=caido | [ringex] sync cliente 'caido' desde X hasta Y (UTC).",
        f"{hace(20)} WARNING plataforma/syncLastCalls.sync_telefonia_service tenant=caido | [rcex] 429 en /restapi/v1.0/account/~/call-log, Retry-After=60, esperando 60s",
        f"{hace(10)} ERROR plataforma/syncLastCalls.sync_telefonia_service tenant=caido | Error en get_records_telefonia: Demasiados intentos fallidos",
        "    Traceback (most recent call last):",
        f"{hace(10)} ERROR plataforma/syncLastCalls tenant=caido | [ringex] fallo: exit=1 cmd=x"])
    out = contratos.verificar(ctx_s, job_sync("caido"), EvidenceQuality.FULL)
    f0 = (out.actual or {}).get("fuentes", [{}])[0]
    r.append(ok("intentos sin ninguna respuesta en la ventana: TELEFONIA_SIN_RESPUESTA",
                out.state is State.FAILED
                and out.reason_code == "TELEFONIA_SIN_RESPUESTA", out))
    r.append(ok("con el ultimo error y la ultima respuesta como evidencia",
                "fallo: exit=1" in (f0.get("ultimo_error") or "")
                and f0.get("ultima_respuesta") and f0.get("intentos_en_ventana") == 1, f0))

    escribir("syncLastCalls", "mudo", corrida(90, 2, "mudo"))
    out = contratos.verificar(ctx_s, job_sync("mudo"), EvidenceQuality.FULL)
    r.append(ok("un log que no se escribe hace media hora: SYNC_SIN_ACTIVIDAD",
                out.state is State.FAILED and out.reason_code == "SYNC_SIN_ACTIVIDAD", out))

    out = contratos.verificar(ctx_s, job_sync("nadie"), EvidenceQuality.FULL)
    r.append(ok("sin el archivo no se afirma nada: UNKNOWN, no falla",
                out.state is State.UNKNOWN
                and out.reason_code == "LOG_DEL_SYNC_NO_ENCONTRADO", out))

    escribir("syncLastCalls", "ambos", corrida(5, 4, "ambos"))
    escribir("syncLastCallsCX", "ambos", [
        f"{hace(5)} INFO plataforma/syncLastCallsCX tenant=ambos | [ringcx] sync cliente 'ambos' desde X hasta Y (UTC).",
        f"{hace(5)} ERROR plataforma/syncLastCallsCX tenant=ambos | [ringcx] fallo: exit=1 cmd=x"])
    out = contratos.verificar(ctx_s, job_sync("ambos", source="both"), EvidenceQuality.FULL)
    r.append(ok("con --source both, que falle RingCX alcanza para fallar",
                out.state is State.FAILED
                and out.reason_code == "TELEFONIA_SIN_RESPUESTA"
                and len(out.actual["fuentes"]) == 2, out))
    escribir("syncLastCallsCX", "ambos", [
        f"{hace(5)} INFO plataforma/syncLastCallsCX.sync_ringcx_service tenant=ambos | [rcx] 0 llamadas obtenidas en total"])
    out = contratos.verificar(ctx_s, job_sync("ambos", source="both"), EvidenceQuality.FULL)
    r.append(ok("y con las dos respondiendo (RingCX con 0 llamadas), OK",
                out.state is State.OK, out))

    # mirado desde antes, lo posterior no cuenta
    from sysop.core.models import CheckContext as _CC
    ctx_antes = _CC(resource=ctx_s.resource, expectation=None,
                    now=datetime.now(timezone.utc) - _td3(minutes=40),
                    budget=ctx_s.budget, deps=ctx_s.deps)
    escribir("syncLastCalls", "tarde", corrida(5, 1, "tarde"))
    out = contratos.verificar(ctx_antes, job_sync("tarde"), EvidenceQuality.FULL)
    r.append(ok("una respuesta posterior al momento evaluado no cuenta",
                out.state is not State.OK, out))

    # recien rotado: el archivo nuevo no cubre la ventana, el anterior si
    escribir("syncLastCalls", "rotado", corrida(15, 2, "rotado"), sufijo=".1")
    escribir("syncLastCalls", "rotado", [
        f"{hace(1)} INFO plataforma/syncLastCalls tenant=rotado | [ringex] sync cliente 'rotado' desde X hasta Y (UTC)."])
    out = contratos.verificar(ctx_s, job_sync("rotado"), EvidenceQuality.FULL)
    r.append(ok("recien rotado el log, se mira tambien el anterior",
                out.state is State.OK, out))

    # un log mas grande que la cola que se lee
    viejo_relleno = [f"{hace(600)} INFO plataforma/syncLastCalls tenant=grande | " + "x" * 200] * 4000
    escribir("syncLastCalls", "grande", viejo_relleno + corrida(3, 7, "grande"))
    out = contratos.verificar(ctx_s, job_sync("grande"), EvidenceQuality.FULL)
    r.append(ok("un log grande se lee solo por el final y encuentra la respuesta",
                out.state is State.OK
                and out.actual["fuentes"][0]["registros_ultima_respuesta"] == 7, out))

    r.append(ok("la duracion minima sale del posicional del comando",
                contratos._duracion_minima({"posicionales": ["10"], "args": {}}) == 10))
    r.append(ok("y si no esta, no se inventa un numero",
                contratos._duracion_minima({"posicionales": [], "args": {}}) is None))

    # --- workers ---
    c2 = ctx_de(conn, cfg=CfgSupervisorApagado())
    out = checks_workers._wrk02(c2)
    r.append(ok("sin supervisor de lineas no se esperan workers",
                out.state is State.DISABLED_EXPECTED, out))

    c3 = ctx_de(conn)
    c3.deps["_nac_cache"] = {"workers": datos.Respuesta(None, EvidenceQuality.NONE,
                                                        "nada", "sin fuente")}
    out = checks_workers._wrk01(c3)
    r.append(ok("no poder derivar los workers es una falla con nombre propio",
                out.state is State.FAILED
                and out.reason_code == "DERIVACION_NO_DISPONIBLE", out))
    r.append(ok("y los demas controles de workers no concluyen nada",
                checks_workers._wrk02(c3).state is State.UNKNOWN
                and checks_workers._wrk03(c3).state is State.UNKNOWN))

    r.append(ok("la identidad de un worker es tenant + linea",
                checks_workers._identidad({"client_id": "clientea", "media_id": 4})
                == "clientea:4"))

    # ------------------------------------------------------------------
    # El argv de un worker se lee por FLAGS, no por posicion.
    #
    # Hasta el 2026-09-03 se parseaba `--modo <agente> <tenant> <media>`, un
    # formato que no usa nadie: el worker documenta el suyo en
    # `workerBot/index.js:745` y el supervisor lo lanza asi
    # (`line_supervisor.py:134-140`). El resultado en srv2 fue que `client_id` se
    # llevaba el nombre del AGENTE y `media_id` quedaba en None, y con la
    # identidad rota salieron a la vez WRK-02 "faltan 6", WRK-03 "sobran 6" y
    # WRK-04 "duplicado", con los seis workers corriendo bien.
    # ------------------------------------------------------------------
    real = ["/usr/bin/node", "/opt/plataforma/workerBot/index.js",
            "--both", "--agent", "AGENTE1", "--client", "clientea", "--media", "7"]
    campos = checks_workers._flags(real)
    r.append(ok("de un --both real salen agente, tenant y linea",
                campos == {"agent_id": "AGENTE1", "client_id": "clientea",
                           "media_id": "7"}, campos))
    r.append(ok("y su identidad es la linea, no el agente",
                checks_workers._identidad(
                    {"client_id": campos["client_id"],
                     "media_id": checks_workers._entero(campos["media_id"])})
                == "clientea:7"))

    inb = checks_workers._flags(["node", "index.js", "--inbound",
                                 "--media", "9", "--client", "acme"])
    r.append(ok("--inbound no lleva --agent, y eso NO es un error",
                inb.get("agent_id") is None and inb["client_id"] == "acme"
                and inb["media_id"] == "9", inb))

    out_ = checks_workers._flags(["node", "index.js", "--outbound", "--agent",
                                  "CALLTEST", "--client", "clientea", "--lead", "2"])
    r.append(ok("--outbound no lleva --media", out_.get("media_id") is None, out_))
    r.append(ok("y no cuenta como worker de linea: es una llamada, no una linea",
                "outbound" not in checks_workers.MODOS_DE_LINEA))

    # Un flag sin valor no se puede llevar puesto al de al lado.
    roto = checks_workers._flags(["node", "index.js", "--both", "--agent",
                                  "--client", "clientea", "--media", "7"])
    r.append(ok("un flag sin valor queda vacio, no se come el flag siguiente",
                roto.get("agent_id") is None and roto["client_id"] == "clientea",
                roto))

    # Los seis de srv2, tal cual estaban corriendo el 2026-09-03: los seis
    # esperados tienen que encontrarse entre los seis vivos.
    srv2 = [("both", "AGENTE1", "clientea", "7"), ("both", "AGENTE2", "acme", "25"),
           ("shift", "AGENTE3", "clientea", "15"), ("both", "AGENTE4", "clienteb", "4"),
           ("shift", "AGENTE3", "clientea", "11"), ("both", "AGENTE5", "clientea", "2")]
    vivos = []
    for modo, ag, cli, med in srv2:
        c = checks_workers._flags(["node", "index.js", "--" + modo, "--agent", ag,
                                   "--client", cli, "--media", med])
        vivos.append({"client_id": c["client_id"],
                      "media_id": checks_workers._entero(c["media_id"])})
    corriendo = {checks_workers._identidad(w) for w in vivos}
    esperados = {"clientea:7", "acme:25", "clientea:15", "clienteb:4", "clientea:11", "clientea:2"}
    r.append(ok("los seis de srv2 se emparejan con los seis esperados",
                corriendo == esperados, sorted(corriendo)))
    r.append(ok("y los dos AGENTE3 son DOS lineas distintas, no un duplicado",
                len({i for i in corriendo if i.startswith("clientea:1")}) == 2,
                sorted(corriendo)))

    # --- datos: la caida a SQL se marca ---
    r.append(ok("sin back ni base, la respuesta es NONE y no una lista vacia",
                datos.workers_esperados({}).calidad is EvidenceQuality.NONE))
    deps = {}
    datos.workers_esperados(deps); datos.workers_esperados(deps)
    r.append(ok("la respuesta se cachea por ciclo, no se pregunta dos veces",
                list(deps["_nac_cache"]) == ["workers"]))
    r.append(ok("el modo del worker se deriva de la direccion de la linea",
                datos._modo("outbound") == "shift" and datos._modo("inbound") == "inbound"))

    # --- el cache entre ciclos: reusa mientras vale, no cachea un fallo ---
    datos.olvidar()
    veces = []

    def buena():
        veces.append(1)
        return datos.Respuesta([1], EvidenceQuality.FULL, "back")

    datos._cache({}, "workers", buena)
    datos._cache({}, "workers", buena)      # otro ciclo, todavia vigente
    r.append(ok("dos ciclos seguidos no preguntan dos veces", len(veces) == 1,
                len(veces)))
    datos.olvidar()
    datos._cache({}, "workers", buena)
    r.append(ok("vencida la vigencia, se vuelve a preguntar", len(veces) == 2))

    datos.olvidar()
    mal = lambda: datos.Respuesta(None, EvidenceQuality.NONE, "nada", "sin fuente")
    datos._cache({}, "jobs", mal)
    datos._cache({}, "jobs", buena)
    r.append(ok("un fallo NO se cachea: esconderia la recuperacion",
                len(veces) == 3, len(veces)))
    datos.olvidar()

    # --- JOB-01: el inventario compara contra lo conocido ---
    # Las fechas van en el mismo formato que usa el codigo real (isoformat).
    # Un test que escribe fechas en otro formato es como quedo escondido el bug
    # de las ventanas temporales: la comparacion rota lo daba por bueno igual.
    _ahora = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO resource (resource_id, host_id, kind, state, "
                 "first_seen, last_seen) VALUES ('job:t:1','t','job','MANAGED',"
                 "?, ?)", (_ahora, _ahora))
    c4 = ctx_de(conn)
    c4.deps["_nac_cache"] = {"jobs": datos.Respuesta(
        [{"job_id": 1, "client_id": "t", "is_active": True},
         {"job_id": 2, "client_id": "t", "is_active": True}],
        EvidenceQuality.FULL, "back")}
    out = checks_jobs._job01(c4)
    r.append(ok("JOB-01 ve el trabajo nuevo que no estaba en el inventario",
                out.state is State.DEGRADED and "job:t:2" in str(out.actual), out))

    # ------------------------------------------------------------------
    # OPS-10: un umbral mas grande que el disco es un umbral apagado
    #
    # En srv3 el tope estaba en 2048 MiB sobre un disco con 1.884 MiB libres.
    # ------------------------------------------------------------------
    import os as _os
    from sysop import settings as _st
    from sysop.adapters.apps.plataforma import checks_ops

    class StatVfs:
        f_frsize = 1048576

        def __init__(self, libre_mib):
            self.f_bavail = int(libre_mib)

    def ops10_con(libre_mib, almacen_mib, cfg_over=None, con=None):
        _orig_statvfs, _orig_stat = _os.statvfs, type(_st.DB_PATH).stat

        class CfgOps(CfgFalsa):
            def umbral(self, ruta, default=None):
                return (cfg_over or {}).get(ruta, default)

        class StatFalso:
            st_size = int(almacen_mib * 1048576)

        _os.statvfs = lambda p: StatVfs(libre_mib)
        type(_st.DB_PATH).stat = lambda self, **k: StatFalso()
        try:
            c = ctx_de(con or conn, cfg=CfgOps())
            return checks_ops._ops10(c)
        finally:
            _os.statvfs, type(_st.DB_PATH).stat = _orig_statvfs, _orig_stat

    out = ops10_con(libre_mib=100000, almacen_mib=100)
    r.append(ok("con el disco holgado, 100 MiB de almacen esta bien",
                out.state is State.OK, out))
    r.append(ok("y el tope efectivo es el absoluto",
                out.actual["tope_mib"] == 2048, out.actual))

    out = ops10_con(libre_mib=1884, almacen_mib=600)
    r.append(ok("con 1.884 MiB libres, 600 de almacen YA es un problema",
                out.state is State.DEGRADED and out.reason_code == "ALMACEN_GRANDE", out))
    r.append(ok("porque el tope bajo a una fraccion de lo que queda libre",
                out.actual["tope_mib"] == 471.0, out.actual))
    r.append(ok("y el detalle dice cuanto queda libre, para poder actuar",
                out.actual["libre_mib"] == 1884.0 and out.actual["tope_absoluto_mib"] == 2048,
                out.actual))

    out = ops10_con(libre_mib=1884, almacen_mib=86)
    r.append(ok("los 86 MiB que tiene hoy srv3 no alarman", out.state is State.OK, out))

    out = ops10_con(libre_mib=1884, almacen_mib=600, cfg_over={
        "agente.max_almacen_frac_libre": 0.9})
    r.append(ok("la fraccion se puede aflojar por config", out.state is State.OK, out))

    # La cuenta de la ultima hora va por `id` desde la ultima medicion de OPS-10
    # anterior a la ventana (sin indice por fecha recorria la tabla entera). Tiene
    # que dar lo mismo que contar por fecha.
    from datetime import timedelta as _td
    conn_o = db.connect(d / "ops10.db"); db.init_schema(conn_o)
    ahora_o = datetime.now(timezone.utc)
    for check, minutos, estado in (("X-01", 200, "UNKNOWN"), ("OPS-10", 120, "OK"),
                                   ("X-01", 90, "UNKNOWN"), ("OPS-10", 61, "OK"),
                                   ("X-01", 59, "UNKNOWN"), ("X-01", 30, "OK"),
                                   ("OPS-10", 1, "OK")):
        conn_o.execute(
            "INSERT INTO check_result (check_id, resource_id, observed_at, state, severity) "
            "VALUES (?, 'host:t', ?, ?, 'MAJOR')",
            (check, (ahora_o - _td(minutes=minutos)).isoformat(), estado))
    conn_o.commit()
    out = ops10_con(libre_mib=100000, almacen_mib=100, con=conn_o)
    r.append(ok("OPS-10 cuenta solo la ultima hora (3 resultados, 1 UNKNOWN)",
                out.actual["resultados_1h"] == 3 and out.actual["unknown_pct"] == 33.3,
                out.actual))
    conn_o.execute("DELETE FROM check_result WHERE check_id = 'OPS-10' AND id < 7")
    conn_o.commit()
    out = ops10_con(libre_mib=100000, almacen_mib=100, con=conn_o)
    r.append(ok("y sin medicion previa de OPS-10 cuenta igual (piso en cero)",
                out.actual["resultados_1h"] == 3, out.actual))
    conn_o.close()

    # ------------------------------------------------------------------
    # JOB-06: un ledger vacio NO es un trabajo sano
    #
    # `rc_cron_runs` esta vacia en las dos instalaciones. Antes este control
    # encontraba cero corridas en curso y devolvia OK, o sea que afirmaba no
    # haber ninguna colgada mirando una tabla que nadie escribe.
    # ------------------------------------------------------------------
    def ctx_job06(corridas, job=None, job_id=7):
        c = ctx_de(conn, recurso=Resource(resource_id="job:t:%d" % job_id, host_id="t",
                                          kind="job", scope={"job_id": job_id}))
        c.deps["_nac_cache"] = {
            "corridas": datos.Respuesta(corridas, EvidenceQuality.FULL, "sql"),
            "jobs": datos.Respuesta([job] if job else [], EvidenceQuality.FULL, "back"),
        }
        return c

    job_corrio = {"job_id": 7, "client_id": "t", "is_active": True,
                  "last_run_at": "2026-08-31 10:00:00", "comando": "x", "script": "x.py"}
    job_nunca = {"job_id": 7, "client_id": "t", "is_active": True,
                 "last_run_at": None, "comando": "x", "script": "x.py"}

    out = checks_jobs._job06(ctx_job06({}, job_corrio))
    r.append(ok("JOB-06 con el ledger vacio NO dice OK",
                out.state is not State.OK, out))
    r.append(ok("dice UNKNOWN y que el trabajo corrio sin dejar fila",
                out.state is State.UNKNOWN and out.reason_code == "LEDGER_SIN_ESTE_JOB",
                out))
    r.append(ok("y con evidencia NONE, no FULL",
                out.evidence_quality is EvidenceQuality.NONE, out.evidence_quality))

    out = checks_jobs._job06(ctx_job06({}, job_nunca))
    r.append(ok("un trabajo que nunca corrio se distingue del ledger roto",
                out.state is State.UNKNOWN
                and out.reason_code == "SIN_CORRIDAS_REGISTRADAS", out))

    out = checks_jobs._job06(ctx_job06({7: {"estado": "success"}}, job_corrio))
    r.append(ok("con entrada en el ledger y sin corridas en curso, SI dice OK",
                out.state is State.OK, out))

    out = checks_jobs._job06(ctx_job06(
        {7: {"en_curso": [{"run_id": 1, "started_at": "x", "corriendo_seg": 60}]}},
        job_corrio))
    r.append(ok("una corrida en curso dentro del tope es OK", out.state is State.OK, out))

    out = checks_jobs._job06(ctx_job06(
        {7: {"en_curso": [{"run_id": 2, "started_at": "x", "corriendo_seg": 99999}]}},
        job_corrio))
    r.append(ok("una corrida pasada del tope es STUCK", out.state is State.STUCK, out))
    r.append(ok("y dice cual es y hace cuanto",
                out.actual.get("run_id") == 2 and out.actual.get("corriendo_seg") == 99999,
                out.actual))
    # ------------------------------------------------------------------
    # Un trabajo DESACTIVADO no es un punto ciego
    #
    # JOB-08 preguntaba `is_active` y JOB-06/07 no, mirando el mismo recurso.
    # En srv3, con los 6 trabajos en is_active = 0, eso daba 12.711 mediciones
    # de JOB-07 reportadas como ceguera -- el 100% de las suyas -- por algo que
    # esta apagado a proposito.
    #
    # Lo que NO puede pasar es que esto tape la ceguera de verdad: en srv2 hay
    # 13 trabajos activos y el ledger igual de vacio, y ahi UNKNOWN es la
    # respuesta correcta. Por eso se prueban los dos lados.
    # ------------------------------------------------------------------
    job_apagado = {"job_id": 7, "client_id": "t", "is_active": False,
                   "last_run_at": None, "comando": "x", "script": "x.py"}

    for nombre, fn in (("JOB-06", checks_jobs._job06), ("JOB-07", checks_jobs._job07)):
        out = fn(ctx_job06({}, job_apagado))
        r.append(ok(f"{nombre}: un trabajo desactivado NO es ceguera",
                    out.state is State.DISABLED_EXPECTED, out))
        r.append(ok(f"{nombre}: y dice que esta desactivado",
                    out.actual.get("activo") is False, out.actual))

        # El lado que importa no romper.
        out = fn(ctx_job06({}, job_nunca))
        r.append(ok(f"{nombre}: con el trabajo ACTIVO y sin ledger, sigue UNKNOWN",
                    out.state is State.UNKNOWN, out))

    # Y las dos cegueras se distinguen por su codigo, porque llevan a acciones
    # distintas: una se arregla cuando vuelve la base, la otra cuando alguien
    # escriba el ledger.
    sin_base = ctx_de(conn, deps_extra={"_nac_cache": {
        "corridas": datos.Respuesta(None, EvidenceQuality.NONE, "nada", "la base no responde"),
        "jobs": datos.Respuesta([job_nunca], EvidenceQuality.FULL, "back")}},
        recurso=Resource(resource_id="job:t:7", host_id="t", kind="job",
                         scope={"job_id": 7}))
    out = checks_jobs._job07(sin_base)
    r.append(ok("JOB-07 sin base dice SIN_BASE_PARA_LEER_CORRIDAS",
                out.reason_code == "SIN_BASE_PARA_LEER_CORRIDAS", out))
    out = checks_jobs._job07(ctx_job06({}, job_nunca))
    r.append(ok("y con base pero sin ledger dice LEDGER_SIN_CORRIDAS",
                out.reason_code == "LEDGER_SIN_CORRIDAS", out))
    r.append(ok("o sea que las dos cegueras NO comparten codigo",
                "SIN_BASE_PARA_LEER_CORRIDAS" != "LEDGER_SIN_CORRIDAS"))


    out = checks_jobs._job06(ctx_de(
        conn, deps_extra={"_nac_cache": {
            "corridas": datos.Respuesta(None, EvidenceQuality.NONE, "nada", "sin base")}},
        recurso=Resource(resource_id="job:t:7", host_id="t", kind="job",
                         scope={"job_id": 7})))
    r.append(ok("y si no se pudo leer el ledger, tampoco dice OK",
                out.state is State.UNKNOWN, out))

    # ------------------------------------------------------------------
    # El gate de la conexion compartida
    #
    # Que un control quede ciego no se ve: devuelve UNKNOWN y el ciclo sigue.
    # Por eso esto se prueba en los dos sentidos Y ademas se verifica, leyendo
    # el codigo, que ningun control consulte la base sin haberla pedido.
    # ------------------------------------------------------------------
    from sysop.adapters.apps import plataforma as app_nac
    from sysop.core.models import CheckDefinition, Hysteresis  # noqa: F401

    def control(cid, deps_decl=()):
        return CheckDefinition(
            check_id=cid, version=1, titulo="x", selector="host",
            expectation_id=None, schedule_sec=30, evaluate=lambda c: None,
            dependencies=deps_decl)

    rec_host = Resource(resource_id="host:t", host_id="t", kind="host")
    rec_db = Resource(resource_id="db:t", host_id="t", kind="db")

    abiertas = []
    _orig_conexion = app_nac.mysql_col.conexion

    class ConexionFalsa:
        def __init__(self):
            abiertas.append(1)

        def __enter__(self):
            return "conn-falsa"

        def __exit__(self, *a):
            return False

    app_nac.mysql_col.conexion = lambda t: ConexionFalsa()
    try:
        # 1) un control que la pide, sobre un recurso que NO es una base
        abiertas.clear()
        deps = {}
        cerrar = app_nac.proveer([(control("QUE-02", ("mysql",)), rec_host)], deps)
        r.append(ok("un control que consulta la base la recibe aunque su recurso sea un host",
                    deps.get("mysql") == "conn-falsa" and len(abiertas) == 1,
                    "deps=%s abiertas=%s" % (deps, len(abiertas))))
        if cerrar:
            cerrar()

        # 2) ninguno la pide: no se abre nada
        abiertas.clear()
        deps = {}
        cerrar = app_nac.proveer([(control("OPS-10"), rec_host)], deps)
        r.append(ok("si nadie la pide, no se abre una conexion de gusto",
                    "mysql" not in deps and not abiertas,
                    "deps=%s abiertas=%s" % (deps, len(abiertas))))
        if cerrar:
            cerrar()

        # 3) al planificar todavia no se sabe: se abre igual
        abiertas.clear()
        deps = {}
        cerrar = app_nac.proveer(None, deps)
        r.append(ok("al planificar (controles=None) se abre igual",
                    deps.get("mysql") == "conn-falsa", "deps=%s" % (deps,)))
        if cerrar:
            cerrar()

        # 4) uno solo que la pida alcanza para todo el ciclo
        abiertas.clear()
        deps = {}
        cerrar = app_nac.proveer(
            [(control("OPS-10"), rec_host), (control("OPS-01"), rec_host),
             (control("DB-02", ("mysql",)), rec_db)], deps)
        r.append(ok("con un solo control que la pida se abre UNA sola conexion",
                    deps.get("mysql") == "conn-falsa" and len(abiertas) == 1,
                    "abiertas=%s" % (len(abiertas),)))
        if cerrar:
            cerrar()
    finally:
        app_nac.mysql_col.conexion = _orig_conexion

    # 5) EL QUE IMPORTA A FUTURO: que nadie consulte la base sin declararlo.
    #    Se lee el codigo y se compara contra lo declarado, asi un control nuevo
    #    que se olvide de pedirla no se descubre en produccion tres semanas
    #    despues, cuando alguien note que siempre da UNKNOWN.
    import ast, os
    base = os.path.dirname(app_nac.__file__)
    arboles, directo, llamadas = {}, {}, {}
    for arch in os.listdir(base):
        if not arch.endswith(".py") or arch == "__init__.py":
            continue
        mod = arch[:-3]
        arboles[mod] = ast.parse(open(os.path.join(base, arch)).read())
        for f in ast.walk(arboles[mod]):
            if isinstance(f, ast.FunctionDef):
                directo[(mod, f.name)] = any(
                    isinstance(x, ast.Constant) and x.value == "mysql"
                    for x in ast.walk(f))
                ll = set()
                for x in ast.walk(f):
                    if isinstance(x, ast.Call):
                        fn = x.func
                        if isinstance(fn, ast.Name):
                            ll.add((mod, fn.id))
                        elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                            ll.add((fn.value.id, fn.attr))
                llamadas[(mod, f.name)] = ll

    def usa_base(clave, visto=None):
        visto = visto or set()
        if clave in visto:
            return False
        visto.add(clave)
        if directo.get(clave):
            return True
        return any(c in directo and usa_base(c, visto) for c in llamadas.get(clave, ()))

    faltan, sobran = [], []
    for mod, arbol in arboles.items():
        for nodo in ast.walk(arbol):
            if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Name)
                    and nodo.func.id == "CheckDefinition"):
                continue
            cid = ev = None
            declarada = False
            for kw in nodo.keywords:
                if kw.arg == "check_id" and isinstance(kw.value, ast.Constant):
                    cid = kw.value.value
                if kw.arg == "evaluate" and isinstance(kw.value, ast.Name):
                    ev = kw.value.id
                if kw.arg == "dependencies":
                    declarada = any(isinstance(x, ast.Constant) and x.value == "mysql"
                                    for x in ast.walk(kw.value))
            if not cid or not ev:
                continue
            necesita = usa_base((mod, ev))
            # DB-01 es la excepcion legitima: su trabajo ES probar si se puede
            # conectar, asi que abre la suya y no debe declarar la compartida.
            if necesita and not declarada and cid != "DB-01":
                faltan.append(cid)
            if declarada and not necesita:
                sobran.append(cid)

    r.append(ok("ningun control consulta la base sin declarar dependencies=('mysql',)",
                not faltan, "les falta: %s" % (sorted(faltan),)))
    r.append(ok("ninguno la declara de gusto (abriria una conexion que no usa)",
                not sobran, "sobran: %s" % (sorted(sobran),)))

    shutil.rmtree(d, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
