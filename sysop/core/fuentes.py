"""De dónde salen el agente, los destinatarios y la cuenta de correo.

Tres cosas que el núcleo necesita y que cambian según la plataforma. Ninguna se
hardcodea: se declaran en `config/app.yaml` con un tipo de fuente, y quien las
resuelve depende de ese tipo.

    tipo: archivo   las resuelve este módulo. Sirve para cualquier plataforma,
                    incluida una que no tenga tablas de agentes ni de reportes.
    tipo: mysql     las resuelve el adaptador de la aplicación, que sabe en qué
                    tabla vive cada cosa, CON un `respaldo:` detrás.

El núcleo pide `agente()` y le llega. Nunca sabe de dónde salió.

Y una regla que viene del diseño anterior porque sigue siendo cierta:

    Si la configuración del que avisa vive en la base que vigila, entonces
    cuando esa base se cae, el que tenía que avisar se queda mudo.

LA CADENA SON DOS ESLABONES, NO TRES. Mientras MySQL conteste manda MySQL, que
es lo que se edita desde la pantalla de la plataforma. Cuando no contesta manda
el archivo declarado en `respaldo:`, que se versiona, se lee de un `cat` y se
edita a mano.

Hasta el 2026-09-02 el respaldo era una caché en SQLite que se llenaba sola con
la última lectura buena. Se sacó por tres razones medidas, no de opinión:

  - No era una garantía sino un recuerdo: con el almacén vacío y la base caída,
    `puede_notificar` daba False. Una instalación nueva quedaba muda y nadie se
    enteraba hasta el día que hacía falta.
  - Era estado invisible que juntaba basura. Al sacarla, la tabla tenía dos
    claves de un diseño anterior (`notify:smtp`, `notify:destinatarios`) que
    nadie leía ni escribía desde hacía cinco días, con la password SMTP en claro
    adentro.
  - Duplicaba secretos en un tercer lugar, además de la base y el entorno.

El precio de sacarla es que el archivo se mantiene a mano y puede quedar viejo.
Eso NO se corrige solo a propósito: se avisa. `desactualizados` lleva las
diferencias entre lo que dice la base y lo que dice el archivo, y el informe las
publica. Esa es la red de seguridad.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from sysop import settings

log = logging.getLogger("sysop.fuentes")

TZ_POR_DEFECTO = "America/Argentina/Buenos_Aires"

# El agente vivo trae su `agent_id` como nombre y el del archivo trae el rótulo
# que le hayan puesto. Que difieran no significa que el respaldo esté viejo.
_NO_COMPARAR = {"nombre"}


@dataclass(frozen=True)
class CuentaSMTP:
    host: str
    port: int
    secure: bool          # True = SMTPS (465); False = STARTTLS (587)
    user: str
    password: str = field(repr=False)   # nunca en un log ni en un repr
    from_addr: str = ""

    def __str__(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"


@dataclass(frozen=True)
class Agente:
    """El que diagnostica. Sale de la plataforma o de un archivo."""
    prompt: str
    modelo: str
    api_key: str = field(repr=False)
    vendor: str = "gemini"
    base_url: str = ""
    nombre: str = ""

    @property
    def utilizable(self) -> bool:
        return bool(self.prompt and self.modelo and self.api_key)


@dataclass(frozen=True)
class Config:
    cuenta: Optional[CuentaSMTP]
    destinatarios: tuple[str, ...]
    agente: Optional[Agente]
    timezone_name: str = TZ_POR_DEFECTO
    # "vivo" si se leyó de la fuente ahora, "respaldo" si la fuente no respondía
    # y se usó el archivo. El informe lo dice: un respaldo viejo puede estar
    # mandando a alguien que ya no está.
    origen: str = "vivo"
    # Diferencias entre la fuente viva y el archivo de respaldo, una por fuente
    # que difiera. Se llena SOLO cuando la fuente respondió: es el aviso de que
    # el archivo quedó atrás y hay que actualizarlo antes de necesitarlo.
    desactualizados: tuple[str, ...] = ()

    @property
    def puede_notificar(self) -> bool:
        return self.cuenta is not None and bool(self.destinatarios)

    @property
    def puede_diagnosticar(self) -> bool:
        return self.agente is not None and self.agente.utilizable


@dataclass
class _Resuelta:
    """El resultado de UNA fuente, con de dónde salió y qué no coincide."""
    valor: Any
    origen: str                      # "vivo" | "respaldo" | "nada"
    difiere_en: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Resolución
# ---------------------------------------------------------------------------

def resolver(spec: dict[str, Any], adaptador: Any,
             config_dir: Optional[Any] = None) -> Config:
    """Las tres fuentes, con caída al archivo de respaldo.

    `spec` es el bloque `fuentes:` de `app.yaml`. `adaptador` sólo se usa para
    las que declaran `tipo: mysql`; puede ser None si ninguna lo declara.
    """
    spec = spec or {}
    config_dir = Path(config_dir or settings.CONFIG_DIR)
    # Las tres fuentes suelen apuntar al MISMO archivo de respaldo. Se lee una
    # vez por llamada, no una por fuente.
    leidos: dict[str, Any] = {}

    # El correo es critico: sin cuenta ni destinatarios el observador es mudo.
    # El agente no: sin el, el observador avisa igual, sin diagnostico.
    r_cuenta = _una("correo", spec.get("correo"), adaptador, "leer_correo",
                    _correo_de_archivo, config_dir, leidos)
    r_dest = _una("destinatarios", spec.get("destinatarios"), adaptador,
                  "leer_destinatarios", _destinatarios_de_archivo,
                  config_dir, leidos)
    r_agente = _una("agente", spec.get("agente"), adaptador, "leer_agente",
                    _agente_de_archivo, config_dir, leidos, critica=False)

    todas = (("correo", r_cuenta), ("destinatarios", r_dest), ("agente", r_agente))

    # Alcanza con que UNA venga del archivo para que el correo tenga que
    # decirlo: el destinatario puede estar desactualizado aunque la cuenta no.
    origen = "respaldo" if any(r.origen == "respaldo" for _, r in todas) else "vivo"

    desactualizados = tuple(
        f"{nombre}: {'; '.join(r.difiere_en)}"
        for nombre, r in todas if r.difiere_en)

    return Config(cuenta=r_cuenta.valor,
                  destinatarios=tuple(r_dest.valor or ()),
                  agente=r_agente.valor,
                  timezone_name=(spec.get("timezone") or TZ_POR_DEFECTO),
                  origen=origen,
                  desactualizados=desactualizados)


def _una(nombre: str, spec_fuente: Optional[dict[str, Any]], adaptador: Any,
         metodo: str, desde_archivo: Any, config_dir: Path,
         leidos: dict[str, Any], critica: bool = True) -> _Resuelta:
    """Resuelve UNA fuente: archivo directo, o la fuente viva con el archivo detrás."""
    if not spec_fuente:
        return _Resuelta(None, "nada")

    tipo_fuente = str(spec_fuente.get("tipo", "")).lower()

    if tipo_fuente == "archivo":
        # El archivo es local: si no se puede leer no hay a qué caer, el
        # problema es de instalación y tiene que verse.
        try:
            return _Resuelta(desde_archivo(spec_fuente, config_dir), "vivo")
        except Exception as e:  # noqa: BLE001
            log.error("no se pudo resolver la fuente %s desde archivo: %s",
                      nombre, e)
            return _Resuelta(None, "nada")

    if tipo_fuente != "mysql":
        log.error("tipo de fuente desconocido en %s: %r", nombre, tipo_fuente)
        return _Resuelta(None, "nada")

    # El respaldo se lee SIEMPRE, aunque la fuente viva conteste: es lo que
    # permite comparar y avisar que quedó viejo antes de que haga falta.
    respaldo = _respaldo(nombre, spec_fuente, desde_archivo, config_dir, leidos)

    valor = None
    fn = getattr(adaptador, metodo, None) if adaptador else None
    if fn is None:
        log.error("la fuente %s pide tipo mysql y el adaptador no "
                  "implementa %s()", nombre, metodo)
    else:
        try:
            valor = fn(spec_fuente)
            if valor is None:
                log.warning("la fuente %s no devolvio nada", nombre)
        except Exception as e:  # noqa: BLE001
            log.warning("fallo leyendo la fuente %s: %s", nombre, e)

    if valor is not None:
        return _Resuelta(valor, "vivo", _diferencias(valor, respaldo))

    if respaldo is None:
        # Una fuente no critica que todavia no existe no es una falla: el
        # observador funciona sin ella. Gritarlo cada cinco minutos entrena a
        # ignorar el log, que es donde despues hay que buscar lo que si importa.
        (log.error if critica else log.info)(
            "la fuente %s no responde y no hay respaldo declarado", nombre)
        return _Resuelta(None, "nada")

    log.warning("la fuente %s no responde: se usa el respaldo del archivo", nombre)
    return _Resuelta(respaldo, "respaldo")


# ---------------------------------------------------------------------------
# El archivo de respaldo
# ---------------------------------------------------------------------------

def _respaldo(nombre: str, spec_fuente: dict[str, Any], desde_archivo: Any,
              config_dir: Path, leidos: dict[str, Any]) -> Any:
    """La sección `nombre` del archivo que declara `respaldo:`.

    Devuelve None si no hay respaldo declarado, si el archivo no está, o si le
    falta la sección. Los tres casos se loguean distinto porque son problemas
    distintos: uno es una decisión, los otros dos son una instalación a medias.
    """
    ref = spec_fuente.get("respaldo")
    if not ref:
        return None

    datos = _cargar_archivo(str(ref), config_dir, leidos)
    if not datos:
        return None

    seccion = datos.get(nombre)
    if seccion is None:
        log.error("el respaldo %s no tiene la seccion `%s`", ref, nombre)
        return None
    # `destinatarios` se escribe como lista pelada, que es lo natural de leer.
    if isinstance(seccion, list):
        seccion = {"lista": seccion}

    try:
        return desde_archivo(seccion, config_dir)
    except Exception as e:  # noqa: BLE001
        log.error("no se pudo leer la seccion `%s` del respaldo %s: %s",
                  nombre, ref, e)
        return None


def _cargar_archivo(ref: str, config_dir: Path,
                    leidos: dict[str, Any]) -> Optional[dict[str, Any]]:
    if ref in leidos:
        return leidos[ref]
    p = Path(ref)
    if not p.is_absolute():
        p = config_dir / p
    datos: Optional[dict[str, Any]] = None
    if not p.exists():
        log.error("el respaldo declarado no existe: %s", p)
    else:
        try:
            datos = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            log.error("no se pudo leer el respaldo %s: %s", p, e)
    leidos[ref] = datos
    return datos


def _diferencias(vivo: Any, respaldo: Any) -> tuple[str, ...]:
    """En qué NO coincide el archivo con lo que dice la fuente viva.

    La red de seguridad del diseño: el archivo se mantiene a mano, así que lo
    único que evita que quede viejo sin que nadie lo note es que el informe lo
    diga mientras todavía no hace falta.

    NUNCA devuelve valores de credenciales, sólo el nombre del campo que
    difiere. Para los destinatarios sí van las direcciones: son lo que hay que
    corregir y no son un secreto.
    """
    if respaldo is None:
        return ("no esta declarado en el archivo",)

    if isinstance(vivo, (tuple, list)):
        v, r = set(vivo), set(respaldo or ())
        partes = []
        if v - r:
            partes.append("al archivo le faltan " + ", ".join(sorted(v - r)))
        if r - v:
            partes.append("el archivo tiene de mas " + ", ".join(sorted(r - v)))
        return tuple(partes)

    campos = [k for k in getattr(vivo, "__dataclass_fields__", ())
              if k not in _NO_COMPARAR
              and _norm(getattr(vivo, k, None)) != _norm(getattr(respaldo, k, None))]
    return (", ".join(campos),) if campos else ()


def _norm(v: Any) -> Any:
    """Compara textos sin que un CRLF cuente como diferencia.

    El prompt del agente viaja entre MySQL, un editor de Windows y un archivo
    del repo. Este repo ya perdio tiempo con diffs que eran solo saltos de
    linea; una alerta diaria de "el prompt difiere" por lo mismo entrena a
    ignorarla.
    """
    if isinstance(v, str):
        return v.replace("\r\n", "\n").strip()
    return v


# ---------------------------------------------------------------------------
# Lectores de archivo. Genéricos: no saben de ninguna plataforma.
# ---------------------------------------------------------------------------

def _correo_de_archivo(s: dict[str, Any],
                       base_dir: Optional[Path] = None) -> CuentaSMTP:
    return CuentaSMTP(
        host=str(s["host"]),
        port=int(s.get("port", 587)),
        secure=bool(s.get("secure", False)),
        user=_valor_o_env(s, "user"),
        password=_valor_o_env(s, "password"),
        from_addr=str(s.get("from") or s.get("from_addr") or _valor_o_env(s, "user")),
    )


def _destinatarios_de_archivo(s: dict[str, Any],
                              base_dir: Optional[Path] = None) -> tuple[str, ...]:
    lista = s.get("lista") or []
    if isinstance(lista, str):
        lista = [x.strip() for x in lista.split(",")]
    return tuple(x for x in lista if x)


def _agente_de_archivo(s: dict[str, Any],
                       base_dir: Optional[Path] = None) -> Agente:
    prompt = s.get("prompt", "")
    # Si apunta a un archivo, se lee. Un prompt de sistema no entra cómodo en
    # una línea de YAML y termina escrito a medias.
    #
    # La ruta puede ser relativa al directorio de configuración, que es lo
    # natural de escribir en el YAML (`prompts/sysop.md`) y lo que hace que el
    # prompt viaje con el repo en vez de vivir suelto en /etc.
    #
    # Se resuelve contra el directorio que se esta leyendo, NO contra el
    # global: si no, apuntar el observador a otra configuracion le lee el
    # yaml de un lado y el prompt del otro. En produccion los dos coinciden y
    # el error no se ve, que es lo que lo hace peligroso.
    if prompt:
        p = Path(str(prompt))
        if not p.is_absolute():
            p = Path(base_dir or settings.CONFIG_DIR) / p
        if p.exists():
            prompt = p.read_text(encoding="utf-8")
    return Agente(
        prompt=str(prompt),
        modelo=str(s.get("modelo", "")),
        api_key=_valor_o_env(s, "api_key"),
        vendor=str(s.get("vendor", "gemini")),
        base_url=str(s.get("base_url", "")),
        nombre=str(s.get("nombre", "archivo")),
    )


def _valor_o_env(s: dict[str, Any], clave: str) -> str:
    """Acepta `user:` con el valor, o `user_env:` con el nombre de la variable.

    Lo segundo es lo que corresponde para credenciales: el YAML va a git y una
    contraseña adentro es una fuga con fecha de vencimiento.
    """
    if s.get(f"{clave}_env"):
        return os.getenv(str(s[f"{clave}_env"]), "")
    return str(s.get(clave, "") or "")
