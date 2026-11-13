"""Adaptador del sistema operativo. Linux, y nada más que Linux.

Es el que no cambia nunca. Todos los servidores corren Ubuntu, así que procesos,
memoria, discos, unidades de systemd, journal y puertos se miran igual en todos
lados. Este adaptador **no sabe qué aplicación corre encima**, y esa ignorancia
es la que lo hace reutilizable: instalarlo en un servidor de otra plataforma no
pide tocarle una línea.

Lo específico de cada servidor no está en el código sino en `config/so.yaml` más
el archivo del host: qué mounts se esperan, qué unidades tienen que estar
activas, qué puertos deberían escuchar.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from sysop.adapters.so_linux import checks_host, checks_svc
from sysop.adapters.so_linux.collectors import systemd

NOMBRE = "so_linux"
VERSION = 1


def planificar(cfg: Any, host_id: str,
               deps: Optional[dict[str, Any]] = None) -> list:
    return (checks_host.planificar(cfg, host_id)
            + checks_svc.planificar(cfg, host_id))


def descubrir(cfg: Any, host_id: str,
              deps: Optional[dict[str, Any]] = None) -> list:
    return (checks_host.descubrir_recursos(cfg, host_id)
            + checks_svc.descubrir_recursos(cfg, host_id)
            + checks_svc.descubrir_no_declaradas(cfg, host_id))


def tipos_de_recurso() -> tuple[str, ...]:
    return ("host", "mount", "systemd", "endpoint", "socket", "tls")


def proveer(controles: Optional[list],
            deps: dict[str, Any]) -> Optional[Callable[[], None]]:
    """Prepara lo compartido del ciclo y devuelve cómo soltarlo.

    Una sola lectura de systemd para todas las unidades del ciclo. Sin esto eran
    catorce procesos `systemctl` por vuelta, medidos en srv3 como la mitad del
    presupuesto de CPU del observador.

    `controles=None` significa "todavía no se sabe cuáles": pasa al planificar,
    antes de tener la lista. Acá no hay nada que abrir a ciegas, así que no se
    hace nada.
    """
    if controles is None:
        return None
    unidades = sorted({r.scope["unidad"] for _, r in controles
                       if r.kind == "systemd" and "unidad" in r.scope})
    if unidades:
        deps["systemd_cache"] = systemd.mostrar_varias(unidades)
    return None
