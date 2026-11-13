"""El extractor de logs.

Lo que protege: que un traceback no quede suelto sin fecha ni componente, que
cuarenta y siete errores casi iguales se cuenten como uno, y que entre corridas
se lea SOLO lo nuevo.
"""
import shutil, tempfile
from pathlib import Path

from sysop import logs
from sysop.store import db


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


CRUDO = """2026-08-20 20:05:34,093 [INFO] [MainThread] bot: arranco
2026-08-20 20:06:00,001 [ERROR] [MainThread] channel: fallo el turno
Traceback (most recent call last):
  File "/opt/x.py", line 42, in run
    conn = get()
mysql.connector.errors.DatabaseError: 1129 (HY000): Host '203.0.113.10' is blocked
2026-08-20 20:07:00,001 [INFO] [MainThread] bot: todo bien
[2026-08-21T11:30:25.500Z] [WARN] [SHIFT] in-hours fallo: connect ECONNREFUSED 127.0.0.1:10997
"""


def main():
    r = []
    d = Path(tempfile.mkdtemp())
    conn = db.connect(d / "t.db"); db.init_schema(conn)
    f = d / "app.log"
    f.write_text(CRUDO, encoding="utf-8")

    # --- el traceback viaja con su encabezado ---
    evs = logs.eventos(CRUDO.splitlines())
    r.append(ok("una linea sin fecha es continuacion, no un evento suelto",
                len(evs) == 4, [len(e) for e in evs]))
    con_traceback = [e for e in evs if any("Traceback" in l for l in e)]
    r.append(ok("el traceback queda pegado a la linea que dice cuando y quien",
                len(con_traceback) == 1 and "20:06:00" in con_traceback[0][0],
                con_traceback[0][0][:40] if con_traceback else None))
    r.append(ok("y el error de mysql viaja adentro de ese mismo evento",
                any("is blocked" in l for l in con_traceback[0])))

    # --- que se considera interesante ---
    r.append(ok("un INFO no interesa", not logs.interesa(evs[0])))
    r.append(ok("un ERROR si", logs.interesa(evs[1])))
    r.append(ok("un ECONNREFUSED sin nivel ERROR tambien",
                logs.interesa(evs[3]), evs[3]))

    # --- agrupacion por firma ---
    r.append(ok("dos errores que solo difieren en la IP tienen la misma firma",
                logs.firma_de("Host '10.0.0.1' bloqueado, intento 4")
                == logs.firma_de("Host '192.168.1.9' bloqueado, intento 17")))
    r.append(ok("pero dos errores distintos NO se colapsan",
                logs.firma_de("Host bloqueado") != logs.firma_de("Timeout de red")))

    # El timestamp con milisegundos y zona tiene que normalizarse ENTERO. Con un
    # `\b` al final del patron quedaba `<ts>864Z` y el mismo error no agrupaba:
    # 218 eventos daban 200 grupos en el log del worker.
    a = "[2026-08-19T16:28:38.864Z] [ERROR] BOTH: registro SIP fallo (media=6)"
    b = "[2026-08-19T16:29:08.773Z] [ERROR] BOTH: registro SIP fallo (media=6)"
    r.append(ok("dos lineas que solo difieren en los milisegundos agrupan",
                logs.firma_de(a) == logs.firma_de(b), (logs.firma_de(a),
                                                       logs.firma_de(b))))
    r.append(ok("y no queda un resto del timestamp en la firma",
                "864" not in logs.firma_de(a) and "Z]" not in logs.firma_de(a),
                logs.firma_de(a)))
    for fecha in ("2026-08-20 20:05:34,093", "2026-08-28T15:33:18.482-03:00",
                  "2026-08-28T15:33:18Z", "2026-08-28T15:33:18"):
        r.append(ok(f"normaliza el formato {fecha[:24]}",
                    logs.firma_de(f"x {fecha} y") == "x <ts> y",
                    logs.firma_de(f"x {fecha} y")))

    e = logs.extraer(str(f))
    r.append(ok("el extracto cuenta eventos y grupos, no lineas",
                e["eventos_relevantes"] == 2 and e["grupos_distintos"] == 2, e))
    r.append(ok("y trae un ejemplo de cada grupo con su cuenta",
                all("ejemplo" in g and "veces" in g for g in e["grupos"])))

    # --- el mismo error repetido se cuenta, no se repite ---
    f2 = d / "repetido.log"
    f2.write_text("\n".join(
        f"2026-08-2{i%9} 10:00:0{i%9} [ERROR] app: no se pudo entregar el mensaje {i}"
        for i in range(47)), encoding="utf-8")
    e2 = logs.extraer(str(f2))
    r.append(ok("47 fallas iguales son UN grupo de 47, no 47 mensajes",
                e2["grupos_distintos"] == 1 and e2["grupos"][0]["veces"] == 47,
                (e2["grupos_distintos"], e2["grupos"][0]["veces"])))

    # --- cursor: entre corridas solo lo nuevo ---
    b = logs.barrer(conn, [{"ruta": str(f)}])
    r.append(ok("la primera pasada encuentra los errores",
                b and b[0]["eventos_relevantes"] == 2))
    b2 = logs.barrer(conn, [{"ruta": str(f)}])
    r.append(ok("la segunda, sin nada nuevo, no devuelve nada", b2 == [], b2))

    with f.open("a", encoding="utf-8") as fh:
        fh.write("2026-08-22 09:00:00,000 [ERROR] app: algo nuevo se rompio\n")
    b3 = logs.barrer(conn, [{"ruta": str(f)}])
    r.append(ok("y despues de escribir, solo lo nuevo",
                b3 and b3[0]["eventos_relevantes"] == 1, b3))

    # --- rotacion: si el archivo encoge, se relee ---
    f.write_text("2026-08-22 10:00:00,000 [ERROR] app: post rotacion\n", encoding="utf-8")
    b4 = logs.barrer(conn, [{"ruta": str(f)}])
    r.append(ok("si el archivo roto y encogio, no se lee basura",
                b4 and b4[0]["eventos_relevantes"] == 1, b4))

    # --- un directorio entero, y errores que no rompen el barrido ---
    b5 = logs.barrer(conn, [{"ruta": str(d)}], avanzar=False, desde_cero=True)
    r.append(ok("un directorio expande a todos sus .log", len(b5) >= 2, len(b5)))
    b5b = logs.barrer(conn, [{"ruta": str(d)}], avanzar=False)
    r.append(ok("y sin desde_cero respeta el cursor de cada uno",
                len(b5b) < len(b5), (len(b5b), len(b5))))
    b6 = logs.barrer(conn, [{"ruta": "/no/existe/x.log"}])
    r.append(ok("una ruta que no existe se reporta y no rompe el barrido",
                b6 and "error" in b6[0]))

    # --- umbral por patron: el ruido con reintento se calla hasta que crece ---
    # Tres del error que se reintenta solo y uno de otra cosa, en el mismo
    # archivo: el filtro tiene que quedarse con el segundo y solo con ese.
    g = d / "ruido.log"
    g.write_text("".join(
        "2026-09-08 10:0%d:00,000 [ERROR] analyze: respuesta Gemini vacia -> "
        "exit 1 (no se persiste, retry desde rango) call_id=%d\n" % (i, 700 + i)
        for i in range(3)
    ) + "2026-09-08 10:09:00,000 [ERROR] db: Refresh token has expired\n",
        encoding="utf-8")
    regla = [{"patron": "respuesta Gemini vacia", "minimo_veces": 20}]

    e1 = logs.extraer(str(g), 0, None, 12, regla)
    r.append(ok("bajo su umbral, el ruido con reintento no se reporta",
                len(e1["grupos"]) == 1
                and "Refresh token" in e1["grupos"][0]["ejemplo"], e1["grupos"]))
    r.append(ok("pero lo omitido queda contado, no desaparece",
                e1.get("omitidos") == {"grupos": 1, "eventos": 3}, e1.get("omitidos")))

    e2 = logs.extraer(str(g), 0, None, 12,
                      [{"patron": "respuesta Gemini vacia", "minimo_veces": 3}])
    r.append(ok("al llegar al umbral vuelve a reportarse",
                len(e2["grupos"]) == 2 and "omitidos" not in e2, e2.get("grupos")))

    e3 = logs.extraer(str(g), 0, None, 12, None)
    r.append(ok("sin reglas se comporta igual que antes",
                len(e3["grupos"]) == 2 and "omitidos" not in e3, e3.get("grupos")))

    # Un archivo donde TODO queda bajo su umbral no puede entrar al informe como
    # una entrada vacia: `eventos_relevantes` cuenta antes del filtro.
    h = d / "solo_ruido.log"
    h.write_text("2026-09-08 11:00:00,000 [ERROR] a: respuesta Gemini vacia\n",
                 encoding="utf-8")
    b7 = logs.barrer(conn, [{"ruta": str(h), "ignorar": regla}])
    r.append(ok("si todo quedo bajo umbral, el archivo no entra al informe",
                b7 == [], b7))

    shutil.rmtree(d, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
