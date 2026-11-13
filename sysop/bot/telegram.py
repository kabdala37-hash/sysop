"""Cliente de la API de Telegram. Lo minimo, y nada mas.

Cuatro llamadas: recibir mensajes, mandar, mostrar "escribiendo" y saber quien
es el bot. No se usa ninguna libreria de terceros porque no hace falta: son
peticiones HTTP contra una API estable, y una dependencia menos es una menos que
actualizar en un servidor que tiene que seguir avisando cuando todo lo demas se
rompio.

El long polling de `getUpdates` es a proposito y no un webhook: un webhook
necesita que el servidor este publicado en internet con TLS, y el observador no
abre ni un puerto. Ademas sigue funcionando si nginx esta caido, que es
exactamente uno de los momentos en los que uno quiere preguntarle que pasa.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger("sysop.bot.telegram")

API = "https://api.telegram.org/bot{token}/{metodo}"

# Telegram corta los mensajes en 4096 caracteres. Se parte antes, en 3800, para
# dejar aire al prefijo que se le agrega a cada parte.
LIMITE = 3800


class Telegram:
    def __init__(self, token: str, timeout: float = 35.0) -> None:
        self._token = token
        self._timeout = timeout

    # -- primitivas ---------------------------------------------------------

    def _pedir(self, metodo: str, payload: Optional[dict[str, Any]] = None,
               timeout: Optional[float] = None) -> Any:
        import httpx                                          # noqa: PLC0415

        url = API.format(token=self._token, metodo=metodo)
        with httpx.Client(timeout=timeout or self._timeout) as c:
            resp = c.post(url, json=payload or {})
        if resp.status_code != 200:
            # El texto del error de Telegram dice cual es el problema real
            # ("chat not found", "bot was blocked"), y perderlo deja un 400 mudo.
            raise RuntimeError(f"telegram {metodo} {resp.status_code}: "
                               f"{resp.text[:200]}")
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {metodo}: {str(data)[:200]}")
        return data.get("result")

    # -- lo que se usa ------------------------------------------------------

    def quien_soy(self) -> dict[str, Any]:
        """Verifica el token contra la API. Es lo primero que corre el bot."""
        return self._pedir("getMe", timeout=15.0) or {}

    def recibir(self, offset: int, espera_seg: int = 30) -> list[dict[str, Any]]:
        """Mensajes nuevos. Bloquea hasta `espera_seg` si no hay nada.

        `offset` es el id del ultimo update mas uno: es lo que le dice a
        Telegram que ya se procesaron los anteriores y puede olvidarlos. Sin
        avanzarlo, el mismo mensaje vuelve para siempre.
        """
        # El timeout HTTP tiene que ser MAYOR que el del long polling: si son
        # iguales, la peticion se corta justo cuando el servidor iba a
        # contestar y cada vuelta parece un error de red.
        return self._pedir("getUpdates",
                           {"offset": offset, "timeout": espera_seg,
                            "allowed_updates": ["message"]},
                           timeout=espera_seg + 10) or []

    def escribiendo(self, chat_id: int) -> None:
        """El acuse de recibo. Una respuesta puede tardar y el silencio no dice nada."""
        try:
            self._pedir("sendChatAction", {"chat_id": chat_id, "action": "typing"},
                        timeout=10.0)
        except Exception as e:  # noqa: BLE001
            # No poder mostrar "escribiendo" no es motivo para no contestar.
            log.debug("no se pudo mandar la accion de escritura: %s", e)

    def mandar(self, chat_id: int, texto: str) -> None:
        """Manda un mensaje, partido si no entra.

        Se manda como texto plano a proposito. Con `parse_mode` de Markdown,
        cualquier `_` o `*` que venga de un log del servidor rompe el formato y
        Telegram devuelve 400: el mensaje se pierde entero por un caracter que
        el agente ni eligio.
        """
        for parte in _partir(texto or "(sin respuesta)"):
            self._pedir("sendMessage", {"chat_id": chat_id, "text": parte,
                                        "disable_web_page_preview": True})


def _partir(texto: str) -> list[str]:
    """Corta en trozos que entren, respetando los saltos de linea."""
    if len(texto) <= LIMITE:
        return [texto]
    partes: list[str] = []
    actual = ""
    for linea in texto.split("\n"):
        # Una linea sola mas larga que el limite (un traceback pegado, por
        # ejemplo) se parte a lo bruto: es preferible a descartarla.
        while len(linea) > LIMITE:
            if actual:
                partes.append(actual)
                actual = ""
            partes.append(linea[:LIMITE])
            linea = linea[LIMITE:]
        if len(actual) + len(linea) + 1 > LIMITE:
            partes.append(actual)
            actual = linea
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        partes.append(actual)
    total = len(partes)
    return [f"({i}/{total})\n{p}" if total > 1 else p
            for i, p in enumerate(partes, 1)]
