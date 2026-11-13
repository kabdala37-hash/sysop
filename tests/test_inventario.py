"""El inventario: que un recurso que se fue se informe UNA vez y no para siempre.

Lo que protege esta bateria es la senal del log. Un observador que repite todos
los ciclos una novedad que ya no es novedad entrena a ignorarlo, y el dia que
diga algo de verdad nadie lo va a leer.
"""
import shutil
import tempfile
from pathlib import Path

from sysop.core import inventory
from sysop.core.models import Resource, ResourceState
from sysop.store import db


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


def rec(rid, kind="systemd"):
    return Resource(resource_id=rid, host_id="t", kind=kind)


def main():
    r = []
    d = Path(tempfile.mkdtemp())
    conn = db.connect(d / "t.db")
    db.init_schema(conn)
    KINDS = ("systemd",)

    def sync(*rids):
        return inventory.sincronizar(conn, [rec(x) for x in rids], "t", KINDS)

    # --- primera vez: se establece la linea de base, no se juzga ---
    c = sync("a", "b", "c")
    r.append(ok("la primera corrida marca primera_vez", c.primera_vez, c))
    r.append(ok("y no reporta novedades aunque todo sea nuevo",
                not c.hay_novedades, c))
    r.append(ok("pero si cuenta los recursos", len(c.nuevos) == 3, c.nuevos))

    # --- un recurso nuevo ---
    c = sync("a", "b", "c", "d")
    r.append(ok("un recurso nuevo se informa", [x.resource_id for x in c.nuevos] == ["d"],
                c.nuevos))
    r.append(ok("y no inventa desaparecidos", c.desaparecidos == (), c.desaparecidos))

    # --- uno que se va: se informa UNA vez ---
    c = sync("a", "b", "c")
    r.append(ok("el que se fue se informa", c.desaparecidos == ("d",), c.desaparecidos))
    fila = conn.execute("SELECT state FROM resource WHERE resource_id = 'd'").fetchone()
    r.append(ok("y queda RETIRED, no borrado (su historia es evidencia)",
                fila["state"] == ResourceState.RETIRED.value, dict(fila)))

    # --- EL BUG: en los ciclos siguientes NO se vuelve a informar ---
    for vuelta in range(1, 4):
        c = sync("a", "b", "c")
        r.append(ok("vuelta %d: el que ya se habia ido no se informa de nuevo" % vuelta,
                    c.desaparecidos == (), c.desaparecidos))
        r.append(ok("vuelta %d: y no hay novedades que loguear" % vuelta,
                    not c.hay_novedades, c))

    # --- si vuelve, no rompe y no se cuenta como desaparecido ---
    c = sync("a", "b", "c", "d")
    r.append(ok("el que vuelve no se informa como desaparecido",
                c.desaparecidos == (), c.desaparecidos))
    c = sync("a", "b", "c", "d")
    r.append(ok("y en el ciclo siguiente tampoco", c.desaparecidos == (), c.desaparecidos))

    # --- se va otra vez: eso SI es una novedad nueva ---
    c = sync("a", "b", "c")
    r.append(ok("si se va de nuevo, se informa de nuevo", c.desaparecidos == ("d",),
                c.desaparecidos))
    c = sync("a", "b", "c")
    r.append(ok("y otra vez, una sola vez", c.desaparecidos == (), c.desaparecidos))

    # --- el filtro por kind no hace desaparecer a los de otro tipo ---
    inventory.sincronizar(conn, [rec("m1", "mount")], "t", ("mount",))
    c = inventory.sincronizar(conn, [rec("a"), rec("b"), rec("c")], "t", KINDS)
    r.append(ok("sincronizar un kind no hace desaparecer los de otro",
                c.desaparecidos == (), c.desaparecidos))
    fila = conn.execute("SELECT state FROM resource WHERE resource_id = 'm1'").fetchone()
    r.append(ok("y el mount sigue vivo", fila["state"] != ResourceState.RETIRED.value,
                dict(fila)))

    # --- varios a la vez ---
    c = inventory.sincronizar(conn, [rec("a")], "t", KINDS)
    r.append(ok("dos que se van juntos se informan juntos",
                sorted(c.desaparecidos) == ["b", "c"], c.desaparecidos))
    c = inventory.sincronizar(conn, [rec("a")], "t", KINDS)
    r.append(ok("y despues ninguno", c.desaparecidos == (), c.desaparecidos))

    # ------------------------------------------------------------------
    # Clasificar a mano: "esto es nuestro, no avises".
    #
    # Las tres reglas no son nuevas, ya estaban decididas en el codigo: todo
    # silencio vence (`core/config.py`), un critico no se silencia nunca
    # (`alertas.py`) y descubrir no es aprobar. Esta bateria protege esas tres.
    # ------------------------------------------------------------------
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    inventory.sincronizar(conn, [rec("systemd:ajeno.service")], "t", KINDS)

    mal = []
    for caso, kw in (
            ("un estado inventado", dict(estado="LO_QUE_SEA")),
            ("sin motivo", dict(motivo="")),
            ("un motivo de dos letras", dict(motivo="ok")),
            ("sin autor", dict(quien="")),
            ("cero dias", dict(dias=0)),
            ("mas de 30 dias", dict(dias=31)),
            ("dias que no son numero", dict(dias="pronto"))):
        args = dict(resource_id="systemd:ajeno.service", host_id="t",
                    estado="IGNORED", motivo="es de otro sistema",
                    quien="telegram:Operador", dias=30)
        args.update(kw)
        try:
            inventory.clasificar(conn, **args)
            mal.append(caso)
        except inventory.ClasificacionInvalida:
            pass
    r.append(ok("se rechaza lo que no se puede clasificar (7 casos)", not mal, mal))

    try:
        inventory.clasificar(conn, "systemd:no-existe.service", "t", "IGNORED",
                             "no importa", "telegram:E", 5)
        r.append(ok("no se puede clasificar algo que el observador no vio", False))
    except inventory.ClasificacionInvalida as e:
        r.append(ok("no se puede clasificar algo que el observador no vio",
                    "no hay ningun recurso" in str(e), str(e)[:70]))

    hecho = inventory.clasificar(conn, "systemd:ajeno.service", "t", "EXTERNAL",
                                 "es de otro sistema, lo confirmo Operador",
                                 "telegram:Operador", 30)
    r.append(ok("clasificar guarda quien, por que y hasta cuando",
                hecho["quien"] == "telegram:Operador" and hecho["dias"] == 30
                and hecho["vence_at"] > _dt.now(_tz.utc).isoformat(), hecho))
    r.append(ok("y el recurso sale de la lista de sin clasificar",
                not any(x["resource_id"] == "systemd:ajeno.service"
                        for x in inventory.sin_clasificar(conn, "t"))))
    r.append(ok("el descubrimiento NO se lo lleva puesto en el ciclo siguiente",
                (inventory.sincronizar(conn, [rec("systemd:ajeno.service")], "t", KINDS)
                 or True) and
                conn.execute("SELECT state FROM resource WHERE resource_id=?",
                             ("systemd:ajeno.service",)).fetchone()["state"] == "EXTERNAL"))

    # LA REGLA QUE NO SE NEGOCIA
    r.append(ok("un WARNING sobre eso queda silenciado",
                inventory.silenciado(conn, "systemd:ajeno.service", "t",
                                     "warning") is not None))
    r.append(ok("PERO un MAJOR avisa igual",
                inventory.silenciado(conn, "systemd:ajeno.service", "t",
                                     "major") is None))
    r.append(ok("y un CRITICAL tambien",
                inventory.silenciado(conn, "systemd:ajeno.service", "t",
                                     "critical") is None))

    # Una clasificacion nueva reemplaza a la anterior, no se acumulan.
    inventory.clasificar(conn, "systemd:ajeno.service", "t", "IGNORED",
                         "cambio de opinion", "telegram:Operador", 7)
    vig = inventory.clasificaciones(conn, "t")
    r.append(ok("vigente hay UNA sola, la ultima",
                len(vig) == 1 and vig[0]["estado"] == "IGNORED", vig))

    # Vencida no rige: es lo que hace que un silencio no sea un control borrado.
    conn.execute("UPDATE resource_clasificacion SET vence_at = ? "
                 "WHERE resource_id = ?",
                 ((_dt.now(_tz.utc) - _td(days=1)).isoformat(),
                  "systemd:ajeno.service"))
    conn.commit()
    r.append(ok("una clasificacion VENCIDA deja de regir sola",
                inventory.clasificacion_vigente(conn, "systemd:ajeno.service", "t")
                is None))
    r.append(ok("y por lo tanto el WARNING vuelve a sonar",
                inventory.silenciado(conn, "systemd:ajeno.service", "t",
                                     "warning") is None))
    r.append(ok("pero queda en la historia si se la pide",
                len(inventory.clasificaciones(conn, "t", incluir_vencidas=True)) >= 1))

    inventory.clasificar(conn, "systemd:ajeno.service", "t", "IGNORED",
                         "otra vez, para probar el deshacer", "telegram:E", 3)
    r.append(ok("desclasificar deshace", inventory.desclasificar(
        conn, "systemd:ajeno.service", "t", "telegram:E")))
    r.append(ok("y el recurso vuelve a la lista de sin clasificar",
                any(x["resource_id"] == "systemd:ajeno.service"
                    for x in inventory.sin_clasificar(conn, "t"))))
    r.append(ok("deshacer algo que no estaba clasificado devuelve False",
                not inventory.desclasificar(conn, "systemd:ajeno.service", "t", "x")))

    shutil.rmtree(d, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
