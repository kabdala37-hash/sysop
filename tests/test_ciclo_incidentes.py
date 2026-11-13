"""El ciclo completo: resultado -> histeresis -> incidente.

Es la bateria que protege lo que hace que el observador no sea ruido. Cada caso
de aca corresponde a una forma concreta de fallar que ya se rompio una vez
durante el desarrollo.
"""
import shutil
import tempfile
from pathlib import Path

from sysop.store import db
from sysop.core import engine, incidents
from sysop.core.models import (CheckDefinition, CheckOutcome, EvidenceQuality,
                               Hysteresis, Resource, Severity, State)

LLENO = (State.FAILED, "FS_CASI_LLENO")
RO    = (State.FAILED, "FS_SOLO_LECTURA")
OTRO  = (State.DEGRADED, "MONTADO_SOLO_LECTURA")
SANO  = (State.OK, "")
DESC  = (State.UNKNOWN, "CHECK_ERROR")


class Escenario:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        self.conn = db.connect(self.dir / "t.db")
        db.init_schema(self.conn)
        self.guion = []
        self.res = Resource(resource_id="mount:/mnt/datos", host_id="h", kind="mount")
        self.cd = CheckDefinition(
            check_id="HST-09", version=1, titulo="disco", selector="mount:*",
            expectation_id=None, schedule_sec=60, evaluate=self._ev,
            severity=Severity.MAJOR, hysteresis=Hysteresis(2, 3))

    marcas: dict = {}

    def _ev(self, ctx):
        st, rc = self.guion.pop(0)
        return CheckOutcome(state=st, reason_code=rc,
                            actual={"uso_pct": 93.0 if st.es_problema else 70.0},
                            evidence_quality=EvidenceQuality.FULL)

    def correr(self, *pares):
        self.guion.extend(pares)
        while self.guion:
            acc = engine.ResultadoCiclo()
            r, ev = engine.ejecutar_control(self.conn, self.cd, self.res)
            engine.procesar(self.conn, self.cd, self.res, ev, r, "h", acc)
        return self

    @property
    def total(self):
        return self.conn.execute("SELECT COUNT(*) c FROM incident").fetchone()["c"]

    @property
    def abiertos(self):
        return self.conn.execute(
            "SELECT COUNT(*) c FROM incident WHERE closed_at IS NULL").fetchone()["c"]

    def estado(self, i=0):
        f = self.conn.execute(
            "SELECT state FROM incident ORDER BY opened_at LIMIT 1 OFFSET ?", (i,)
        ).fetchone()
        return f["state"] if f else None

    def recuperado_en(self, i=0):
        f = self.conn.execute(
            "SELECT recovered_at FROM incident ORDER BY opened_at LIMIT 1 OFFSET ?", (i,)
        ).fetchone()
        return f["recovered_at"] if f else None

    def eventos(self, kind):
        return self.conn.execute(
            "SELECT COUNT(*) c FROM incident_event WHERE kind = ?", (kind,)
        ).fetchone()["c"]

    def limpiar(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def caso(nombre, fn):
    e = Escenario()
    try:
        fn(e)
        print("  OK   %s" % nombre)
        return True
    except AssertionError as ex:
        print("  MAL  %s :: %s" % (nombre, ex))
        return False
    finally:
        e.limpiar()


def main():
    casos = [
        ("un pico aislado no abre incidente",
         lambda e: (e.correr(LLENO, SANO), assert_eq(e.total, 0))),

        ("dos muestras seguidas abren",
         lambda e: (e.correr(LLENO, LLENO), assert_eq(e.total, 1), assert_eq(e.abiertos, 1))),

        # ------------------------------------------------------------------
        # Dos picos AISLADOS no son una racha, por lejos que esten entre si.
        # La racha se contaba con un SELECT filtrado por motivo, que se salteaba
        # las muestras sanas del medio: dos capturas separadas por horas quedaban
        # una al lado de la otra y `abre_tras=2` abria en la primera mala de la
        # segunda tanda.
        # Medido en srv3 el 2026-09-10: SVC-01 sobre plataforma-api.service
        # tuvo cinco FAILED aislados en 48 h —el segundo exacto en que el sync
        # reinicia el back— cada uno rodeado de OK, y cada uno mando un correo
        # CRITICAL.
        # ------------------------------------------------------------------
        ("dos picos aislados separados por muestras sanas no abren",
         lambda e: (e.correr(LLENO, SANO, SANO, LLENO), assert_eq(e.total, 0))),

        ("y despues de los picos, dos seguidas si abren",
         lambda e: (e.correr(LLENO, SANO, LLENO, LLENO), assert_eq(e.total, 1))),

        ("veinte muestras mas no abren veinte incidentes",
         lambda e: (e.correr(*([LLENO] * 22)), assert_eq(e.total, 1))),

        ("una muestra sana no cierra",
         lambda e: (e.correr(LLENO, LLENO, SANO), assert_eq(e.abiertos, 1))),

        # ------------------------------------------------------------------
        # Un control que pasa de UN problema a OTRO no puede dejar el primero
        # vivo para siempre. Da UN resultado por ciclo, asi que dos motivos no
        # pueden estar activos a la vez sobre el mismo recurso.
        #
        # Sin esto, la unica via de recuperacion era que el control volviera a
        # estar sano, y uno que cambia de motivo nunca pasa por ahi.
        # `cerrar_sin_evidencia` tampoco lo alcanza: cubre solo los UNKNOWN.
        # Medido en srv2 el 2026-09-03: al mejorar JOB-05, trece incidentes
        # quedaron huerfanos de golpe mientras el control abria trece nuevos.
        # ------------------------------------------------------------------
        ("al cambiar el motivo, el incidente del motivo viejo se recupera",
         lambda e: (e.correr(LLENO, LLENO, OTRO, OTRO),
                    assert_eq(e.total, 2),
                    assert_eq(e.estado(0), "RECOVERED"),
                    assert_eq(e.estado(1), "OPEN"))),

        ("y el nuevo queda abierto con su propio motivo",
         lambda e: (e.correr(LLENO, LLENO, OTRO, OTRO),
                    assert_eq(OTRO[1] in (e.conn.execute(
                        "SELECT cause_json FROM incident WHERE state='OPEN'"
                    ).fetchone()["cause_json"] or ""), True))),

        ("si el motivo viejo vuelve antes del cierre, se reabre el mismo",
         lambda e: (e.correr(LLENO, LLENO, OTRO, OTRO, LLENO, LLENO),
                    assert_eq(e.total, 2),
                    assert_eq(e.estado(0), "OPEN"))),

        ("y con el control sano se recuperan los dos",
         lambda e: (e.correr(LLENO, LLENO, OTRO, OTRO, SANO, SANO, SANO),
                    assert_eq(e.abiertos, 2),
                    assert_eq(e.estado(0), "RECOVERED"),
                    assert_eq(e.estado(1), "RECOVERED"))),

        ("tres sanas dejan RECOVERED, no CLOSED",
         lambda e: (e.correr(LLENO, LLENO, SANO, SANO, SANO),
                    assert_eq(e.estado(), "RECOVERED"), assert_eq(e.abiertos, 1))),

        ("si vuelve antes de cerrar, reabre el mismo",
         lambda e: (e.correr(LLENO, LLENO, SANO, SANO, SANO, LLENO, LLENO),
                    assert_eq(e.total, 1), assert_eq(e.estado(), "OPEN"))),

        ("UNKNOWN no cierra un incidente abierto",
         lambda e: (e.correr(LLENO, LLENO, DESC, DESC, DESC, DESC),
                    assert_eq(e.estado(), "OPEN"))),

        ("UNKNOWN no abre incidente por si solo",
         lambda e: (e.correr(DESC, DESC, DESC), assert_eq(e.total, 0))),

        # Los dos casos de abajo son la regresion del bug que dejaba un
        # incidente abierto para siempre: `recuperar` se llamaba en cada ciclo y
        # reescribia `recovered_at`, asi que la ventana de cierre se reiniciaba
        # sola. En srv3 dio 841 "incidente recuperado" en diez horas.
        ("veinte ciclos sanos emiten UNA sola recuperacion",
         lambda e: (e.correr(LLENO, LLENO, *([SANO] * 20)),
                    assert_eq(e.eventos("recuperado"), 1))),

        ("los ciclos sanos posteriores no mueven recovered_at",
         lambda e: (e.correr(LLENO, LLENO, SANO, SANO, SANO),
                    marcar(e, "t0", e.recuperado_en()),
                    e.correr(*([SANO] * 15)),
                    assert_eq(e.recuperado_en(), e.marcas["t0"]))),

        ("recuperado y estable termina cerrando",
         lambda e: (e.correr(LLENO, LLENO, *([SANO] * 10)),
                    assert_eq(len(incidents.cerrar_recuperados(e.conn, 0)), 1),
                    assert_eq(e.estado(), "CLOSED"), assert_eq(e.abiertos, 0))),

        # El agujero que dejaba UNKNOWN: no mueve incidentes -y esta bien que no
        # los mueva- pero eso los dejaba abiertos para siempre cuando el control
        # dejaba de poder evaluarse. Paso con un falso positivo de HST-10.
        ("un incidente que lleva horas en UNKNOWN se cierra por falta de evidencia",
         lambda e: (e.correr(LLENO, LLENO, DESC, DESC, DESC),
                    envejecer(e, 30),
                    assert_eq(len(incidents.cerrar_sin_evidencia(e.conn, 24)), 1),
                    assert_eq(e.estado(), "CLOSED"))),

        ("pero NO se cierra si hubo una medicion concluyente en la ventana",
         lambda e: (e.correr(LLENO, LLENO, DESC, DESC, LLENO),
                    assert_eq(len(incidents.cerrar_sin_evidencia(e.conn, 24)), 0),
                    assert_eq(e.abiertos, 1))),

        ("ni si el UNKNOWN es reciente",
         lambda e: (e.correr(LLENO, LLENO, DESC, DESC),
                    assert_eq(len(incidents.cerrar_sin_evidencia(e.conn, 24)), 0))),

        ("otro motivo del mismo recurso es incidente aparte",
         lambda e: (e.correr(LLENO, LLENO, RO, RO), assert_eq(e.total, 2))),

        ("un motivo nuevo no hereda la racha del anterior",
         lambda e: (e.correr(*([LLENO] * 10)), assert_eq(e.total, 1),
                    e.correr(RO), assert_eq(e.total, 1))),

        ("al sanar se recuperan todos los motivos",
         lambda e: (e.correr(LLENO, LLENO, RO, RO, SANO, SANO, SANO),
                    assert_eq(e.estado(0), "RECOVERED"),
                    assert_eq(e.estado(1), "RECOVERED"))),

        ("cierre por ventana estable",
         lambda e: (e.correr(LLENO, LLENO, SANO, SANO, SANO),
                    assert_eq(len(incidents.cerrar_recuperados(e.conn, 0)), 1),
                    assert_eq(e.abiertos, 0))),

        ("un control que explota da UNKNOWN y no tumba el ciclo",
         caso_explota),
    ]
    ok = sum(caso(n, f) for n, f in casos)
    print("\n%d/%d" % (ok, len(casos)))
    return 0 if ok == len(casos) else 1


def caso_explota(e):
    def boom(ctx):
        raise RuntimeError("collector roto")
    e.cd = CheckDefinition(check_id="X", version=1, titulo="x", selector="*",
                           expectation_id=None, schedule_sec=60, evaluate=boom)
    r, _ = engine.ejecutar_control(e.conn, e.cd, e.res)
    assert_eq(r.state, State.UNKNOWN)
    assert_eq(r.reason_code, "CHECK_ERROR")
    assert_eq(r.evidence_quality, EvidenceQuality.NONE)


def envejecer(e, horas):
    """Corre hacia atras los resultados NO-UNKNOWN, dejando los UNKNOWN recientes.

    Es lo que simula "el control lleva un dia sin poder ver nada": lo ultimo
    concluyente quedo viejo y desde entonces solo hay UNKNOWN.
    """
    e.conn.execute(
        "UPDATE check_result SET observed_at = datetime(observed_at, ?) "
        "WHERE state != 'UNKNOWN'", (f"-{int(horas)} hours",))
    return True


def marcar(e, clave, valor):
    """Guarda un valor intermedio para compararlo mas adelante en el mismo caso."""
    e.marcas = dict(e.marcas)
    e.marcas[clave] = valor
    return True


def assert_eq(a, b):
    assert a == b, "esperaba %r y dio %r" % (b, a)
    return True


if __name__ == "__main__":
    raise SystemExit(main())
