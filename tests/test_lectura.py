"""`leer()`: que traer 80 lineas no cueste el tamaño del archivo.

Antes esto hacia `f.readlines()` y recortaba despues. El observador puede pedir
un `.journal` de 54 MB o un `syslog.1` de 186 MB, y corre en un server con 1,9
GB disponibles y SIN swap.

El caso que importa no es que devuelva las lineas correctas —eso ya lo hacia—
sino CUANTA MEMORIA cuesta, asi que se mide.
"""
import shutil
import tempfile
import tracemalloc
from pathlib import Path

from sysop.llm import herramientas as h


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


def main():
    r = []
    d = Path(tempfile.mkdtemp())

    # --- correccion basica ---
    chico = d / "chico.log"
    chico.write_text("".join(f"linea {i}\n" for i in range(1, 501)), encoding="utf-8")

    out = h.leer(str(chico), lineas=5)
    r.append(ok("trae las ULTIMAS lineas por defecto",
                out["contenido"].strip().endswith("linea 500"), out["contenido"][-40:]))
    r.append(ok("y dice cuantas hay en total", out["lineas_totales"] == 500, out))
    r.append(ok("y numera bien desde el final", "496: linea 496" in out["contenido"],
                out["contenido"][:60]))

    out = h.leer(str(chico), lineas=3, desde_el_final=False)
    r.append(ok("con desde_el_final=False trae las primeras",
                "1: linea 1" in out["contenido"] and "linea 500" not in out["contenido"],
                out["contenido"]))

    out = h.leer(str(chico), buscar="linea 42")
    r.append(ok("buscar encuentra la linea", "42: linea 42" in out["contenido"],
                out["contenido"]))
    # 11 y no 1: "linea 42" tambien esta adentro de 420..429. Es substring, no
    # palabra, y esta bien que asi sea — buscar en un log es buscar un pedazo.
    r.append(ok("y cuenta TODAS las coincidencias, no solo la exacta",
                out["coincidencias"] == 11, out["coincidencias"]))

    out = h.leer(str(chico), buscar="linea", lineas=4)
    r.append(ok("con muchas coincidencias devuelve las ultimas y las cuenta todas",
                out["coincidencias"] == 500 and out["mostradas"] == 4, out))

    out = h.leer(str(chico), buscar="no-esta-en-ningun-lado")
    r.append(ok("sin coincidencias no rompe", out["coincidencias"] == 0, out))

    # --- LO QUE IMPORTA: la memoria ---
    grande = d / "grande.log"
    with grande.open("w", encoding="utf-8") as f:
        for i in range(300_000):          # ~20 MB
            f.write(f"2026-08-31 linea numero {i} con relleno para que pese algo\n")
    tam = grande.stat().st_size
    r.append(ok("el archivo de prueba pesa lo suficiente (>15 MB)",
                tam > 15 * 1024 * 1024, "%s bytes" % tam))

    tracemalloc.start()
    tracemalloc.reset_peak()
    out = h.leer(str(grande), lineas=80)
    _, pico = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    r.append(ok("leer 80 lineas de un archivo de 20 MB devuelve las 80",
                out["mostradas"] == 80, out.get("mostradas")))
    r.append(ok("y son las del final", "linea numero 299999" in out["contenido"],
                out["contenido"][-60:]))
    # Con readlines() el pico era del orden del archivo entero (y varias veces
    # mas, por el overhead de cada str). 2 MB deja margen de sobra y sigue
    # estando un orden de magnitud por debajo.
    r.append(ok("y el pico de memoria queda MUY por debajo del archivo (< 2 MB)",
                pico < 2 * 1024 * 1024,
                "pico=%.1f MB sobre un archivo de %.1f MB"
                % (pico / 1048576.0, tam / 1048576.0)))
    print("        (pico medido: %.2f MB para un archivo de %.1f MB)"
          % (pico / 1048576.0, tam / 1048576.0))

    tracemalloc.start()
    tracemalloc.reset_peak()
    out = h.leer(str(grande), buscar="numero 299999")
    _, pico_b = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    r.append(ok("buscar en un archivo grande tambien encuentra",
                out["coincidencias"] >= 1, out))
    r.append(ok("y tampoco se carga el archivo en memoria (< 2 MB)",
                pico_b < 2 * 1024 * 1024, "pico=%.1f MB" % (pico_b / 1048576.0)))

    # --- archivo por encima del tope: se lee la cola y SE DICE ---
    h_orig = h.MAX_ARCHIVO_BYTES
    h.MAX_ARCHIVO_BYTES = 1024 * 1024        # 1 MB, para no escribir 64
    try:
        out = h.leer(str(grande), lineas=10)
        r.append(ok("un archivo por encima del tope se lee igual",
                    out["mostradas"] == 10, out))
        r.append(ok("trae el final, que es lo que interesa",
                    "linea numero 299999" in out["contenido"], out["contenido"][-50:]))
        r.append(ok("AVISA que miro solo un pedazo (si no, diagnostica a ciegas)",
                    "parcial" in out and "MiB" in out["parcial"], out.get("parcial")))
        r.append(ok("y no miente con un total de lineas que no conto",
                    "lineas_totales" not in out, out.get("lineas_totales")))
    finally:
        h.MAX_ARCHIVO_BYTES = h_orig

    # --- bordes ---
    vacio = d / "vacio.log"
    vacio.write_text("", encoding="utf-8")
    out = h.leer(str(vacio))
    r.append(ok("un archivo vacio no rompe", out.get("mostradas") == 0, out))

    out = h.leer(str(d / "no-existe.log"))
    r.append(ok("un archivo que no existe da error claro", "no existe" in out.get("error", ""),
                out))
    out = h.leer(str(d))
    r.append(ok("un directorio da error y sugiere listar",
                "directorio" in out.get("error", ""), out))

    shutil.rmtree(d, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
