"""La conversacion con el agente: historial, herramientas y presupuesto.

Es el mismo agente que diagnostica y las mismas herramientas de lectura, pero
con dos diferencias que importan:

  - Responde TEXTO, no el JSON del diagnostico. El diagnostico se valida contra
    un esquema porque su salida la consume el codigo; aca la consume una
    persona.
  - Tiene historial. Una conversacion donde cada pregunta arranca de cero
    obliga a repetir el contexto en cada mensaje, que es justo lo que uno no
    quiere hacer desde el telefono a las tres de la manana.

El presupuesto es propio y separado del de los diagnosticos: ver `PREFIJO_CHAT`
en `sysop/llm/gateway.py`.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

import yaml

from sysop.llm import gateway, herramientas as herr_sistema

log = logging.getLogger("sysop.bot.conversacion")

# Cuantas veces puede pedir datos antes de tener que contestar. Mas alto que en
# el diagnostico (4) porque una pregunta abierta suele necesitar mirar varias
# cosas: el informe, el incidente que menciona y el log del servicio.
MAX_VUELTAS = 6

# Turnos que se recuerdan por conversacion. Cada turno son dos entradas (la
# pregunta y la respuesta) mas lo que haya pedido el agente en el medio.
MAX_TURNOS = 12

# Tope por PESO, que es el que de verdad importa. Contar turnos no alcanza: un
# solo turno donde el agente leyo un informe entero pesa mas que diez preguntas
# cortas. Medido en la primera prueba real: la segunda pregunta de una
# conversacion ya iba con 18.873 tokens de entrada, casi todos resultados de
# herramientas arrastrados del turno anterior.
#
# En caracteres y no en tokens a proposito: contar tokens de verdad exige
# llamar al modelo o cargar un tokenizador, y para decidir cuando podar alcanza
# con el orden de magnitud (~4 caracteres por token).
# Medido en srv3: un informe completo pesa 3.429 caracteres, un journal de 60
# lineas 6.618, y una pregunta con su respuesta 168. Con este tope entran unos
# 12 turnos con lectura pesada, y la charla sin herramientas no lo toca nunca:
# ahi manda MAX_TURNOS mucho antes.
#
# No lo limita la ventana del modelo, que es de ~1M de tokens y esto son 20.000.
# Lo limita el COSTO: cada turno reenvia el historial entero, asi que el gasto
# de una conversacion crece con el cuadrado de su largo. Con este valor, el tope
# de 1 USD/dia da unas 160 preguntas.
MAX_CHARS_HISTORIAL = 80000

# El TECHO, que es otra cosa que el tope de arriba y no hay que confundirlos.
#
#   MAX_CHARS_HISTORIAL  es economico: cuanto conviene gastar por pregunta.
#   Este                 es fisico: cuanto ENTRA en el modelo.
#
# El de arriba muerde siempre en la practica. Este existe para dos casos donde
# el otro no alcanza: que alguien lo suba mucho en el yaml, y que se cambie a un
# modelo de ventana chica. En los dos, pasarse no degrada nada -- la API
# devuelve 400 y el bot deja de contestar, justo cuando se lo necesita.
#
# 90% y no 100% porque la ventana la comparten el historial, el prompt del
# sistema y la respuesta.
PCT_VENTANA = 90

# Tokens de ventana del modelo. Se declara y no se consulta: la API no la
# expone de forma confiable, y un default optimista es peor que uno explicito.
VENTANA_TOKENS = 1_000_000

# Cuantos caracteres entran en un token. Aproximacion, y alcanza: esto decide
# cuando podar, no cuanto facturar.
CHARS_POR_TOKEN = 4

# Una conversacion sin actividad se olvida. No es por memoria: es que retomar
# un hilo de ayer con el estado del servidor de ayer confunde mas de lo que
# ayuda, porque lo que se pregunta es siempre "que esta pasando AHORA".
TTL_SEG = 3 * 3600

# Lo que queda en lugar del resultado de una herramienta que se descarto. El
# par pedido/respuesta se conserva (sacar uno de los dos hace que Gemini
# responda 400), pero el contenido se reemplaza.
DESCARTADO = {"nota": "resultado descartado por antiguedad. Si lo necesitas, "
                      "volve a pedirlo: ademas va a estar actualizado."}


class Conversaciones:
    """El historial por chat, en memoria.

    En memoria y no en SQLite a proposito: se pierde al reiniciar el bot, y eso
    esta bien. Lo que no se puede perder son los informes y los incidentes, que
    viven en el almacen y se consultan con herramientas.
    """

    def __init__(self, max_turnos: int = MAX_TURNOS,
                 max_chars: int = MAX_CHARS_HISTORIAL,
                 ttl_seg: float = TTL_SEG,
                 ventana_tokens: int = VENTANA_TOKENS,
                 pct_ventana: float = PCT_VENTANA) -> None:
        self._h: dict[int, list[dict[str, Any]]] = {}
        self._visto: dict[int, float] = {}
        # Los tres se configuran desde `telegram.yaml`. Los valores de arriba
        # son el default, no la unica opcion: cuanto contexto conviene guardar
        # depende de para que se use el canal, y eso no se decide en el codigo.
        self.max_turnos = max_turnos
        self.ttl_seg = ttl_seg
        # Gana el mas chico de los dos. El techo casi nunca es el que manda, y
        # esta bien que asi sea: si llegara a mandar, significa que el modelo no
        # tiene lugar ni para lo que ya decidimos que conviene gastar.
        techo = int(ventana_tokens * pct_ventana / 100 * CHARS_POR_TOKEN)
        self.max_chars = min(max_chars, techo)
        self.techo_ventana = techo
        if techo < max_chars:
            log.warning("el tope de contexto lo baja la ventana del modelo: "
                        "%d chars configurados, %d que entran en el %d%% de "
                        "%d tokens", max_chars, techo, pct_ventana, ventana_tokens)

    def de(self, chat_id: int) -> tuple[list[dict[str, Any]], bool]:
        """El historial del chat, y si se renovo por inactividad."""
        renovado = False
        if self._h.get(chat_id) and \
                time.time() - self._visto.get(chat_id, 0) > self.ttl_seg:
            self._h.pop(chat_id, None)
            renovado = True
        self._visto[chat_id] = time.time()
        return self._h.setdefault(chat_id, []), renovado

    def olvidar(self, chat_id: int) -> None:
        self._h.pop(chat_id, None)

    def podar(self, chat_id: int) -> None:
        """Deja el historial abajo de los dos topes: turnos y peso.

        El orden importa. Primero se descartan los RESULTADOS de herramientas
        viejos, que es donde esta el peso, y recien despues se tiran turnos
        enteros. Un resultado viejo, ademas de pesado, es una foto vencida del
        servidor: que el modelo la vuelva a pedir no es una perdida, es lo
        correcto.
        """
        h = self._h.get(chat_id)
        if not h:
            return

        inicios = self._inicios(h)
        if len(inicios) > self.max_turnos:
            h = h[inicios[-self.max_turnos]:]

        # Los resultados del ULTIMO turno se conservan: son sobre los que se
        # esta conversando ahora mismo.
        if _peso(h) > self.max_chars:
            corte = self._inicios(h)[-1:] or [len(h)]
            for c in h[:corte[0]]:
                for parte in c.get("parts", []):
                    if isinstance(parte, dict) and "functionResponse" in parte:
                        parte["functionResponse"]["response"] = DESCARTADO

        # Si con eso todavia no entra, se tiran turnos desde el principio.
        while _peso(h) > self.max_chars:
            inicios = self._inicios(h)
            if len(inicios) <= 1:
                break
            h = h[inicios[1]:]

        self._h[chat_id] = h

    @staticmethod
    def _inicios(h: list[dict[str, Any]]) -> list[int]:
        """Donde arranca cada turno: un mensaje de texto del usuario.

        No sirve cualquier entrada con role 'user': las respuestas de
        herramientas tambien viajan con ese rol, y cortar en una de ellas deja
        un `functionResponse` sin su `functionCall`, que Gemini rechaza con 400.
        """
        return [i for i, c in enumerate(h)
                if c.get("role") == "user"
                and any("text" in p for p in c.get("parts", []))]


def _peso(h: list[dict[str, Any]]) -> int:
    """Cuanto pesa el historial, en caracteres."""
    return len(json.dumps(h, ensure_ascii=False, default=str))


def responder(conn: Any, agente: Any, cfg_llm: dict[str, Any],
              historial: list[dict[str, Any]], pregunta: str,
              ejecutores: dict[str, Any], declaraciones: list[dict[str, Any]],
              chat_id: int, timeout: float = 60.0,
              tope_usd: float = 1.0, mapa: Optional[dict[str, Any]] = None) -> str:
    """Una vuelta completa de conversacion. Devuelve el texto para el usuario.

    Nunca lanza: el usuario del otro lado tiene que recibir SIEMPRE una
    respuesta, aunque sea la del error. Un bot mudo es indistinguible de un bot
    caido, y el que pregunta esta justamente tratando de averiguar que se cayo.
    """
    if agente is None or not agente.utilizable:
        return ("No tengo agente configurado (le falta el prompt, el modelo o "
                "la credencial). Miralo con: sysop-agent config")

    gastado = gateway.gastado_hoy(conn, "chat")
    if gastado >= tope_usd:
        return (f"Llegue al tope de gasto del dia para el chat "
                f"({gastado:.2f} de {tope_usd:.2f} USD). Los diagnosticos de "
                f"los incidentes siguen andando: tienen presupuesto aparte.")

    vendor = (agente.vendor or "gemini").lower()
    if not vendor.startswith("gemini"):
        # Las herramientas estan implementadas para el dialecto de Gemini. Con
        # otro proveedor se contesta igual, sin poder mirar el servidor, y se
        # dice: una respuesta a ciegas presentada como si no lo fuera es peor
        # que no contestar.
        log.warning("el agente usa vendor %r: el chat va sin herramientas", vendor)

    historial.append({"role": "user", "parts": [{"text": pregunta}]})

    t0 = time.monotonic()
    # El uso se acumula ACA y no adentro: si la vuelta falla en el intento
    # numero cinco, esos tokens se gastaron igual y tienen que quedar
    # registrados. Antes se perdian con la excepcion y el gasto del dia salia
    # mas bajo que el real.
    uso: dict[str, int] = {"entrada": 0, "salida": 0}
    try:
        texto = _gemini_chat(agente, historial, declaraciones, ejecutores,
                             timeout, uso, mapa=mapa,
                             con_herramientas=vendor.startswith("gemini"))
    except Exception as e:  # noqa: BLE001
        log.warning("fallo la conversacion del chat %s: %s", chat_id, e)
        # La pregunta se saca del historial: si quedara, cada intento siguiente
        # arrastraria una pregunta sin respuesta y el hilo se desalinea.
        if historial and historial[-1].get("role") == "user":
            historial.pop()
        gateway._registrar(conn, f"{gateway.PREFIJO_CHAT}{chat_id}", agente.modelo,
                           uso, gateway._costo(agente.modelo, uso, cfg_llm),
                           int((time.monotonic() - t0) * 1000), False,
                           str(e)[:400])
        return f"No pude contestar: {str(e)[:300]}"

    costo = gateway._costo(agente.modelo, uso, cfg_llm)
    gateway._registrar(conn, f"{gateway.PREFIJO_CHAT}{chat_id}", agente.modelo,
                       uso, costo, int((time.monotonic() - t0) * 1000), True, None)
    historial.append({"role": "model", "parts": [{"text": texto}]})
    log.info("chat %s respondido en %d ms, %d/%d tokens, %.4f USD", chat_id,
             int((time.monotonic() - t0) * 1000), uso.get("entrada", 0),
             uso.get("salida", 0), costo)
    return texto


# Lo que se le dice en la ultima vuelta. Quitarle las herramientas no alcanza:
# con functionCalls en el historial, Gemini sigue emitiendolos igual. Pasó en la
# primera prueba real, con una pregunta abierta ("como viene el disco") que se
# comio las siete vueltas sin contestar nada.
CIERRE = ("Ya no podes pedir mas datos. Respondé AHORA, en texto, con lo que "
          "ya miraste. Si no alcanza para una conclusion, decí qué miraste y "
          "qué te falta.")


def _instruccion(agente: Any, mapa: Optional[dict[str, Any]]) -> str:
    """El prompt del chat mas el mapa del servidor.

    El mapa sale del YAML del host y es el MISMO que recibe el diagnostico. Que
    lo compartan no es una optimizacion: es lo que evita que el chat y el
    diagnostico tengan dos versiones distintas de que es este servidor, cual es
    su rol y que rutas mirar. Antes el prompt del chat repetia a mano la
    advertencia de que reiniciar un worker corta llamadas; ahora eso vive en un
    solo lugar, el yaml del host, y se cambia una vez.
    """
    if not mapa:
        return agente.prompt
    return (agente.prompt + "\n\n" + "=" * 60
            + "\nESTE SERVIDOR (declarado en el yaml del host, es la fuente de "
              "verdad):\n" + "=" * 60 + "\n"
            + yaml.safe_dump(mapa, allow_unicode=True, sort_keys=False,
                             default_flow_style=False))


def _gemini_chat(agente: Any, historial: list[dict[str, Any]],
                 declaraciones: list[dict[str, Any]], ejecutores: dict[str, Any],
                 timeout: float, uso_total: dict[str, int],
                 mapa: Optional[dict[str, Any]] = None,
                 con_herramientas: bool = True) -> str:
    """El bucle de herramientas, en modo texto.

    Es el mismo mecanismo que `gateway._gemini` pero sin `responseSchema`: aca
    la salida la lee una persona. Y el historial se MUTA, porque lo que el
    modelo pidio y lo que se le contesto tiene que quedar en el hilo para la
    proxima pregunta.
    """
    import httpx                                          # noqa: PLC0415

    url = gateway.GEMINI_URL.format(model=agente.modelo)
    instruccion = _instruccion(agente, mapa)
    usadas: list[str] = []

    for vuelta in range(MAX_VUELTAS + 1):
        ultima = vuelta == MAX_VUELTAS
        # En la ultima vuelta se manda el pedido de cierre SIN dejarlo en el
        # historial: es una instruccion de este intento, no algo que el usuario
        # pregunto, y guardarla ensuciaria el hilo de la proxima pregunta.
        contents = historial + [{"role": "user", "parts": [{"text": CIERRE}]}] \
            if ultima else historial
        body: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": instruccion}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.2},
        }
        if con_herramientas and not ultima:
            body["tools"] = [{"functionDeclarations": declaraciones}]

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
        texto = "".join(p.get("text", "") for p in partes
                        if isinstance(p, dict) and p.get("text"))
        if not pedidos or ultima:
            if usadas:
                log.info("el agente consulto: %s", "; ".join(usadas[:8]))
            if texto.strip():
                return texto
            if ultima:
                # Se le acabaron las vueltas y encima no dijo nada. Devolver
                # que consulto es mas util que un error: al menos se ve por
                # donde estuvo mirando.
                return ("No llegue a una conclusion. Consulte: "
                        + ("; ".join(usadas[:8]) if usadas else "nada")
                        + ". Preguntame algo mas concreto.")
            fin = cand.get("finishReason")
            raise RuntimeError(f"el modelo no devolvio texto (finishReason={fin})")

        # Las partes del modelo van TAL CUAL: en Gemini 3 traen la firma del
        # razonamiento y reconstruirlas rompe el hilo.
        historial.append({"role": "model", "parts": partes})
        respuestas = []
        for fc in pedidos:
            nombre = fc.get("name", "")
            args = fc.get("args") or {}
            usadas.append(f"{nombre}({_corto(args)})")
            # Se loguea cada pedido y no solo el resumen final: cuando una
            # conversacion se va de vueltas, esto es lo que dice en que se
            # quedo trabado.
            log.info("el agente pidio %s(%s)", nombre, _corto(args))
            respuestas.append({"functionResponse":
                               {"name": nombre,
                                "response": _ejecutar(nombre, args, ejecutores)}})
        historial.append({"role": "user", "parts": respuestas})

    # Inalcanzable: la ultima vuelta siempre devuelve algo.
    raise RuntimeError("me quede sin vueltas antes de poder contestar")


def _ejecutar(nombre: str, args: dict[str, Any],
              ejecutores: dict[str, Any]) -> dict[str, Any]:
    """Corre una herramienta. Nunca lanza: un error es un resultado mas."""
    fn = ejecutores.get(nombre)
    if fn is None:
        # Las de sistema se despachan por su propio modulo, que ya filtra
        # secretos y valida rutas. No se reimplementa ese control aca.
        return herr_sistema.ejecutar(nombre, args)
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
