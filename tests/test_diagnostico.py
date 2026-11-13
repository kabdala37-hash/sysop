"""El diagnostico con modelo: que se le manda, que se acepta, que puede silenciar.

La bateria protege la regla que no se negocia: **el modelo no puede hacer que un
problema critico deje de avisar.** Todo lo demas es secundario.
"""
import shutil, tempfile
from datetime import datetime, timezone
from pathlib import Path

from sysop import diagnostico as dg
from sysop.core import incidents
from sysop.core.models import CheckResult, EvidenceQuality, Severity, State
from sysop.llm import contexto, esquema, gateway
from sysop.store import db


def nuevo():
    d = Path(tempfile.mkdtemp())
    c = db.connect(d / "t.db"); db.init_schema(c)
    return c, d


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


def abrir(conn, check_id, resource_id, sev, motivo="ROTO"):
    r = CheckResult(check_id=check_id, resource_id=resource_id,
                    observed_at=datetime.now(timezone.utc), state=State.FAILED,
                    severity=sev, reason_code=motivo, expected={"x": 1},
                    actual={"x": 2}, evidence_quality=EvidenceQuality.FULL,
                    evidence_refs=[], rule_version=1)
    return incidents.abrir(conn, r)


D_REAL = esquema.Diagnostico(es_real=True, confianza="alta", causa="c", accion="a")
D_DESCARTE = esquema.Diagnostico(es_real=False, confianza="alta", causa="c",
                                 accion="a", descarte="fue un pico")
D_DUDA = esquema.Diagnostico(es_real=False, confianza="media", causa="c",
                             accion="a", descarte="puede que sea un pico")


def main():
    r = []

    # --- esquema estricto ---
    r.append(ok("un objeto completo se acepta",
                esquema.validar({"es_real": True, "confianza": "alta",
                                 "causa_probable": "x", "accion_sugerida": "y"})
                is not None))
    r.append(ok("prosa en vez de JSON se rechaza",
                esquema.validar("me parece que el disco esta lleno") is None))
    r.append(ok("una confianza inventada se rechaza",
                esquema.validar({"es_real": True, "confianza": "segurisimo",
                                 "causa_probable": "x", "accion_sugerida": "y"})
                is None))
    r.append(ok("es_real como string se rechaza (no se convierte)",
                esquema.validar({"es_real": "true", "confianza": "alta",
                                 "causa_probable": "x", "accion_sugerida": "y"})
                is None))
    r.append(ok("un descarte con confianza media NO es descartable",
                not D_DUDA.descartable))
    r.append(ok("con confianza alta si", D_DESCARTE.descartable))

    # --- redaccion ---
    sucio = {"password": "x", "DB_PASSWORD": "y", "api_key": "z",
             "nota": "llamar al +5491100000000 o a juan@cliente.com",
             "hondo": {"lista": [{"Authorization": "Bearer t"}]},
             "usuario": "sysop_ro", "conexiones": 232}
    limpio = contexto.redactar(sucio)
    plano = str(limpio)
    r.append(ok("no salen credenciales",
                "x" not in [limpio["password"]] and limpio["api_key"] == "[redactado]"
                and limpio["hondo"]["lista"][0]["Authorization"] == "[redactado]"))
    r.append(ok("no salen telefonos ni correos de cliente",
                "5491100000000" not in plano and "juan@cliente.com" not in plano))
    r.append(ok("SI sale la identidad interna, que es lo que sirve para diagnosticar",
                limpio["usuario"] == "sysop_ro" and limpio["conexiones"] == 232))

    # Las marcas de tiempo son la evidencia que distingue un pico de una
    # tendencia. El patron de telefono se las comia enteras: el paquete llegaba
    # con "[telefono]T15:02:[telefono]" y el modelo diagnosticaba sin horas.
    fechas = {"cuando": "2026-08-28T15:02:33.123456+00:00",
              "corto": "2026-08-28",
              "mezcla": "el 2026-08-28T15:02:33 llamar al +5491100000000"}
    lf = contexto.redactar(fechas)
    r.append(ok("una marca de tiempo ISO sobrevive entera",
                lf["cuando"] == fechas["cuando"] and lf["corto"] == "2026-08-28",
                lf))
    r.append(ok("y en la misma linea la fecha queda y el telefono no",
                "2026-08-28T15:02:33" in lf["mezcla"]
                and "5491100000000" not in lf["mezcla"], lf["mezcla"]))

    # --- que NO se puede silenciar ---
    r.append(ok("un critico nunca se filtra, sea el control que sea",
                dg.sin_filtro("QUE-01", "critical", {})))
    r.append(ok("los controles de la lista dura tampoco, aun en major",
                all(dg.sin_filtro(c, "major", {})
                    for c in ("SVC-01", "DB-01", "HST-09", "HST-12"))))
    r.append(ok("un control cualquiera en major si se filtra",
                not dg.sin_filtro("QUE-01", "major", {})))
    r.append(ok("la config puede sumar controles, no sacar",
                dg.sin_filtro("QUE-01", "major", {"sin_filtro": ["QUE-01"]})
                and dg.sin_filtro("SVC-01", "major", {"sin_filtro": []})))

    # --- descartar no es cerrar, y un critico no se descarta ---
    conn, d = nuevo()
    i_menor = abrir(conn, "QUE-01", "cola:x", Severity.MAJOR)
    dg.guardar(conn, i_menor, D_DESCARTE, filtrado=True, modelo="m")
    r.append(ok("un major descartado con confianza alta deja de contar",
                dg.descartado(conn, i_menor)))
    abierto = conn.execute("SELECT closed_at FROM incident WHERE incident_id=?",
                           (i_menor,)).fetchone()["closed_at"]
    r.append(ok("pero el incidente sigue ABIERTO: descartar no es cerrar",
                abierto is None))

    i_dudoso = abrir(conn, "QUE-02", "cola:y", Severity.MAJOR)
    dg.guardar(conn, i_dudoso, D_DUDA, filtrado=True, modelo="m")
    r.append(ok("con confianza media el aviso sale igual",
                not dg.descartado(conn, i_dudoso)))

    i_critico = abrir(conn, "SVC-01", "systemd:back", Severity.CRITICAL)
    dg.guardar(conn, i_critico, D_DESCARTE,
               filtrado=not dg.sin_filtro("SVC-01", "critical", {}), modelo="m")
    r.append(ok("UN CRITICO DESCARTADO POR EL MODELO AVISA IGUAL",
                not dg.descartado(conn, i_critico)))

    activos = incidents.activos(conn, Severity.WARNING)
    cuentan, descartados = dg.separar(conn, activos)
    r.append(ok("separar deja el descartado aparte y los otros dos adentro",
                len(activos) == 3 and len(descartados) == 1 and len(cuentan) == 2,
                (len(cuentan), len(descartados))))
    r.append(ok("y el que cuenta lleva su diagnostico adjunto",
                any(i.get("diagnostico") for i in cuentan)))

    # --- presupuesto y corte ---
    # El `at` se escribe con isoformat() en produccion (gateway._registrar), no
    # con `datetime('now')` de SQLite. El test usaba lo segundo y pasaba igual
    # porque la comparacion rota daba por buena cualquier fila del dia: dos
    # errores que se tapaban entre si.
    from datetime import datetime as _dt, timezone as _tz
    conn.execute("INSERT INTO llm_call (at, costo_usd, ok) VALUES (?, 3.0, 1)",
                 (_dt.now(_tz.utc).isoformat(),))
    r.append(ok("el gasto del dia se acumula", gateway.gastado_hoy(conn) == 3.0))

    class AgenteFalso:
        utilizable = True
        modelo = "m"
        vendor = "gemini"
        prompt = "p"
        api_key = "k"
        base_url = ""
    r.append(ok("con el presupuesto agotado no se llama al modelo",
                gateway.diagnosticar(conn, AgenteFalso(), {}, i_menor,
                                     {"presupuesto_diario_usd": 2.0}) is None))

    c2 = gateway._Corte()
    for _ in range(gateway.FALLOS_PARA_CORTAR):
        c2.fallo()
    r.append(ok("tres fallos seguidos abren el corte", c2.cortado))
    c3 = gateway._Corte()
    c3.fallo(); c3.exito(); c3.fallo(); c3.fallo()
    r.append(ok("un exito en el medio reinicia la cuenta", not c3.cortado))

    # --- un incidente que el modelo no supo contestar no bloquea la cola ---
    conn2, d2 = nuevo()
    viejo = abrir(conn2, "QUE-01", "cola:vieja", Severity.MAJOR)
    nuevo_i = abrir(conn2, "QUE-02", "cola:nueva", Severity.MAJOR)
    r.append(ok("el mas viejo sale primero",
                dg.pendientes(conn2, 1)[0]["incident_id"] == viejo))
    for _ in range(dg.MAX_INTENTOS):
        conn2.execute("INSERT INTO llm_call (at, incident_id, ok) "
                      "VALUES (?, ?, 1)",
                      (_dt.now(_tz.utc).isoformat(), viejo))
    quedan = [i["incident_id"] for i in dg.pendientes(conn2, 5)]
    r.append(ok("agotados los intentos deja de reintentarse", viejo not in quedan))
    r.append(ok("y el que venia atras deja de estar tapado", nuevo_i in quedan))
    shutil.rmtree(d2, ignore_errors=True)

    # --- sin agente configurado no explota nada ---
    r.append(ok("sin agente, diagnosticar devuelve None sin romper",
                gateway.diagnosticar(conn, None, {}, i_menor, {}) is None))

    shutil.rmtree(d, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
