"""El camino de emergencia: avisar lo crítico cuando el almacén no anda.

Medido el 2026-09-14 rompiendo la base de verdad: con el disco lleno el daemon
seguía vivo y **no mandaba la alerta de un crítico abierto**, porque todo correo
pasaba por la tabla `outbox`. Vivo y mudo, que es la peor forma de fallar de un
observador. Con la base bloqueada, cada tick se trababa 30 s y la alerta salía
minutos tarde.

Esto NO es un segundo motor. Es lo mínimo para no quedar mudo:

    1. corre los controles con los mismos evaluadores, sin almacén
    2. cuenta la racha en memoria, con el `abre_tras` de cada control
    3. si abre un CRÍTICO, lo manda por SMTP directo, sin bandeja
    4. no lo repite si el registro dice que ya salió y no se cerró
    5. avisa que el almacén falló, lo recuerda cada 6 h, y avisa al volver

Sin diagnóstico con modelo (D3): sin almacén no hay contexto para el paquete, y
el gasto es lo más fácil de descontrolar. Cuanto menos código corra acá, menos
cosas se pueden romper justo en el momento en que hace falta que ande.

Las decisiones D1-D6 están en `.docs/plan_sysop_resiliencia/00-PLAN.md`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from sysop import alertas
from sysop.core import engine, incidents
from sysop.core.models import Severity, State
from sysop.notify import outbox, render, smtp
from sysop.store import db, registro

log = logging.getLogger("sysop.emergencia")

# El correo "el almacén falló" sale recién si la falla dura esto. Un lock de
# unos segundos (el informe escribiendo, un VACUUM) no es un problema a avisar;
# las alertas de lo crítico salen igual desde el primer tick degradado.
AVISO_TRAS_SEG = 60
# D5: mientras siga roto, un recordatorio cada tanto.
RECORDATORIO_SEG = 6 * 3600
# D4: cuánto hacia atrás se mira el registro para no repetir una alerta.
VENTANA_DEDUPE_H = 24
# Al volver el almacén, cuánto se espera antes de decidir que un problema que
# se avisó por acá ya no está (el motor normal tiene que llegar a verlo).
GRACIA_CONCILIAR_SEG = 180

# Reintentos de un correo directo: mismos tiempos que la bandeja.
BACKOFF_MIN = (5, 15, 30)
# Cada envío puede tardar hasta 30 s. Más de esto por tick deja de ser un tick.
CORREOS_POR_TICK = 3


@dataclass
class _Correo:
    tipo: str
    periodo: str
    asunto: str
    html: str
    intentos: int = 0
    proximo: float = 0.0
    message_id: Optional[str] = None


@dataclass
class Emergencia:
    activa: bool = False
    falla: Optional[db.Falla] = None
    desde: float = 0.0                # monotonic de la primera falla de la racha
    desde_iso: str = ""
    avisada: bool = False
    ultimo_aviso: float = 0.0
    _rachas: dict = field(default_factory=dict)     # (check, recurso) -> (motivo, n)
    _sanos: dict = field(default_factory=dict)      # (check, recurso) -> n
    _abiertos: dict = field(default_factory=dict)   # (check, recurso) -> incidente
    _a_conciliar: dict = field(default_factory=dict)  # fp completo -> (id, desde)
    _cola: list = field(default_factory=list)
    _sin_cuenta_avisado: bool = False
    # Sondas sanas seguidas; el daemon sale del modo degradado con dos.
    sanos_seguidos: int = 0
    # Controles que no pueden correr sin la base. Se saltean hasta que vuelva:
    # si no, cada tick dejaba un warning por cada uno (arnes: 11 en 7 ticks).
    _necesitan_base: set = field(default_factory=set)

    # -- entrar y salir -----------------------------------------------------

    def registrar_falla(self, falla: db.Falla) -> None:
        self.sanos_seguidos = 0
        if not self.activa:
            self.activa = True
            self.desde = time.monotonic()
            self.desde_iso = datetime.now(timezone.utc).isoformat()
            self.avisada = False
            log.error("almacen no disponible, modo degradado -> %s", falla)
            registro.anotar("almacen_falla", clase=falla.clase, mensaje=falla.mensaje)
            registro.anotar("modo_degradado_entra", clase=falla.clase)
        elif self.falla is not None and falla.clase != self.falla.clase:
            log.error("el almacen sigue sin andar, ahora -> %s", falla)
            registro.anotar("almacen_falla", clase=falla.clase, mensaje=falla.mensaje)
        self.falla = falla

    def salir(self, ctx: Any, fuentes: Any) -> None:
        """El almacén volvió. Se avisa solo si antes se avisó que se había ido."""
        if not self.activa:
            return
        dur = int(time.monotonic() - self.desde)
        log.info("el almacen volvio tras %d s; fin del modo degradado", dur)
        registro.anotar("almacen_recupera", segundos=dur)
        registro.anotar("modo_degradado_sale", segundos=dur)
        if self.avisada:
            self._encolar_aviso(
                ctx, "recuperado", "[OK] almacen del observador recuperado",
                f"El almacen volvio a funcionar despues de {_duracion(dur)}. "
                f"La ultima falla fue {self.falla}.", abiertos=[])
        # Lo que se aviso por aca queda pendiente de conciliar con el motor
        # normal: si sigue mal, el motor lo abre y el dedupe evita repetirlo; si
        # ya no esta, hay que anotarlo cerrado o el dedupe taparia una recurrencia
        # durante 24 h.
        ahora = time.monotonic()
        for inc in self._abiertos.values():
            self._a_conciliar[inc["fingerprint"]] = (inc["incident_id"], ahora,
                                                     inc["gracia"])
        self._abiertos.clear()
        # Las rachas NO se borran: las sigue alimentando `observar()` con la base
        # sana, y borrarlas es lo que hacia que una falla intermitente no llegara
        # nunca a abrir un critico.
        self._necesitan_base.clear()
        self.activa = False
        self.falla = None
        self.despachar(fuentes)

    def conciliar(self, conn: Any) -> None:
        """Con el almacén de vuelta: ¿lo que se avisó en emergencia sigue abierto?"""
        if not self._a_conciliar:
            return
        ahora = time.monotonic()
        for fp, (iid, desde, gracia) in list(self._a_conciliar.items()):
            if ahora - desde < gracia:
                continue
            try:
                sigue = incidents.abierto(conn, fp) is not None
            except Exception:  # noqa: BLE001
                continue
            if not sigue:
                registro.anotar("emergencia_recuperado", incident_id=iid, fp=fp[:8],
                                motivo="el motor normal no lo volvio a ver")
            self._a_conciliar.pop(fp)

    def avisar(self, ctx: Any, clave: str, asunto: str, texto: str) -> None:
        """Un aviso suelto sobre el almacén (base apartada, base recreada)."""
        registro.anotar("almacen_aviso", clave=clave, mensaje=texto)
        self._encolar_aviso(ctx, clave, asunto, texto,
                            abiertos=list(self._abiertos.values()))

    def observar(self, resultados: list) -> None:
        """Con la base sana: lleva la racha en memoria, sin abrir ni avisar nada."""
        for cd, recurso, r in resultados:
            self._procesar(None, cd, recurso, r, sombra=True)

    # -- el tick degradado --------------------------------------------------

    def tick(self, ctx: Any, fuentes: Any, pares: list, sched: Any,
             presupuesto_seg: float) -> int:
        """Corre los controles vencidos sin almacén. Devuelve alertas encoladas."""
        abiertas = 0
        vencidos = [(cd, rec) for cd, rec in sched.vencidos(pares)
                    if cd.check_id not in self._necesitan_base]
        for cd, recurso, r, evidencia in _evaluar(ctx, vencidos, presupuesto_seg):
            if r.reason_code == "CHECK_ERROR":
                # Casi siempre pidio la conexion y no hay. Se saltea mientras
                # dure: da UNKNOWN igual, y no se mira el texto del error porque
                # la politica de evidencia de un control puede no dejarlo pasar.
                self._necesitan_base.add(cd.check_id)
                continue
            if self._procesar(ctx, cd, recurso, r):
                abiertas += 1
        self._avisos_del_almacen(ctx)
        self.despachar(fuentes)
        return abiertas

    def _procesar(self, ctx: Any, cd: Any, recurso: Any, r: Any,
                  sombra: bool = False) -> bool:
        clave = (cd.check_id, recurso.resource_id)
        if r.state is State.UNKNOWN:
            # No mueve nada, igual que en el motor: no poder mirar no es ni
            # problema ni recuperacion.
            return False
        if r.state.es_problema and r.severity == Severity.CRITICAL:
            motivo, n = self._rachas.get(clave, (None, 0))
            n = n + 1 if motivo == r.reason_code else 1
            self._rachas[clave] = (r.reason_code, n)
            self._sanos.pop(clave, None)
            if not sombra and clave not in self._abiertos and n >= cd.hysteresis.abre_tras:
                return self._abrir(ctx, cd, clave, r)
            return False

        self._rachas.pop(clave, None)
        if clave in self._abiertos:
            s = self._sanos.get(clave, 0) + 1
            if s >= cd.hysteresis.cierra_tras:
                inc = self._abiertos.pop(clave)
                self._sanos.pop(clave, None)
                registro.anotar("emergencia_recuperado", incident_id=inc["incident_id"],
                                fp=inc["fingerprint"][:8])
                log.info("emergencia: %s dejo de verse", inc["incident_id"])
            else:
                self._sanos[clave] = s
        return False

    def _abrir(self, ctx: Any, cd: Any, clave: tuple, r: Any) -> bool:
        fp = incidents.fingerprint(r.check_id, r.resource_id, r.reason_code)
        ahora = datetime.now(timezone.utc)
        inc = {"incident_id": f"EMG-{fp[:8]}-{ahora.strftime('%Y%m%dT%H%M')}",
               "fingerprint": fp, "check_id": r.check_id,
               "resource_id": r.resource_id, "severity": r.severity.value,
               "state": "OPEN", "opened_at": ahora.isoformat(),
               "cause": {"reason_code": r.reason_code, "expected": r.expected,
                         "actual": r.actual,
                         "evidence_quality": r.evidence_quality.value},
               # Cuanto puede tardar el motor normal en volver a verlo: su
               # cadencia por las muestras que necesita, con margen por el
               # jitter. Con 180 s fijos, JOB-03 (cada 300 s) se daba por
               # recuperado antes de que el motor llegara a mirarlo y la alerta
               # salia dos veces (revision).
               "gracia": max(GRACIA_CONCILIAR_SEG,
                             cd.schedule_sec * cd.hysteresis.abre_tras * 1.5 + 60)}
        self._abiertos[clave] = inc
        registro.anotar("emergencia_abierto", incident_id=inc["incident_id"],
                        fp=fp[:8], check_id=r.check_id, resource_id=r.resource_id,
                        severity=r.severity.value, reason_code=r.reason_code,
                        actual=r.actual)

        previa = registro.alerta_vigente(fp[:8], VENTANA_DEDUPE_H)
        if previa is not None:
            log.warning("emergencia: %s en %s ya se aviso %s y no consta cerrado; "
                        "no se repite", r.check_id, r.resource_id,
                        previa.get("at", "")[:19])
            return False

        otros = [i for c, i in self._abiertos.items() if c != clave][:5]
        nota = (f"El almacen del observador no esta disponible ({self.falla}). "
                f"Esta alerta salio por el camino de emergencia: sin diagnostico "
                f"del modelo y con la racha contada en memoria.")
        self._cola.append(_Correo(
            # El mismo tipo que la alerta normal: asi el dedupe del registro
            # reconoce las dos como el mismo aviso.
            tipo=alertas.TIPO, periodo=inc["incident_id"],
            asunto=render.asunto_urgente(ctx.cfg.installation, inc) + " (sin almacen)",
            html=render.alerta_urgente(ctx.cfg.installation, inc, otros, nota)))
        log.warning("emergencia: alerta directa por %s (%s en %s)",
                    inc["incident_id"], r.check_id, r.resource_id)
        return True

    def _avisos_del_almacen(self, ctx: Any) -> None:
        if not self.activa or self.falla is None:
            return
        ahora = time.monotonic()
        if not self.avisada:
            if ahora - self.desde < AVISO_TRAS_SEG:
                return
            asunto = f"[CRITICAL] almacen del observador: {self.falla.clase}"
            texto = (f"El almacen del observador no funciona desde "
                     f"{self.desde_iso[:19].replace('T', ' ')} UTC: {self.falla}. "
                     f"El observador sigue corriendo y avisa lo critico por correo "
                     f"directo, sin diagnostico. Lo que no es critico no se avisa "
                     f"hasta que el almacen vuelva.")
        elif ahora - self.ultimo_aviso >= RECORDATORIO_SEG:
            asunto = f"[CRITICAL] almacen del observador sigue sin andar: {self.falla.clase}"
            texto = (f"Sigue sin funcionar desde {self.desde_iso[:19].replace('T', ' ')} "
                     f"UTC ({_duracion(int(ahora - self.desde))}): {self.falla}.")
        else:
            return
        self.avisada = True
        self.ultimo_aviso = ahora
        self._encolar_aviso(ctx, "falla", asunto, texto,
                            abiertos=list(self._abiertos.values()))

    def _encolar_aviso(self, ctx: Any, clave: str, asunto_corto: str, texto: str,
                       abiertos: list) -> None:
        inst = ctx.cfg.installation
        self._cola.append(_Correo(
            tipo="almacen",
            periodo=f"{clave}:{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}",
            asunto=f"[Sysop IA][{inst}]{asunto_corto}",
            html=render.aviso_almacen(inst, asunto_corto, texto, abiertos)))

    # -- correo sin bandeja -------------------------------------------------

    def despachar(self, fuentes: Any) -> None:
        if not self._cola:
            return
        if fuentes is None or not fuentes.puede_notificar:
            if not self._sin_cuenta_avisado:
                log.error("emergencia: hay %d correo(s) sin poder salir: no hay "
                          "cuenta de correo o destinatarios", len(self._cola))
                self._sin_cuenta_avisado = True
            return
        self._sin_cuenta_avisado = False
        ahora = time.monotonic()
        mandados = 0
        for c in list(self._cola):
            if mandados >= CORREOS_POR_TICK:
                break
            if c.proximo > ahora:
                continue
            msg = smtp.armar(fuentes.cuenta, fuentes.destinatarios, c.asunto,
                             c.html, message_id=c.message_id)
            c.message_id = msg["Message-ID"]
            envio = smtp.enviar(fuentes.cuenta, msg)
            mandados += 1
            c.intentos += 1
            if envio.ok:
                outbox.anotar_envio(c.tipo, c.periodo, c.asunto, "directo")
                self._cola.remove(c)
                log.info("emergencia: enviado %s (%s)", c.tipo, c.periodo)
                continue
            if "autenticacion" in envio.error.lower() or c.intentos >= len(BACKOFF_MIN):
                registro.anotar("correo_fallido", tipo=c.tipo, periodo=c.periodo,
                                asunto=c.asunto, error=envio.error)
                self._cola.remove(c)
                log.error("emergencia: no se pudo enviar %s (%s) tras %d intento(s): %s",
                          c.tipo, c.periodo, c.intentos, envio.error)
            else:
                c.proximo = ahora + BACKOFF_MIN[c.intentos - 1] * 60
                log.warning("emergencia: fallo el envio de %s, reintento en %d min: %s",
                            c.tipo, BACKOFF_MIN[c.intentos - 1], envio.error)


def _evaluar(ctx: Any, controles: list, presupuesto_seg: float) -> list:
    """Corre controles sin persistir nada. Los que necesitan el almacén dan UNKNOWN.

    Abre los mismos recursos compartidos que un ciclo normal (la lectura de
    systemd, la conexión MySQL): sin eso cada control abriría los suyos y el
    tick degradado costaría más que el sano.
    """
    deps: dict[str, Any] = {"cfg": ctx.cfg, "conn": None}
    cierres = []
    for proveer in ctx.proveedores:
        try:
            c = proveer(controles, deps)
            if c is not None:
                cierres.append(c)
        except Exception:  # noqa: BLE001
            log.exception("emergencia: fallo un proveedor de recursos")
    out = []
    inicio = time.monotonic()
    try:
        for cd, recurso in controles:
            if time.monotonic() - inicio > presupuesto_seg:
                log.error("emergencia: presupuesto agotado, %d control(es) sin correr",
                          len(controles) - len(out))
                break
            r, evidencia = engine.ejecutar_control(None, cd, recurso, deps)
            out.append((cd, recurso, r, evidencia or {}))
    finally:
        for c in reversed(cierres):
            try:
                c()
            except Exception:  # noqa: BLE001
                pass
    return out


def _duracion(seg: int) -> str:
    if seg < 120:
        return f"{seg} s"
    if seg < 7200:
        return f"{seg // 60} min"
    return f"{seg // 3600} h {(seg % 3600) // 60} min"
