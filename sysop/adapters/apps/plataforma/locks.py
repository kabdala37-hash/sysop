"""Bloqueos de instancia única, verificados SIN tomarlos.

La plataforma usa `fcntl.flock` sobre archivos `.pid` en `/tmp` para garantizar
que un trabajo no corra dos veces (`acmeBot/locker/lock_instance.py`).

La especificación original propone averiguar si un `.pid` está abandonado
**intentando tomar el bloqueo no bloqueante**. Eso es peligroso:

    Si en ese instante no hay nadie corriendo, el observador TOMA el bloqueo.
    El trabajo que arranque medio segundo después se encuentra con que ya hay
    una instancia y se apaga solo, con `SystemExit(2)`.

O sea que el control provocaría exactamente la falla que dice observar, y de
forma intermitente, que es la peor manera de romper algo.

La vía correcta es `/proc/locks`, que el kernel expone como archivo de solo
lectura. Cada línea de tipo FLOCK trae `major:minor:inode`, y el inodo se
compara con el del `.pid` vía `stat`. Verificado en srv3 el 2026-08-27:
distinguió correctamente el daemon del canal (bloqueo vivo) de un `.pid`
huérfano del 20 de agosto.

Un detalle que convierte el archivo huérfano en señal útil: `PidFileLock.release()`
hace `unlink` al salir. Un `.pid` que sobrevive SIN bloqueo significa que el
proceso murió sin ejecutar su limpieza, o sea SIGKILL, OOM o corte de energía.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("sysop.nac.locks")

PROC_LOCKS = Path("/proc/locks")


@dataclass(frozen=True)
class EstadoLock:
    archivo: str
    existe: bool
    tiene_lock: bool
    pid_escrito: Optional[int]
    pid_del_kernel: Optional[int]
    inode: Optional[int]

    @property
    def huerfano(self) -> bool:
        """Archivo presente pero sin bloqueo: el proceso murió sin limpiar."""
        return self.existe and not self.tiene_lock

    @property
    def corriendo(self) -> bool:
        return self.existe and self.tiene_lock


def _inodos_con_flock() -> dict[int, int]:
    """`{inode: pid}` de todos los FLOCK vivos del sistema.

    Se lee una sola vez por ciclo y se consulta para todos los pidfiles: abrir
    y parsear `/proc/locks` por cada archivo sería recorrer la misma tabla
    docenas de veces.
    """
    out: dict[int, int] = {}
    try:
        texto = PROC_LOCKS.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("no se pudo leer /proc/locks: %s", e)
        return out

    for linea in texto.splitlines():
        # 1: FLOCK  ADVISORY  WRITE 12345 103:01:2052 0 EOF
        campos = linea.split()
        if len(campos) < 6 or campos[1] != "FLOCK":
            continue
        try:
            pid = int(campos[4])
        except ValueError:
            continue
        m = re.match(r"^([0-9a-f]+):([0-9a-f]+):(\d+)$", campos[5])
        if not m:
            continue
        out[int(m.group(3))] = pid
    return out


def estado(pidfile: Path, inodos: Optional[dict[int, int]] = None) -> EstadoLock:
    """Si el trabajo dueño de este pidfile está realmente corriendo."""
    if inodos is None:
        inodos = _inodos_con_flock()

    if not pidfile.exists():
        return EstadoLock(str(pidfile), False, False, None, None, None)

    try:
        st = pidfile.stat()
        inode = st.st_ino
    except OSError:
        return EstadoLock(str(pidfile), True, False, None, None, None)

    # El PID escrito adentro es informativo. NO se usa para decidir: puede estar
    # reciclado por otro proceso, o el archivo puede haber quedado truncado. La
    # verdad la tiene el kernel.
    pid_escrito: Optional[int] = None
    try:
        txt = pidfile.read_text(encoding="utf-8", errors="ignore").strip()
        if txt:
            pid_escrito = int(txt)
    except (OSError, ValueError):
        pass

    pid_kernel = inodos.get(inode)
    return EstadoLock(
        archivo=str(pidfile), existe=True, tiene_lock=pid_kernel is not None,
        pid_escrito=pid_escrito, pid_del_kernel=pid_kernel, inode=inode,
    )


def relevar(directorio: str = "/tmp",
            patron: str = "plataforma_*.pid") -> list[EstadoLock]:
    """Todos los pidfiles de la plataforma y su estado real."""
    inodos = _inodos_con_flock()
    d = Path(directorio)
    if not d.is_dir():
        return []
    try:
        archivos = sorted(d.glob(patron))
    except OSError:
        return []
    return [estado(p, inodos) for p in archivos]


def nombre_logico(pidfile: str, prefijo: str = "plataforma") -> tuple[str, str]:
    """`(nombre del trabajo, alcance)` a partir del nombre del archivo.

    El formato lo arma `build_pidfile_path`:
    `<prefijo>_<nombre>_<scope>.pid`. El alcance suele ser el tenant, y hace
    falta separarlo para poder colgar el resultado del recurso correcto.
    """
    base = Path(pidfile).name
    if base.endswith(".pid"):
        base = base[:-4]
    if base.startswith(prefijo + "_"):
        base = base[len(prefijo) + 1:]
    partes = base.rsplit("_", 1)
    if len(partes) == 2:
        return partes[0], partes[1]
    return base, ""
