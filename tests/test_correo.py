"""Reglas de envio: cuando sale un correo, cuando no, y que nunca salga dos veces.

Desde el 2026-08-28 hay dos canales y ninguno lo decide el daemon por horario:

    informe periodico   lo dispara cron. La cadencia vive en /etc/cron.d
    alerta urgente      la dispara un incidente critico, en el momento
"""
import shutil, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sysop import alertas, informe as informe_mod
from sysop.core import incidents
from sysop.core.fuentes import Config, CuentaSMTP
from sysop.core.models import (CheckResult, EvidenceQuality, Severity, State)
from sysop.notify import outbox
from sysop.store import db


def nuevo():
    d = Path(tempfile.mkdtemp())
    c = db.connect(d / "t.db"); db.init_schema(c)
    return c, d


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


class CfgFalsa:
    installation = "test"


class CtxFalso:
    def __init__(self, conn):
        self.conn = conn
        self.cfg = CfgFalsa()


CUENTA = CuentaSMTP(host="127.0.0.1", port=1, secure=False, user="u",
                    password="p", from_addr="f@n.com")


def config_notificacion():
    return Config(cuenta=CUENTA, destinatarios=("a@b.c",), agente=None)


def abrir_incidente(conn, check_id, resource_id, severidad, motivo="ROTO"):
    r = CheckResult(
        check_id=check_id, resource_id=resource_id,
        observed_at=datetime.now(timezone.utc), state=State.FAILED,
        severity=severidad, reason_code=motivo, expected={}, actual={},
        evidence_quality=EvidenceQuality.FULL, evidence_refs=[], rule_version=1)
    return incidents.abrir(conn, r)


def main():
    r = []

    # --- idempotencia de la bandeja ---
    conn2, d2 = nuevo()
    i1 = outbox.encolar(conn2, "informe", "2026-08-27T08", "a", "<p>x</p>", ["a@b.c"])
    i2 = outbox.encolar(conn2, "informe", "2026-08-27T08", "a", "<p>x</p>", ["a@b.c"])
    r.append(ok("el mismo periodo no se encola dos veces",
                i1 is not None and i2 is None, (i1, i2)))
    i3 = outbox.encolar(conn2, "urgente", "INC-1", "b", "<p>y</p>", ["a@b.c"])
    r.append(ok("pero una urgencia del mismo dia SI (tipo distinto)", i3 is not None))

    # --- reintentos y Message-ID estable ---
    conn3, d3 = nuevo()
    outbox.encolar(conn3, "informe", "2026-08-27T08", "s", "<p>x</p>", ["a@b.c"])
    res1 = outbox.despachar(conn3, CUENTA)
    mid1 = conn3.execute("SELECT message_id, attempts, next_attempt_at FROM outbox").fetchone()
    r.append(ok("un fallo deja el correo para reintentar, no lo pierde",
                res1["reintentar"] == 1, res1))
    r.append(ok("y guarda el Message-ID desde el primer intento",
                mid1["message_id"] is not None))

    for _ in range(outbox.MAX_INTENTOS):
        conn3.execute("UPDATE outbox SET next_attempt_at = datetime('now','-1 hour')")
        outbox.despachar(conn3, CUENTA)
    fin = conn3.execute("SELECT state, attempts, message_id FROM outbox").fetchone()
    r.append(ok("tras agotar los reintentos queda FAILED, no reintentando para siempre",
                fin["state"] == "FAILED", dict(fin)))
    r.append(ok("y el Message-ID NO cambio entre reintentos",
                fin["message_id"] == mid1["message_id"]))
    r.append(ok("un correo fallido se puede reportar en el siguiente informe",
                len(outbox.fallidos(conn3)) == 1))

    # --- credencial mala: se corta ya, no gasta 3 intentos ---
    conn4, d4 = nuevo()
    outbox.encolar(conn4, "informe", "2026-08-28T08", "s", "<p>x</p>", ["a@b.c"])
    import sysop.notify.smtp as s
    orig = s.enviar
    s.enviar = lambda cuenta, msg, timeout_sec=30.0: s.Envio(
        ok=False, message_id=msg["Message-ID"], error="autenticacion: 535 bad password")
    outbox.despachar(conn4, CUENTA)
    s.enviar = orig
    f4 = conn4.execute("SELECT state, attempts FROM outbox").fetchone()
    r.append(ok("una credencial mala falla al primer intento, sin reintentar",
                f4["state"] == "FAILED" and f4["attempts"] == 1, dict(f4)))

    # --- alerta urgente: solo lo critico, y una sola vez ---
    conn5, d5 = nuevo()
    ctx = CtxFalso(conn5)
    cfg = config_notificacion()

    abrir_incidente(conn5, "HST-09", "mount:/", Severity.MAJOR)
    r.append(ok("un incidente major NO dispara alerta urgente",
                alertas.revisar(ctx, cfg) == 0))

    inc_id = abrir_incidente(conn5, "SVC-01", "systemd:back", Severity.CRITICAL)
    r.append(ok("un incidente critico SI la dispara",
                alertas.revisar(ctx, cfg) == 1))
    r.append(ok("y no la vuelve a mandar en el ciclo siguiente",
                alertas.revisar(ctx, cfg) == 0))
    r.append(ok("la urgencia se encola con el id del incidente como periodo",
                outbox.ya_enviado(conn5, alertas.TIPO, inc_id)))

    incidents.recuperar(conn5, inc_id)
    inc2 = abrir_incidente(conn5, "SVC-01", "systemd:otro", Severity.CRITICAL)
    r.append(ok("otro incidente critico distinto si dispara el suyo",
                alertas.revisar(ctx, cfg) == 1, inc2))

    # un critico ya recuperado no vuelve a alertar
    incidents.recuperar(conn5, inc2)
    r.append(ok("un critico ya recuperado no alerta",
                alertas.revisar(ctx, cfg) == 0))

    # --- el informe no se manda dos veces, y --forzar si ---
    conn7, d7 = nuevo()
    p1 = outbox.encolar(conn7, informe_mod.TIPO, "2026-08-28T08", "a", "<p>x</p>", ["a@b.c"])
    r.append(ok("el informe del periodo se encola una vez", p1 is not None))
    r.append(ok("y el segundo del mismo periodo no",
                outbox.encolar(conn7, informe_mod.TIPO, "2026-08-28T08", "a",
                               "<p>x</p>", ["a@b.c"]) is None))
    r.append(ok("pero forzado, con el periodo al minuto, si entra",
                outbox.encolar(conn7, informe_mod.TIPO, "2026-08-28T08:15", "a",
                               "<p>x</p>", ["a@b.c"]) is not None))

    # --- deteccion del observador caido ---
    conn6, d6 = nuevo()
    r.append(ok("sin un solo resultado, el observador figura caido",
                informe_mod._observador_caido(conn6) is not None))

    ahora = datetime.now(timezone.utc)
    conn6.execute(
        "INSERT INTO check_result (check_id, resource_id, observed_at, state, "
        "severity, rule_version) VALUES (?,?,?,?,?,1)",
        ("HST-01", "host", ahora.isoformat(), "OK", "info"))
    r.append(ok("con un resultado recien escrito, no",
                informe_mod._observador_caido(conn6) is None))

    viejo = (ahora - timedelta(seconds=informe_mod.SILENCIO_MAX_SEG + 120)).isoformat()
    conn6.execute("UPDATE check_result SET observed_at = ?", (viejo,))
    caido = informe_mod._observador_caido(conn6)
    r.append(ok("con el ultimo resultado viejo, vuelve a figurar caido",
                caido is not None and "minutos" in caido, caido))

    for x in (d2, d3, d4, d5, d6, d7):
        shutil.rmtree(x, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
