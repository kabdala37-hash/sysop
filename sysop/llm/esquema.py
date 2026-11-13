"""Qué se le pide al modelo y qué se acepta de vuelta.

El esquema es estricto a propósito y va en la propia llamada (`responseSchema`
de Gemini), no sólo en el prompt: un modelo que devuelve prosa cuando se le pidió
JSON obliga a parsear texto libre, y ahí es donde un observador empieza a
inventar diagnósticos.

Regla que no se negocia: **el modelo no fija estados.** Dice si el problema le
parece real y por qué. La severidad, el estado y la apertura del incidente los
decidió antes el plano determinístico, sobre evidencia medida.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

CONFIANZAS = ("alta", "media", "baja")

# El esquema tal como lo entiende la API de Gemini. DeepSeek y los compatibles
# con OpenAI no lo aceptan igual, así que para esos va descrito en el prompt y
# se valida acá con el mismo rigor.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "es_real": {
            "type": "boolean",
            "description": ("true si el problema descrito es real y merece "
                            "atencion; false si la evidencia se explica por una "
                            "causa benigna conocida"),
        },
        "confianza": {"type": "string", "enum": list(CONFIANZAS)},
        "causa_probable": {
            "type": "string",
            "description": "Una o dos frases. Que lo esta causando, segun la evidencia",
        },
        "accion_sugerida": {
            "type": "string",
            "description": ("Que conviene hacer, concreto. Si no hay accion "
                            "segura, decirlo"),
        },
        "explicacion_descarte": {
            "type": "string",
            "description": "Solo si es_real es false: por que se descarta",
        },
        "resumen": {
            "type": "string",
            "description": ("Solo en modo periodico: dos o tres frases sobre "
                            "como estuvo el periodo en conjunto, incluyendo los "
                            "patrones que se repiten aunque no hayan generado "
                            "aviso"),
        },
    },
    "required": ["es_real", "confianza", "causa_probable", "accion_sugerida"],
}


@dataclass(frozen=True)
class Diagnostico:
    es_real: bool
    confianza: str
    causa: str
    accion: str
    descarte: str = ""
    # Sólo en modo periódico: la lectura del conjunto. En modo urgente queda
    # vacío, porque ahí no hay conjunto que leer, hay un problema.
    resumen: str = ""

    @property
    def descartable(self) -> bool:
        """Sólo un descarte con confianza alta silencia un incidente.

        Con confianza media o baja el modelo está adivinando, y adivinar en la
        dirección de "no pasa nada" es exactamente el error que no se puede
        permitir: el costo de un falso positivo es un correo de más, el de un
        falso negativo es no enterarse.
        """
        return not self.es_real and self.confianza == "alta"


def validar(crudo: Any) -> Optional[Diagnostico]:
    """Convierte la respuesta del modelo en un diagnóstico, o None.

    Devolver None no es un error del observador: es el modelo respondiendo algo
    que no se puede usar. El incidente sigue su curso sin diagnóstico, que es el
    comportamiento correcto. Nunca se completa con valores por defecto: un
    diagnóstico inventado es peor que ninguno.
    """
    if isinstance(crudo, str):
        try:
            crudo = json.loads(crudo)
        except json.JSONDecodeError:
            return None
    if not isinstance(crudo, dict):
        return None

    if not isinstance(crudo.get("es_real"), bool):
        return None
    confianza = str(crudo.get("confianza", "")).lower().strip()
    if confianza not in CONFIANZAS:
        return None

    causa = str(crudo.get("causa_probable", "")).strip()
    accion = str(crudo.get("accion_sugerida", "")).strip()
    if not causa:
        return None

    return Diagnostico(
        es_real=bool(crudo["es_real"]),
        confianza=confianza,
        causa=causa[:2000],
        accion=accion[:2000],
        descarte=str(crudo.get("explicacion_descarte", "")).strip()[:2000],
        resumen=str(crudo.get("resumen", "")).strip()[:3000],
    )


# ---------------------------------------------------------------------------
# Revalidacion: lo que se le pide antes de que un incidente salga en el informe
# ---------------------------------------------------------------------------
#
# El diagnostico de arriba se hace UNA vez, al abrir el incidente, y el informe
# lo repetia todos los dias. Medido en srv2 el 2026-09-17: un trabajo arreglado
# el 16 seguia saliendo con la causa del 13, y un "401 de Telefonia" que el
# modelo saco de `quedan 2401s` salia como hecho. La revalidacion mira de nuevo
# y tiene que DEMOSTRAR lo que dice con citas que el codigo verifica.

VEREDICTOS = ("real", "falsa_alarma", "arreglado_pendiente", "no_se_pudo_validar")

RESPONSE_SCHEMA_REVALIDAR: dict[str, Any] = {
    "type": "object",
    "properties": {
        "veredicto": {
            "type": "string", "enum": list(VEREDICTOS),
            "description": ("real = el problema sigue, visto ahora; "
                            "falsa_alarma = la medicion salto pero no hay "
                            "problema; arreglado_pendiente = la causa ya no "
                            "esta pero falta la corrida que lo confirme; "
                            "no_se_pudo_validar = no hay conclusion con evidencia"),
        },
        "confianza": {"type": "string", "enum": list(CONFIANZAS)},
        "causa_probable": {
            "type": "string",
            "description": "Una o dos frases. Que lo causa, segun lo que miraste",
        },
        "accion_sugerida": {
            "type": "string",
            "description": "Que conviene hacer. Vacio si no hay nada que hacer",
        },
        "motivo": {
            "type": "string",
            "description": ("Por que ese veredicto: que miraste y que "
                            "encontraste. Si es arreglado_pendiente, que se "
                            "arreglo"),
        },
        "confirmar_despues_de": {
            "type": "string",
            "description": ("Solo arreglado_pendiente: fecha ISO 8601 en UTC "
                            "desde la que se puede confirmar, por ejemplo la "
                            "proxima corrida. Vacio si no se sabe"),
        },
        "evidencia": {
            "type": "array",
            "description": ("Lo que sostiene el veredicto, COPIADO TEXTUAL de "
                            "lo que devolvio una herramienta o del paquete"),
            "items": {
                "type": "object",
                "properties": {
                    "fuente": {"type": "string",
                               "description": "de donde sale, ej: leer /var/log/x.log"},
                    "cita": {"type": "string",
                             "description": "el texto tal cual aparece"},
                },
                "required": ["fuente", "cita"],
            },
        },
    },
    "required": ["veredicto", "confianza", "causa_probable", "accion_sugerida",
                 "motivo", "evidencia"],
}

# Cuanto tiene que medir una cita, ya normalizada, para contar. Un numero o una
# palabra sueltos aparecen en cualquier lado: `401` esta adentro de `2401s`.
MIN_CITA = 12

# Hasta cuando puede quedar algo "arreglado, falta confirmar". Un trabajo
# semanal necesita siete dias; mas que eso es un silencio sin fecha.
MAX_DIAS_CONFIRMAR = 8


@dataclass(frozen=True)
class Revalidacion:
    veredicto: str
    confianza: str
    causa: str
    accion: str
    motivo: str
    # Cada cita como {"fuente", "cita", "verificada", "fresca"}.
    evidencia: tuple = ()
    confirmar_despues_de: str = ""
    # Lo que el codigo le corrigio a la respuesta, dicho en castellano. Va al
    # correo: un veredicto degradado sin explicacion es otra caja negra.
    nota: str = ""

    @property
    def citas_verificadas(self) -> list[dict[str, Any]]:
        return [c for c in self.evidencia if c.get("verificada")]


def sin_validar(motivo: str) -> Revalidacion:
    """El veredicto de cuando no hubo respuesta que usar."""
    return Revalidacion(veredicto="no_se_pudo_validar", confianza="baja",
                        causa="", accion="", motivo=motivo)


def validar_revalidacion(crudo: Any) -> Optional[Revalidacion]:
    """Igual de estricto que `validar`: lo que no cumple es None, no se completa."""
    if isinstance(crudo, str):
        try:
            crudo = json.loads(crudo)
        except json.JSONDecodeError:
            return None
    if not isinstance(crudo, dict):
        return None

    veredicto = str(crudo.get("veredicto", "")).lower().strip()
    confianza = str(crudo.get("confianza", "")).lower().strip()
    if veredicto not in VEREDICTOS or confianza not in CONFIANZAS:
        return None
    motivo = str(crudo.get("motivo", "")).strip()
    if not motivo:
        return None
    evidencia = crudo.get("evidencia")
    if not isinstance(evidencia, list):
        return None
    citas = []
    for c in evidencia[:8]:
        if isinstance(c, dict) and isinstance(c.get("cita"), str) and c["cita"].strip():
            citas.append({"fuente": str(c.get("fuente", ""))[:200],
                          "cita": c["cita"].strip()[:600]})

    return Revalidacion(
        veredicto=veredicto,
        confianza=confianza,
        causa=str(crudo.get("causa_probable", "")).strip()[:2000],
        accion=str(crudo.get("accion_sugerida", "")).strip()[:2000],
        motivo=motivo[:2000],
        evidencia=tuple(citas),
        confirmar_despues_de=_fecha_iso(crudo.get("confirmar_despues_de")),
    )


def _fecha_iso(v: Any) -> str:
    """Una fecha que sirva, en UTC, o vacio. No se inventa una."""
    if not isinstance(v, str) or not v.strip():
        return ""
    try:
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat()


_NO_TEXTO = re.compile(r"[^0-9a-záéíóúñü._/\-]+")


def normalizar(texto: str) -> str:
    """Lo que se compara al verificar una cita.

    Se tira la puntuacion y se colapsan los espacios, en la cita y en lo que se
    miro. Asi una fila que la herramienta devolvio como JSON
    (`"status": "success"`) y el modelo copio como `status=success` se
    reconocen como lo mismo, y un salto de linea escapado no hace fallar una
    cita que es literal.
    """
    return " ".join(_NO_TEXTO.sub(" ", str(texto).lower()).split())


def textos(valor: Any) -> str:
    """Todo el texto de un resultado, para buscar citas adentro."""
    if isinstance(valor, str):
        return valor
    try:
        return json.dumps(valor, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(valor)


# `clave: valor` o `clave=valor`, con o sin comillas. El valor corta en la
# puntuacion que separa campos.
_PAR = re.compile(r'"?([A-Za-z_][\w.]*)"?\s*[:=]\s*"?([^,|{}\[\]\s"]+)')


def _escalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v).strip().lower()


def _pares(texto: str) -> set[tuple[str, str]]:
    return {(k.lower(), v.lower()) for k, v in _PAR.findall(str(texto))}


def conjuntos(valor: Any) -> list[set[tuple[str, str]]]:
    """Los pares clave/valor de un resultado, agrupados por donde aparecen.

    La cita textual no alcanza con un resultado estructurado: medido en srv3 el
    2026-09-17, el agente cito la telemetria real como
    `memoria:{disponible_kb:2011368,total_kb:3926208}`, con las claves en otro
    orden que el JSON, y la cita no se reconocia. Por pares se reconoce, y
    sigue sin pasar un par que no existe.

    Un conjunto por diccionario (con los de sus diccionarios hijos: una muestra
    y su `valor` van juntos) y uno por linea de texto con pares (`k=v | k=v`,
    como devuelve `consultar`). Los elementos de una lista van cada uno aparte,
    para no mezclar dos discos en una sola cita.
    """
    out: list[set[tuple[str, str]]] = []

    def de_texto(t: str) -> None:
        for linea in t.splitlines():
            ps = _pares(linea)
            if ps:
                out.append(ps)

    def visitar(v: Any) -> set[tuple[str, str]]:
        if isinstance(v, dict):
            propio: set[tuple[str, str]] = set()
            for k, x in v.items():
                clave = str(k).lower()
                if isinstance(x, dict):
                    propio |= visitar(x)
                elif isinstance(x, (list, tuple)):
                    for e in x:
                        visitar(e)
                elif isinstance(x, str):
                    t = x.strip().lower()
                    propio.add((clave, t))
                    if t.split():
                        propio.add((clave, t.split()[0]))
                    de_texto(x)
                else:
                    propio.add((clave, _escalar(x)))
            out.append(propio)
            return propio
        if isinstance(v, (list, tuple)):
            for e in v:
                visitar(e)
        elif isinstance(v, str):
            de_texto(v)
        return set()

    visitar(valor)
    return out


def _esta(cita: str, textos_n: list[str],
          grupos: list[set[tuple[str, str]]]) -> bool:
    n = normalizar(cita)
    if len(n) >= MIN_CITA and any(n in t for t in textos_n):
        return True
    pares = _pares(cita)
    return len(pares) >= 2 and any(pares <= g for g in grupos)


def verificar(rev: Revalidacion, paquete: Any,
              vistos: Iterable[Any]) -> Revalidacion:
    """Marca cada cita como verificada o no, y aplica las reglas.

    `paquete` es la medicion que se le mando; `vistos`, lo que devolvieron las
    herramientas en esta misma respuesta (texto o el resultado tal cual). Una
    cita vale si aparece textual, o si todos sus pares `clave: valor` estan
    juntos en un mismo resultado. Las reglas:

    - `falsa_alarma` y `arreglado_pendiente` necesitan confianza alta y al
      menos una cita verificada de algo mirado AHORA (una herramienta, no el
      paquete, que es la medicion vieja). Si no, pasan a `no_se_pudo_validar`.
    - Un `real` sin ninguna cita verificada sigue siendo real (la medicion lo
      sostiene), pero su causa no se publica: es lo que evita un "401" sacado
      de `2401s`.
    """
    vistos = list(vistos)
    vistos_n = [normalizar(textos(v)) for v in vistos]
    vistos_g = [g for v in vistos for g in conjuntos(v)]
    paquete_n = [normalizar(textos(paquete))]
    paquete_g = conjuntos(paquete)

    citas = []
    for c in rev.evidencia:
        cita = c.get("cita", "")
        fresca = _esta(cita, vistos_n, vistos_g)
        verificada = fresca or _esta(cita, paquete_n, paquete_g)
        citas.append({**c, "verificada": verificada, "fresca": fresca})
    rev = replace(rev, evidencia=tuple(citas))

    if rev.veredicto in ("falsa_alarma", "arreglado_pendiente"):
        frescas = [c for c in citas if c["fresca"]]
        if rev.confianza != "alta" or not frescas:
            falta = ("confianza " + rev.confianza if rev.confianza != "alta"
                     else "una cita verificable de lo que miro ahora")
            return replace(
                rev, veredicto="no_se_pudo_validar",
                nota=(f"el agente lo dio por {rev.veredicto.replace('_', ' ')} "
                      f"pero le falto {falta}: no se saca de la lista"))
        return rev

    if rev.veredicto == "real" and not any(c["verificada"] for c in citas):
        return replace(
            rev, causa="", accion="",
            nota=("el agente confirmo que sigue pero no pudo citar evidencia de "
                  "la causa: no se publica"))
    return rev
