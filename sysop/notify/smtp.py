"""Envío de correo.

Soporta STARTTLS (587) y SMTPS (465), que son los dos modos que ya usa la
plataforma. La diferencia con el `email_sender.py` de acmeBot es que acá el
`Message-ID` lo genera y lo devuelve el llamador: los reintentos tienen que
reusar el mismo, o un problema de red transitorio termina mandando el mismo
digest tres veces.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Optional, Sequence

from sysop.core.fuentes import CuentaSMTP

log = logging.getLogger("sysop.notify.smtp")


@dataclass(frozen=True)
class Envio:
    ok: bool
    message_id: str
    respuesta: str = ""
    error: str = ""


def armar(cuenta: CuentaSMTP, destinatarios: Sequence[str], asunto: str,
          html: str, texto: str = "",
          message_id: Optional[str] = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cuenta.from_addr
    msg["To"] = ", ".join(destinatarios)
    msg["Subject"] = asunto
    msg["Date"] = formatdate(localtime=True)
    # Estable entre reintentos: es lo que hace que el servidor de correo del
    # destinatario reconozca el reenvío como el mismo mensaje y no lo duplique.
    msg["Message-ID"] = message_id or make_msgid(domain="example.com")
    msg["Auto-Submitted"] = "auto-generated"     # no dispara autorespondedores

    msg.set_content(texto or _a_texto(html))
    msg.add_alternative(html, subtype="html")
    return msg


def _a_texto(html: str) -> str:
    import re
    t = re.sub(r"<br\s*/?>", "\n", html)
    t = re.sub(r"</(p|div|tr|h[1-6])>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def enviar(cuenta: CuentaSMTP, msg: EmailMessage,
           timeout_sec: float = 30.0) -> Envio:
    """Un intento. No reintenta: de eso se ocupa la bandeja de salida, que es
    la que sabe cuántas veces se probó y con qué Message-ID."""
    mid = msg["Message-ID"]
    smtp = None
    try:
        ctx = ssl.create_default_context()
        if cuenta.secure:
            smtp = smtplib.SMTP_SSL(cuenta.host, cuenta.port,
                                    timeout=timeout_sec, context=ctx)
        else:
            smtp = smtplib.SMTP(cuenta.host, cuenta.port, timeout=timeout_sec)
            smtp.ehlo()
            smtp.starttls(context=ctx)
            smtp.ehlo()
        smtp.login(cuenta.user, cuenta.password)
        rechazos = smtp.send_message(msg)
        if rechazos:
            return Envio(ok=False, message_id=mid,
                         error=f"destinatarios rechazados: {rechazos}")
        return Envio(ok=True, message_id=mid, respuesta="250 aceptado")
    except smtplib.SMTPAuthenticationError as e:
        # Se distingue a propósito: una credencial mala no se arregla
        # reintentando, y la bandeja de salida no debería gastar tres intentos.
        return Envio(ok=False, message_id=mid, error=f"autenticacion: {e}")
    except Exception as e:  # noqa: BLE001
        return Envio(ok=False, message_id=mid, error=f"{type(e).__name__}: {e}")
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass


def probar(cuenta: CuentaSMTP, destinatarios: Sequence[str],
           instalacion: str) -> Envio:
    """La operación de 'enviar correo de prueba' que pide la especificación.

    Comprueba de verdad lo que dice comprobar: resuelve el host, negocia TLS,
    autentica y entrega. Un OK acá significa que el canal funciona, no que la
    configuración parece razonable.
    """
    html = (
        "<p>Prueba de configuracion del observador.</p>"
        f"<p>Instalacion: <b>{instalacion}</b><br>"
        f"Cuenta: {cuenta.user}<br>"
        f"Servidor: {cuenta.host}:{cuenta.port} "
        f"({'SMTPS' if cuenta.secure else 'STARTTLS'})</p>"
        "<p>Si estas leyendo esto, el canal de notificacion funciona.</p>"
    )
    msg = armar(cuenta, destinatarios,
                f"[Sysop IA][{instalacion}] Prueba de configuracion", html)
    return enviar(cuenta, msg)
