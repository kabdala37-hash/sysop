"""El logging del observador, escrito como manda la convención.

Referencia: `CONVENCION-DE-LOGS.md`, en la raíz de este repo.

    <timestamp ISO con zona> <NIVEL> <app>/<componente> <ctx> | <mensaje> | <datos>

Es el primer componente que se migra, y a propósito: es el que consume los logs
de todos los demás, así que si el formato tiene un problema se descubre acá sin
tocar producción.

Dos cosas que el formato pide y que hay que resolver en código, no en el patrón:

- **El nombre del logger es `sysop.engine` y la convención pide
  `sysop-agent/engine`.** Se traduce en el formatter en vez de renombrar los
  loggers, que romperían los filtros por módulo.
- **El contexto va antes del `|`, y sólo si existe.** Un `logger.info("x",
  extra={"ctx": {"tenant": "clientea"}})` sale como `tenant=clientea | x`; sin `extra`,
  no sale nada de más.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional

APP = "sysop-agent"

# Diez megas por cinco: lo mismo que ya usa acmeBot. La convención pide
# rotación obligatoria, y este servicio corre para siempre.
MAX_BYTES = 10_000_000
COPIAS = 5


class Formato(logging.Formatter):
    """El formato de la convención, con la zona horaria del servidor."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).astimezone().isoformat(
            timespec="milliseconds")
        origen = f"{APP}/{_componente(record.name)}"
        ctx = _pares(getattr(record, "ctx", None))
        datos = _pares(getattr(record, "datos", None))

        linea = f"{ts} {record.levelname} {origen}"
        if ctx:
            linea += f" {ctx}"
        linea += f" | {record.getMessage()}"
        if datos:
            linea += f" | {datos}"

        if record.exc_info:
            # Indentado debajo del evento, como pide la convención: así la línea
            # que dice cuándo y quién viaja pegada a la que dice qué.
            traza = self.formatException(record.exc_info)
            linea += "\n" + "\n".join("    " + l for l in traza.splitlines())
        return linea


def _componente(nombre: str) -> str:
    """`sysop.core.engine` -> `core.engine`. El prefijo ya está en la app."""
    return nombre[6:] if nombre.startswith("sysop.") else nombre


def _pares(d: Any) -> str:
    if not isinstance(d, dict) or not d:
        return ""
    return " ".join(f"{k}={_valor(v)}" for k, v in d.items())


def _valor(v: Any) -> str:
    s = str(v)
    # Un espacio adentro rompería el parseo de pares: se marca en vez de
    # perderlo en silencio.
    return s.replace(" ", "_")[:120] if s else '""'


def configurar(nivel: str = "INFO", archivo: Optional[Path] = None,
               a_stdout: bool = True) -> None:
    """Deja el logging raíz como manda la convención.

    Al journal siempre (systemd lo captura de stdout) y a archivo si se pasa
    uno. La convención pide las dos: el journal sobrevive a que alguien borre el
    archivo, y el archivo sobrevive a que el journal rote antes de que alguien
    mire.
    """
    root = logging.getLogger()
    root.setLevel(nivel)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = Formato()
    if a_stdout:
        h = logging.StreamHandler()
        h.setFormatter(fmt)
        root.addHandler(h)

    if archivo is not None:
        try:
            archivo.parent.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(str(archivo), maxBytes=MAX_BYTES,
                                     backupCount=COPIAS, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            # Sin permiso sobre el directorio de logs el servicio arranca igual:
            # queda el journal, que es el canal que importa.
            root.warning("no se pudo abrir el log en %s | error=%s", archivo, e)

    # El conector de MySQL es muy verboso en INFO y llena el journal con el
    # nombre del plugin de autenticacion en cada conexion.
    logging.getLogger("mysql.connector").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
