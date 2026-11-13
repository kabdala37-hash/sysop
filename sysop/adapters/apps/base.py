"""El contrato que cumple un adaptador de aplicación.

Un adaptador conoce la semántica de UNA plataforma sin contaminar el núcleo. El
núcleo sabe de umbrales, estados, incidentes y correo; el adaptador sabe qué
significa que un worker falte o que una cola esté estancada.

Es un módulo, no una clase: `sysop.adapters.apps.<nombre>` con estas funciones
de nivel de módulo. Igual que el adaptador de sistema operativo, así los dos se
usan de la misma forma desde el daemon.

Regla que vale la pena repetir, porque es la que se viola sola con el tiempo:
un adaptador NO reimplementa la lógica de su plataforma. La consume. Si la
copia, las dos versiones divergen el día que alguien toca la original, y el
observador empieza a mentir sin que nadie se entere.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable


@runtime_checkable
class AdaptadorApp(Protocol):
    NOMBRE: str
    VERSION: int

    # -- observación --------------------------------------------------------

    def planificar(self, cfg: Any, host_id: str) -> list:
        """Los pares (control, recurso) que aporta este adaptador."""

    def descubrir(self, cfg: Any, host_id: str) -> list:
        """Qué recursos existen. Lo descubierto entra sin clasificar:
        encontrar algo no es aprobarlo."""

    def tipos_de_recurso(self) -> tuple[str, ...]:
        """Qué tipos de recurso administra, para que el inventario no borre
        los de otro adaptador."""

    def proveer(self, controles: list,
                deps: dict[str, Any]) -> Optional[Callable[[], None]]:
        """Prepara lo compartido del ciclo (una conexión, un caché) y devuelve
        cómo soltarlo. Devolver None significa que no hay nada que soltar."""

    # -- fuentes de datos ---------------------------------------------------
    #
    # Estas tres son las que hacen que el núcleo no sepa de dónde salen las
    # cosas. Se usan sólo cuando `app.yaml` declara `tipo: mysql` para la
    # fuente; con `tipo: archivo` las resuelve el núcleo y el adaptador ni se
    # entera. Una plataforma sin tablas de agentes no necesita implementarlas.

    def leer_correo(self, spec: dict[str, Any]) -> Optional[Any]:
        """La cuenta SMTP con la que se manda. `None` si no se pudo leer."""

    def leer_destinatarios(self, spec: dict[str, Any]) -> Optional[Any]:
        """A quién se le manda. `None` si no se pudo leer."""

    def leer_agente(self, spec: dict[str, Any]) -> Optional[Any]:
        """Prompt, modelo y credencial del agente que diagnostica.
        `None` si no se pudo leer."""

    # -- opcionales ---------------------------------------------------------
    #
    # No hacen falta para observar. Si estan, el agente las usa.

    def herramientas(self) -> tuple[list, dict[str, Any]]:
        """Herramientas de LECTURA propias de la plataforma para el agente:
        (declaraciones para el modelo, {nombre: funcion})."""

    def confirmacion(self, resource_id: str,
                     ahora: Optional[Any] = None) -> Optional[Any]:
        """Desde cuando se puede confirmar que un arreglo sobre este recurso
        anduvo (un trabajo: su proxima corrida). `None` si no se sabe."""


def cumple(modulo: Any) -> list[str]:
    """Qué le falta a un módulo para ser un adaptador. Vacío = está completo.

    Se usa al arrancar, para fallar con un mensaje que diga qué falta en vez de
    con un AttributeError en el primer ciclo, media hora después.
    """
    requeridas = ("planificar", "descubrir", "tipos_de_recurso", "proveer")
    return [n for n in requeridas if not callable(getattr(modulo, n, None))]
