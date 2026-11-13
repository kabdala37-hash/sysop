"""Proyección de agotamiento por regresión robusta.

Se usa Theil-Sen (la mediana de las pendientes entre todos los pares de puntos)
y no mínimos cuadrados. La diferencia importa acá: un solo punto anómalo, como
el instante en que un backup ocupa 40 GiB y después los libera, inclina la recta
de mínimos cuadrados lo suficiente como para anunciar que el disco se llena en
tres días. La mediana de pendientes ignora ese punto.

El costo es O(n²) en el número de puntos, así que la serie se submuestrea. Con
unos cientos de puntos por mount alcanza y sobra para una tendencia de 30 días.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional, Sequence

from sysop.store import db


@dataclass(frozen=True)
class Proyeccion:
    pendiente_pct_por_dia: float
    dias_para_llenar: Optional[float]   # None = no crece o no se puede proyectar
    puntos: int
    horas_de_historia: float

    @property
    def confiable(self) -> bool:
        """Sin historia suficiente no se proyecta.

        Con cuatro puntos de media hora se puede calcular una pendiente, pero
        extrapolarla a treinta días es inventar. Y una proyección inventada que
        dice "se llena en 2 días" produce una alerta crítica falsa, que es peor
        que no tener el control.
        """
        return self.puntos >= 12 and self.horas_de_historia >= 6.0

    def suficiente_para(self, dias_anunciados: float) -> bool:
        """¿Alcanza la historia para sostener ESE horizonte?

        `confiable` es el piso absoluto y no depende de lo que se va a anunciar.
        Ahí estaba el agujero: con 12,3 horas de historia el control anunció que
        el disco se llenaba en 10,9 días y abrió un incidente `major`. Doce
        horas no alcanzan para hablar de once días, por robusta que sea la
        pendiente.

        La regla es proporcional: para afirmar un horizonte de N días hace falta
        al menos N/10 de historia. Un agotamiento en 3 días, que es el umbral
        crítico y el que hay que ver rápido, se sostiene con unas 7 horas; uno
        en 30 días pide 3 días de historia antes de decir nada.
        """
        if not self.confiable:
            return False
        return self.horas_de_historia >= self.horas_necesarias(dias_anunciados)

    @staticmethod
    def horas_necesarias(dias_anunciados: float) -> float:
        return max(6.0, (dias_anunciados / 10.0) * 24.0)


def theil_sen(puntos: Sequence[tuple[float, float]]) -> float:
    """Pendiente robusta: mediana de las pendientes entre todos los pares."""
    n = len(puntos)
    if n < 2:
        return 0.0
    pendientes = []
    for i in range(n):
        x1, y1 = puntos[i]
        for j in range(i + 1, n):
            x2, y2 = puntos[j]
            if x2 != x1:
                pendientes.append((y2 - y1) / (x2 - x1))
    if not pendientes:
        return 0.0
    pendientes.sort()
    m = len(pendientes) // 2
    if len(pendientes) % 2:
        return pendientes[m]
    return (pendientes[m - 1] + pendientes[m]) / 2.0


def _submuestrear(puntos: list[tuple[float, float]],
                  maximo: int = 200) -> list[tuple[float, float]]:
    if len(puntos) <= maximo:
        return puntos
    paso = len(puntos) / maximo
    return [puntos[int(i * paso)] for i in range(maximo)]


def serie_de_uso(conn: Any, check_id: str, resource_id: str,
                 campo: str = "uso_pct",
                 dias: int = 30) -> list[tuple[float, float]]:
    """Serie (días desde el primer punto, valor) desde los resultados guardados.

    Se reusa lo que ya persiste el control de ocupación en vez de mantener una
    tabla de métricas aparte: la historia ya está, y duplicarla sería una
    segunda fuente que se puede desincronizar de la primera.
    """
    filas = conn.execute(
        "SELECT observed_at, actual_json FROM check_result "
        "WHERE check_id = ? AND resource_id = ? AND state != 'UNKNOWN' "
        "  AND actual_json IS NOT NULL "
        "  AND observed_at >= ? "
        "ORDER BY observed_at",
        (check_id, resource_id, db.corte(days=int(dias))),
    ).fetchall()

    puntos: list[tuple[float, float]] = []
    t0: Optional[float] = None
    for f in filas:
        try:
            d = json.loads(f["actual_json"])
            v = float(d[campo])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        try:
            ts = datetime.fromisoformat(f["observed_at"]).timestamp()
        except ValueError:
            continue
        if t0 is None:
            t0 = ts
        puntos.append(((ts - t0) / 86400.0, v))
    return puntos


def proyectar(puntos: list[tuple[float, float]],
              objetivo_pct: float = 100.0) -> Proyeccion:
    puntos = _submuestrear(puntos)
    if len(puntos) < 2:
        return Proyeccion(0.0, None, len(puntos), 0.0)

    horas = (puntos[-1][0] - puntos[0][0]) * 24.0
    pendiente = theil_sen(puntos)
    actual = puntos[-1][1]

    # Pendiente cero o negativa: no se está llenando. No se proyecta un número
    # gigante ni negativo, se dice que no aplica.
    if pendiente <= 0:
        return Proyeccion(pendiente, None, len(puntos), horas)

    faltante = objetivo_pct - actual
    if faltante <= 0:
        return Proyeccion(pendiente, 0.0, len(puntos), horas)
    return Proyeccion(pendiente, faltante / pendiente, len(puntos), horas)
