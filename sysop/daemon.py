"""El proceso que corre solo.

Un tick cada `TICK_SEG`. En cada uno:

    1. correr los controles que vencieron
    2. diagnosticar con el modelo lo que se haya abierto
    3. cada tanto, refrescar el inventario y cerrar los incidentes estables
    4. mandar alerta si apareció algo crítico, y despachar la bandeja

Lo que el daemon **no** hace es el informe periódico. Ese lo dispara cron
(`sysop-agent informe`), justamente para que salga aunque este proceso esté
muerto: un informe que no llega es indistinguible de un "todo bien".

Nada de esto puede tumbar el proceso. Un tick que explota se registra y el
siguiente arranca igual: un observador que se muere por un bug en un control es
peor que no tenerlo, porque además deja de avisar que no está mirando.

**Tampoco el almacén.** Cada tick empieza preguntándole si puede escribir. Si
no puede (corrupto, bloqueado, lleno, borrado, sin permiso) el tick no toca la
base y corre el camino de emergencia (`emergencia.py`), que avisa lo crítico por
correo directo. Antes, con el disco lleno, el daemon seguía vivo y no mandaba la
alerta de un crítico; con la base corrupta se moría al arrancar.
"""
from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import threading
import time
from typing import Any, Optional

from sysop import alertas, contexto, diagnostico, log_setup, settings
from sysop.core import engine, fuentes as fuentes_mod, incidents, inventory
from sysop.core.scheduler import Scheduler
from sysop.emergencia import Emergencia
from sysop.notify import outbox
from sysop.store import db, registro

log = logging.getLogger("sysop.daemon")

TICK_SEG = 15
# Cada cuántos ticks se hacen las tareas que no necesitan ir a ritmo de control.
CADA_INVENTARIO = 20      # 5 min
CADA_CIERRE = 40          # 10 min
CADA_CONFIG = 20          # 5 min
CADA_RETENCION = 240      # 1 h
# Cada cuánto se reintenta abrir un almacén que no abre. Uno bloqueado o sin
# escritura no se reabre: la sonda de cada tick ya es el reintento.
CADA_REABRIR = 20         # 5 min
# Con la degradacion forzada (la sonda pasa, guardar o avisar no), cada cuanto
# se deja correr un tick normal para ver si se arreglo.
CADA_REINTENTO_FORZADA = 4   # 1 min
# Sondas sanas seguidas para salir del modo degradado.
SALIR_TRAS = 2

VENTANA_CIERRE_SEG = 900
# Cuántas horas puede un incidente quedar sin una sola medición concluyente
# antes de cerrarse por falta de evidencia.
SIN_EVIDENCIA_H = 24


class Daemon:
    def __init__(self) -> None:
        self._parar = threading.Event()
        self.ctx: Optional[contexto.Contexto] = None
        self.pares: list = []
        self.sched = Scheduler()
        self.fuentes: Optional[fuentes_mod.Config] = None
        self.tick = 0
        self.emergencia = Emergencia()
        self._falla_forzada: Optional[db.Falla] = None
        self._fallos_almacen = 0
        self._error_del_tick: Optional[BaseException] = None
        # (firma, falla) de una base corrupta que no se pudo apartar.
        self._corrupta: Optional[tuple] = None
        # Arranca en 0 a proposito: la primera poda del proceso compacta. Un
        # reinicio no dispara VACUUM salvo que ademas haya algo que podar.
        self._ultimo_vacuum = 0.0
        # Métricas propias, para OPS-10: el observador tiene que poder reportar
        # que él mismo está fallando.
        self.metricas = {"ticks": 0, "controles": 0, "errores": 0,
                         "unknown": 0, "ticks_lentos": 0, "alertas": 0,
                         "diagnosticos": 0}

    # -- ciclo de vida ------------------------------------------------------

    def _senal(self, signum: int, _frame: Any) -> None:
        log.info("senal %s recibida, terminando", signum)
        self._parar.set()

    def arrancar(self) -> int:
        signal.signal(signal.SIGTERM, self._senal)
        signal.signal(signal.SIGINT, self._senal)
        registro.configurar("daemon")

        try:
            # `verificar`: una base con una pagina rota abre bien y falla
            # despues. `rotar` (D1): si esta corrupta se aparta y se arranca
            # con una nueva, en vez de quedar degradado para siempre.
            self.ctx = contexto.cargar(verificar=True, rotar=True)
        except FileNotFoundError as e:
            # Sin expectativas no hay contra qué comparar. Arrancar igual sería
            # fingir que se observa: mejor fallar acá, ruidosamente.
            log.error("%s", e)
            return 2
        except RuntimeError as e:
            log.error("%s", e)
            return 3

        cfg = self.ctx.cfg
        log.info("observador arrancando: instalacion=%s host=%s rol=%s app=%s",
                 cfg.installation, cfg.host, cfg.rol, cfg.app or "ninguna")

        if self.ctx.falla_almacen is not None:
            self.emergencia.registrar_falla(self.ctx.falla_almacen)
        if self.ctx.rotada:
            self.emergencia.avisar(
                self.ctx, "apartada", "[MAJOR] almacen del observador: base corrupta apartada",
                f"La base estaba corrupta y se aparto a {self.ctx.rotada}. Se "
                f"arranco con una nueva: se perdio la historia de mediciones e "
                f"incidentes, el registro en archivo sigue intacto.")

        try:
            self._replanificar()
        except Exception:  # noqa: BLE001
            # Sin almacen, planificar puede fallar. Se reintenta cada
            # CADA_CONFIG ticks; morirse aca era lo que dejaba mudo al observador.
            log.exception("no se pudo planificar al arrancar")
        self._recargar_fuentes()

        while not self._parar.is_set():
            inicio = time.monotonic()
            try:
                self._un_tick()
            except Exception:  # noqa: BLE001
                self.metricas["errores"] += 1
                log.exception("el tick fallo entero")

            dur = time.monotonic() - inicio
            if dur > TICK_SEG:
                # No se acumula deuda: se salta lo que no llegó. Encadenar ticks
                # atrasados hace que el observador se vaya quedando cada vez más
                # atrás justo cuando el servidor está cargado.
                self.metricas["ticks_lentos"] += 1
                log.warning("el tick tardo %.1fs, mas que el intervalo de %ds",
                            dur, TICK_SEG)
            self._parar.wait(max(0.0, TICK_SEG - dur))

        log.info("terminado. %s", self.metricas)
        if self.ctx and self.ctx.conn is not None:
            self.ctx.conn.close()
        return 0

    # -- el tick ------------------------------------------------------------

    def _un_tick(self) -> None:
        self.tick += 1
        self.metricas["ticks"] += 1
        registro.latir()
        if self.tick % CADA_RETENCION == 0:
            # Fuera de la base y antes de mirarla: el registro se poda aunque
            # el almacen este roto.
            self._podar_registro()

        if not self._almacen_sano():
            self._tick_degradado()
            return
        conn = self.ctx.conn
        self.emergencia.conciliar(conn)

        vencidos = self.sched.vencidos(self.pares)
        if vencidos:
            acc = engine.correr_ciclo(
                conn, vencidos, settings.HOST_ID,
                deps={"cfg": self.ctx.cfg, "conn": conn},
                presupuesto_seg=TICK_SEG * 0.8,
                proveedores=self.ctx.proveedores)
            self.metricas["controles"] += acc.corridos
            self.metricas["errores"] += acc.errores
            self.metricas["unknown"] += acc.desconocidos
            if acc.abiertos:
                log.info("%d incidente(s) abierto(s): %s",
                         len(acc.abiertos), ", ".join(acc.abiertos))
            if acc.recuperados:
                log.info("%d incidente(s) recuperado(s)", len(acc.recuperados))
            if acc.errores_almacen:
                self._error_almacen(acc.ultimo_error_almacen)
            # La emergencia sigue la racha tambien con la base sana: si no,
            # al entrar en modo degradado arrancaba de cero y la alerta salia un
            # ciclo tarde (medido con el arnes: 61 s contra 30 s).
            self.emergencia.observar(acc.resultados)

        self._diagnosticar()

        # Con `try` propio, como el diagnostico y las alertas: sin eso una falla
        # aca se llevaba el resto del tick, incluida la notificacion.
        if self.tick % CADA_INVENTARIO == 0:
            try:
                self._inventariar()
            except Exception:  # noqa: BLE001
                log.exception("fallo el inventario")
        if self.tick % CADA_CIERRE == 0:
            try:
                self._cerrar(conn)
            except Exception:  # noqa: BLE001
                log.exception("fallo el cierre de incidentes")
        if self.tick % CADA_CONFIG == 0:
            self._recargar_fuentes()
            self._replanificar()
        if self.tick % CADA_RETENCION == 0:
            self._aplicar_retencion()

        self._notificar()
        self._cerrar_tick()

    def _cerrar(self, conn: Any) -> None:
        cerrados = incidents.cerrar_recuperados(conn, VENTANA_CIERRE_SEG)
        if cerrados:
            log.info("%d incidente(s) cerrado(s) por estabilidad", len(cerrados))
        # Los que llevan un dia sin poder evaluarse. No es que se hayan
        # resuelto: es que no se pueden sostener, y que el control no vea
        # nada ya se reporta en la ceguera del informe.
        sin_ev = incidents.cerrar_sin_evidencia(conn, SIN_EVIDENCIA_H)
        if sin_ev:
            log.info("%d incidente(s) cerrado(s) por falta de evidencia",
                     len(sin_ev))

    # -- el almacen -----------------------------------------------------------

    def _almacen_sano(self) -> bool:
        """Sonda + reapertura. True si el tick puede usar la base."""
        falla = db.sondear(self.ctx.conn, settings.DB_PATH, self.ctx.inodo)
        if falla is not None and self.ctx.conn is None and self.ctx.falla_almacen:
            # Sin conexion la sonda solo sabe "no esta abierto"; el motivo real
            # es el de la ultima apertura, y es el que decide y va al correo.
            falla = self.ctx.falla_almacen
        if falla is None and self._falla_forzada is not None:
            # La sonda pasa pero guardar o avisar falla. Cada tanto se deja
            # correr un tick normal: si anda, `_cerrar_tick` levanta la falla.
            if self.tick % CADA_REINTENTO_FORZADA != 0:
                falla = self._falla_forzada

        if falla is not None and self._toca_reabrir(falla):
            perdido = falla.clase == db.PERDIDO
            falla = self._reabrir(falla)
            if falla is None and perdido:
                self.emergencia.avisar(
                    self.ctx, "recreada", "[MAJOR] almacen del observador: base recreada",
                    f"La base {settings.DB_PATH} desaparecio con el observador "
                    f"corriendo y se creo una nueva. Se perdio la historia de "
                    f"mediciones e incidentes; el registro en archivo sigue intacto "
                    f"y evita repetir las alertas ya enviadas.")

        if falla is not None:
            self.emergencia.registrar_falla(falla)
            return False

        if self.emergencia.activa:
            self.emergencia.sanos_seguidos += 1
            # Dos sondas sanas seguidas antes de salir. Saliendo a la primera,
            # una falla que va y vuelve borraba la racha en memoria en cada
            # vuelta y el critico no llegaba nunca a abrir (revision: 6 entradas
            # al modo degradado y cero correos).
            if self.emergencia.sanos_seguidos >= SALIR_TRAS:
                self.emergencia.salir(self.ctx, self.fuentes)
                # HST-01 mide el hueco entre latidos guardados en la base, y en
                # modo degradado no se guardan. Sin esto, un lock de un minuto
                # abria "observador interrumpido" con el daemon corriendo todo
                # el tiempo. La caida del almacen ya queda en el registro.
                try:
                    from sysop.adapters.so_linux.collectors import journal  # noqa: PLC0415
                    journal.guardar_cursor(self.ctx.conn, "agente:latido",
                                           str(time.time()))
                except Exception:  # noqa: BLE001
                    log.exception("no se pudo renovar el latido de HST-01")
        return True

    def _toca_reabrir(self, falla: db.Falla) -> bool:
        if falla.clase == db.PERDIDO:
            # Enseguida: sqlite sigue escribiendo en el archivo borrado sin dar
            # error, y todo lo que se escriba ahi se pierde.
            return True
        if self.ctx.conn is None and falla.clase == db.BLOQUEADO:
            # Un lock al arrancar: se reintenta en cada tick (con espera corta),
            # no cada cinco minutos con un correo de "almacen fallo" en el medio.
            return True
        if self.ctx.conn is not None and falla.clase in (db.BLOQUEADO, db.SIN_ESCRITURA):
            return False
        # La primera vez en cuanto se ve, despues cada CADA_REABRIR ticks.
        return (not self.emergencia.activa) or self.tick % CADA_REABRIR == 0

    def _reabrir(self, falla: db.Falla) -> Optional[db.Falla]:
        viejo, self.ctx.conn = self.ctx.conn, None
        # `ctx.conn` queda en None ANTES de intentar: si la apertura falla, lo
        # que queda es "no esta abierto" (se reintenta) y no una conexion
        # cerrada que la sonda veia como SIN_ESCRITURA para siempre.
        if viejo is not None:
            try:
                viejo.close()
            except Exception:  # noqa: BLE001
                pass
        path = settings.DB_PATH
        firma = db.firma(path)
        if (falla.clase == db.CORRUPTO and self._corrupta is not None
                and firma is not None and firma == self._corrupta[0]):
            # La misma base corrupta que ya se verifico y no se pudo apartar:
            # no se vuelve a pasar `quick_check` (segundos sobre una base
            # grande), solo se reintenta apartarla por si se libero espacio.
            alm = db.apartar_y_abrir(path, self._corrupta[1], espera=1.0)
        else:
            alm = db.abrir(path, crear=True, verificar=True, rotar=True, espera=1.0)
        self._corrupta = (db.firma(path), alm.causa) if alm.causa is not None else None

        self.ctx.conn, self.ctx.inodo = alm.conn, alm.inodo
        self.ctx.falla_almacen = alm.falla
        if alm.rotada:
            self.emergencia.avisar(
                self.ctx, "apartada", "[MAJOR] almacen del observador: base corrupta apartada",
                f"La base se corrompio con el observador corriendo y se aparto a "
                f"{alm.rotada}. Se sigue con una nueva; el registro en archivo "
                f"sigue intacto.")
        if alm.falla is None:
            log.info("almacen reabierto")
            self._falla_forzada = None
            self._fallos_almacen = 0
            try:
                self._replanificar()
            except Exception:  # noqa: BLE001
                log.exception("no se pudo replanificar al reabrir el almacen")
        return alm.falla

    def _error_almacen(self, e: BaseException) -> None:
        """Un error del sqlite en guardar o avisar, con la sonda pasando."""
        if self._error_del_tick is None:
            self._error_del_tick = e

    def _cerrar_tick(self) -> None:
        """Si guardar o avisar falla dos ticks seguidos, se fuerza la degradacion.

        La sonda escribe en `cursor`. Una pagina rota puede dejar eso andando y
        hacer fallar `incident` o `outbox`: revision del 2026-09-14, 11 ticks con
        "fallo revisando alertas urgentes", cero correos y nunca modo degradado.
        """
        e, self._error_del_tick = self._error_del_tick, None
        if e is None:
            self._fallos_almacen = 0
            if self._falla_forzada is not None:
                log.info("el almacen vuelve a guardar y avisar")
            self._falla_forzada = None
            return
        self._fallos_almacen += 1
        if self._fallos_almacen < 2:
            return
        try:
            r = self.ctx.conn.execute("PRAGMA quick_check").fetchone()[0]
        except Exception as qe:  # noqa: BLE001
            r = str(qe)
        if r != "ok":
            # Corrupta de verdad: la reapertura la aparta (D1).
            self._falla_forzada = db.Falla(db.CORRUPTO, f"quick_check: {str(r)[:200]} "
                                                        f"(al guardar o avisar: {e})")
        else:
            clase = db.clasificar(e)
            if clase in (db.CORRUPTO, db.NO_ABRE, db.PERDIDO):
                # Sin evidencia de corrupcion no se reabre ni se aparta nada.
                clase = db.SIN_ESCRITURA
            self._falla_forzada = db.Falla(clase, f"falla al guardar o avisar: {e}")
        log.error("el almacen pasa la sonda pero no guarda o no avisa -> %s",
                  self._falla_forzada)

    def _tick_degradado(self) -> None:
        if self.tick % CADA_CONFIG == 0:
            self._recargar_fuentes()
            try:
                self._replanificar()
            except Exception:  # noqa: BLE001
                # Planificar puede necesitar la base. Se sigue con lo ultimo
                # planificado, que es mejor que no mirar nada.
                log.exception("no se pudo replanificar sin almacen")
        try:
            self.metricas["alertas"] += self.emergencia.tick(
                self.ctx, self.fuentes, self.pares, self.sched,
                presupuesto_seg=TICK_SEG * 0.8)
        except Exception:  # noqa: BLE001
            log.exception("fallo el tick de emergencia")

    def _podar_registro(self) -> None:
        try:
            dias = int(self.ctx.cfg.umbral("agente.retencion_dias.registro", 90))
            n = registro.podar(dias)
            if n:
                log.info("registro podado", extra={"datos": {"archivos": n}})
        except Exception:  # noqa: BLE001
            log.exception("fallo la poda del registro")

    def _diagnosticar(self) -> None:
        """Le pide al modelo que confirme y explique lo que se abrió.

        Va DESPUES de correr los controles y ANTES de notificar, para que la
        alerta urgente salga con el diagnóstico adentro y no en un correo
        aparte. De a uno por tick: la llamada es HTTP y el tick dura quince
        segundos.
        """
        if self.fuentes is None:
            return
        try:
            self.metricas["diagnosticos"] += diagnostico.revisar(
                self.ctx, self.fuentes, self.ctx.cfg.llm)
        except Exception:  # noqa: BLE001
            log.exception("fallo el diagnostico; los avisos salen igual")

    def _replanificar(self) -> None:
        """Recalcula qué controles corresponden.

        Se rehace cada tanto y no una sola vez al arrancar: un mount nuevo o un
        endpoint que se habilita en la configuración tienen que entrar en
        observación sin reiniciar el servicio.
        """
        self.pares = self.ctx.planificar()

    def _inventariar(self) -> None:
        # El log lo escribe `inventory.sincronizar`, que ademas distingue el
        # inventario inicial de un cambio posterior. Aca se logueaba lo mismo
        # otra vez, asi que cada novedad salia DUPLICADA en el archivo.
        inventory.sincronizar(
            self.ctx.conn, self.ctx.descubrir(), settings.HOST_ID,
            self.ctx.tipos_de_recurso())

    def _aplicar_retencion(self) -> None:
        """Poda lo viejo. El observador no puede llenar el disco que vigila.

        LOS 400 DIAS DE `check_result` ERAN UNA BOMBA. Medido en srv3: 94.000
        filas por dia, 24,5 MB diarios. A 400 dias son ~9,8 GB sobre un disco de
        13,5 GiB con 1,84 GiB libres, o sea que el observador se comia solo el
        disco que vigila, y su propio control de almacen (OPS-10) tenia el
        umbral en 2 GiB: MAS que el espacio libre, asi que el disco llegaba al
        100% antes de que dijera una palabra.

        La ventana la fija quien mas historia necesita, que es la tendencia:
        `trend` proyecta sobre 30 dias. 45 deja margen sin volver a la bomba.

        EL ORDEN NO ALCANZA. `check_result.evidence_id` referencia
        `observation` con la FK activa (`store/db.py`) y sin `ON DELETE`, y
        `engine.persistir` guarda evidencia en todo resultado que no da OK. Una
        medicion de 20 dias (se queda, retiene 45) apunta a una observacion de
        20 dias (se va, retiene 7): el DELETE de `observation` fallaba SIEMPRE.
        Paso en srv2 desde el 2026-09-12: nunca podaba, y como encima no habia
        indice sobre `evidence_id`, tardaba 30 s en fallar y el informe lo
        mostraba como "observador interrumpido". Por eso antes de borrar la
        evidencia se suelta la referencia: la medicion queda, sin su detalle.
        """
        cfg = self.ctx.cfg
        dias = lambda k, d: int(cfg.umbral(f"agente.retencion_dias.{k}", d))  # noqa: E731
        podado = 0
        try:
            conn = self.ctx.conn
            # Primero lo que APUNTA, despues lo apuntado. Ver la nota de arriba.
            for tabla, campo, d in (
                ("check_result", "observed_at", dias("check_result", 45)),
                ("observation", "observed_at", dias("observation", 7)),
                ("incident_event", "at", dias("incident_event", 90)),
                ("llm_call", "at", dias("llm_call", 90)),
            ):
                # Con `datetime('now', ...)` el efecto era el OPUESTO al de
                # las consultas de lectura: como aca se compara con `<`, las
                # filas del dia del corte quedaban por ENCIMA del umbral y no
                # se borraban. O sea que se retenia un dia de mas.
                corte = db.corte(days=d)
                if tabla == "observation":
                    # El mismo `corte` que el DELETE: calcularlo dos veces
                    # dejaria afuera del UPDATE lo que cae entre los dos.
                    conn.execute(
                        "UPDATE check_result SET evidence_id = NULL "
                        "WHERE evidence_id IN "
                        "  (SELECT id FROM observation WHERE observed_at < ?)",
                        (corte,))
                cur = conn.execute(
                    f"DELETE FROM {tabla} WHERE {campo} < ?", (corte,))
                podado += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            cur = conn.execute(
                "DELETE FROM outbox WHERE state='SENT' AND created_at < ?",
                (db.corte(days=dias("outbox", 90)),))
            podado += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            log.exception("fallo la retencion")
            return

        # `cursor` NO se poda: es estado, no historia. Borrar un cursor del
        # journal hace que se reprocesen eventos ya vistos.

        if podado:
            log.info("retencion aplicada", extra={"datos": {"filas": podado}})
        self._compactar(podado)

    def _compactar(self, podado: int) -> None:
        """VACUUM, como mucho una vez por dia y solo si hubo algo que podar.

        Sin esto la poda no devuelve un byte al sistema: SQLite marca las
        paginas como libres y las reusa, pero el archivo no se achica nunca. Con
        el disco al 86% eso convierte la retencion en un gesto simbolico.

        Es caro (reescribe la base entera), por eso no va en cada poda.
        """
        if not podado:
            return
        ahora = time.time()
        if ahora - self._ultimo_vacuum < 86400:
            return
        self._ultimo_vacuum = ahora

        # Con la retencion en regimen, lo que poda cada hora lo reusan las
        # inserciones siguientes y el VACUUM no devuelve casi nada: en srv2
        # (2026-10) 1.505 -> 1.500 MiB, con el tick trabado ~35 s todos los
        # dias y un "observador interrumpido" en el informe. Solo vale la pena
        # si las paginas libres son una parte apreciable del archivo.
        try:
            libres = self.ctx.conn.execute("PRAGMA freelist_count").fetchone()[0]
            pagina = self.ctx.conn.execute("PRAGMA page_size").fetchone()[0]
            total = self.ctx.conn.execute("PRAGMA page_count").fetchone()[0]
        except Exception:  # noqa: BLE001
            log.exception("no se pudo medir el espacio libre del almacen")
            return
        minimo = float(self.ctx.cfg.umbral("agente.vacuum_min_libre_frac", 0.10))
        if not total or libres / total < minimo:
            log.info("VACUUM salteado: no hay espacio que devolver",
                     extra={"datos": {"libre_mib": round(libres * pagina / 1048576.0, 1),
                                      "total_mib": round(total * pagina / 1048576.0, 1)}})
            return

        # El tamaño es SOLO para el log, asi que se mide aparte y tolerando el
        # fallo. Medirlo adentro del try, y antes del VACUUM, hacia que no poder
        # stat-ear el archivo cancelara la compactacion entera: la unica señal
        # habria sido una linea de excepcion, y el disco seguiria sin achicarse.
        antes = self._tam_mib()
        try:
            self.ctx.conn.execute("VACUUM")
        except Exception:  # noqa: BLE001
            log.exception("fallo el VACUUM")
            return
        # En WAL el VACUUM deja el -wal del tamaño de la base nueva hasta la poda
        # siguiente, una hora despues: en srv2, 1,5 GB menos de disco cada dia.
        try:
            self.ctx.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            log.exception("fallo el checkpoint despues del VACUUM")
        log.info("almacen compactado",
                 extra={"datos": {"antes_mib": antes, "despues_mib": self._tam_mib()}})

    @staticmethod
    def _tam_mib():
        """El tamaño del almacen, o None si no se puede leer."""
        try:
            return round(settings.DB_PATH.stat().st_size / 1048576.0, 1)
        except OSError:
            return None

    # -- correo -------------------------------------------------------------

    def _recargar_fuentes(self) -> None:
        try:
            self.fuentes = self.ctx.fuentes()
            if self.fuentes.origen == "respaldo":
                log.warning("config desde el respaldo del archivo: "
                            "la fuente no respondia")
            for d in self.fuentes.desactualizados:
                # No es una falla: el archivo se mantiene a mano y este es
                # el aviso de que quedo atras, mientras todavia no hace falta.
                log.warning("el respaldo quedo desactualizado -> %s", d)
        except Exception:  # noqa: BLE001
            log.exception("no se pudieron resolver las fuentes")

    def _notificar(self) -> None:
        """Sólo urgencias. El informe periódico lo dispara cron."""
        c = self.fuentes
        if c is None or not c.puede_notificar:
            return
        try:
            self.metricas["alertas"] += alertas.revisar(
                self.ctx, c, self.ctx.cfg.alertas)
        except Exception as e:  # noqa: BLE001
            log.exception("fallo revisando alertas urgentes")
            if isinstance(e, sqlite3.Error):
                self._error_almacen(e)

        # Siempre, aunque no se haya encolado nada: puede haber reintentos de un
        # envío anterior esperando su turno.
        try:
            res = outbox.despachar(self.ctx.conn, c.cuenta)
            if res["enviados"] or res["fallidos"]:
                log.info("correo: %s", res)
        except Exception as e:  # noqa: BLE001
            log.exception("fallo el despacho de la bandeja")
            if isinstance(e, sqlite3.Error):
                self._error_almacen(e)
        # Los avisos del almacen (base apartada, recreada, recuperada) salen sin
        # bandeja, tambien con el almacen sano.
        try:
            self.emergencia.despachar(c)
        except Exception:  # noqa: BLE001
            log.exception("fallo el despacho de los avisos del almacen")


def main() -> int:
    # Formato de CONVENCION-DE-LOGS.md. Al journal por stdout y a archivo con
    # rotacion: las dos, como pide la convencion.
    log_setup.configurar("INFO", settings.LOG_DIR / "daemon.log")
    return Daemon().arrancar()


if __name__ == "__main__":
    sys.exit(main())
