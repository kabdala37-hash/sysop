"""Bandeja de salida persistente.

Un correo nace acá ANTES de intentar enviarse. Eso resuelve las dos formas de
fallar que tiene un notificador:

* **Duplicar.** La clave única `(report_type, period_start)` hace imposible que
  el mismo período se mande dos veces, aunque el servicio reinicie en medio del
  envío o alguien lo relance a mano.
* **Perder.** Si el envío falla, la fila queda pendiente con su próximo intento
  agendado. Al arrancar, el servicio retoma lo que quedó a medias.

Los reintentos reusan el mismo `Message-ID`, que es lo que hace que el servidor
del destinatario reconozca el reenvío como el mismo mensaje en vez de mostrar
tres copias.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sysop.notify import smtp
from sysop.core.fuentes import CuentaSMTP
from sysop.store import db, registro

log = logging.getLogger("sysop.notify.outbox")

# A los 5, 15 y 30 minutos. Después queda FAILED y visible como problema del
# propio observador: seguir reintentando para siempre esconde que el canal de
# notificación está roto, que es de las peores cosas que pueden pasar acá.
BACKOFF_MIN = (5, 15, 30)
MAX_INTENTOS = len(BACKOFF_MIN)


@dataclass(frozen=True)
class Pendiente:
    id: int
    report_type: str
    period_start: str
    asunto: str
    body_html: str
    destinatarios: tuple[str, ...]
    message_id: Optional[str]
    intentos: int


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def encolar(conn: Any, report_type: str, period_start: str, asunto: str,
            body_html: str, destinatarios: Sequence[str]) -> Optional[int]:
    """Crea el correo. Devuelve None si ese período ya estaba encolado.

    Devolver None en vez de lanzar es a propósito: que el período ya exista es
    el comportamiento CORRECTO y esperado, no un error. Es lo que pasa cada vez
    que el planificador vuelve a evaluar la misma hora.
    """
    try:
        cur = conn.execute(
            "INSERT INTO outbox (report_type, period_start, state, subject, "
            "body_html, recipients_json, created_at, next_attempt_at) "
            "VALUES (?,?,'PENDING',?,?,?,?,?)",
            (report_type, period_start, asunto, body_html,
             json.dumps(list(destinatarios)), _ahora().isoformat(),
             _ahora().isoformat()),
        )
        log.info("encolado %s para el periodo %s", report_type, period_start)
        return int(cur.lastrowid)
    except Exception as e:  # noqa: BLE001
        if "UNIQUE" in str(e).upper():
            return None
        raise


def pendientes(conn: Any, limite: int = 10) -> list[Pendiente]:
    filas = conn.execute(
        "SELECT * FROM outbox WHERE state = 'PENDING' "
        "  AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "ORDER BY created_at LIMIT ?",
        (_ahora().isoformat(), limite),
    ).fetchall()
    return [Pendiente(
        id=f["id"], report_type=f["report_type"], period_start=f["period_start"],
        asunto=f["subject"] or "", body_html=f["body_html"] or "",
        destinatarios=tuple(json.loads(f["recipients_json"] or "[]")),
        message_id=f["message_id"], intentos=f["attempts"],
    ) for f in filas]


def despachar(conn: Any, cuenta: CuentaSMTP, limite: int = 10) -> dict[str, int]:
    """Intenta enviar lo pendiente. Devuelve el conteo por resultado."""
    res = {"enviados": 0, "reintentar": 0, "fallidos": 0}

    for p in pendientes(conn, limite):
        if p.report_type == "urgente" and _ya_salio_por_otro_lado(conn, p):
            continue
        msg = smtp.armar(cuenta, p.destinatarios, p.asunto, p.body_html,
                         message_id=p.message_id)
        # Se persiste el Message-ID en el PRIMER intento, antes de saber si
        # salió. Si el envío llegó pero la respuesta se perdió, el reintento usa
        # el mismo id y el destinatario no ve dos correos.
        if not p.message_id:
            conn.execute("UPDATE outbox SET message_id = ? WHERE id = ?",
                         (msg["Message-ID"], p.id))

        envio = smtp.enviar(cuenta, msg)
        intentos = p.intentos + 1

        if envio.ok:
            # ANTES del UPDATE: el correo ya salio, y si la base no escribe lo
            # unico que queda para no mandarlo otra vez es el registro.
            anotar_envio(p.report_type, p.period_start, p.asunto, "bandeja")
            conn.execute(
                "UPDATE outbox SET state='SENT', attempts=?, sent_at=?, "
                "smtp_response=?, next_attempt_at=NULL WHERE id=?",
                (intentos, _ahora().isoformat(), envio.respuesta, p.id))
            res["enviados"] += 1
            log.info("enviado %s (%s) a %d destinatarios",
                     p.report_type, p.period_start, len(p.destinatarios))
            continue

        # Una credencial mala no se arregla reintentando: se corta ya y se hace
        # visible, en vez de gastar tres intentos y media hora para llegar al
        # mismo lugar.
        definitivo = "autenticacion" in envio.error.lower()

        if definitivo or intentos >= MAX_INTENTOS:
            conn.execute(
                "UPDATE outbox SET state='FAILED', attempts=?, last_error=?, "
                "next_attempt_at=NULL WHERE id=?",
                (intentos, envio.error[:500], p.id))
            res["fallidos"] += 1
            log.error("no se pudo enviar %s (%s) tras %d intentos: %s",
                      p.report_type, p.period_start, intentos, envio.error)
        else:
            proximo = _ahora() + timedelta(minutes=BACKOFF_MIN[intentos - 1])
            conn.execute(
                "UPDATE outbox SET attempts=?, last_error=?, next_attempt_at=? "
                "WHERE id=?",
                (intentos, envio.error[:500], proximo.isoformat(), p.id))
            res["reintentar"] += 1
            log.warning("fallo el envio de %s, reintento en %d min: %s",
                        p.report_type, BACKOFF_MIN[intentos - 1], envio.error)

    return res


def _ya_salio_por_otro_lado(conn: Any, p: Pendiente) -> bool:
    """¿El registro dice que esta alerta (o la de ese problema) ya salio?

    Dos casos: la alerta quedo pendiente con el SMTP fallando, se rompio la
    base y salio por el camino de emergencia; o salio, pero el UPDATE a SENT no
    se pudo escribir. En los dos, reintentarla era mandarla otra vez.
    """
    from sysop.core.incidents import fp_corto               # noqa: PLC0415
    previa = registro.alerta_vigente(fp_corto(p.period_start))
    if previa is None:
        return False
    conn.execute(
        "UPDATE outbox SET state='SENT', sent_at=?, next_attempt_at=NULL, "
        "smtp_response=? WHERE id=?",
        (_ahora().isoformat(),
         f"no se reenvia: ya salio {str(previa.get('at', ''))[:19]} "
         f"({previa.get('via')}, {previa.get('periodo')})", p.id))
    log.warning("alerta %s no se reenvia: el registro dice que ya salio (%s)",
                p.period_start, previa.get("periodo"))
    return True


def anotar_envio(tipo: str, periodo: str, asunto: str, via: str) -> None:
    """Deja en el registro en archivo que un correo salio.

    En una alerta el periodo es el id del incidente, y de ahi sale el
    fingerprint corto con el que se evita repetirla si la base se pierde.
    """
    from sysop.core.incidents import fp_corto               # noqa: PLC0415
    registro.anotar("correo_enviado", tipo=tipo, periodo=periodo,
                    fp=fp_corto(periodo) if tipo == "urgente" else "",
                    asunto=asunto, via=via)


def fallidos(conn: Any, desde_horas: int = 24) -> list[dict[str, Any]]:
    """Correos que no se pudieron entregar.

    El informe siguiente que SÍ salga tiene que mencionarlos: si no, el único
    registro de que la notificación falló está en un log que nadie mira.
    """
    return [dict(f) for f in conn.execute(
        "SELECT report_type, period_start, attempts, last_error FROM outbox "
        "WHERE state='FAILED' AND created_at >= ?",
        (db.corte(hours=int(desde_horas)),),
    ).fetchall()]


def ya_enviado(conn: Any, report_type: str, period_start: str) -> bool:
    f = conn.execute(
        "SELECT 1 FROM outbox WHERE report_type=? AND period_start=?",
        (report_type, period_start)).fetchone()
    return f is not None
