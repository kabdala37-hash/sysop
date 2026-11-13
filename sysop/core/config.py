"""Carga de expectativas declaradas.

Qué unidades tienen que estar corriendo, qué mounts vigilar y con qué umbrales
NO vive en el código. Mudar la plataforma a un servidor nuevo tiene que ser
escribir un YAML, no editar Python.

Tres capas, y el orden es el que manda: gana la más específica.

    config/so.yaml          lo del sistema operativo. No nombra ninguna
                            aplicación: sirve igual en un servidor de otra
                            plataforma
    config/app.yaml         lo de la aplicación, incluido de dónde salen el
                            agente, los destinatarios y la cuenta de correo
    config/hosts/<host>.yaml  lo propio de ESE servidor

El corte entre los dos primeros no es cosmético: es el que permite que el
adaptador de sistema operativo se instale en cualquier lado, y que agregar una
aplicación nueva sea escribir un `app.yaml` en vez de tocar el núcleo.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml

CAPAS = ("so.yaml", "app.yaml")


def _merge(base: dict[str, Any], encima: dict[str, Any]) -> dict[str, Any]:
    """Fusión profunda. Un diccionario se completa; una lista se REEMPLAZA.

    Lo segundo es a propósito: si el host declara `esperadas_activas`, quiere
    decir exactamente esas y no esas más las comunes. Concatenar haría imposible
    quitar algo heredado, y un servidor terminaría esperando una unidad que ahí
    no existe.
    """
    out = copy.deepcopy(base)
    for k, v in encima.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@dataclass(frozen=True)
class UnidadIgnorada:
    unidad: str
    motivo: str
    vence: Optional[date]

    @property
    def vigente(self) -> bool:
        """Un silencio sin fecha es un control borrado. Vencido, vuelve a sonar."""
        return self.vence is None or self.vence >= date.today()


@dataclass(frozen=True)
class ExpectativasHost:
    host: str
    installation: str
    rol: str
    raw: dict[str, Any]

    @property
    def app(self) -> str:
        """Qué adaptador de aplicación corresponde. Vacío = ninguno.

        Un servidor sin aplicación conocida es un caso válido: se observa el
        sistema operativo y nada más.
        """
        return str(self.raw.get("app", "") or "")

    @property
    def mapa(self) -> dict[str, Any]:
        """El bloque `mapa:`: qué es este servidor y qué hay en cada ruta.

        Es contexto para el agente, no una restricción: le dice qué existe para
        que no busque a ciegas.
        """
        return dict(self.raw.get("mapa", {}) or {})

    @property
    def alertas(self) -> dict[str, Any]:
        """El bloque `alertas:`. Vacío significa todo en su valor por defecto."""
        return dict(self.raw.get("alertas", {}) or {})

    @property
    def llm(self) -> dict[str, Any]:
        """El bloque `llm:`: presupuesto, precios y qué no se filtra."""
        return dict(self.raw.get("llm", {}) or {})

    @property
    def fuentes(self) -> dict[str, Any]:
        """El bloque `fuentes:`: de dónde salen agente, destinatarios y correo."""
        return dict(self.raw.get("fuentes", {}) or {})

    # -- systemd ------------------------------------------------------------

    @property
    def units_activas(self) -> tuple[str, ...]:
        return tuple(self.raw.get("systemd", {}).get("esperadas_activas", []) or [])

    @property
    def units_detenidas(self) -> tuple[str, ...]:
        return tuple(self.raw.get("systemd", {}).get("esperadas_detenidas", []) or [])

    @property
    def units_ignoradas(self) -> tuple[UnidadIgnorada, ...]:
        out = []
        for it in self.raw.get("systemd", {}).get("ignoradas", []) or []:
            v = it.get("vence")
            out.append(UnidadIgnorada(
                unidad=it["unidad"],
                motivo=it.get("motivo", ""),
                vence=date.fromisoformat(v) if isinstance(v, str) else v,
            ))
        return tuple(out)

    def clasificar_unidad(self, unidad: str) -> str:
        """A qué grupo pertenece una unidad encontrada en el servidor.

        Lo NO declarado vuelve `unclassified`, nunca `ok`. Encontrar algo no es
        aprobarlo: una unidad nueva se reporta para que alguien decida qué es.
        """
        if unidad in self.units_activas:
            return "esperada_activa"
        if unidad in self.units_detenidas:
            return "esperada_detenida"
        for ig in self.units_ignoradas:
            if ig.unidad == unidad:
                return "ignorada" if ig.vigente else "ignorada_vencida"
        return "unclassified"

    # -- umbrales -----------------------------------------------------------

    def umbral(self, ruta: str, default: Any = None) -> Any:
        """Lee un umbral por ruta con puntos: `filesystems.umbrales_uso_pct.major`."""
        nodo: Any = self.raw
        for parte in ruta.split("."):
            if not isinstance(nodo, dict) or parte not in nodo:
                return default
            nodo = nodo[parte]
        return nodo

    @property
    def endpoints_habilitados(self) -> tuple[dict[str, Any], ...]:
        return tuple(e for e in self.raw.get("endpoints", []) or []
                     if e.get("habilitado", True))


def cargar(config_dir: Path, host: str) -> ExpectativasHost:
    base: dict[str, Any] = {}

    for capa in CAPAS:
        f = config_dir / capa
        if f.exists():
            base = _merge(base, yaml.safe_load(f.read_text(encoding="utf-8")) or {})

    propio = config_dir / "hosts" / f"{host}.yaml"
    if not propio.exists():
        raise FileNotFoundError(
            f"no hay configuracion para el host '{host}' en "
            f"{config_dir / 'hosts'}. Sin declaracion no se puede calificar "
            "nada: crear el YAML."
        )
    base = _merge(base, yaml.safe_load(propio.read_text(encoding="utf-8")) or {})

    return ExpectativasHost(
        host=base.get("host", host),
        installation=base.get("installation", host),
        rol=base.get("rol", "desconocido"),
        raw=base,
    )
