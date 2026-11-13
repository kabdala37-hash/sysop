"""Decide cuándo un resultado se convierte en problema y cuándo deja de serlo.

Un control corre cada 15 o 30 segundos. Sin esta capa, un pico de medio minuto
genera un incidente, y cuatro picos en una hora generan cuatro correos. Con
ella, hace falta que el problema se sostenga.

Es asimétrica a propósito: abre rápido y cierra lento. Abrir tarde pierde
tiempo de reacción; cerrar temprano hace que un problema intermitente se
reporte como recuperado entre pico y pico, y nadie llega a verlo.

El conteo sale de la historia persistida y no de una variable en memoria. Si el
observador reinicia en medio de una racha, tiene que seguir contando donde
estaba: perder el conteo significaría no abrir nunca un incidente en un servicio
que reinicia seguido, que es justo el que más lo necesita.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sysop.core.models import Hysteresis, State


@dataclass(frozen=True)
class Decision:
    abrir: bool = False
    cerrar: bool = False
    racha: int = 0
    motivo: str = ""


def _ultimos(conn: Any, check_id: str, resource_id: str,
             n: int) -> list[tuple[str, str]]:
    """Los últimos n resultados, del más nuevo al más viejo: (state, motivo).

    Devuelve la historia CRUDA, sin filtrar por motivo. El filtro por motivo lo
    hace quien cuenta la racha, cortándola cuando el motivo cambia.

    Filtrarlo en el SQL parece lo mismo y no lo es: la consulta se saltea las
    muestras sanas del medio, así que dos capturas aisladas separadas por horas
    quedan una al lado de la otra y parecen una racha. Con eso, `abre_tras=2`
    abre en la PRIMERA muestra mala de la segunda tanda, que es exactamente lo
    que la histéresis existe para evitar.

    Medido en srv3 el 2026-09-10: SVC-01 sobre `plataforma-api.service` tuvo
    cinco muestras FAILED aisladas en 48 h —el instante en que el sync reinicia
    el back—, cada una rodeada de OK, y cada una mando un correo CRITICAL.
    """
    sql = ("SELECT state, reason_code FROM check_result "
           "WHERE check_id = ? AND resource_id = ? "
           "ORDER BY observed_at DESC, id DESC LIMIT ?")
    return [(f["state"], f["reason_code"] or "")
            for f in conn.execute(sql, (check_id, resource_id, n)).fetchall()]


def evaluar(conn: Any, check_id: str, resource_id: str,
            estado_actual: State, h: Hysteresis,
            hay_incidente_mismo_motivo: bool,
            hay_incidentes_vivos: bool,
            reason_code: str = "") -> Decision:
    """Qué hacer con el resultado que se acaba de obtener.

    `estado_actual` ya tiene que estar persistido en `check_result`: esta
    función lee la historia, incluido ese último resultado.

    Los dos flags de incidente son distintos a propósito. Para ABRIR importa si
    ya hay uno de ESTE motivo: un mount puede estar a la vez casi lleno y
    montado de solo lectura, y son dos problemas que se arreglan por separado.
    Para CERRAR importa si hay alguno vivo, cualquiera sea, porque el control
    volviendo a sano los resuelve a todos.
    """
    # UNKNOWN no abre ni cierra nada. No es salud, pero tampoco es falla: es
    # falta de evidencia. Si abriera incidentes, una fuente caída generaría uno
    # por cada recurso que dependa de ella. Y si cerrara, un problema real
    # quedaría "recuperado" apenas se cae el collector que lo estaba viendo,
    # que es la peor combinación posible.
    if estado_actual is State.UNKNOWN:
        return Decision(motivo="UNKNOWN no mueve el incidente")

    if estado_actual.es_problema:
        if hay_incidente_mismo_motivo:
            return Decision(racha=0, motivo="ya hay incidente de este motivo")
        recientes = _ultimos(conn, check_id, resource_id, h.abre_tras)
        # Todos problema DEL MISMO MOTIVO, y que haya suficientes muestras: con
        # menos historia que el umbral no se abre. Un control recién agregado no
        # puede disparar en su primera corrida.
        #
        # El motivo corta la racha igual que una muestra sana. Si un mount venía
        # veinte muestras "casi lleno" y en la veintiuna aparece "solo lectura",
        # ese segundo problema recién se vio una vez y no hereda las veinte
        # anteriores. Lo que NO puede pasar es que las sanas del medio
        # desaparezcan: ahí dos capturas separadas por horas parecerían seguidas.
        racha = 0
        for s, motivo in recientes:
            if State(s).es_problema and motivo == reason_code:
                racha += 1
            else:
                break
        if racha >= h.abre_tras:
            return Decision(abrir=True, racha=racha,
                            motivo=f"{racha} muestras seguidas en problema")
        return Decision(racha=racha,
                        motivo=f"{racha}/{h.abre_tras} para abrir")

    # Estado sano
    if not hay_incidentes_vivos:
        return Decision(motivo="sano y sin incidente")

    recientes = _ultimos(conn, check_id, resource_id, h.cierra_tras)
    racha = 0
    for s, _motivo in recientes:
        st = State(s)
        if st.es_problema:
            break
        # Un UNKNOWN en el medio corta la racha de recuperación sin contar como
        # problema: no se puede declarar recuperado algo que no se pudo ver.
        if st is State.UNKNOWN:
            return Decision(racha=racha,
                            motivo="hay un UNKNOWN en la ventana de cierre")
        racha += 1
    if racha >= h.cierra_tras:
        return Decision(cerrar=True, racha=racha,
                        motivo=f"{racha} muestras seguidas sanas")
    return Decision(racha=racha, motivo=f"{racha}/{h.cierra_tras} para cerrar")
