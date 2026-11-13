"""Lectura del host: filesystems, memoria, uptime, carga y presión.

Todo sale de `/proc`, `/sys` y `statvfs`. Sin dependencias externas y sin
ejecutar nada que pueda colgarse: leer un archivo de procfs es una operación
acotada, correr `df` no lo es (un NFS caído lo deja colgado para siempre, y el
observador se queda mudo justo cuando hay algo que reportar).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("sysop.so.host")

PROC = Path("/proc")

# Nada de esto es un filesystem real que valga la pena vigilar: son vistas del
# kernel, memoria o imágenes de solo lectura. Vigilarlas produce ruido puro.
TIPOS_PSEUDO = frozenset({
    "tmpfs", "devtmpfs", "squashfs", "overlay", "efivarfs", "autofs",
    "proc", "sysfs", "cgroup", "cgroup2", "devpts", "debugfs", "tracefs",
    "securityfs", "pstore", "bpf", "configfs", "fusectl", "hugetlbfs",
    "mqueue", "ramfs", "binfmt_misc", "nsfs", "rpc_pipefs",
})


@dataclass(frozen=True)
class Mount:
    target: str
    source: str
    fstype: str
    opciones: str = ""

    @property
    def solo_lectura(self) -> bool:
        """Un filesystem que se remontó ro es una falla grave y silenciosa: la
        aplicación empieza a fallar al escribir, no al leer, así que los
        síntomas aparecen lejos de la causa."""
        return "ro" in self.opciones.split(",")


@dataclass(frozen=True)
class UsoFS:
    mount: str
    total_bytes: int
    usado_bytes: int
    libre_bytes: int
    inodos_total: int
    inodos_usados: int

    @property
    def uso_pct(self) -> float:
        return 100.0 * self.usado_bytes / self.total_bytes if self.total_bytes else 0.0

    @property
    def inodos_pct(self) -> float:
        return 100.0 * self.inodos_usados / self.inodos_total if self.inodos_total else 0.0


@dataclass(frozen=True)
class Memoria:
    total_kb: int
    disponible_kb: int

    @property
    def disponible_pct(self) -> float:
        return 100.0 * self.disponible_kb / self.total_kb if self.total_kb else 0.0


# ---------------------------------------------------------------------------
# Filesystems
# ---------------------------------------------------------------------------

def listar_mounts() -> list[Mount]:
    """Mounts del sistema, leídos de /proc/1/mountinfo.

    **De PID 1 y no de `self`, y esto no es un detalle.** La unidad systemd del
    observador usa `ProtectSystem=strict`, que remonta el filesystem como
    read-only PARA EL PROCESO. Leyendo `/proc/self/mountinfo` el observador ve
    su propia vista restringida y reporta que todos los mounts estan en solo
    lectura, en cualquier host y siempre. Verificado en srv3 el 2026-08-27: tres
    incidentes criticos falsos en el primer arranque del servicio.

    PID 1 corre en el namespace real del sistema, que es lo que hay que
    observar. Su mountinfo es legible por cualquier usuario.

    Se usa mountinfo y no `findmnt` porque es un archivo: no hay proceso que
    pueda colgarse ni salida que parsear con formato variable entre versiones.
    """
    out: list[Mount] = []
    vistos: set[str] = set()
    try:
        texto = (PROC / "1" / "mountinfo").read_text(encoding="utf-8")
    except OSError:
        # Sin acceso al de init se cae al propio, marcando que puede estar
        # sesgado por el hardening del servicio.
        try:
            texto = (PROC / "self" / "mountinfo").read_text(encoding="utf-8")
            log.warning("no se pudo leer /proc/1/mountinfo; se usa el propio, "
                        "que bajo ProtectSystem puede decir ro donde hay rw")
        except OSError:
            return out

    for linea in texto.splitlines():
        # mountinfo: campos fijos, luego " - ", luego fstype source superopts
        if " - " not in linea:
            continue
        izq, der = linea.split(" - ", 1)
        campos_izq = izq.split()
        campos_der = der.split()
        if len(campos_izq) < 6 or len(campos_der) < 2:
            continue
        target = campos_izq[4].replace("\\040", " ")
        # Un mismo target puede aparecer más de una vez (bind mounts). Gana el
        # primero: contar dos veces el mismo filesystem duplicaría las alertas.
        if target in vistos:
            continue
        vistos.add(target)
        out.append(Mount(
            target=target,
            source=campos_der[1],
            fstype=campos_der[0],
            opciones=campos_izq[5],
        ))
    return out


def mounts_administrados(excluir_tipos: Iterable[str] = (),
                         excluir_prefijos: Iterable[str] = ()) -> list[Mount]:
    """Los mounts que vale la pena vigilar.

    Se DESCUBREN, no se listan en configuración: srv2 tiene `/mnt/datos` y srv3 no,
    así que cualquier lista fija sería falsa en alguno de los dos. La
    configuración aporta las exclusiones y los umbrales, no el inventario.
    """
    tipos = TIPOS_PSEUDO | set(excluir_tipos)
    prefijos = tuple(excluir_prefijos)
    out = []
    for m in listar_mounts():
        if m.fstype in tipos:
            continue
        if prefijos and m.target.startswith(prefijos):
            continue
        out.append(m)
    return out


def uso(mount: str) -> Optional[UsoFS]:
    """Ocupación de un mount. None si no se puede leer (no es un OK)."""
    try:
        st = os.statvfs(mount)
    except OSError:
        return None
    # f_bavail y no f_bfree: los bloques reservados para root no están
    # disponibles para la aplicación, y contarlos como libres hace que un disco
    # que ya rechaza escrituras se vea con espacio.
    total = st.f_blocks * st.f_frsize
    libre = st.f_bavail * st.f_frsize
    usado = total - st.f_bfree * st.f_frsize
    return UsoFS(
        mount=mount,
        total_bytes=total,
        usado_bytes=usado,
        libre_bytes=libre,
        inodos_total=st.f_files,
        inodos_usados=st.f_files - st.f_ffree,
    )


# ---------------------------------------------------------------------------
# Memoria, carga, arranque
# ---------------------------------------------------------------------------

def memoria() -> Optional[Memoria]:
    try:
        texto = (PROC / "meminfo").read_text(encoding="utf-8")
    except OSError:
        return None
    vals: dict[str, int] = {}
    for linea in texto.splitlines():
        partes = linea.split(":")
        if len(partes) == 2:
            m = re.match(r"\s*(\d+)", partes[1])
            if m:
                vals[partes[0]] = int(m.group(1))
    total = vals.get("MemTotal", 0)
    # MemAvailable y no MemFree: el kernel cuenta acá lo que puede recuperar del
    # cache. MemFree en un servidor sano es siempre bajo, y alertar por eso es
    # el error clásico de monitoreo de memoria en Linux.
    disp = vals.get("MemAvailable", vals.get("MemFree", 0))
    return Memoria(total_kb=total, disponible_kb=disp) if total else None


def uptime_seg() -> Optional[float]:
    try:
        return float((PROC / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def boot_id() -> Optional[str]:
    """Identificador del arranque actual.

    Es lo que distingue un reinicio de un uptime que se lee raro: si cambia el
    boot id, la máquina rearrancó, sin depender de comparar números de uptime
    entre muestras.
    """
    try:
        return (PROC / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
    except OSError:
        return None


def loadavg() -> Optional[tuple[float, float, float]]:
    try:
        p = (PROC / "loadavg").read_text().split()
        return float(p[0]), float(p[1]), float(p[2])
    except (OSError, ValueError, IndexError):
        return None


def presion(recurso: str) -> Optional[dict[str, float]]:
    """PSI: cuánto tiempo estuvo bloqueada la ejecución esperando un recurso.

    Es mejor señal que el uso: un disco al 100% de utilización con una sola cola
    corta no molesta a nadie, y uno al 40% con esperas largas sí.
    """
    try:
        texto = (PROC / "pressure" / recurso).read_text(encoding="utf-8")
    except OSError:
        return None   # kernel sin PSI o cgroup sin exponerlo
    out: dict[str, float] = {}
    for linea in texto.splitlines():
        partes = linea.split()
        if not partes:
            continue
        etiqueta = partes[0]
        for kv in partes[1:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                try:
                    out[f"{etiqueta}.{k}"] = float(v)
                except ValueError:
                    pass
    return out


def reloj_sincronizado() -> tuple[Optional[bool], Optional[float]]:
    """(sincronizado, offset en ms). (None, None) si no se puede determinar.

    Un reloj corrido rompe todo lo demás en silencio: las ventanas de los cron
    dejan de coincidir, las cohortes de llamadas se calculan mal y los
    incidentes quedan fechados en el futuro.
    """
    try:
        r = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized",
                            "--value"], capture_output=True, text=True, timeout=5)
        sync = r.stdout.strip() == "yes" if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        sync = None

    offset_ms: Optional[float] = None
    try:
        r = subprocess.run(["chronyc", "-c", "tracking"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            campos = r.stdout.strip().split(",")
            if len(campos) > 4:
                offset_ms = abs(float(campos[4])) * 1000.0
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    return sync, offset_ms


def snapshot(excluir_tipos: Iterable[str] = (),
             excluir_prefijos: Iterable[str] = ()) -> dict[str, Any]:
    """Foto completa del host para un ciclo."""
    ms = mounts_administrados(excluir_tipos, excluir_prefijos)
    return {
        "mounts": [
            {"mount": m.target, "source": m.source, "fstype": m.fstype,
             "solo_lectura": m.solo_lectura, "uso": uso(m.target)}
            for m in ms
        ],
        "memoria": memoria(),
        "uptime_seg": uptime_seg(),
        "boot_id": boot_id(),
        "loadavg": loadavg(),
        "cpus": os.cpu_count() or 1,
        "presion_cpu": presion("cpu"),
        "presion_io": presion("io"),
        "presion_memoria": presion("memory"),
    }
