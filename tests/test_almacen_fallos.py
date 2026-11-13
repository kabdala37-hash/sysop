"""El observador sigue avisando aunque se rompa su almacen.

Cada escenario sale de una prueba real del 2026-09-14 contra el codigo de ese
dia (`.docs/plan_sysop_resiliencia/`), donde el resultado era:

    disco lleno / sin escritura   daemon vivo y MUDO: la alerta critica no salia
    base corrupta                 daemon, informe y bot muertos, sin un correo
    base borrada en caliente      informe falso "OBSERVADOR CAIDO", alerta doble
    base bloqueada                ticks de 30 s, alerta minutos tarde
    base sin escritura            una llamada paga al modelo por tick

Los escenarios del daemon corren el `Daemon.arrancar()` real, con ticks de 1 s,
SMTP falso y el almacen roto de verdad (archivo corrupto, conexion de solo
lectura, archivo borrado, directorio sin permiso, lock de otro proceso).

Tarda alrededor de un minuto y medio.
"""
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sysop import settings

logging.disable(logging.CRITICAL)

from sysop import cli, daemon as dmn, emergencia as emg  # noqa: E402
from sysop import diagnostico as dg  # noqa: E402
from sysop.bot import daemon as bot_mod  # noqa: E402
from sysop.core import incidents  # noqa: E402
from sysop.core.models import (CheckResult, EvidenceQuality, Severity, State)  # noqa: E402
from sysop.llm import gateway  # noqa: E402
from sysop.notify import smtp  # noqa: E402
from sysop.store import db, registro  # noqa: E402

PY = sys.executable
RAIZ = Path(__file__).resolve().parent.parent

LAB = """
version: 1
host: lab
installation: lab
rol: laboratorio
systemd:
  esperadas_activas:
    - acme-lab-inexistente.service
  esperadas_detenidas: []
  ignoradas: []
filesystems:
  descubrir: false
alertas:
  urgentes: true
fuentes:
  correo:
    tipo: archivo
    host: 127.0.0.1
    port: 2525
    secure: false
    user: lab@example.com
    password: x
  destinatarios:
    tipo: archivo
    lista: [lab@example.com]
"""


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


# ---------------------------------------------------------------------------
# Entorno de laboratorio
# ---------------------------------------------------------------------------

ENVIOS = []


def enviar_falso(cuenta, msg, timeout_sec=30.0):
    ENVIOS.append({"asunto": str(msg["Subject"]), "cuerpo": msg.get_body(("html",)).get_content()})
    return smtp.Envio(ok=True, message_id=msg["Message-ID"], respuesta="250 falso")


smtp.enviar = enviar_falso


def entorno():
    d = Path(tempfile.mkdtemp())
    (d / "cfg" / "hosts").mkdir(parents=True)
    (d / "cfg" / "hosts" / "lab.yaml").write_text(LAB)
    settings.STATE_DIR = d / "state"
    settings.STATE_DIR.mkdir()
    settings.DB_PATH = settings.STATE_DIR / "sysop.db"
    settings.LOG_DIR = d / "log"
    settings.CONFIG_DIR = d / "cfg"
    settings.HOST_ID = "lab"
    settings.INSTALLATION = "lab"
    registro.configurar("test", settings.STATE_DIR)
    ENVIOS.clear()
    return d


def urgentes():
    return [e for e in ENVIOS if "URGENTE" in e["asunto"]]


def del_almacen():
    return [e for e in ENVIOS if "almacen del observador" in e["asunto"]]


def correr_daemon(segundos, hook=None, antes=None):
    """`Daemon.arrancar()` de verdad, con ticks de 1 s y todos los controles vencidos."""
    d = dmn.Daemon()
    duraciones = []
    orig = d._un_tick

    def tick_medido():
        t = time.monotonic()
        try:
            orig()
        finally:
            duraciones.append(time.monotonic() - t)
    d._un_tick = tick_medido
    d.sched.vencidos = lambda pares, ahora=None: list(pares)
    if antes:
        antes(d)
    threading.Timer(segundos, d._parar.set).start()
    if hook:
        threading.Thread(target=hook, args=(d,), daemon=True).start()
    rc = d.arrancar()
    return d, rc, duraciones


def esperar(cond, max_seg=10.0):
    fin = time.monotonic() + max_seg
    while time.monotonic() < fin:
        if cond():
            return True
        time.sleep(0.1)
    return False


def md5(p):
    return hashlib.md5(Path(p).read_bytes()).hexdigest()


def main():
    r = []
    dmn.TICK_SEG = 1
    dmn.CADA_REABRIR = 3
    dmn.CADA_CONFIG = 1000
    dmn.CADA_INVENTARIO = 1000
    aviso_original = emg.AVISO_TRAS_SEG

    # =======================================================================
    print("\n-- registro en archivo --")
    d = entorno()
    t0 = datetime.now(timezone.utc) - timedelta(seconds=1)

    r.append(ok("escribe una linea JSON por evento en el archivo del dia",
                registro.anotar("prueba", incident_id="INC-a1b2c3d4-20260914T1930",
                                mensaje="tel 11 4444-5555")))
    archivo = settings.STATE_DIR / "registro" / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
    linea = json.loads(archivo.read_text().splitlines()[-1])
    r.append(ok("los ids NO pasan por la redaccion (el dedupe depende de eso)",
                linea["incident_id"] == "INC-a1b2c3d4-20260914T1930", linea))
    r.append(ok("el texto libre SI se redacta", "4444" not in linea["mensaje"], linea))
    r.append(ok("el archivo queda 0600", (archivo.stat().st_mode & 0o777) == 0o600,
                oct(archivo.stat().st_mode)))

    guion = ("import sys; sys.path.insert(0, %r)\n"
             "from sysop.store import registro\n"
             "registro.configurar(sys.argv[1], %r)\n"
             "for i in range(2000): registro.anotar('carga', i=i, relleno='x' * (i %% 700))\n"
             % (str(RAIZ), str(settings.STATE_DIR)))
    procs = [subprocess.Popen([PY, "-c", guion, f"p{n}"]) for n in range(3)]
    for p in procs:
        p.wait()
    carga = registro.leer(t0, ("carga",))
    r.append(ok("tres procesos escribiendo a la vez: 6000 lineas, ninguna rota",
                len(carga) == 6000, len(carga)))

    with open(archivo, "ab") as f:
        f.write(b'{"at": "2099-01-01T00:00:00+00:00", "evento": "cort\n')
    registro.anotar("despues_del_corte")
    try:
        leido = registro.leer(t0)
        r.append(ok("una linea cortada por un corte de luz no impide leer el resto",
                    any(e["evento"] == "despues_del_corte" for e in leido)
                    and len([e for e in leido if e["evento"] == "carga"]) == 6000))
    except Exception as e:  # noqa: BLE001
        r.append(ok("una linea cortada por un corte de luz no impide leer el resto", False, e))

    # El archivo del dia ya existe: sacarle permiso al directorio solo no
    # alcanza, un append a un archivo existente no lo necesita.
    os.chmod(archivo, 0o400)
    os.chmod(settings.STATE_DIR / "registro", 0o500)
    escrito = registro.anotar("con_disco_lleno", incident_id="X")
    r.append(ok("sin poder escribir, anotar no lanza y dice que no quedo en disco",
                escrito is False))
    r.append(ok("  pero el mismo proceso lo sigue leyendo desde memoria",
                len(registro.leer(t0, ("con_disco_lleno",))) == 1))
    os.chmod(settings.STATE_DIR / "registro", 0o700)
    os.chmod(archivo, 0o600)
    registro.anotar("vuelve")
    r.append(ok("  y al volver el disco se escribe lo pendiente, en orden",
                "con_disco_lleno" in archivo.read_text()
                and archivo.read_text().index("con_disco_lleno") < archivo.read_text().index('"vuelve"')))

    viejo = settings.STATE_DIR / "registro" / "2020-01-01.jsonl"
    viejo.write_text("{}\n")
    r.append(ok("podar borra los dias viejos y deja el de hoy",
                registro.podar(90) == 1 and not viejo.exists() and archivo.exists()))

    registro._estado = None
    r.append(ok("sin configurar no escribe ni lee (un test como root no toca produccion)",
                registro.anotar("x") is False and registro.leer(t0) == []
                and registro.edad_latido() is None))
    registro.configurar("test", settings.STATE_DIR)

    # --- dedupe de alertas ---
    registro.anotar("correo_enviado", tipo="urgente", periodo="INC-aaaa1111-20260914T1000",
                    fp="aaaa1111", asunto="x", via="bandeja")
    r.append(ok("una alerta enviada y no cerrada esta vigente",
                registro.alerta_vigente("aaaa1111") is not None))
    registro.anotar("incidente_cerrado", incident_id="INC-aaaa1111-20260914T1000",
                    fp="aaaa1111", motivo="estable")
    r.append(ok("despues del cierre ya no: una recurrencia legitima se avisa",
                registro.alerta_vigente("aaaa1111") is None))
    shutil.rmtree(d, ignore_errors=True)

    # =======================================================================
    print("\n-- el almacen: clasificar, sondear, abrir --")
    d = entorno()
    alm = db.abrir(settings.DB_PATH)
    r.append(ok("una base sana abre sin falla", alm.conn is not None and alm.falla is None))
    r.append(ok("la sonda de una base sana no da falla",
                db.sondear(alm.conn, settings.DB_PATH, alm.inodo) is None))

    ro = sqlite3.connect(f"file:{settings.DB_PATH}?mode=ro", uri=True, isolation_level=None)
    f = db.sondear(ro, settings.DB_PATH, alm.inodo)
    r.append(ok("sin escritura -> SIN_ESCRITURA (mensaje real de sqlite)",
                f is not None and f.clase == db.SIN_ESCRITURA, f))

    bloqueo = subprocess.Popen([PY, "-c",
        "import sqlite3,time,sys; c=sqlite3.connect(sys.argv[1], isolation_level=None); "
        "c.execute('BEGIN EXCLUSIVE'); print('ok', flush=True); time.sleep(6)",
        str(settings.DB_PATH)], stdout=subprocess.PIPE, text=True)
    bloqueo.stdout.readline()
    t = time.monotonic()
    f = db.sondear(alm.conn, settings.DB_PATH, alm.inodo)
    espera = time.monotonic() - t
    r.append(ok("bloqueada -> BLOQUEADO", f is not None and f.clase == db.BLOQUEADO, f))
    r.append(ok("  y la sonda espera ~1 s, no los 10 s que trababan el tick",
                espera < 2.5, f"{espera:.2f}s"))
    bloqueo.kill()
    bloqueo.wait()

    alm.conn.close()
    otro = settings.DB_PATH.with_name("otro.db")
    shutil.copy(settings.DB_PATH, otro)
    inodo_viejo = alm.inodo
    os.replace(otro, settings.DB_PATH)
    c = db.connect(settings.DB_PATH)
    f = db.sondear(c, settings.DB_PATH, inodo_viejo)
    r.append(ok("reemplazada por otro archivo -> PERDIDO", f is not None and f.clase == db.PERDIDO, f))
    for s in ("", "-wal", "-shm"):
        Path(str(settings.DB_PATH) + s).unlink(missing_ok=True)
    f = db.sondear(c, settings.DB_PATH, db.inodo(settings.DB_PATH))
    r.append(ok("borrada -> PERDIDO (sqlite sigue escribiendo sin dar error)",
                f is not None and f.clase == db.PERDIDO, f))
    c.close()

    alm = db.abrir(settings.DB_PATH, crear=False)
    r.append(ok("crear=False con la base inexistente -> NO_ABRE",
                alm.conn is None and alm.falla and alm.falla.clase == db.NO_ABRE, alm.falla))
    r.append(ok("  y NO la crea (el informe creaba una vacia y mentia)",
                not settings.DB_PATH.exists()))

    settings.DB_PATH.write_bytes(os.urandom(8192))
    antes = md5(settings.DB_PATH)
    alm = db.abrir(settings.DB_PATH, crear=False)
    r.append(ok("archivo basura -> CORRUPTO", alm.falla and alm.falla.clase == db.CORRUPTO, alm.falla))
    r.append(ok("  sin rotar no se toca el archivo", md5(settings.DB_PATH) == antes))

    Uso = namedtuple("Uso", "total used free")
    real_disk = db.shutil.disk_usage
    db.shutil.disk_usage = lambda p: Uso(10, 10, 0)
    alm = db.abrir(settings.DB_PATH, rotar=True)
    db.shutil.disk_usage = real_disk
    r.append(ok("corrupta sin espacio para la copia: no se aparta y se dice por que",
                alm.conn is None and "no se aparto" in (alm.falla.mensaje if alm.falla else ""),
                alm.falla))
    r.append(ok("  el archivo queda intacto", md5(settings.DB_PATH) == antes))

    alm = db.abrir(settings.DB_PATH, rotar=True)
    rotas = list(settings.STATE_DIR.glob("sysop.db.roto-*"))
    r.append(ok("corrupta con espacio: se aparta y se arranca con una nueva",
                alm.conn is not None and alm.falla is None and alm.rotada and len(rotas) == 1,
                (alm.falla, rotas)))
    alm.conn.close()
    settings.DB_PATH.write_bytes(os.urandom(8192))
    alm = db.abrir(settings.DB_PATH, rotar=True)
    r.append(ok("  una sola copia: la rota anterior se reemplaza",
                len(list(settings.STATE_DIR.glob("sysop.db.roto-*"))) == 1))
    alm.conn.close()

    # Una pagina rota en el medio: abrir anda, quick_check no.
    for s in ("", "-wal", "-shm"):
        Path(str(settings.DB_PATH) + s).unlink(missing_ok=True)
    c = db.connect(settings.DB_PATH)
    db.init_schema(c)
    c.executemany("INSERT INTO check_result (check_id, resource_id, observed_at, state, "
                  "severity) VALUES ('X','r','2026-09-14T00:00:00+00:00','OK','INFO')",
                  [()] * 30000)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    with open(settings.DB_PATH, "r+b") as fh:
        fh.seek(settings.DB_PATH.stat().st_size // 2)
        fh.write(os.urandom(4096))
    sin_verificar = db.abrir(settings.DB_PATH)
    verificada = db.abrir(settings.DB_PATH, verificar=True)
    r.append(ok("una pagina rota abre bien sin verificar (por eso se verifica)",
                sin_verificar.falla is None))
    r.append(ok("  y con quick_check sale CORRUPTO",
                verificada.falla is not None and verificada.falla.clase == db.CORRUPTO,
                verificada.falla))
    if sin_verificar.conn:
        sin_verificar.conn.close()
    shutil.rmtree(d, ignore_errors=True)

    # =======================================================================
    print("\n-- gasto del modelo con la base sin escritura --")
    d = entorno()
    c = db.connect(settings.DB_PATH)
    db.init_schema(c)
    res = CheckResult(check_id="QUE-01", resource_id="cola:x",
                      observed_at=datetime.now(timezone.utc), state=State.FAILED,
                      severity=Severity.MAJOR, reason_code="ROTO", expected={}, actual={},
                      evidence_quality=EvidenceQuality.FULL, evidence_refs=[], rule_version=1)
    iid = incidents.abrir(c, res)
    ro = sqlite3.connect(f"file:{settings.DB_PATH}?mode=ro", uri=True, isolation_level=None)
    ro.row_factory = sqlite3.Row
    r.append(ok("el incidente sin diagnostico esta pendiente",
                any(p["incident_id"] == iid for p in dg.pendientes(ro))))

    class AgenteFalso:
        utilizable = True
        modelo = "m"
        vendor = "gemini"
        prompt = "p"
        api_key = "k"
        base_url = ""
    llamadas = []
    real_llamar = gateway._llamar

    def llamar_falso(agente, paquete, timeout, **_):
        llamadas.append(1)
        return ('{"es_real": true, "confianza": "alta", "causa": "c", "accion": "a", '
                '"descarte": ""}', {"entrada": 1_000_000, "salida": 0})
    gateway._llamar = llamar_falso
    for _ in range(5):
        # Lo que hace `diagnostico.revisar` en cada tick: pendientes y, por
        # cada uno, una llamada. Con la base de solo lectura nada se guarda.
        for p in dg.pendientes(ro):
            gateway.diagnosticar(ro, AgenteFalso(), {}, p["incident_id"],
                                 {"presupuesto_diario_usd": 100.0})
    gateway._llamar = real_llamar
    r.append(ok("5 ticks con la base sin escritura: como mucho MAX_INTENTOS llamadas pagas "
                "(antes: una por tick)", len(llamadas) == dg.MAX_INTENTOS, len(llamadas)))
    r.append(ok("llm_call no crecio (la base no escribe)...",
                c.execute("SELECT COUNT(*) FROM llm_call").fetchone()[0] == 0))
    r.append(ok("  pero el gasto del dia lo ve igual, desde el registro",
                gateway.gastado_hoy(ro, "diagnostico") >= 0.29, gateway.gastado_hoy(ro, "diagnostico")))
    c.close()
    ro.close()
    shutil.rmtree(d, ignore_errors=True)

    # =======================================================================
    print("\n-- daemon: base sana (linea base) --")
    d = entorno()
    dm, rc, dur = correr_daemon(6)
    r.append(ok("arranca y termina bien", rc == 0, rc))
    r.append(ok("el critico sale UNA vez, por la bandeja", len(urgentes()) == 1,
                [e["asunto"] for e in ENVIOS]))
    r.append(ok("  sin marca de emergencia", urgentes() and "sin almacen" not in urgentes()[0]["asunto"]))
    r.append(ok("el latido existe y esta fresco",
                registro.edad_latido() is not None and registro.edad_latido() < 5))
    r.append(ok("el envio quedo en el registro con su fingerprint",
                any(e.get("tipo") == "urgente" and e.get("fp")
                    for e in registro.leer(datetime.now(timezone.utc) - timedelta(minutes=5),
                                           ("correo_enviado",)))))
    shutil.rmtree(d, ignore_errors=True)

    # =======================================================================
    print("\n-- daemon: la base deja de escribir (el caso del disco lleno) --")
    d = entorno()
    emg.AVISO_TRAS_SEG = 2
    estados = {}

    def sin_escritura(dm):
        esperar(lambda: dm.ctx is not None)
        ro = sqlite3.connect(f"file:{settings.DB_PATH}?mode=ro", uri=True,
                             isolation_level=None, check_same_thread=False)
        ro.row_factory = sqlite3.Row
        dm.ctx.conn = ro
        time.sleep(7)
        estados["urgentes_durante"] = len(urgentes())
        rw = db.connect(settings.DB_PATH)
        rw.close()
        rw = sqlite3.connect(str(settings.DB_PATH), isolation_level=None, check_same_thread=False)
        rw.row_factory = sqlite3.Row
        dm.ctx.conn = rw
    dm, rc, dur = correr_daemon(14, hook=sin_escritura)
    emg.AVISO_TRAS_SEG = aviso_original
    r.append(ok("no se cae", rc == 0, rc))
    r.append(ok("LA ALERTA CRITICA SALE sin la base (antes: vivo y mudo)",
                estados.get("urgentes_durante", 0) >= 1 and "sin almacen" in urgentes()[0]["asunto"],
                [e["asunto"] for e in ENVIOS]))
    r.append(ok("sale el aviso de que el almacen fallo, con la clase",
                any("SIN_ESCRITURA" in e["asunto"] for e in del_almacen()),
                [e["asunto"] for e in ENVIOS]))
    r.append(ok("al volver la base sale el aviso de recuperado",
                any("recuperado" in e["asunto"] for e in del_almacen())))
    r.append(ok("y la alerta NO se repite cuando el motor normal abre el incidente",
                len(urgentes()) == 1, [e["asunto"] for e in urgentes()]))
    r.append(ok("ningun tick se trabo", max(dur) < 3.0, f"{max(dur):.2f}s"))
    shutil.rmtree(d, ignore_errors=True)

    # =======================================================================
    print("\n-- daemon: base corrupta al arrancar --")
    d = entorno()
    settings.DB_PATH.write_bytes(os.urandom(8192))
    dm, rc, dur = correr_daemon(6)
    r.append(ok("no se muere (antes: exit 1 y systemd lo daba por perdido)", rc == 0, rc))
    r.append(ok("aparta la corrupta y arranca con una nueva",
                len(list(settings.STATE_DIR.glob("sysop.db.roto-*"))) == 1))
    r.append(ok("avisa que la aparto", any("apartada" in e["asunto"] for e in del_almacen()),
                [e["asunto"] for e in ENVIOS]))
    r.append(ok("y el critico sale por la bandeja de la base nueva", len(urgentes()) == 1,
                [e["asunto"] for e in urgentes()]))
    shutil.rmtree(d, ignore_errors=True)

    print("\n-- daemon: base corrupta y sin espacio para apartarla --")
    d = entorno()
    emg.AVISO_TRAS_SEG = 2
    settings.DB_PATH.write_bytes(os.urandom(8192))
    antes = md5(settings.DB_PATH)
    db.shutil.disk_usage = lambda p: Uso(10, 10, 0)
    dm, rc, dur = correr_daemon(8)
    db.shutil.disk_usage = real_disk
    emg.AVISO_TRAS_SEG = aviso_original
    r.append(ok("no se muere", rc == 0, rc))
    r.append(ok("el critico sale igual, por el camino de emergencia",
                len(urgentes()) == 1 and "sin almacen" in urgentes()[0]["asunto"],
                [e["asunto"] for e in ENVIOS]))
    r.append(ok("avisa CORRUPTO", any("CORRUPTO" in e["asunto"] for e in del_almacen()),
                [e["asunto"] for e in ENVIOS]))
    r.append(ok("  y el correo dice por que no la aparto",
                any("no se aparto" in e["cuerpo"] for e in del_almacen())))
    r.append(ok("la base corrupta queda intacta", md5(settings.DB_PATH) == antes))
    shutil.rmtree(d, ignore_errors=True)

    # =======================================================================
    print("\n-- daemon: borran la base con el daemon corriendo --")
    d = entorno()

    def borrar(dm):
        esperar(lambda: len(urgentes()) >= 1, 8)
        for s in ("", "-wal", "-shm"):
            Path(str(settings.DB_PATH) + s).unlink(missing_ok=True)
    dm, rc, dur = correr_daemon(10, hook=borrar)
    r.append(ok("lo detecta y recrea la base", settings.DB_PATH.exists()
                and any("recreada" in e["asunto"] for e in del_almacen()),
                [e["asunto"] for e in ENVIOS]))
    r.append(ok("la alerta NO sale dos veces (antes: dos correos del mismo incidente)",
                len(urgentes()) == 1, [e["asunto"] for e in urgentes()]))

    # El informe corrido ahora, con la base nueva casi vacia y el latido fresco.
    ENVIOS.clear()
    cli.main(["informe", "--forzar"])
    r.append(ok("el informe NO dice 'OBSERVADOR CAIDO' con el daemon vivo",
                ENVIOS and "OBSERVADOR CAIDO" not in ENVIOS[-1]["asunto"],
                [e["asunto"] for e in ENVIOS]))
    registro.configurar("test", settings.STATE_DIR)
    shutil.rmtree(d, ignore_errors=True)

    # =======================================================================
    print("\n-- daemon: directorio de estado sin permiso y sin base --")
    d = entorno()
    emg.AVISO_TRAS_SEG = 2
    (settings.STATE_DIR / "registro").mkdir(mode=0o700)
    os.chmod(settings.STATE_DIR, 0o500)
    dm, rc, dur = correr_daemon(8)
    os.chmod(settings.STATE_DIR, 0o700)
    emg.AVISO_TRAS_SEG = aviso_original
    r.append(ok("no se muere (antes: 'unable to open database file' y exit 1)", rc == 0, rc))
    r.append(ok("el critico sale por emergencia",
                len(urgentes()) == 1 and "sin almacen" in urgentes()[0]["asunto"],
                [e["asunto"] for e in ENVIOS]))
    r.append(ok("avisa NO_ABRE", any("NO_ABRE" in e["asunto"] for e in del_almacen()),
                [e["asunto"] for e in ENVIOS]))
    shutil.rmtree(d, ignore_errors=True)

    # =======================================================================
    print("\n-- daemon: otro proceso bloquea la base 6 s --")
    d = entorno()

    def bloquear(dm):
        esperar(lambda: len(urgentes()) >= 1, 8)
        p = subprocess.Popen([PY, "-c",
            "import sqlite3,time,sys; c=sqlite3.connect(sys.argv[1], isolation_level=None); "
            "c.execute('BEGIN EXCLUSIVE'); print('ok', flush=True); time.sleep(6)",
            str(settings.DB_PATH)], stdout=subprocess.PIPE, text=True)
        p.stdout.readline()
        p.wait()
    dm, rc, dur = correr_daemon(14, hook=bloquear)
    r.append(ok("ningun tick pasa de 3 s (antes: 30 s por tick)", max(dur) < 3.0, f"{max(dur):.2f}s"))
    r.append(ok("un lock corto no manda correo del almacen", not del_almacen(),
                [e["asunto"] for e in ENVIOS]))
    r.append(ok("ni repite la alerta", len(urgentes()) == 1, [e["asunto"] for e in urgentes()]))
    hst01 = sqlite3.connect(settings.DB_PATH).execute(
        "SELECT COUNT(*) FROM check_result WHERE check_id='HST-01' "
        "AND reason_code='AGENTE_INTERRUMPIDO'").fetchone()[0]
    r.append(ok("sin 'observador interrumpido' falso al volver", hst01 == 0, hst01))
    shutil.rmtree(d, ignore_errors=True)

    # =======================================================================
    print("\n-- informe de cron sin almacen --")
    d = entorno()
    registro.anotar("incidente_abierto", incident_id="INC-bbbb2222-20260914T0900",
                    fp="bbbb2222", check_id="SVC-01", resource_id="systemd:el-back.service",
                    severity="critical", reason_code="NO_ACTIVA", actual={})
    settings.DB_PATH.write_bytes(os.urandom(8192))
    antes = md5(settings.DB_PATH)
    rc = cli.main(["informe"])
    r.append(ok("con la base corrupta el informe SALE (antes: traceback y ningun correo)",
                rc == 0 and ENVIOS and "ALMACEN NO DISPONIBLE" in ENVIOS[-1]["asunto"],
                (rc, [e["asunto"] for e in ENVIOS])))
    r.append(ok("  con los incidentes que dice el registro",
                ENVIOS and "systemd:el-back.service" in ENVIOS[-1]["cuerpo"]))
    r.append(ok("  y el informe no toca la base corrupta", md5(settings.DB_PATH) == antes))
    n = len(ENVIOS)
    cli.main(["informe"])
    r.append(ok("el mismo periodo no sale dos veces", len(ENVIOS) == n, len(ENVIOS)))

    registro.configurar("test", settings.STATE_DIR)
    settings.DB_PATH.unlink()
    ENVIOS.clear()
    cli.main(["informe", "--forzar"])
    r.append(ok("con la base borrada sale degradado y NO crea una vacia",
                ENVIOS and "ALMACEN NO DISPONIBLE" in ENVIOS[-1]["asunto"]
                and not settings.DB_PATH.exists(), [e["asunto"] for e in ENVIOS]))

    # --- el bot ---
    registro.configurar("test", settings.STATE_DIR)
    b = bot_mod.Bot(ruta_db=settings.DB_PATH)
    conn, falla = b._almacen()
    r.append(ok("el bot sin base no explota: devuelve la falla",
                conn is None and falla is not None and falla.clase == db.NO_ABRE, falla))
    texto = bot_mod._texto_sin_almacen(falla)
    r.append(ok("  y contesta con lo que dice el registro",
                "systemd:el-back.service" in texto and "no esta disponible" in texto, texto))
    shutil.rmtree(d, ignore_errors=True)

    revision(r, Uso, real_disk, aviso_original)

    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


def revision(r, Uso, real_disk, aviso_original):
    """Lo que encontraron la revision adversarial y el arnes con tick real.

    Cada caso rompia algo del codigo de la primera version de este cambio.
    """
    from sysop import informe as informe_mod                 # noqa: PLC0415
    from sysop.notify import outbox                          # noqa: PLC0415

    print("\n-- revision: corrupta y sin permiso para apartarla --")
    d = entorno()
    emg.AVISO_TRAS_SEG = 2
    (settings.STATE_DIR / "registro").mkdir(mode=0o700)
    settings.DB_PATH.write_bytes(os.urandom(8192))
    os.chmod(settings.STATE_DIR, 0o500)
    try:
        dm, rc, dur = correr_daemon(8)
    except Exception as e:  # noqa: BLE001
        rc = f"EXCEPCION {type(e).__name__}: {e}"
    os.chmod(settings.STATE_DIR, 0o700)
    emg.AVISO_TRAS_SEG = aviso_original
    r.append(ok("no se muere con el rename sin permiso (antes: PermissionError al arrancar)",
                rc == 0, rc))
    r.append(ok("  el critico sale por emergencia",
                len(urgentes()) == 1 and "sin almacen" in urgentes()[0]["asunto"],
                [e["asunto"] for e in ENVIOS]))
    r.append(ok("  y el aviso dice que no se pudo apartar",
                any("no se pudo apartar" in e["cuerpo"] for e in del_almacen()),
                [e["asunto"] for e in ENVIOS]))
    shutil.rmtree(d, ignore_errors=True)

    print("\n-- revision: la conexion queda cerrada --")
    d = entorno()

    despues = []

    def cerrar_en_el_tick(dm):
        # sqlite no deja cerrar desde otro hilo: se cierra desde el propio tick.
        orig = dm._almacen_sano

        def envuelto():
            if urgentes() and not despues:
                dm.ctx.conn.close()
                despues.append("cerrada")
            sano = orig()
            if despues:
                despues.append(sano)
            return sano
        dm._almacen_sano = envuelto
    dm, rc, dur = correr_daemon(10, antes=cerrar_en_el_tick)
    r.append(ok("con la conexion cerrada reabre y vuelve a sano (antes: SIN_ESCRITURA para siempre)",
                len(despues) > 2 and despues[-1] is True and not dm.emergencia.activa, despues))
    shutil.rmtree(d, ignore_errors=True)

    print("\n-- revision: la sonda pasa pero no se puede encolar la alerta --")
    d = entorno()
    real_encolar = outbox.encolar

    def encolar_roto(*a, **k):
        raise sqlite3.DatabaseError("database disk image is malformed")
    outbox.encolar = encolar_roto
    dm, rc, dur = correr_daemon(10)
    outbox.encolar = real_encolar
    r.append(ok("la alerta sale igual, forzando el modo degradado (antes: 11 ticks mudo)",
                len(urgentes()) == 1 and "sin almacen" in urgentes()[0]["asunto"],
                [e["asunto"] for e in ENVIOS]))
    r.append(ok("  sin corrupcion real no aparta la base",
                not list(settings.STATE_DIR.glob("sysop.db.roto-*"))))
    shutil.rmtree(d, ignore_errors=True)

    print("\n-- revision: la sonda va y viene --")
    d = entorno()
    real_sondear = db.sondear
    vuelta = {"n": 0}

    def sondear_alterna(*a, **k):
        vuelta["n"] += 1
        if vuelta["n"] % 2:
            return db.Falla(db.SIN_ESCRITURA, "va y viene")
        return real_sondear(*a, **k)
    db.sondear = sondear_alterna
    outbox.encolar = encolar_roto
    dm, rc, dur = correr_daemon(10)
    outbox.encolar = real_encolar
    db.sondear = real_sondear
    r.append(ok("con una falla intermitente el critico igual se avisa (antes: cero correos)",
                len(urgentes()) == 1, [e["asunto"] for e in ENVIOS]))
    shutil.rmtree(d, ignore_errors=True)

    print("\n-- revision: lock al arrancar --")
    d = entorno()
    c = db.connect(settings.DB_PATH)
    db.init_schema(c)
    c.close()
    bloqueo = subprocess.Popen([PY, "-c",
        "import sqlite3,time,sys; c=sqlite3.connect(sys.argv[1], isolation_level=None); "
        "c.execute('BEGIN EXCLUSIVE'); print('ok', flush=True); time.sleep(12)",
        str(settings.DB_PATH)], stdout=subprocess.PIPE, text=True)
    bloqueo.stdout.readline()
    dm, rc, dur = correr_daemon(20)
    bloqueo.wait()
    r.append(ok("un lock al arrancar se reintenta en cada tick y sale solo",
                not dm.emergencia.activa and dm.ctx.conn is not None, dm.emergencia.falla))
    r.append(ok("  sin correo del almacen por un lock de segundos", not del_almacen(),
                [e["asunto"] for e in ENVIOS]))
    shutil.rmtree(d, ignore_errors=True)

    print("\n-- revision: base corrupta sin espacio no se re-verifica --")
    d = entorno()
    settings.DB_PATH.write_bytes(os.urandom(8192))
    verificadas = []
    real_una = db._abrir_una_vez

    def contar(path, crear, verificar, espera=10.0):
        if verificar:
            verificadas.append(1)
        return real_una(path, crear, verificar, espera)
    db._abrir_una_vez = contar
    db.shutil.disk_usage = lambda p: Uso(10, 10, 0)
    dm, rc, dur = correr_daemon(10)
    db.shutil.disk_usage = real_disk
    db._abrir_una_vez = real_una
    r.append(ok("quick_check al arrancar y en la primera reapertura, no en cada una",
                len(verificadas) <= 2, len(verificadas)))
    shutil.rmtree(d, ignore_errors=True)

    print("\n-- revision: piezas sueltas --")
    d = entorno()
    raro = settings.STATE_DIR / "a#b?c"
    raro.mkdir()
    c = db.connect(raro / "sysop.db")
    db.init_schema(c)
    c.close()
    antes = sorted(x.name for x in settings.STATE_DIR.rglob("*"))
    alm = db.abrir(raro / "sysop.db", crear=False)
    r.append(ok("crear=False con '#' y '?' en la ruta abre ESA base y no crea otra",
                alm.falla is None and sorted(x.name for x in settings.STATE_DIR.rglob("*"))
                == sorted(set(antes) | {"sysop.db-wal", "sysop.db-shm"}), alm.falla))
    if alm.conn:
        alm.conn.close()

    registro.anotar("correo_enviado", tipo="urgente", periodo="INC-cccc3333-x", fp="cccc3333")
    registro.anotar("incidente_recuperado", incident_id="INC-cccc3333-x", fp="cccc3333")
    r.append(ok("dedupe: recuperado apaga la alerta (la base pudo perderse antes del cierre)",
                registro.alerta_vigente("cccc3333") is None))
    registro.anotar("incidente_reabierto", incident_id="INC-cccc3333-x", fp="cccc3333")
    r.append(ok("dedupe: reabierto la vuelve a prender", registro.alerta_vigente("cccc3333") is not None))

    c = db.connect(settings.DB_PATH)
    db.init_schema(c)
    viejo = incidents.abrir(c, CheckResult(
        check_id="QUE-01", resource_id="cola:vieja", observed_at=datetime.now(timezone.utc),
        state=State.FAILED, severity=Severity.MAJOR, reason_code="A", expected={}, actual={},
        evidence_quality=EvidenceQuality.FULL, evidence_refs=[], rule_version=1))
    time.sleep(0.01)
    nuevo = incidents.abrir(c, CheckResult(
        check_id="QUE-02", resource_id="cola:nueva", observed_at=datetime.now(timezone.utc),
        state=State.FAILED, severity=Severity.MAJOR, reason_code="B", expected={}, actual={},
        evidence_quality=EvidenceQuality.FULL, evidence_refs=[], rule_version=1))
    for _ in range(dg.MAX_INTENTOS):
        registro.anotar("llm_gasto", incident_id=viejo, costo_usd=0.0, ok=False)
    pend = dg.pendientes(c)
    r.append(ok("un incidente agotado en la punta no tapa la cola (antes: lista vacia)",
                [x["incident_id"] for x in pend] == [nuevo], [x["incident_id"] for x in pend]))

    c.execute("INSERT INTO check_result (check_id, resource_id, observed_at, state, severity) "
              "VALUES ('X','r',?,'OK','INFO')",
              ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
    registro.latir()
    caido = informe_mod._observador_caido(c)
    r.append(ok("latido fresco pero sin resultados hace una hora: se avisa (antes: callaba)",
                caido is not None and "no guarda" in caido, caido))

    registro.anotar("incidente_abierto", incident_id="INC-dddd4444-x", fp="dddd4444",
                    check_id="SVC-01", resource_id="systemd:perdido.service",
                    severity="critical", reason_code="NO_ACTIVA")
    registro.anotar("emergencia_abierto", incident_id="EMG-dddd4444-y", fp="dddd4444",
                    check_id="SVC-01", resource_id="systemd:perdido.service",
                    severity="critical", reason_code="NO_ACTIVA")
    abiertos = [i for i in registro.abiertos_segun_registro(
        datetime.now(timezone.utc) - timedelta(hours=1)) if i["fp"] == "dddd4444"]
    r.append(ok("el mismo problema abierto por el motor y por emergencia cuenta una vez",
                len(abiertos) == 1 and abiertos[0]["incident_id"].startswith("INC-"), abiertos))
    extra = informe_mod._solo_en_registro(c)
    r.append(ok("el informe normal ve lo que el registro da por abierto y la base no "
                "(antes: [OK] tras recrear la base)",
                any(i["resource_id"] == "systemd:perdido.service" for i in extra), extra))

    ENVIOS.clear()
    outbox.encolar(c, "urgente", "INC-dddd4444-z", "asunto", "<p>x</p>", ["a@b.c"])
    registro.anotar("correo_enviado", tipo="urgente", periodo="EMG-dddd4444-y",
                    fp="dddd4444", via="directo")
    from sysop.core.fuentes import CuentaSMTP                # noqa: PLC0415
    outbox.despachar(c, CuentaSMTP(host="h", port=1, secure=False, user="u",
                                   password="p", from_addr="f@n.com"))
    estado = c.execute("SELECT state FROM outbox WHERE period_start='INC-dddd4444-z'").fetchone()[0]
    r.append(ok("la bandeja no reenvia una alerta que ya salio por emergencia",
                not ENVIOS and estado == "SENT", (ENVIOS, estado)))
    c.close()

    fake_cd = type("CD", (), {"schedule_sec": 300, "check_id": "JOB-03",
                               "hysteresis": type("H", (), {"abre_tras": 1})()})()
    e = emg.Emergencia()
    e.falla = db.Falla(db.SIN_ESCRITURA, "x")
    ctx = type("Ctx", (), {"cfg": type("Cfg", (), {"installation": "lab"})()})()
    res = CheckResult(check_id="JOB-03", resource_id="job:x", observed_at=datetime.now(timezone.utc),
                      state=State.FAILED, severity=Severity.CRITICAL, reason_code="R",
                      expected={}, actual={}, evidence_quality=EvidenceQuality.FULL,
                      evidence_refs=[], rule_version=1)
    e._abrir(ctx, fake_cd, ("JOB-03", "job:x"), res)
    gracia = list(e._abiertos.values())[0]["gracia"]
    r.append(ok("la gracia para conciliar sale de la cadencia del control (JOB-03 cada 300 s)",
                gracia >= 450, gracia))
    shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
