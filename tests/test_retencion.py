"""La retencion del almacen: que el observador no llene el disco que vigila.

Estuvo en 400 dias para `check_result`, que a 24,5 MB por dia son ~9,8 GB sobre
un disco de 13,5 GiB. Esta bateria fija el comportamiento nuevo y, sobre todo,
el ORDEN de la poda: `check_result` referencia `observation` con la FK activa,
asi que borrar la apuntada primero rompe la retencion el dia que alguien empiece
a guardar evidencia.
"""
import shutil
import tempfile
from pathlib import Path

from sysop import daemon as dmn
from sysop.store import db


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


class CfgFalsa:
    def __init__(self, **over):
        self.over = over

    def umbral(self, ruta, default=None):
        return self.over.get(ruta, default)


class CtxFalso:
    def __init__(self, conn, cfg):
        self.conn = conn
        self.cfg = cfg


def poblar(conn):
    """Una fila vieja y una nueva en cada tabla."""
    conn.execute("INSERT INTO observation (id, host_id, source, observed_at, payload_json) "
                 "VALUES (1,'t','x',datetime('now','-500 days'),'{}')")
    conn.execute("INSERT INTO observation (id, host_id, source, observed_at, payload_json) "
                 "VALUES (2,'t','x',datetime('now','-1 day'),'{}')")
    for i, edad in ((1, "-500 days"), (2, "-1 day")):
        conn.execute(
            "INSERT INTO check_result (id, check_id, resource_id, observed_at, state, "
            "  severity, evidence_quality, rule_version) "
            "VALUES (?,?,?,datetime('now',?),'OK','MAJOR','FULL',1)",
            (i, "X-01", "host:t", edad))
    conn.execute("INSERT INTO incident (incident_id, fingerprint, resource_id, check_id, "
                 "  opened_at, state, severity, cause_json) "
                 "VALUES ('i1','fp1','host:t','X-01',datetime('now','-500 days'),"
                 "        'OPEN','MAJOR','{}')")
    for i, edad in ((1, "-500 days"), (2, "-1 day")):
        conn.execute("INSERT INTO incident_event (id, incident_id, at, kind, payload_json) "
                     "VALUES (?, 'i1', datetime('now',?), 'abierto', '{}')", (i, edad))
    for i, edad in ((1, "-500 days"), (2, "-1 day")):
        conn.execute("INSERT INTO llm_call (id, at, modelo, tokens_in, tokens_out, "
                     "  costo_usd, ms, ok) VALUES (?, datetime('now',?), 'm', 1, 1, 0.0, 1, 1)",
                     (i, edad))
    conn.execute("INSERT INTO cursor (key, value, updated_at) "
                 "VALUES ('journal', '123', datetime('now','-500 days'))")
    conn.commit()


def cuantas(conn, tabla):
    return conn.execute(f"SELECT COUNT(*) n FROM {tabla}").fetchone()["n"]


def main():
    r = []
    d = Path(tempfile.mkdtemp())
    conn = db.connect(d / "t.db")
    db.init_schema(conn)
    poblar(conn)

    dm = dmn.Daemon()
    dm.ctx = CtxFalso(conn, CfgFalsa())
    dm._compactar = lambda podado: None   # el VACUUM se prueba aparte

    r.append(ok("antes de podar estan las dos filas de cada tabla",
                all(cuantas(conn, t) == 2 for t in
                    ("observation", "check_result", "incident_event", "llm_call"))))

    dm._aplicar_retencion()

    for tabla in ("observation", "check_result", "incident_event", "llm_call"):
        r.append(ok("%s: se fue la vieja y quedo la nueva" % tabla,
                    cuantas(conn, tabla) == 1, cuantas(conn, tabla)))
    r.append(ok("cursor NO se poda: es estado, no historia (borrarlo reprocesa eventos)",
                cuantas(conn, "cursor") == 1, cuantas(conn, "cursor")))

    # LA VENTANA IMPORTA, no solo que pode. Con filas de 500 dias este test
    # pasaba igual con la retencion vieja de 400, o sea que no probaba nada de
    # la ventana. Una fila de 100 dias distingue las dos: sobrevive a 400 y no
    # a 45. Comprobado rompiendo el codigo.
    conn.execute(
        "INSERT INTO check_result (id, check_id, resource_id, observed_at, state, "
        "  severity, evidence_quality, rule_version) "
        "VALUES (100,'X-01','host:t',datetime('now','-100 days'),'OK','MAJOR','FULL',1)")
    conn.commit()
    dm._aplicar_retencion()
    r.append(ok("una medicion de 100 dias tambien se va: la ventana es 45, no 400",
                conn.execute("SELECT COUNT(*) n FROM check_result WHERE id=100"
                             ).fetchone()["n"] == 0))
    conn.execute(
        "INSERT INTO check_result (id, check_id, resource_id, observed_at, state, "
        "  severity, evidence_quality, rule_version) "
        "VALUES (101,'X-01','host:t',datetime('now','-30 days'),'OK','MAJOR','FULL',1)")
    conn.commit()
    dm._aplicar_retencion()
    r.append(ok("pero una de 30 dias se queda: la tendencia proyecta sobre 30",
                conn.execute("SELECT COUNT(*) n FROM check_result WHERE id=101"
                             ).fetchone()["n"] == 1))

    # --- la FK: borrar observation antes que check_result romperia ---
    conn.execute("INSERT INTO observation (id, host_id, source, observed_at, payload_json) "
                 "VALUES (9,'t','x',datetime('now','-500 days'),'{}')")
    conn.execute(
        "INSERT INTO check_result (id, check_id, resource_id, observed_at, state, severity, "
        "  evidence_id, evidence_quality, rule_version) "
        "VALUES (9,'X-01','host:t',datetime('now','-500 days'),'OK','MAJOR',9,'FULL',1)")
    conn.commit()
    try:
        dm._aplicar_retencion()
        quedo = cuantas(conn, "observation")
        r.append(ok("con evidencia apuntada, la poda no se rompe por la FK",
                    quedo == 1, "quedaron %s en observation" % quedo))
    except Exception as e:  # noqa: BLE001
        r.append(ok("con evidencia apuntada, la poda no se rompe por la FK", False, e))

    # --- EL CASO DE srv2: medicion que se queda apuntando a evidencia que se va ---
    # El caso de arriba no lo agarraba: las dos filas tenian 500 dias y la
    # medicion se borraba primero. Con 20 dias la medicion sobrevive (45) y la
    # observacion no (7), que es lo que pasa todos los dias en produccion.
    conn.execute("INSERT INTO observation (id, host_id, source, observed_at, payload_json) "
                 "VALUES (20,'t','x',?,'{}')", (db.corte(days=20),))
    conn.execute(
        "INSERT INTO check_result (id, check_id, resource_id, observed_at, state, severity, "
        "  evidence_id, evidence_quality, rule_version) "
        "VALUES (20,'X-01','host:t',?,'FAILED','MAJOR',20,'FULL',1)", (db.corte(days=20),))
    conn.commit()
    dm._aplicar_retencion()
    r.append(ok("la evidencia de 20 dias se borra aunque una medicion la apunte",
                conn.execute("SELECT COUNT(*) n FROM observation WHERE id=20"
                             ).fetchone()["n"] == 0))
    fila = conn.execute("SELECT evidence_id FROM check_result WHERE id=20").fetchone()
    r.append(ok("y la medicion se queda, sin la referencia",
                fila is not None and fila["evidence_id"] is None,
                None if fila is None else fila["evidence_id"]))
    plan = " ".join(str(tuple(p)) for p in conn.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM check_result WHERE evidence_id = 1"))
    r.append(ok("la FK se verifica por indice, no recorriendo check_result",
                "ix_result_evidencia" in plan, plan))

    # --- la retencion se puede configurar sin tocar codigo ---
    conn.execute("INSERT INTO llm_call (id, at, modelo, tokens_in, tokens_out, costo_usd, "
                 "  ms, ok) VALUES (50, datetime('now','-30 days'), 'm', 1, 1, 0.0, 1, 1)")
    conn.commit()
    antes = cuantas(conn, "llm_call")
    dm.ctx = CtxFalso(conn, CfgFalsa(**{"agente.retencion_dias.llm_call": 7}))
    dm._aplicar_retencion()
    r.append(ok("bajando la retencion por config, la fila de 30 dias se va",
                cuantas(conn, "llm_call") < antes,
                "antes=%s ahora=%s" % (antes, cuantas(conn, "llm_call"))))

    # --- VACUUM: solo si hubo poda, y no dos veces el mismo dia ---
    dm2 = dmn.Daemon()
    dm2.ctx = CtxFalso(conn, CfgFalsa())
    corridas = []
    conn_real = conn

    class ConnEspia:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a):
            if sql.strip().upper().startswith("VACUUM"):
                corridas.append(1)
                return None
            return self._real.execute(sql, *a)

        def __getattr__(self, n):
            return getattr(self._real, n)

    # Fraccion minima en 0: aca se prueba el ritmo, el espacio libre va aparte.
    dm2.ctx = CtxFalso(ConnEspia(conn_real), CfgFalsa(**{"agente.vacuum_min_libre_frac": 0}))
    dm2._compactar(0)
    r.append(ok("sin nada podado no se compacta (VACUUM es caro)", not corridas, corridas))
    dm2._compactar(10)
    r.append(ok("con filas podadas si se compacta", len(corridas) == 1, corridas))
    dm2._compactar(10)
    r.append(ok("pero no dos veces el mismo dia", len(corridas) == 1, corridas))
    dm2._ultimo_vacuum = 0.0
    dm2._compactar(10)
    r.append(ok("y al dia siguiente si", len(corridas) == 2, corridas))

    # --- despues del VACUUM el -wal queda en cero, no del tamaño de la base ---
    conn.execute("CREATE TABLE relleno (x BLOB)")
    for _ in range(200):
        conn.execute("INSERT INTO relleno VALUES (randomblob(8192))")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("DELETE FROM relleno")
    conn.commit()
    dm3 = dmn.Daemon()
    dm3.ctx = CtxFalso(conn, CfgFalsa())
    dm3._compactar(10)
    wal = Path(str(d / "t.db") + "-wal")
    tam = wal.stat().st_size if wal.exists() else 0
    r.append(ok("el VACUUM no deja el -wal inflado", tam == 0, tam))

    # --- sin paginas libres el VACUUM no se hace: no devuelve nada y traba el tick ---
    corridas.clear()
    dm4 = dmn.Daemon()
    dm4.ctx = CtxFalso(ConnEspia(conn_real), CfgFalsa())
    dm4._compactar(10)
    r.append(ok("con la base recien compactada (sin paginas libres) se saltea",
                not corridas, corridas))
    conn.execute("CREATE TABLE relleno2 (x BLOB)")
    for _ in range(200):
        conn.execute("INSERT INTO relleno2 VALUES (randomblob(8192))")
    conn.commit()
    conn.execute("DELETE FROM relleno2")
    conn.commit()
    dm4._ultimo_vacuum = 0.0
    dm4._compactar(10)
    r.append(ok("con mucho espacio libre si se compacta", len(corridas) == 1, corridas))

    shutil.rmtree(d, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
