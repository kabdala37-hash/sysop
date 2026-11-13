"""Sockets en escucha, sondas HTTP y vencimiento de certificados.

Los listeners salen de `/proc/net/tcp`, que es un archivo: no hay proceso que
pueda colgarse ni formato de salida que cambie entre versiones de `ss`.
"""
from __future__ import annotations

import logging
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("sysop.so.net")

PROC_NET = Path("/proc/net")
LISTEN = "0A"   # TCP_LISTEN en /proc/net/tcp


@dataclass(frozen=True)
class Listener:
    ip: str
    puerto: int

    @property
    def publico(self) -> bool:
        """Escuchar en 0.0.0.0 o :: expone el puerto a todo el mundo.

        Distinguirlo de loopback importa: la plataforma corre el back y el
        supervisor en 127.0.0.1 a propósito, y que uno de esos aparezca en
        0.0.0.0 es un cambio de exposición, no un detalle.
        """
        return self.ip in ("0.0.0.0", "::")


def _hex_a_ip(h: str) -> str:
    if len(h) == 8:                       # IPv4, little endian
        b = bytes.fromhex(h)
        return ".".join(str(x) for x in reversed(b))
    if len(h) == 32:                      # IPv6
        try:
            b = bytes.fromhex(h)
            palabras = [b[i:i + 4][::-1] for i in range(0, 16, 4)]
            return socket.inet_ntop(socket.AF_INET6, b"".join(palabras))
        except (OSError, ValueError):
            return h
    return h


def listeners() -> list[Listener]:
    out: list[Listener] = []
    for archivo in ("tcp", "tcp6"):
        p = PROC_NET / archivo
        try:
            lineas = p.read_text(encoding="utf-8").splitlines()[1:]
        except OSError:
            continue
        for linea in lineas:
            campos = linea.split()
            if len(campos) < 4 or campos[3] != LISTEN:
                continue
            local = campos[1]
            if ":" not in local:
                continue
            ip_hex, puerto_hex = local.rsplit(":", 1)
            try:
                out.append(Listener(_hex_a_ip(ip_hex), int(puerto_hex, 16)))
            except ValueError:
                continue
    return out


def escucha_en(puerto: int) -> list[Listener]:
    return [l for l in listeners() if l.puerto == puerto]


# ---------------------------------------------------------------------------
# Sondas HTTP
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Sonda:
    ok: bool
    codigo: Optional[int]
    latencia_ms: float
    error: str = ""


def probar_http(url: str, timeout_sec: float = 5.0,
                espera_codigo: int = 200) -> Sonda:
    """Sonda a un endpoint local. Nunca lanza.

    Un 4xx tambien cuenta como respuesta: significa que el servicio está
    atendiendo. Lo que se mide acá es si contesta y en cuánto tiempo, no si el
    pedido era válido.
    """
    inicio = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "sysop-agent"})
        with urllib.request.urlopen(req, timeout=timeout_sec) as r:
            ms = (time.monotonic() - inicio) * 1000
            return Sonda(ok=r.status == espera_codigo, codigo=r.status,
                         latencia_ms=ms)
    except urllib.error.HTTPError as e:
        ms = (time.monotonic() - inicio) * 1000
        return Sonda(ok=e.code == espera_codigo, codigo=e.code, latencia_ms=ms)
    except Exception as e:  # noqa: BLE001
        ms = (time.monotonic() - inicio) * 1000
        return Sonda(ok=False, codigo=None, latencia_ms=ms,
                     error=f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Certificado:
    dias_restantes: Optional[int]
    subject: str = ""
    error: str = ""


def vencimiento_tls(host: str, puerto: int = 443,
                    timeout_sec: float = 5.0) -> Certificado:
    """Días hasta que vence el certificado.

    Se valida la cadena a propósito: un certificado que no valida es un
    problema aunque le queden trescientos días, y saltearse la verificación
    convertiría este control en uno que sólo mira la fecha.
    """
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, puerto), timeout=timeout_sec) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
        if not cert or "notAfter" not in cert:
            return Certificado(dias_restantes=None, error="sin notAfter")
        vence = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        vence = vence.replace(tzinfo=timezone.utc)
        dias = (vence - datetime.now(timezone.utc)).days
        subject = ""
        for parte in cert.get("subject", ()):
            for k, v in parte:
                if k == "commonName":
                    subject = v
        return Certificado(dias_restantes=dias, subject=subject)
    except Exception as e:  # noqa: BLE001
        return Certificado(dias_restantes=None, error=f"{type(e).__name__}: {e}")
