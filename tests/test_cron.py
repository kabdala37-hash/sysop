"""Parser de cron, contra las expresiones REALES de los 17 jobs de srv2."""
from datetime import datetime, timezone
from sysop.adapters.apps.plataforma import cron

# Sacadas de rc_cron_jobs en produccion el 2026-08-27.
REALES = [
    ("*/15 * * * *", 900,    "Analizar Llamadas / Procesar Llamadas / Max"),
    ("*/10 * * * *", 600,    "Sincronizar llamadas / Test de sync"),
    ("*/5 * * * *",  300,    "Mail Dispatcher / Canal Sender / Canal Dispatch"),
    ("55 9 * * *",   86400,  "Daily Department Email Report"),
    ("0 6 * * *",    86400,  "Reconcile day (clientea)"),
    ("0 5 * * *",    86400,  "Reconcile day (clienteb)"),
    ("0 10 * * *",   86400,  "Email Report"),
    ("0 11 * * 1,4", None,   "Radar de Leads (lunes y jueves)"),
    ("30 11 * * 1,4", None,  "Radar de Ventas"),
    ("0 11 * * 1",   604800, "Reporte No Llamar (TCPA), solo lunes"),
]


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


def main():
    r = []
    print("=== las 10 expresiones distintas que hay en produccion ===")
    for expr, periodo, desc in REALES:
        try:
            c = cron.parsear(expr)
            p = cron.periodo_seg(expr)
            bien = (periodo is None) or (p == periodo)
            r.append(ok("%-16s %-8s %s" % (expr, "%ss" % p, desc), bien,
                        "esperaba %s" % periodo))
        except cron.CronInvalido as e:
            r.append(ok(expr, False, e))

    print("\n=== ultima corrida esperada ===")
    # Jueves 27 de agosto de 2026, 14:37 UTC
    ahora = datetime(2026, 8, 27, 14, 37, tzinfo=timezone.utc)
    casos = [
        ("*/15 * * * *", "2026-08-27 14:30", "cada 15 min"),
        ("*/5 * * * *",  "2026-08-27 14:35", "cada 5 min"),
        ("55 9 * * *",   "2026-08-27 09:55", "hoy mas temprano"),
        ("0 11 * * 1",   "2026-08-24 11:00", "el lunes pasado"),
        ("0 11 * * 1,4", "2026-08-27 11:00", "hoy es jueves"),
        ("0 6 * * *",    "2026-08-27 06:00", "esta madrugada"),
    ]
    for expr, esperado, desc in casos:
        u = cron.ultima_esperada(expr, ahora)
        got = u.strftime("%Y-%m-%d %H:%M") if u else "None"
        r.append(ok("%-14s -> %-17s %s" % (expr, got, desc), got == esperado,
                    "esperaba %s" % esperado))

    print("\n=== la zona horaria del job importa ===")
    # 0 8 * * * en Buenos Aires (UTC-3) = 11:00 UTC
    u_utc = cron.ultima_esperada("0 8 * * *", ahora, "UTC")
    u_ar = cron.ultima_esperada("0 8 * * *", ahora, "America/Argentina/Buenos_Aires")
    r.append(ok("misma expresion, distinto TZ, distinto instante",
                u_utc.utctimetuple() != u_ar.utctimetuple(),
                "%s vs %s" % (u_utc, u_ar)))
    r.append(ok("  y en AR las 08:00 locales son las 11:00 UTC",
                u_ar.astimezone(timezone.utc).hour == 11,
                u_ar.astimezone(timezone.utc)))

    print("\n=== dia-del-mes y dia-de-semana se combinan con OR, no AND ===")
    # "0 0 1 * 1" = el dia 1 O los lunes. El 2026-09-01 es martes: igual toca.
    c = cron.parsear("0 0 1 * 1")
    t_martes1 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    r.append(ok("el dia 1 aunque no sea lunes", cron._coincide(c, t_martes1)))
    t_lunes7 = datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc)
    r.append(ok("y un lunes aunque no sea dia 1", cron._coincide(c, t_lunes7)))

    print("\n=== domingo se escribe 0 o 7 ===")
    dom = datetime(2026, 8, 30, 3, 0, tzinfo=timezone.utc)   # domingo
    r.append(ok("con 0", cron._coincide(cron.parsear("0 3 * * 0"), dom)))
    r.append(ok("con 7", cron._coincide(cron.parsear("0 3 * * 7"), dom)))

    print("\n=== una expresion invalida es un HALLAZGO, no un 'no toca' ===")
    for mala in ("* * * *", "60 * * * *", "*/0 * * * *", "abc * * * *", "* 25 * * *"):
        try:
            cron.parsear(mala)
            r.append(ok("%-16s rechazada" % mala, False, "la acepto"))
        except cron.CronInvalido:
            r.append(ok("%-16s rechazada" % mala, True))

    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
