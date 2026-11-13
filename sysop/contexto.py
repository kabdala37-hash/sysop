"""Lo que necesitan por igual el daemon y la línea de comandos.

Existe para que el informe que dispara cron vea exactamente lo mismo que ve el
daemon: la misma configuración, los mismos adaptadores, el mismo almacén. Si
cada uno armara su propia versión, el correo terminaría describiendo un sistema
distinto del que se está observando.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any, Optional

from sysop import settings
from sysop.adapters import so_linux
from sysop.adapters.apps import base as apps_base
from sysop.core import fuentes as fuentes_mod
from sysop.core.config import ExpectativasHost
from sysop.core.config import cargar as cargar_expectativas
from sysop.store import db

log = logging.getLogger("sysop.contexto")


@dataclass
class Contexto:
    conn: Any
    cfg: ExpectativasHost
    app: Optional[Any]          # el módulo del adaptador de aplicación
    # Por qué no hay almacén, si no lo hay. `conn` es None en ese caso: cada
    # proceso decide qué puede hacer igual (el daemon avisa, el informe sale
    # degradado) en vez de morirse al arrancar.
    falla_almacen: Optional[db.Falla] = None
    inodo: Optional[int] = None
    rotada: Optional[str] = None

    @property
    def adaptadores(self) -> tuple[Any, ...]:
        return (so_linux, self.app) if self.app else (so_linux,)

    @property
    def proveedores(self) -> tuple[Any, ...]:
        return tuple(a.proveer for a in self.adaptadores)

    def _con_recursos(self, metodo: str) -> list:
        """Corre `metodo` en cada adaptador con los recursos compartidos abiertos.

        Planificar y descubrir necesitan consultar: la lista de trabajos de la
        plataforma sale de la base o del back, así que sin conexión no se sabe
        qué controles corresponden. Se abre, se usa y se cierra: no queda una
        conexión colgada entre ciclos.
        """
        deps: dict[str, Any] = {"cfg": self.cfg, "conn": self.conn}
        cierres = []
        for proveer in self.proveedores:
            try:
                c = proveer(None, deps)
                if c is not None:
                    cierres.append(c)
            except Exception:  # noqa: BLE001
                log.exception("fallo un proveedor al %s", metodo)
        try:
            out: list = []
            for a in self.adaptadores:
                out.extend(getattr(a, metodo)(self.cfg, settings.HOST_ID, deps))
            return out
        finally:
            for c in reversed(cierres):
                try:
                    c()
                except Exception:  # noqa: BLE001
                    pass

    def planificar(self) -> list:
        return self._con_recursos("planificar")

    def descubrir(self) -> list:
        return self._con_recursos("descubrir")

    def tipos_de_recurso(self) -> tuple[str, ...]:
        tipos: list[str] = []
        for a in self.adaptadores:
            tipos.extend(a.tipos_de_recurso())
        return tuple(dict.fromkeys(tipos))

    def fuentes(self) -> fuentes_mod.Config:
        return fuentes_mod.resolver(self.cfg.fuentes, self.app)


def cargar_adaptador_app(nombre: str) -> Optional[Any]:
    """Importa el adaptador por nombre y verifica que cumpla el contrato.

    Se verifica al arrancar y no en el primer ciclo: un adaptador incompleto
    tiene que fallar con un mensaje que diga qué le falta, no con un
    AttributeError media hora después, en medio de un tick.
    """
    if not nombre:
        return None
    try:
        mod = importlib.import_module(f"sysop.adapters.apps.{nombre}")
    except ImportError as e:
        raise RuntimeError(
            f"config declara app '{nombre}' y no existe "
            f"sysop/adapters/apps/{nombre}: {e}") from e

    faltan = apps_base.cumple(mod)
    if faltan:
        raise RuntimeError(
            f"el adaptador '{nombre}' no cumple el contrato: le falta "
            + ", ".join(f"{n}()" for n in faltan))
    return mod


def cargar(ruta_db: Optional[Any] = None, crear: bool = True,
           verificar: bool = False, rotar: bool = False) -> Contexto:
    """El contexto, con o sin almacén.

    Un almacén que no abre ya NO lanza: queda en `falla_almacen`. Antes lo hacía,
    y ni el daemon ni el informe ni el bot lo atrapaban (solo esperaban errores
    de configuración): con la base corrupta los tres morían sin mandar un correo.
    Los errores de configuración siguen lanzando, porque sin expectativas no hay
    contra qué comparar.
    """
    alm = db.abrir(ruta_db, crear=crear, verificar=verificar, rotar=rotar)
    if alm.falla is not None:
        log.error("el almacen no esta disponible -> %s", alm.falla)
    cfg = cargar_expectativas(settings.CONFIG_DIR, settings.HOST_ID)
    return Contexto(conn=alm.conn, cfg=cfg, app=cargar_adaptador_app(cfg.app),
                    falla_almacen=alm.falla, inodo=alm.inodo, rotada=alm.rotada)
