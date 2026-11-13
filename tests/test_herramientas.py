"""Las herramientas de lectura del agente.

Puede moverse por donde necesite: no hay lista de rutas permitidas. Lo que esta
bateria protege es lo otro: que sea SOLO lectura, que los archivos de
credenciales no salgan, y que un error de herramienta sea un resultado y no una
excepcion que tumbe el diagnostico.
"""
import shutil, tempfile
from pathlib import Path

from sysop.llm import herramientas as h


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


def main():
    r = []
    d = Path(tempfile.mkdtemp())
    (d / "app.log").write_text(
        "\n".join([f"linea {i} todo bien" for i in range(200)]
                  + ["2026-08-28 ERROR conexion rechazada a mysql",
                     "2026-08-28 INFO ya vuelve"]), encoding="utf-8")
    (d / "secreto.env").write_text("DB_PASSWORD=nomelaroben\n", encoding="utf-8")
    (d / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (d / "con_datos.log").write_text(
        "llamar al +5491100000000 o escribir a juan@cliente.com\n", encoding="utf-8")
    (d / "sub").mkdir()

    # --- moverse libremente ---
    l = h.listar(str(d))
    nombres = {e["nombre"] for e in l["entradas"]}
    r.append(ok("lista cualquier directorio, sin lista blanca",
                {"app.log", "sub", "secreto.env"} <= nombres, nombres))
    r.append(ok("y marca cuales son secretos, para que ni los pida",
                any(e["nombre"] == "secreto.env" and e.get("secreto")
                    for e in l["entradas"])))
    r.append(ok("lee un archivo fuera de cualquier catalogo",
                "ERROR conexion rechazada" in h.leer(str(d / "app.log"))["contenido"]))

    # --- pero los secretos no salen ---
    for archivo in ("secreto.env", "id_rsa"):
        res = h.leer(str(d / archivo))
        r.append(ok(f"{archivo} no se lee", "error" in res and "contenido" not in res,
                    res))
    r.append(ok("y el motivo explica que no hace falta para diagnosticar",
                "no hace falta" in h.leer(str(d / "secreto.env"))["error"].lower()))

    # --- lo que vuelve pasa por el redactor ---
    c = h.leer(str(d / "con_datos.log"))["contenido"]
    r.append(ok("un telefono en un log no viaja al modelo", "5491100000000" not in c, c))
    r.append(ok("un correo tampoco", "juan@cliente.com" not in c))

    # --- buscar es lo que hace barato un log grande ---
    todo = h.leer(str(d / "app.log"), lineas=120)
    filtrado = h.leer(str(d / "app.log"), buscar="ERROR")
    r.append(ok("con buscar vuelve solo lo que importa",
                filtrado["coincidencias"] == 1
                and len(filtrado["contenido"]) < len(todo["contenido"]) / 10,
                (filtrado["coincidencias"], len(filtrado["contenido"]),
                 len(todo["contenido"]))))
    r.append(ok("un log largo NO se trunca a 400 chars como la evidencia",
                len(todo["contenido"]) > 1000, len(todo["contenido"])))
    r.append(ok("y siempre dice cuantas lineas tiene el archivo entero",
                filtrado["lineas_totales"] == 202, filtrado["lineas_totales"]))

    # --- los topes acotan lo que entra al contexto ---
    grande = d / "grande.log"
    grande.write_text("x" * 200_000, encoding="utf-8")
    res = h.leer(str(grande), lineas=999)
    r.append(ok("una lectura enorme se trunca",
                len(res["contenido"]) <= h.MAX_BYTES, len(res["contenido"])))

    # --- un error es un resultado, no una excepcion ---
    r.append(ok("un archivo que no existe devuelve error, no explota",
                "error" in h.leer(str(d / "no_existe"))))
    r.append(ok("leer un directorio sugiere la herramienta correcta",
                h.leer(str(d / "sub")).get("sugerencia") == "usar listar"))
    r.append(ok("listar un archivo tambien",
                h.listar(str(d / "app.log")).get("sugerencia") == "usar leer"))
    r.append(ok("una herramienta inexistente no rompe el bucle",
                "error" in h.ejecutar("borrar_todo", {"ruta": "/"})))
    r.append(ok("un argumento de mas se ignora en vez de fallar",
                "error" not in h.ejecutar("listar", {"ruta": str(d), "profundidad": 9})))

    # --- solo lectura: no hay forma de escribir ni ejecutar ---
    r.append(ok("las unicas herramientas son de lectura",
                set(h.EJECUTORES) == {"listar", "leer", "telemetria", "journal"},
                set(h.EJECUTORES)))
    r.append(ok("y lo declarado al modelo coincide con lo ejecutable",
                {x["name"] for x in h.DECLARACIONES} == set(h.EJECUTORES)))

    shutil.rmtree(d, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
