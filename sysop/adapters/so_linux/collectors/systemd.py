"""Estado de las unidades de systemd.

Se usa `systemctl show`, que devuelve pares clave=valor estables entre
versiones, en vez de parsear la salida de `list-units`, que está pensada para
que la lea una persona y cambia de formato.

Todas las llamadas llevan timeout. Un `systemctl` colgado (pasa cuando el bus
está saturado) no puede llevarse puesto el ciclo del observador.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("sysop.so.systemd")

PROPIEDADES = (
    "Id", "LoadState", "ActiveState", "SubState", "UnitFileState",
    "NRestarts", "MainPID", "ExecMainStartTimestampMonotonic",
    "Result", "ActiveEnterTimestamp",
)


@dataclass(frozen=True)
class Unidad:
    id: str
    load_state: str
    active_state: str
    sub_state: str
    unit_file_state: str
    n_restarts: int
    main_pid: int
    result: str
    activa_desde: str = ""

    @property
    def existe(self) -> bool:
        """`not-found` es distinto de `inactive`: la unidad ni siquiera está
        instalada. Confundirlos hace que un servicio que alguien borró se
        reporte como simplemente detenido."""
        return self.load_state != "not-found"

    @property
    def activa(self) -> bool:
        return self.active_state == "active"

    @property
    def en_bucle(self) -> bool:
        """`activating` + `auto-restart` es el estado de un servicio que muere y
        systemd relanza sin parar. No aparece como `failed` (systemd lo está
        reintentando), así que un control que sólo mire `ActiveState != active`
        no lo ve: puede caer justo en el instante en que está activating."""
        return self.active_state == "activating" and self.sub_state == "auto-restart"


def mostrar(unidad: str, timeout_sec: float = 5.0) -> Optional[Unidad]:
    try:
        r = subprocess.run(
            ["systemctl", "show", unidad, "--no-pager",
             "-p", ",".join(PROPIEDADES)],
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("systemctl show %s fallo: %s", unidad, e)
        return None
    if r.returncode != 0:
        return None

    kv: dict[str, str] = {}
    for linea in r.stdout.splitlines():
        if "=" in linea:
            k, v = linea.split("=", 1)
            kv[k] = v

    return Unidad(
        id=kv.get("Id", unidad),
        load_state=kv.get("LoadState", "unknown"),
        active_state=kv.get("ActiveState", "unknown"),
        sub_state=kv.get("SubState", "unknown"),
        unit_file_state=kv.get("UnitFileState", "unknown"),
        n_restarts=int(kv.get("NRestarts", "0") or 0),
        main_pid=int(kv.get("MainPID", "0") or 0),
        result=kv.get("Result", ""),
        activa_desde=kv.get("ActiveEnterTimestamp", ""),
    )


def mostrar_varias(unidades: list[str],
                   timeout_sec: float = 10.0) -> dict[str, Unidad]:
    """Todas las unidades de una sola llamada.

    `systemctl show a.service b.service` devuelve los bloques separados por una
    linea vacia. Importa: con una llamada por unidad, siete unidades son siete
    procesos por ciclo, y eso solo era la mitad del presupuesto de CPU del
    observador entero.
    """
    if not unidades:
        return {}
    try:
        r = subprocess.run(
            ["systemctl", "show", *unidades, "--no-pager",
             "-p", ",".join(PROPIEDADES)],
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("systemctl show (lote) fallo: %s", e)
        return {}
    if r.returncode != 0:
        return {}

    out: dict[str, Unidad] = {}
    for i, bloque in enumerate(r.stdout.split("\n\n")):
        if not bloque.strip():
            continue
        kv = {}
        for linea in bloque.splitlines():
            if "=" in linea:
                k, v = linea.split("=", 1)
                kv[k] = v
        # `Id` viene vacio para una unidad que no existe, asi que el nombre se
        # toma de la posicion: systemctl respeta el orden de los argumentos.
        nombre = kv.get("Id") or (unidades[i] if i < len(unidades) else "")
        if not nombre:
            continue
        out[nombre] = Unidad(
            id=nombre,
            load_state=kv.get("LoadState", "unknown"),
            active_state=kv.get("ActiveState", "unknown"),
            sub_state=kv.get("SubState", "unknown"),
            unit_file_state=kv.get("UnitFileState", "unknown"),
            n_restarts=int(kv.get("NRestarts", "0") or 0),
            main_pid=int(kv.get("MainPID", "0") or 0),
            result=kv.get("Result", ""),
            activa_desde=kv.get("ActiveEnterTimestamp", ""),
        )
    return out


def listar_servicios(timeout_sec: float = 10.0) -> list[str]:
    """Todas las unidades .service cargadas, incluidas las inactivas.

    Sirve para el descubrimiento: lo que aparece acá y no está declarado en las
    expectativas se reporta UNCLASSIFIED. Encontrar algo no es aprobarlo, pero
    tampoco puede quedar invisible.
    """
    try:
        r = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--all",
             "--no-pager", "--plain", "--no-legend"],
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    out = []
    for linea in r.stdout.splitlines():
        partes = linea.split()
        if partes and partes[0].endswith(".service"):
            out.append(partes[0])
    return out
