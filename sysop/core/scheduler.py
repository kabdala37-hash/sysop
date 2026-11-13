"""Decide qué controles corren en cada tick.

Sin esto el motor corre los veintisiete controles en cada vuelta, incluido el
del certificado TLS que declara una cadencia de seis horas. Medido en srv3: 384 ms
por ciclo y 1,75% de un núcleo, por encima del 1% que fija el criterio de
aceptación. Respetando la cadencia declarada, la mayoría de los ticks corren
sólo un puñado.

El jitter no es cosmético. Todos los controles de 30 segundos arrancarían
alineados y producirían un pico cada 30 segundos, con el resto del tiempo
ocioso: el promedio da bien y el pico se nota igual, sobre todo en un servidor
que ya está al 88% de disco y con la base al 46% de sus conexiones.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from sysop.core.models import CheckDefinition, Resource


def _jitter_estable(check_id: str, resource_id: str, periodo: float,
                    pct: int) -> float:
    """Desfase fijo por par (control, recurso).

    Determinístico a propósito, derivado del nombre: un jitter aleatorio haría
    que la cadencia real cambie en cada reinicio del servicio, y dos corridas
    del mismo control podrían quedar separadas por casi el doble del período.
    """
    if pct <= 0:
        return 0.0
    h = hashlib.sha256(f"{check_id}|{resource_id}".encode()).digest()
    fraccion = int.from_bytes(h[:4], "big") / 0xFFFFFFFF
    return periodo * (pct / 100.0) * fraccion


@dataclass
class Scheduler:
    """Lleva cuándo toca cada control. Estado en memoria, a propósito.

    Perder el calendario en un reinicio no tiene consecuencia: en el peor caso
    todo corre una vez de más al arrancar. La histéresis, en cambio, sí se
    persiste, porque perderla significaría no abrir un incidente.
    """
    proximos: dict[tuple[str, str], float] = field(default_factory=dict)

    def _clave(self, cd: CheckDefinition, r: Resource) -> tuple[str, str]:
        return (cd.check_id, r.resource_id)

    def vencidos(self, pares: Iterable[tuple[CheckDefinition, Resource]],
                 ahora: float | None = None
                 ) -> list[tuple[CheckDefinition, Resource]]:
        ahora = time.monotonic() if ahora is None else ahora
        listos: list[tuple[CheckDefinition, Resource]] = []

        for cd, r in pares:
            k = self._clave(cd, r)
            prox = self.proximos.get(k)
            if prox is None:
                # Primera vez: corre ya. Un control recién agregado no puede
                # esperar seis horas para dar su primera lectura.
                listos.append((cd, r))
                self.proximos[k] = ahora + cd.schedule_sec + _jitter_estable(
                    cd.check_id, r.resource_id, cd.schedule_sec, cd.jitter_pct)
            elif ahora >= prox:
                listos.append((cd, r))
                # Se agenda desde AHORA y no desde el vencimiento anterior. Si
                # el servicio estuvo caído una hora, reprogramar desde el
                # vencimiento dispararía una ráfaga de corridas atrasadas para
                # ponerse al día, justo cuando el sistema vuelve a arrancar.
                self.proximos[k] = ahora + cd.schedule_sec + _jitter_estable(
                    cd.check_id, r.resource_id, cd.schedule_sec, cd.jitter_pct)

        return listos

    def proximo_en(self, ahora: float | None = None) -> float:
        """Segundos hasta el próximo vencimiento. Sirve para dormir lo justo."""
        if not self.proximos:
            return 0.0
        ahora = time.monotonic() if ahora is None else ahora
        return max(0.0, min(self.proximos.values()) - ahora)
