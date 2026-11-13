"""Proyeccion de agotamiento: que sea robusta y que no invente."""
from sysop.core import trend


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


def main():
    r = []

    # 1. crecimiento limpio de 1 punto por dia desde 80%
    pts = [(d, 80.0 + d) for d in range(20)]
    p = trend.proyectar(pts)
    r.append(ok("pendiente de un crecimiento limpio",
                abs(p.pendiente_pct_por_dia - 1.0) < 0.01, p.pendiente_pct_por_dia))
    # al ultimo punto esta en 99%, falta 1% -> 1 dia
    r.append(ok("dias para llenar", abs(p.dias_para_llenar - 1.0) < 0.1,
                p.dias_para_llenar))

    # 2. un pico aislado NO tiene que torcer la proyeccion
    pts_pico = [(d, 80.0 + d) for d in range(20)]
    pts_pico[10] = (10.0, 99.5)          # un backup que ocupo y libero
    p2 = trend.proyectar(pts_pico)
    r.append(ok("un outlier no tuerce la pendiente (Theil-Sen)",
                abs(p2.pendiente_pct_por_dia - 1.0) < 0.1, p2.pendiente_pct_por_dia))

    # 2b. comparacion: minimos cuadrados si se torceria
    n = len(pts_pico)
    mx = sum(x for x, _ in pts_pico)/n; my = sum(y for _, y in pts_pico)/n
    num = sum((x-mx)*(y-my) for x, y in pts_pico); den = sum((x-mx)**2 for x, _ in pts_pico)
    mc = num/den
    print("       (minimos cuadrados daria %.3f, Theil-Sen da %.3f)" % (
          mc, p2.pendiente_pct_por_dia))

    # 3. no crece -> no se proyecta un numero absurdo
    p3 = trend.proyectar([(d, 50.0) for d in range(20)])
    r.append(ok("si no crece, no proyecta", p3.dias_para_llenar is None))
    p4 = trend.proyectar([(d, 90.0 - d) for d in range(20)])
    r.append(ok("si decrece, tampoco", p4.dias_para_llenar is None))

    # 4. sin historia suficiente NO es confiable
    p5 = trend.proyectar([(0.0, 80.0), (0.01, 81.0), (0.02, 82.0)])
    r.append(ok("con 3 puntos y 30 min no es confiable", not p5.confiable))
    p6 = trend.proyectar([(d*0.05, 80.0 + d*0.1) for d in range(20)])  # 20 pts, 24h
    r.append(ok("con 20 puntos y 24h si lo es", p6.confiable,
                "%d pts %.1fh" % (p6.puntos, p6.horas_de_historia)))

    # 4b. el piso de historia no alcanza: tiene que escalar con el horizonte.
    # Regresion del incidente falso de srv3: 200 puntos y 12,3 horas anunciaron
    # que / se llenaba en 10,9 dias y abrieron un `major`.
    caso_srv3 = trend.Proyeccion(1.408, 10.9, 200, 12.3)
    r.append(ok("12h de historia no sostienen un horizonte de 11 dias",
                caso_srv3.confiable and not caso_srv3.suficiente_para(10.9)))
    r.append(ok("las mismas 12h si sostienen un horizonte de 2 dias",
                trend.Proyeccion(20.0, 2.0, 200, 12.3).suficiente_para(2.0)))
    r.append(ok("con 4 dias de historia el horizonte de 11 dias vale",
                trend.Proyeccion(1.408, 10.9, 200, 96.0).suficiente_para(10.9)))
    r.append(ok("el horizonte critico de 3 dias pide ~7,2h",
                not trend.Proyeccion(10.0, 3.0, 200, 7.0).suficiente_para(3.0)
                and trend.Proyeccion(10.0, 3.0, 200, 8.0).suficiente_para(3.0)))
    r.append(ok("sin el piso absoluto no vale ningun horizonte",
                not trend.Proyeccion(10.0, 1.0, 4, 20.0).suficiente_para(1.0)))

    # 5. serie larga: se submuestrea y no explota
    import time
    grande = [(d*0.01, 50.0 + d*0.001) for d in range(5000)]
    t0 = time.monotonic(); p7 = trend.proyectar(grande); dt = time.monotonic()-t0
    r.append(ok("5000 puntos se resuelven rapido (submuestreo)", dt < 2.0,
                "%.2fs" % dt))

    # ---------------------------------------------------------------------
    # serie_de_uso() CONTRA UNA BASE DE VERDAD.
    #
    # Hasta el 2026-09-03 esta bateria probaba solo `proyectar()`, que es una
    # funcion pura, y nunca tocaba la que consulta. Por eso no vio que
    # `serie_de_uso` usaba `db.corte()` con `db` sin importar: el modulo
    # importaba bien, pero el `NameError` recien saltaba al ejecutar la consulta.
    # HST-10 quedo ciego --"el control HST-10 fallo: name 'db' is not defined"--
    # y nada de esto fallo. Un control que se cae no es un control que dice que
    # no.
    # ---------------------------------------------------------------------
    import json as _json, shutil, tempfile
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from pathlib import Path
    from sysop.store import db as _db

    d = Path(tempfile.mkdtemp())
    try:
        conn = _db.connect(d / "t.db"); _db.init_schema(conn)
        ahora = _dt.now(_tz.utc)
        for i in range(20):
            conn.execute(
                "INSERT INTO check_result (check_id, resource_id, observed_at, state, "
                "severity, actual_json, evidence_quality, rule_version) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("HST-05", "mount:/", (ahora - _td(days=19 - i)).isoformat(), "OK",
                 "info", _json.dumps({"uso_pct": 60.0 + i}), "FULL", 1))
        # Una fuera de la ventana y una UNKNOWN: no tienen que entrar.
        conn.execute(
            "INSERT INTO check_result (check_id, resource_id, observed_at, state, "
            "severity, actual_json, evidence_quality, rule_version) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("HST-05", "mount:/", (ahora - _td(days=90)).isoformat(), "OK",
             "info", _json.dumps({"uso_pct": 1.0}), "FULL", 1))
        conn.execute(
            "INSERT INTO check_result (check_id, resource_id, observed_at, state, "
            "severity, actual_json, evidence_quality, rule_version) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("HST-05", "mount:/", ahora.isoformat(), "UNKNOWN",
             "info", _json.dumps({"uso_pct": 99.0}), "PARTIAL", 1))
        conn.commit()

        pts = trend.serie_de_uso(conn, "HST-05", "mount:/", dias=30)
        r.append(ok("serie_de_uso lee de la base sin romperse", len(pts) == 20, len(pts)))
        r.append(ok("y deja afuera la vieja y la UNKNOWN",
                    all(60.0 <= v <= 79.0 for _, v in pts), pts[:2]))
        p8 = trend.proyectar(pts)
        r.append(ok("la proyeccion sobre esa serie sube ~1 punto por dia",
                    p8 is not None and 0.9 < p8.pendiente_pct_por_dia < 1.1,
                    p8.pendiente_pct_por_dia if p8 else None))
        r.append(ok("un recurso sin historia devuelve serie vacia, no explota",
                    trend.serie_de_uso(conn, "HST-05", "mount:/inexistente") == []))
        conn.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # Defensa generica contra el mismo error en cualquier otro modulo: `db.` es
    # un helper que se usa suelto y es facil olvidarse el import, porque el
    # archivo compila igual y el NameError espera hasta que la linea se ejecute.
    import ast as _ast
    rotos = []
    for arch in sorted(Path("sysop").rglob("*.py")):
        src = arch.read_text(encoding="utf-8")
        if "db.corte" not in src and "db.ahora" not in src:
            continue
        nombres = set()
        for n in _ast.walk(_ast.parse(src)):
            if isinstance(n, _ast.Import):
                nombres.update((a.asname or a.name.split(".")[0]) for a in n.names)
            elif isinstance(n, _ast.ImportFrom):
                nombres.update((a.asname or a.name) for a in n.names)
        if "db" not in nombres:
            rotos.append(str(arch))
    r.append(ok("todo modulo que usa `db.` lo importa", not rotos, rotos))

    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
