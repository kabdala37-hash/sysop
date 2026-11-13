"""El proceso del bot de Telegram. Servicio aparte del observador.

POR QUE UN PROCESO APARTE Y NO UN HILO DEL DAEMON

El momento en que uno le escribe al bot es, casi siempre, el momento en que algo
se rompio. Si el bot viviera dentro del observador, una excepcion o un OOM que
se lleve al observador se lleva tambien el canal por el que ibas a preguntar que
paso. Son dos funciones con modos de falla distintos y por eso son dos unidades
de systemd distintas, cada una con su Restart.

Ademas el long polling bloquea hasta 30 segundos esperando un mensaje, y el
observador tiene un tick de 15: meter uno adentro del otro obliga a hilos y
sincronizacion para no atrasar las mediciones.

Comparten el almacen SQLite, que esta en WAL justamente para eso: el observador
escribe mientras el bot lee.
"""
from __future__ import annotations

import logging
import queue
import signal
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sysop import contexto, diagnostico, log_setup, settings
from sysop.bot import ajustes as ajustes_mod
from sysop.bot import conversacion as conv_mod
from sysop.bot import herramientas as herr_bot
from sysop.bot.telegram import Telegram
from sysop.llm import herramientas as herr_sistema
from sysop.store import db, registro

log = logging.getLogger("sysop.bot")

# Tras un error de red se espera antes de reintentar, para no castigar a la API
# de Telegram ni llenar el log a mil lineas por minuto si no hay internet.
ESPERA_ERROR_SEG = 15

# Cuanto sobrevive el hilo de un chat sin mensajes. No es por consumo -- un
# hilo dormido no cuesta nada -- sino para no acumular hilos y conexiones a
# SQLite de conversaciones que ya terminaron.
INACTIVIDAD_HILO_SEG = 600


class Despachador:
    """Un hilo por chat: en paralelo ENTRE chats, en orden ADENTRO de cada uno.

    Las dos mitades importan. Sin paralelo, dos personas que preguntan a la vez
    se hacen cola: la segunda espera a que el modelo termine de contestarle a la
    primera. Sin el orden adentro de cada chat, dos mensajes seguidos de la
    misma persona podrian contestarse al reves, y peor, dos hilos mutarian la
    misma lista de historial al mismo tiempo.

    Un pool de hilos generico no sirve para esto: reparte por hilo libre, no por
    conversacion, asi que no garantiza ninguna de las dos cosas.
    """

    def __init__(self, trabajo: Any) -> None:
        self._trabajo = trabajo
        self._colas: dict[int, queue.Queue] = {}
        self._hilos: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()
        self._parar = threading.Event()

    def encolar(self, chat_id: int, texto: str) -> None:
        # El `put` va ADENTRO del lock, junto con la comprobacion de que el
        # hilo sigue vivo. Afuera hay una carrera real: el hilo puede estar
        # saliendo por inactividad justo en ese instante y el mensaje se
        # perderia sin que nadie lo note.
        with self._lock:
            q = self._colas.get(chat_id)
            hilo = self._hilos.get(chat_id)
            if q is None or hilo is None or not hilo.is_alive():
                q = queue.Queue()
                self._colas[chat_id] = q
                hilo = threading.Thread(target=self._servir, args=(chat_id, q),
                                        name=f"chat-{chat_id}", daemon=True)
                self._hilos[chat_id] = hilo
                hilo.start()
            q.put(texto)

    def _servir(self, chat_id: int, q: queue.Queue) -> None:
        while not self._parar.is_set():
            try:
                texto = q.get(timeout=INACTIVIDAD_HILO_SEG)
            except queue.Empty:
                with self._lock:
                    # Otra vez la carrera, del otro lado: si entro un mensaje
                    # entre el timeout y el lock, se sigue atendiendo.
                    if not q.empty():
                        continue
                    self._colas.pop(chat_id, None)
                    self._hilos.pop(chat_id, None)
                    return
            try:
                self._trabajo(chat_id, texto)
            except Exception:  # noqa: BLE001
                log.exception("fallo atendiendo el chat %s", chat_id)
            finally:
                q.task_done()

    def parar(self, espera_seg: float = 5.0) -> None:
        self._parar.set()
        with self._lock:
            hilos = list(self._hilos.values())
        fin = time.monotonic() + espera_seg
        for h in hilos:
            h.join(timeout=max(0.0, fin - time.monotonic()))

    def esperar(self, timeout: float = 30.0) -> bool:
        """Espera a que no quede nada encolado. True si se vacio a tiempo."""
        fin = time.monotonic() + timeout
        while time.monotonic() < fin:
            with self._lock:
                colas = list(self._colas.values())
            if all(q.unfinished_tasks == 0 for q in colas):
                return True
            time.sleep(0.05)
        return False

    @property
    def activos(self) -> int:
        with self._lock:
            return sum(1 for h in self._hilos.values() if h.is_alive())

AYUDA = """Soy el observador de {instalacion}. Preguntame lo que quieras del
servidor y de los informes que te mando.

Comandos:
/estado    los incidentes abiertos ahora, sin gastar modelo
/informe   el ultimo informe que te mande
/reset     me olvido de esta conversacion y empezamos de cero
/ayuda     esto

Todo lo demas es conversacion. Por ejemplo:
- que es esto que me llego recien?
- por que se cayo el back?
- como viene el disco?"""


class Bot:
    def __init__(self, ruta_db: Optional[Any] = None) -> None:
        # `ruta_db` existe para poder probar el bot contra un almacen de
        # prueba. Sin esto, cada hilo abre la base real por su cuenta y un test
        # termina escribiendo en el almacen del servidor que observa.
        self._ruta_db = ruta_db
        self._parar = False
        self._offset = 0

        self.conversaciones = conv_mod.Conversaciones()
        self.despachador = Despachador(self._trabajar)
        # Una conexion SQLite POR HILO. La libreria de Python rechaza usar una
        # conexion desde un hilo distinto del que la creo, y compartir una sola
        # con un lock serializaria justo lo que se quiere paralelizar. SQLite en
        # WAL soporta varias conexiones a la vez: para eso se eligio WAL.
        self._local = threading.local()
        self._defs: dict[str, Any] = {}
        self._api: Any = None
        self._aj: Any = None
        self._ctx: Any = None

    def _conn(self) -> Any:
        c = getattr(self._local, "conn", None)
        if c is None:
            # `crear=False`: el bot nunca crea la base. Si no existe, se dice.
            c = db.connect(self._ruta_db, crear=False) if self._ruta_db \
                else db.connect(crear=False)
            self._local.conn = c
        return c

    def _almacen(self) -> Any:
        """La conexion de este hilo, o la falla si el almacen no responde."""
        try:
            c = self._conn()
            c.execute("SELECT 1 FROM incident LIMIT 1").fetchone()
            return c, None
        except sqlite3.Error as e:
            # Se descarta la conexion: la proxima pregunta vuelve a intentar, y
            # si el almacen se recupero anda sin reiniciar el bot.
            vieja = getattr(self._local, "conn", None)
            self._local.conn = None
            if vieja is not None:
                try:
                    vieja.close()
                except sqlite3.Error:
                    pass
            return None, db.Falla(db.clasificar(e), str(e))

    def _senal(self, signum: int, _frame: Any) -> None:
        log.info("senal %s recibida, terminando", signum)
        self._parar = True

    # -- ciclo de vida ------------------------------------------------------

    def arrancar(self) -> int:
        signal.signal(signal.SIGTERM, self._senal)
        signal.signal(signal.SIGINT, self._senal)

        registro.configurar("bot")
        aj = ajustes_mod.cargar()
        if aj.problema:
            log.error("no se puede arrancar el bot: %s", aj.problema)
            return 2
        if not aj.habilitado:
            log.info("el bot esta deshabilitado en telegram.yaml, no hay nada "
                     "que hacer")
            return 0

        try:
            ctx = contexto.cargar(crear=False)
        except (FileNotFoundError, RuntimeError) as e:
            log.error("%s", e)
            return 3
        if ctx.falla_almacen is not None:
            # No es motivo para no arrancar: el bot es el canal por el que se
            # pregunta que paso, y con el almacen roto es cuando mas hace falta.
            log.error("el bot arranca sin almacen -> %s", ctx.falla_almacen)

        api = Telegram(aj.token)
        try:
            yo = api.quien_soy()
        except Exception as e:  # noqa: BLE001
            # Un token invalido tiene que fallar ACA, ruidosamente, y no en el
            # primer mensaje que alguien mande dentro de tres dias.
            log.error("el token de Telegram no sirve: %s", e)
            return 4

        # Los controles declarados, una vez al arrancar: no cambian salvo que
        # cambie el codigo, y planificar consulta la base en cada llamada.
        try:
            self._defs = {c.check_id: {"titulo": c.titulo,
                                       "cada_seg": c.schedule_sec,
                                       "severidad": getattr(c.severity, "value",
                                                            str(c.severity)),
                                       "abre_tras": c.hysteresis.abre_tras,
                                       "cierra_tras": c.hysteresis.cierra_tras,
                                       "runbook": c.runbook or None}
                          for c, _ in ctx.planificar()}
            log.info("controles declarados: %d", len(self._defs))
        except Exception:  # noqa: BLE001
            # Sin esto el agente sigue andando, solo que sin saber que mide
            # cada control. No es motivo para no arrancar.
            log.exception("no se pudieron leer los controles declarados")

        self.conversaciones = conv_mod.Conversaciones(
            max_turnos=aj.max_turnos, max_chars=aj.max_chars_historial,
            ttl_seg=aj.ttl_horas * 3600, ventana_tokens=aj.ventana_tokens,
            pct_ventana=aj.pct_ventana)

        log.info("bot arrancando: @%s, instalacion=%s, %d autorizado(s), "
                 "contexto %d turnos / %d chars / %.1f h (techo del modelo: "
                 "%d chars)",
                 yo.get("username", "?"), settings.INSTALLATION,
                 len(aj.autorizados), aj.max_turnos,
                 self.conversaciones.max_chars, aj.ttl_horas,
                 self.conversaciones.techo_ventana)

        # Los mensajes viejos se descartan: si el bot estuvo caido un dia, no
        # tiene sentido contestar de golpe todo lo que le escribieron, con el
        # estado de hoy y sobre problemas de ayer.
        self._descartar_pendientes(api, aj)

        while not self._parar:
            try:
                self._una_vuelta(api, aj, ctx)
            except Exception:  # noqa: BLE001
                log.exception("la vuelta del bot fallo entera")
                self._dormir(ESPERA_ERROR_SEG)

        # Se le da un momento a las respuestas en vuelo: cortar a la mitad deja
        # a alguien esperando una contestacion que nunca llega.
        self.despachador.parar()
        log.info("bot terminado")
        return 0

    def _dormir(self, seg: float) -> None:
        """Espera cortable: si llega la senal, no se queda 15 segundos mas."""
        fin = time.monotonic() + seg
        while not self._parar and time.monotonic() < fin:
            time.sleep(0.5)

    def _descartar_pendientes(self, api: Telegram, aj: Any) -> None:
        try:
            updates = api.recibir(self._offset, espera_seg=0)
            if updates:
                self._offset = updates[-1]["update_id"] + 1
                log.info("se descartaron %d mensajes anteriores al arranque",
                         len(updates))
        except Exception as e:  # noqa: BLE001
            log.warning("no se pudieron descartar los pendientes: %s", e)

    # -- una vuelta ---------------------------------------------------------

    def _una_vuelta(self, api: Telegram, aj: Any, ctx: Any) -> None:
        self._api, self._aj, self._ctx = api, aj, ctx
        try:
            updates = api.recibir(self._offset, aj.espera_polling_seg)
        except Exception as e:  # noqa: BLE001
            log.warning("no se pudieron recibir mensajes: %s", e)
            self._dormir(ESPERA_ERROR_SEG)
            return

        for up in updates:
            self._offset = up["update_id"] + 1
            if self._parar:
                return
            msg = up.get("message") or {}
            texto = (msg.get("text") or "").strip()
            chat = (msg.get("chat") or {})
            chat_id = chat.get("id")
            if not texto or chat_id is None:
                continue

            if chat_id not in aj.autorizados:
                # No se contesta. Contestar "no estas autorizado" le confirma a
                # cualquiera que el bot existe y que hay algo del otro lado.
                # El id va al log para poder agregarlo si es de los nuestros.
                log.warning("mensaje de un chat NO autorizado, se ignora. "
                            "chat_id=%s usuario=%r. Si sos vos, agregalo a "
                            "`autorizados` en config/telegram.yaml",
                            chat_id, (msg.get("from") or {}).get("username"))
                continue

            # Se encola y se sigue recibiendo: si se atendiera aca, una
            # pregunta que tarda 30 segundos dejaria al bot sordo ese rato,
            # tambien para los demas.
            self.despachador.encolar(chat_id, texto)

    def _trabajar(self, chat_id: int, texto: str) -> None:
        """Lo que corre en el hilo propio de cada chat."""
        try:
            self._atender(self._api, self._aj, self._ctx, chat_id, texto)
        except Exception as e:  # noqa: BLE001
            log.exception("fallo atendiendo el chat %s", chat_id)
            self._decir(self._api, chat_id, f"Se me rompio algo: {str(e)[:200]}")

    def _decir(self, api: Telegram, chat_id: int, texto: str) -> None:
        try:
            api.mandar(chat_id, texto)
        except Exception as e:  # noqa: BLE001
            log.warning("no se pudo mandar la respuesta a %s: %s", chat_id, e)

    def _atender(self, api: Telegram, aj: Any, ctx: Any,
                 chat_id: int, texto: str) -> None:
        quien = aj.nombres.get(chat_id, chat_id)
        log.info("mensaje de %s: %r", quien, texto[:120])

        # -- comandos: no gastan modelo ------------------------------------
        cmd = texto.split()[0].lower().lstrip("/").split("@")[0] if texto.startswith("/") else ""
        if cmd in ("start", "ayuda", "help"):
            self._decir(api, chat_id,
                        AYUDA.format(instalacion=settings.INSTALLATION))
            return
        if cmd == "reset":
            self.conversaciones.olvidar(chat_id)
            self._decir(api, chat_id, "Listo, me olvide de la conversacion.")
            return

        conn, falla = self._almacen()
        if falla is not None:
            # Antes cada herramienta explotaba y se contestaba "Se me rompio
            # algo". Sin base igual se puede decir que paso: esta en el registro.
            self._decir(api, chat_id, _texto_sin_almacen(falla))
            return
        # `quien` viaja hasta las herramientas porque las de clasificar dejan
        # rastro de quien tomo la decision.
        app = getattr(ctx, "app", None)
        ejecutores = herr_bot.ligar(conn, self._defs, quien=str(quien), app=app,
                                    cfg_llm=ctx.cfg.llm)
        # Las de la plataforma (hoy `consultar`, SQL de lectura) van ademas de
        # las del observador y las de sistema.
        decl_app, ejec_app = diagnostico.herramientas_de(app) or ([], {})
        ejecutores.update(ejec_app)
        if cmd == "estado":
            self._decir(api, chat_id, _texto_estado(ejecutores["estado"]()))
            return
        if cmd == "informe":
            self._decir(api, chat_id, _texto_informe(ejecutores["informes"](limite=1)))
            return

        # -- conversacion ---------------------------------------------------
        api.escribiendo(chat_id)
        agente = self._agente(ctx, aj)
        historial, renovado = self.conversaciones.de(chat_id)
        if renovado:
            # Se avisa en vez de arrancar de cero en silencio: si no, la
            # proxima pregunta ("y eso que me dijiste?") se contesta sin el
            # contexto que el que pregunta da por sentado.
            self._decir(api, chat_id,
                        f"(Pasaron mas de {aj.ttl_horas:.0f} horas sin hablar, asi que "
                        f"empiezo de cero.)")
        respuesta = conv_mod.responder(
            conn=conn, agente=agente, cfg_llm=ctx.cfg.llm,
            historial=historial, pregunta=texto,
            ejecutores=ejecutores,
            declaraciones=(herr_bot.DECLARACIONES + herr_sistema.DECLARACIONES
                           + list(decl_app)),
            chat_id=chat_id, timeout=aj.timeout_seg,
            tope_usd=aj.presupuesto_diario_usd, mapa=ctx.cfg.mapa)
        self.conversaciones.podar(chat_id)
        self._decir(api, chat_id, respuesta)

    def _agente(self, ctx: Any, aj: Any) -> Any:
        """El agente del observador, con el prompt del chat.

        La credencial y el modelo se reusan de `fuentes` (con su respaldo, o
        sea que el bot sigue andando con la base caida). Lo unico propio es el
        prompt: conversar por telefono no es emitir un diagnostico en JSON.

        Se resuelve en CADA mensaje y no al arrancar: asi un cambio de agente o
        de credencial se toma sin reiniciar el bot, igual que en el observador.
        """
        cfg = ctx.fuentes()
        a = cfg.agente
        if a is None:
            return None
        import dataclasses                                   # noqa: PLC0415
        cambios = {}
        if aj.prompt:
            cambios["prompt"] = aj.prompt
        if aj.modelo:
            cambios["modelo"] = aj.modelo
        return dataclasses.replace(a, **cambios) if cambios else a


# ---------------------------------------------------------------------------
# Los comandos que no gastan modelo
# ---------------------------------------------------------------------------

def _texto_estado(d: dict[str, Any]) -> str:
    inc = d.get("incidentes") or []
    if not inc:
        return "No hay incidentes abiertos."
    filas = [f"{len(inc)} incidente(s) abierto(s):", ""]
    for i in inc[:15]:
        filas.append(f"[{i['severidad']}] {i['control']} en {i['recurso']}")
        filas.append(f"  {i['id']}, desde {i['desde']}")
        r = i.get("revalidacion")
        m = i.get("marcado_arreglado")
        if m:
            filas.append(f"  marcado arreglado por {m['quien'].replace('telegram:', '')}, "
                         f"se confirma despues de {str(m['confirma_en'])[:16]}")
        if r:
            filas.append(f"  revisado {str(r['cuando'])[:16]}: "
                         f"{r['veredicto'].replace('_', ' ')}. {r['motivo']}")
        elif i.get("causa_del_modelo"):
            filas.append(f"  {i['causa_del_modelo']}")
    if len(inc) > 15:
        filas.append(f"... y {len(inc) - 15} mas")
    return "\n".join(filas)


def _texto_sin_almacen(falla: Any) -> str:
    ahora = datetime.now(timezone.utc)
    abiertos = registro.abiertos_segun_registro(ahora - timedelta(days=7))
    filas = [f"El almacen del observador no esta disponible ({falla}).",
             "No puedo consultar la base; esto es lo que dice el registro en archivo.",
             ""]
    if abiertos:
        filas.append(f"{len(abiertos)} incidente(s) abierto(s) en los ultimos 7 dias:")
        for i in abiertos[:15]:
            filas.append(f"[{i['severity']}] {i['check_id']} en {i['resource_id']}")
            filas.append(f"  desde {str(i['opened_at'])[:19].replace('T', ' ')}")
    else:
        filas.append("El registro no tiene incidentes abiertos en los ultimos 7 dias.")
    fallas = registro.leer(ahora - timedelta(hours=24),
                           ("almacen_falla", "almacen_recupera", "almacen_aviso"))
    if fallas:
        filas.append("")
        filas.append("Lo ultimo del almacen:")
        for e in fallas[-5:]:
            filas.append(f"  {str(e.get('at', ''))[:19].replace('T', ' ')} "
                         f"{e.get('evento')} {e.get('clase') or e.get('mensaje') or ''}")
    return "\n".join(filas)


def _texto_informe(d: dict[str, Any]) -> str:
    infs = d.get("informes") or []
    if not infs:
        return "Todavia no mande ningun informe."
    i = infs[0]
    cab = f"{i['asunto']}\n{i['cuando']} ({i['estado']})\n"
    return cab + "\n" + (i.get("texto") or "(sin cuerpo)")


def main() -> int:
    log_setup.configurar("INFO", archivo=settings.LOG_DIR / "bot.log")
    return Bot().arrancar()


if __name__ == "__main__":
    raise SystemExit(main())
