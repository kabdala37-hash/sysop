"""La puerta al modelo: una llamada, con presupuesto, timeout y corte.

Tres cosas que tienen que estar antes de la primera llamada, no después:

**Presupuesto duro.** El observador existe para avisar de problemas, no para
generar uno nuevo en la factura. El gasto se acumula en el almacén local y
cuando se pasa del tope diario deja de llamar, sin apagarse: los incidentes
siguen abriéndose y el correo sigue saliendo, sin diagnóstico.

**Corte por fallos.** Si el proveedor no responde, no tiene sentido reintentar
en cada incidente durante horas. A los tres fallos seguidos se abre el corte por
diez minutos.

**Nunca tumba nada.** Cualquier excepción devuelve None. Un diagnóstico es un
extra sobre un aviso que ya se decidió mandar; que falle no puede impedir el
aviso.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sysop.llm import esquema, herramientas
from sysop.store import registro
from sysop.llm.esquema import Diagnostico

log = logging.getLogger("sysop.llm")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

FALLOS_PARA_CORTAR = 3
CORTE_SEG = 600

# Cuántas veces puede el agente pedir datos antes de tener que responder. Cuatro
# alcanza para "mirá el journal, ahora el log del servicio, ahora el disco" sin
# que una sola pregunta se vaya en llamadas.
MAX_VUELTAS = 4

# Precio por millón de tokens, en dólares. Son valores de referencia y se
# sobrescriben en `app.yaml`: el modelo cambia de precio y este archivo no se
# entera. La plataforma ya lleva la tabla real en `rc_token_pricing`.
PRECIOS_POR_DEFECTO = {"entrada": 0.30, "salida": 1.20}


class _Corte:
    """Estado del corte. Vive en memoria: el daemon es un solo proceso."""

    def __init__(self) -> None:
        self.fallos = 0
        self.abierto_hasta = 0.0

    @property
    def cortado(self) -> bool:
        return time.monotonic() < self.abierto_hasta

    def fallo(self) -> None:
        self.fallos += 1
        if self.fallos >= FALLOS_PARA_CORTAR:
            self.abierto_hasta = time.monotonic() + CORTE_SEG
            self.fallos = 0
            log.warning("modelo cortado por %d fallos seguidos; se reintenta en "
                        "%d minutos", FALLOS_PARA_CORTAR, CORTE_SEG // 60)

    def exito(self) -> None:
        self.fallos = 0


_corte = _Corte()


# Las llamadas del chat de Telegram se marcan con este prefijo en
# `llm_call.incident_id`, que no es una FK sino una etiqueta. Sirve para que las
# dos funciones no compitan por el mismo presupuesto: una conversacion larga no
# puede dejar al observador sin diagnosticar sus propios incidentes, que es su
# trabajo, y una tormenta de incidentes no puede dejarte sin poder preguntar
# que esta pasando.
PREFIJO_CHAT = "telegram:"


def gastado_hoy(conn: Any, origen: Optional[str] = None) -> float:
    """Lo gastado hoy en dolares. `origen` separa los dos presupuestos.

        None            todo, para reportar el costo del dia
        "diagnostico"   solo los diagnosticos de incidentes
        "chat"          solo las conversaciones por Telegram
    """
    filtro = ""
    if origen == "diagnostico":
        filtro = ("AND (incident_id IS NULL OR incident_id NOT LIKE "
                  f"'{PREFIJO_CHAT}%')")
    elif origen == "chat":
        filtro = f"AND incident_id LIKE '{PREFIJO_CHAT}%'"
    # `start of day` era el unico caso que funcionaba con `datetime()`, porque
    # el corte cae a medianoche y ahi la comparacion se decide por la fecha. Se
    # unifica igual: dos criterios conviviendo es como vuelve el error.
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00+00:00")
    en_base = 0.0
    try:
        fila = conn.execute(
            "SELECT COALESCE(SUM(costo_usd), 0) g FROM llm_call "
            f"WHERE at >= ? {filtro}", (hoy,)).fetchone()
        en_base = float(fila["g"] or 0.0)
    except Exception as e:  # noqa: BLE001
        log.warning("no se pudo leer el gasto de la base, se usa el registro: %s", e)
    # EL MAYOR de los dos, y no la suma: los dos cuentan las mismas llamadas.
    # Con la base sin escritura `llm_call` no crece, y el tope calculado solo
    # sobre ella no cortaba nunca: simulado el 2026-09-14, 5 ticks con tope de
    # 0,01 USD fueron 5 llamadas pagas y 0 filas. El registro si crece.
    return max(en_base, registro.gastado_hoy(origen, PREFIJO_CHAT))


# Las llamadas de la revalidacion se registran con este prefijo en
# `llm_call.incident_id`. Sin el, contarian como intentos del diagnostico de
# apertura (`diagnostico.pendientes` cuenta por incident_id) y un incidente
# revalidado dos veces quedaria para siempre sin su diagnostico.
PREFIJO_REVALIDAR = "revalidar:"

# Herramientas que aporta el adaptador de la aplicacion: (declaraciones,
# ejecutores). El gateway no sabe de ninguna plataforma; se las pasan.
Extra = Optional[tuple[list[dict[str, Any]], dict[str, Any]]]


def diagnosticar(conn: Any, agente: Any, paquete: dict[str, Any],
                 incident_id: str, cfg_llm: dict[str, Any],
                 extra: Extra = None) -> Optional[Diagnostico]:
    """Le pide al modelo que confirme y explique. None si no se pudo."""
    crudo = _consultar(conn, agente, paquete, incident_id, cfg_llm,
                       esquema.RESPONSE_SCHEMA, extra, [])
    if crudo is None:
        return None
    diag = esquema.validar(crudo)
    if diag is None:
        # Se registra QUE devolvio, no solo que fallo: sin el texto crudo, un
        # modelo que cambia de formato es indistinguible de uno caido.
        log.warning("el modelo devolvio algo no utilizable para %s: %r",
                    incident_id, str(crudo)[:300])
    return diag


def revalidar(conn: Any, agente: Any, paquete: dict[str, Any],
              incident_id: str, cfg_llm: dict[str, Any],
              extra: Extra = None) -> Optional[esquema.Revalidacion]:
    """Le pide que vuelva a mirar un incidente abierto, con evidencia.

    Devuelve la revalidacion YA verificada: cada cita contrastada contra lo que
    el propio agente miro en esta respuesta. None si no hubo respuesta usable.
    """
    vistos: list[Any] = []
    crudo = _consultar(conn, agente, paquete, PREFIJO_REVALIDAR + incident_id,
                       cfg_llm, esquema.RESPONSE_SCHEMA_REVALIDAR, extra, vistos)
    if crudo is None:
        return None
    rev = esquema.validar_revalidacion(crudo)
    if rev is None:
        log.warning("la revalidacion de %s devolvio algo no utilizable: %r",
                    incident_id, str(crudo)[:300])
        return None
    return esquema.verificar(rev, paquete, vistos)


def _consultar(conn: Any, agente: Any, paquete: dict[str, Any], etiqueta: str,
               cfg_llm: dict[str, Any], schema: dict[str, Any], extra: Extra,
               vistos: list[Any]) -> Optional[Any]:
    """La llamada con sus candados: agente, corte, presupuesto y registro."""
    if agente is None or not agente.utilizable:
        return None
    if _corte.cortado:
        log.info("corte del modelo activo: %s va sin diagnostico", etiqueta)
        return None

    tope = float(cfg_llm.get("presupuesto_diario_usd", 2.0))
    gastado = gastado_hoy(conn, "diagnostico")
    if gastado >= tope:
        log.warning("presupuesto diario agotado (%.4f de %.2f USD): %s va sin "
                    "diagnostico", gastado, tope, etiqueta)
        return None

    timeout = float(cfg_llm.get("timeout_seg", 45))
    t0 = time.monotonic()
    try:
        crudo, uso = _llamar(agente, paquete, timeout, schema=schema,
                             extra=extra, vistos=vistos)
    except Exception as e:  # noqa: BLE001
        _corte.fallo()
        _registrar(conn, etiqueta, agente.modelo, {}, 0.0,
                   int((time.monotonic() - t0) * 1000), False, str(e)[:400])
        log.warning("fallo el diagnostico de %s: %s", etiqueta, e)
        return None

    _corte.exito()
    costo = _costo(agente.modelo, uso, cfg_llm)
    _registrar(conn, etiqueta, agente.modelo, uso, costo,
               int((time.monotonic() - t0) * 1000), True, None)
    return crudo


# ---------------------------------------------------------------------------
# Proveedores
# ---------------------------------------------------------------------------

def _llamar(agente: Any, paquete: dict[str, Any], timeout: float,
            schema: Optional[dict[str, Any]] = None, extra: Extra = None,
            vistos: Optional[list[Any]] = None) -> tuple[Any, dict[str, int]]:
    import httpx                                          # noqa: PLC0415

    entrada = json.dumps(paquete, ensure_ascii=False, indent=1, default=str)
    vendor = (agente.vendor or "gemini").lower()
    schema = schema or esquema.RESPONSE_SCHEMA

    if vendor.startswith("gemini"):
        return _gemini(httpx, agente, entrada, timeout, schema=schema,
                       extra=extra, vistos=vistos)

    # DeepSeek y OpenAI hablan el mismo dialecto de chat/completions.
    return _openai_compat(httpx, agente, entrada, timeout, schema)


def _gemini(httpx: Any, agente: Any, entrada: str, timeout: float,
            con_herramientas: bool = True,
            max_vueltas: int = MAX_VUELTAS,
            schema: Optional[dict[str, Any]] = None, extra: Extra = None,
            vistos: Optional[list[Any]] = None) -> tuple[Any, dict[str, int]]:
    """Una conversación con el modelo, con herramientas de lectura.

    El agente puede pedir datos que no le mandamos: listar un directorio, leer
    un log, mirar la telemetría de ahora. Cada pedido se ejecuta acá y vuelve
    como resultado, hasta que responde o se acaban las vueltas.

    En la última vuelta se le sacan las herramientas: si sigue pidiendo datos
    en vez de concluir, hay que forzarlo a responder con lo que tiene. Un
    diagnóstico con menos información es mejor que ninguno.
    """
    url = GEMINI_URL.format(model=agente.modelo)
    contents: list[dict[str, Any]] = [
        {"role": "user", "parts": [{"text": entrada}]}]
    uso_total = {"entrada": 0, "salida": 0}
    usadas: list[str] = []
    decl_extra, ejec_extra = extra or ([], {})
    if vistos is None:
        vistos = []

    for vuelta in range(max_vueltas + 1):
        ultima = vuelta == max_vueltas
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": agente.prompt}]},
            "contents": contents,
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema or esquema.RESPONSE_SCHEMA,
                "temperature": 0.1,
            },
        }
        if con_herramientas and not ultima:
            body["tools"] = [{"functionDeclarations":
                              herramientas.DECLARACIONES + list(decl_extra)}]

        with httpx.Client(timeout=timeout) as c:
            resp = c.post(url, json=body, params={"key": agente.api_key})
        if resp.status_code != 200:
            raise RuntimeError(f"gemini {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        cand = (data.get("candidates") or [{}])[0]
        u = data.get("usageMetadata") or {}
        uso_total["entrada"] += int(u.get("promptTokenCount", 0) or 0)
        uso_total["salida"] += int(u.get("candidatesTokenCount", 0) or 0)

        partes = (cand.get("content") or {}).get("parts", []) or []
        pedidos = [p["functionCall"] for p in partes
                   if isinstance(p, dict) and "functionCall" in p]
        if not pedidos:
            return _texto_de(cand, usadas), uso_total

        # Se devuelven las partes del modelo TAL CUAL, sin reconstruirlas: en
        # Gemini 3 traen la firma del razonamiento y quitarla rompe el hilo.
        contents.append({"role": "model", "parts": partes})
        respuestas = []
        for fc in pedidos:
            nombre = fc.get("name", "")
            args = fc.get("args") or {}
            usadas.append(f"{nombre}({_corto(args)})")
            log.info("el agente pidio %s(%s)", nombre, _corto(args))
            resultado = _ejecutar(nombre, args, ejec_extra)
            # Lo que vio, tal como lo vio: contra esto se verifican las citas.
            vistos.append(resultado)
            respuestas.append({"functionResponse":
                               {"name": nombre, "response": resultado}})
        contents.append({"role": "user", "parts": respuestas})

    raise RuntimeError("el modelo no llego a una conclusion")


def _ejecutar(nombre: str, args: dict[str, Any],
              ejecutores: dict[str, Any]) -> dict[str, Any]:
    """Una herramienta del adaptador o una de sistema. Nunca lanza."""
    fn = ejecutores.get(nombre)
    if fn is None:
        return herramientas.ejecutar(nombre, args)
    try:
        return fn(**{k: v for k, v in (args or {}).items()
                     if k in fn.__code__.co_varnames})
    except TypeError as e:
        return {"error": f"argumentos invalidos: {e}"}
    except Exception as e:  # noqa: BLE001
        log.warning("fallo la herramienta %s: %s", nombre, e)
        return {"error": str(e)[:300]}


def _corto(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in (args or {}).items())


def _texto_de(cand: dict[str, Any], usadas: list[str]) -> str:
    fin = cand.get("finishReason")
    # MAX_TOKENS devuelve 200 con un JSON cortado a la mitad: se ve como una
    # respuesta valida hasta que el parseo falla sin explicar por que.
    if fin and fin not in ("STOP", "FINISH_REASON_STOP"):
        raise RuntimeError(f"gemini corto la respuesta: finishReason={fin}")
    # Se concatena el texto de TODAS las partes. Gemini 3 puede devolver una
    # parte de razonamiento junto a la del contenido, y quedarse con parts[0]
    # a ciegas es como se rompen estas integraciones.
    partes = [p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])
              if isinstance(p, dict) and p.get("text")]
    if usadas:
        log.info("el agente consulto %d fuente(s): %s", len(usadas),
                 "; ".join(usadas[:6]))
    return "".join(partes)


def _openai_compat(httpx: Any, agente: Any, entrada: str, timeout: float,
                   schema: Optional[dict[str, Any]] = None) -> tuple[Any, dict[str, int]]:
    base = (agente.base_url or "https://api.deepseek.com").rstrip("/")
    # El esquema no viaja como parámetro acá, así que se pide en el mensaje y se
    # valida igual de estricto al volver. Sin herramientas: una revalidacion por
    # este camino solo puede citar el paquete, y eso no alcanza para sacar nada
    # de la lista (`esquema.verificar`).
    if schema is None or schema is esquema.RESPONSE_SCHEMA:
        claves = ("las claves: es_real (booleano), confianza (alta|media|baja), "
                  "causa_probable (texto), accion_sugerida (texto) y, si es_real "
                  "es false, explicacion_descarte (texto)")
    else:
        claves = "estas claves: " + "; ".join(
            f"{k} ({v.get('description') or v.get('type')})"
            for k, v in schema["properties"].items())
    sistema = (agente.prompt + "\n\nRESPONDE UNICAMENTE con un objeto JSON con "
               + claves + ". Sin texto alrededor.")
    body = {
        "model": agente.modelo,
        "messages": [{"role": "system", "content": sistema},
                     {"role": "user", "content": entrada}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }
    with httpx.Client(timeout=timeout) as c:
        resp = c.post(f"{base}/v1/chat/completions", json=body,
                      headers={"Authorization": f"Bearer {agente.api_key}"})
    if resp.status_code != 200:
        raise RuntimeError(f"{agente.vendor} {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    texto = data["choices"][0]["message"]["content"]
    u = data.get("usage") or {}
    return texto, {"entrada": int(u.get("prompt_tokens", 0) or 0),
                   "salida": int(u.get("completion_tokens", 0) or 0)}


# ---------------------------------------------------------------------------
# Costo y registro
# ---------------------------------------------------------------------------

def _costo(modelo: str, uso: dict[str, int], cfg_llm: dict[str, Any]) -> float:
    precios = (cfg_llm.get("precios_usd_por_millon") or {})
    p = precios.get(modelo) or precios.get("default") or PRECIOS_POR_DEFECTO
    return (uso.get("entrada", 0) / 1e6 * float(p.get("entrada", 0))
            + uso.get("salida", 0) / 1e6 * float(p.get("salida", 0)))


def _registrar(conn: Any, incident_id: str, modelo: str, uso: dict[str, int],
               costo: float, ms: int, ok: bool, error: Optional[str]) -> None:
    # Primero el registro en archivo: la llamada ya se pago, y si la base no
    # escribe es lo unico que evita que el tope y los intentos queden ciegos.
    registro.anotar("llm_gasto", incident_id=incident_id, modelo=modelo,
                    costo_usd=round(costo, 6), ok=ok,
                    tokens_in=uso.get("entrada", 0),
                    tokens_out=uso.get("salida", 0))
    try:
        conn.execute(
            "INSERT INTO llm_call (at, incident_id, modelo, tokens_in, "
            "tokens_out, costo_usd, ms, ok, error) VALUES (?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), incident_id, modelo,
             uso.get("entrada", 0), uso.get("salida", 0), costo, ms,
             1 if ok else 0, error))
    except Exception:  # noqa: BLE001
        log.exception("no se pudo registrar la llamada al modelo")
