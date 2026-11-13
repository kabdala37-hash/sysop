"""Que paso entre un informe y el siguiente.

El bloque que justifica el modulo son los TRANSITORIOS: fallas que se
recuperaron sin llegar a abrir incidente. No generaron aviso, y hasta que existio
esto no se veian en ningun lado. Un servicio que se cae una muestra a las tres de
la manana y vuelve solo es exactamente eso.
"""
import shutil, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sysop import periodo
from sysop.store import db


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


def nuevo():
    d = Path(tempfile.mkdtemp())
    c = db.connect(d / "t.db"); db.init_schema(c)
    return c, d


AHORA = datetime.now(timezone.utc)


def resultado(conn, check, rec, hace_min, estado, motivo=""):
    conn.execute(
        "INSERT INTO check_result (check_id, resource_id, observed_at, state, "
        "severity, reason_code, rule_version) VALUES (?,?,?,?,?,?,1)",
        (check, rec, (AHORA - timedelta(minutes=hace_min)).isoformat(),
         estado, "major", motivo))


def incidente(conn, iid, check, rec, abierto_hace, cerrado_hace=None):
    conn.execute(
        "INSERT INTO incident (incident_id, fingerprint, resource_id, check_id, "
        "opened_at, state, severity, closed_at) VALUES (?,?,?,?,?,?,?,?)",
        (iid, iid[:8], rec, check,
         (AHORA - timedelta(minutes=abierto_hace)).isoformat(),
         "CLOSED" if cerrado_hace else "OPEN", "major",
         (AHORA - timedelta(minutes=cerrado_hace)).isoformat() if cerrado_hace else None))


def main():
    r = []
    conn, d = nuevo()

    # --- la ventana sale del ultimo informe, no de un numero fijo ---
    desde = periodo.desde_el_ultimo_informe(conn)
    horas = (AHORA - desde).total_seconds() / 3600
    r.append(ok("sin informe previo, la ventana inicial son 24 h",
                23.9 < horas < 24.1, round(horas, 2)))

    conn.execute(
        "INSERT INTO outbox (report_type, period_start, state, subject, body_html, "
        "recipients_json, created_at, sent_at) VALUES "
        "('informe','x','SENT','a','<p/>','[]', ?, ?)",
        ((AHORA - timedelta(hours=6)).isoformat(),
         (AHORA - timedelta(hours=6)).isoformat()))
    horas = (AHORA - periodo.desde_el_ultimo_informe(conn)).total_seconds() / 3600
    r.append(ok("con un informe de hace 6 h, la ventana es de 6 h",
                5.9 < horas < 6.1, round(horas, 2)))
    r.append(ok("y un informe de OTRO tipo no mueve la ventana",
                abs((AHORA - periodo.desde_el_ultimo_informe(conn, "urgente")
                     ).total_seconds() / 3600 - 24) < 0.1))

    # --- transitorios: el bloque que antes no existia ---
    conn2, d2 = nuevo()
    # un pico: dos muestras malas, sin incidente
    resultado(conn2, "SVC-01", "systemd:back", 90, "FAILED", "UNIDAD_CAIDA")
    resultado(conn2, "SVC-01", "systemd:back", 89, "OK")
    # una falla sostenida QUE SI abrio incidente: no es transitorio
    for m in range(60, 40, -2):
        resultado(conn2, "HST-09", "mount:/", m, "FAILED", "FS_CASI_LLENO")
    incidente(conn2, "INC-a", "HST-09", "mount:/", 58)

    res = periodo.resumir(conn2, AHORA - timedelta(hours=3))
    trans = {t["check_id"] for t in res["transitorios"]}
    r.append(ok("un pico sin incidente aparece como transitorio",
                "SVC-01" in trans, trans))
    r.append(ok("una falla que SI abrio incidente no se cuenta dos veces",
                "HST-09" not in trans, trans))
    t = next(t for t in res["transitorios"] if t["check_id"] == "SVC-01")
    r.append(ok("y dice cuantas veces paso", t["muestras"] == 1, t["muestras"]))

    # --- ceguera ---
    for m in range(120, 0, -5):
        resultado(conn2, "JOB-07", "job:x:1", m, "UNKNOWN", "SIN_DATOS_DEL_JOB")
    res = periodo.resumir(conn2, AHORA - timedelta(hours=3))
    ceg = res["ceguera"]
    r.append(ok("un control que no vio nada en todo el periodo se reporta",
                any(c["check_id"] == "JOB-07" and c["muestras"] > 20 for c in ceg),
                ceg[:2]))
    a = res["actividad"]
    r.append(ok("el UNKNOWN se cuenta aparte, ni como problema ni como OK",
                a["unknown"] == 24 and a["problema"] == 11 and a["ok"] == 1
                and a["resultados"] == a["unknown"] + a["problema"] + a["ok"],
                a))

    # --- incidentes del periodo, con duracion ---
    conn3, d3 = nuevo()
    incidente(conn3, "INC-1", "SVC-01", "a", 100, 40)     # abrio y cerro dentro
    incidente(conn3, "INC-2", "SVC-01", "b", 90)          # sigue abierto
    incidente(conn3, "INC-3", "SVC-01", "c", 5000, 30)    # abrio ANTES, cerro dentro
    res = periodo.resumir(conn3, AHORA - timedelta(hours=3))
    i = res["incidentes"]
    r.append(ok("cuenta los que abrieron en la ventana",
                len(i["abiertos_en_el_periodo"]) == 2,
                len(i["abiertos_en_el_periodo"])))
    r.append(ok("los que cerraron en la ventana, aunque hayan abierto antes",
                len(i["cerrados_en_el_periodo"]) == 2,
                len(i["cerrados_en_el_periodo"])))
    r.append(ok("y los que siguen abiertos",
                len(i["siguen_abiertos"]) == 1, len(i["siguen_abiertos"])))
    r.append(ok("la duracion distingue uno de un minuto de uno de tres dias",
                max(i["duracion_minutos"]) > 4000, i["duracion_minutos"]))

    # --- actividad: para poder dimensionar el resto ---
    r.append(ok("un periodo sin una sola medicion no finge actividad",
                periodo.resumir(nuevo()[0], AHORA - timedelta(hours=1)
                                )["actividad"]["resultados"] == 0))

    for x in (d, d2, d3):
        shutil.rmtree(x, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
