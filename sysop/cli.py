"""Línea de comandos. Es lo que reemplaza al panel web.

Tres verbos y nada más:

    sysop-agent estado        qué ve el observador ahora, en la terminal
    sysop-agent informe       arma y manda el informe periódico. Lo llama cron
    sysop-agent probar        corre un ciclo completo sin mandar nada
    sysop-agent diagnosticar  le pide al modelo que explique lo que hay abierto
    sysop-agent logs          los errores de los logs declarados, agrupados
    sysop-agent config        de donde sale la configuracion, y si el
                             respaldo del archivo alcanza sin la base
    sysop-agent telegram      verifica el canal de Telegram sin levantarlo

No hay servidor web, ni puerto abierto, ni clave de API que rotar. Para mirar el
estado hay que estar en el servidor, que es exactamente donde está quien lo va a
arreglar.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sysop import (contexto, diagnostico as diag_mod, informe as informe_mod,
                   log_setup, settings)
from sysop.core import engine, fuentes as fuentes_mod, incidents
from sysop.core.models import Severity
from sysop.store import db, registro

log = logging.getLogger("sysop.cli")

ANCHO = 78


def _linea(c: str = "-") -> None:
    print(c * ANCHO)


def cmd_estado(ctx: Any, args: Any) -> int:
    conn = ctx.conn
    pares = ctx.planificar()

    _linea("=")
    print(f" {ctx.cfg.installation}  (host {ctx.cfg.host}, rol {ctx.cfg.rol})")
    print(f" aplicacion: {ctx.cfg.app or 'ninguna'}    controles: {len(pares)}")
    _linea("=")

    caido = informe_mod._observador_caido(conn)
    print(f" observador: {'CAIDO -- ' + caido if caido else 'corriendo'}")

    r = informe_mod.resumen_controles(conn, len(pares))
    print(f" controles:  {r['ok']} ok   {r['problema']} con problema   "
          f"{r['unknown']} unknown   {r['sin_datos']} sin datos")

    activos = incidents.activos(conn, Severity.WARNING)
    cuentan, descartados = diag_mod.separar(conn, activos)
    abiertos = [i for i in cuentan if i["state"] != "RECOVERED"]
    print(f" incidentes: {len(abiertos)} abierto(s), "
          f"{len(cuentan) - len(abiertos)} recuperado(s) sin cerrar, "
          f"{len(descartados)} descartado(s) por el modelo")

    costo = informe_mod.costo_modelo(conn)
    if costo["llamadas"]:
        print(f" modelo:     {costo['llamadas']} llamada(s) en 24 h, "
              f"{costo['costo_usd']:.4f} USD")
    _linea()

    if not activos:
        print(" sin incidentes activos")
    for i in cuentan + descartados:
        marca = "  " if i["state"] == "OPEN" else "~ "
        if i in descartados:
            marca = "x "
        print(f"{marca}[{i['severity']:<8}] {i['check_id']:<8} {i['resource_id']}")
        causa = i.get("cause") or {}
        if causa.get("reason_code"):
            print(f"             {causa['reason_code']}  {causa.get('actual') or ''}")
        print(f"             desde {i['opened_at'][:19].replace('T', ' ')}"
              f"   {i['incident_id']}")
        d = i.get("diagnostico")
        if d:
            veredicto = "real" if d["es_real"] else "falso positivo"
            print(f"             modelo ({veredicto}, {d['confianza']}): "
                  f"{(d['causa'] or '')[:66]}")
            if d["accion"]:
                print(f"             sugerido: {(d['accion'] or '')[:66]}")
    _linea()
    if descartados:
        print(" x = descartado por el modelo. Sigue abierto y midiendose.")
    return 0


def cmd_diagnosticar(ctx: Any, args: Any) -> int:
    """Le pide al modelo que explique lo que este abierto y sin diagnosticar."""
    cfg = ctx.fuentes()
    if not cfg.puede_diagnosticar:
        print("no hay agente configurado o le falta prompt, modelo o credencial.")
        print(f"fuente declarada: {ctx.cfg.fuentes.get('agente')}")
        return 1

    print(f"agente: {cfg.agente.nombre}  modelo: {cfg.agente.modelo}  "
          f"vendor: {cfg.agente.vendor}")
    pend = diag_mod.pendientes(ctx.conn, limite=args.limite)
    if not pend:
        print("no hay incidentes abiertos sin diagnostico")
        return 0

    for inc in pend:
        print(f"\n {inc['incident_id']}  {inc['check_id']} en {inc['resource_id']}")
        n = diag_mod.revisar(ctx, cfg, ctx.cfg.llm)
        d = diag_mod.de(ctx.conn, inc["incident_id"])
        if not d:
            print("   el modelo no devolvio un diagnostico utilizable")
            continue
        print(f"   es_real={bool(d['es_real'])}  confianza={d['confianza']}")
        print(f"   causa:    {d['causa']}")
        print(f"   sugerido: {d['accion']}")
        if not d["es_real"]:
            print(f"   descarte: {d['descarte']}")
            print(f"   silencia el aviso: {diag_mod.descartado(ctx.conn, inc['incident_id'])}")
    c = informe_mod.costo_modelo(ctx.conn)
    print(f"\n costo acumulado 24 h: {c['costo_usd']:.4f} USD en {c['llamadas']} llamada(s)")
    return 0


def cmd_informe(ctx: Any, args: Any) -> int:
    if args.vista_previa:
        return _vista_previa(ctx, args.vista_previa)
    if ctx.falla_almacen is not None:
        res = informe_mod.degradado(ctx, ctx.falla_almacen, forzar=args.forzar)
    else:
        try:
            res = informe_mod.generar(ctx, forzar=args.forzar)
        except sqlite3.Error as e:
            # La base abrio y fallo a mitad de camino (se lleno, se bloqueo, una
            # pagina rota). El informe sale igual, degradado.
            falla = db.Falla(db.clasificar(e), str(e))
            log.error("el almacen fallo armando el informe -> %s", falla)
            res = informe_mod.degradado(ctx, falla, forzar=args.forzar)
    print(res)
    return 0 if res.get("estado") in ("encolado", "ya_enviado", "enviado") else 1


def _vista_previa(ctx: Any, archivo: str) -> int:
    """El informe entero, revalidacion incluida, sin mandar nada.

    Sirve para ver que diria el correo antes de que salga. Paga las llamadas al
    modelo y guarda los veredictos (son los mismos que usaria el informe de
    verdad), pero no encola, no manda y no avanza los cursores de los logs.
    """
    if ctx.falla_almacen is not None:
        return _sin_almacen(ctx)
    res = informe_mod.generar(ctx, vista_previa=archivo)
    cl = res.pop("clasificacion", None)
    print(f"asunto: {res['asunto']}")
    print(f"revalidado: {'si' if res['revalidado'] else 'NO (sin agente o apagada)'}")
    if cl is not None:
        for titulo, lista in (("lista principal", cl.cuentan),
                              ("arreglados, falta confirmar", cl.pendientes),
                              ("falsas alarmas", cl.falsas),
                              ("descartados al abrir", cl.descartados)):
            lista = [i for i in lista if i.get("state") != "RECOVERED"]
            if not lista:
                continue
            _linea()
            print(f" {titulo} ({len(lista)})")
            for i in lista:
                r = i.get("revalidacion") or {}
                sev = i["severity"] + (f" (medido {i['severidad_medida']})"
                                       if i.get("severidad_medida") else "")
                print(f"  [{sev}] {i['check_id']} en {i['resource_id']}")
                if r:
                    citas = sum(1 for c in r["evidencia"] if c.get("verificada"))
                    print(f"     {r['veredicto']} ({r['confianza']}), "
                          f"{citas} cita(s) verificada(s): {r['motivo'][:160]}")
                    if r.get("nota"):
                        print(f"     nota: {r['nota']}")
                if i.get("pendiente"):
                    print(f"     se confirma despues de {i['pendiente']['confirma_en'][:16]}")
                if i.get("nota"):
                    print(f"     {i['nota']}")
    _linea()
    print(f"html en {archivo}")
    return 0


def _sin_almacen(ctx: Any) -> int:
    """Lo que se puede decir en la terminal cuando la base no abre."""
    print(f"el almacen no esta disponible: {ctx.falla_almacen}")
    abiertos = registro.abiertos_segun_registro(
        datetime.now(timezone.utc) - timedelta(hours=informe_mod.DEGRADADO_ABIERTOS_H))
    print(f"segun el registro en archivo hay {len(abiertos)} incidente(s) abierto(s):")
    for i in abiertos:
        print(f"  [{i['severity']:<8}] {i['check_id']:<8} {i['resource_id']}  "
              f"desde {str(i['opened_at'])[:19].replace('T', ' ')}")
    return 3


def cmd_probar(ctx: Any, args: Any) -> int:
    """Un ciclo completo, sin mandar un solo correo.

    Es lo que hay que correr después de tocar un umbral o un YAML: muestra qué
    contestaría cada control ahora mismo, contra el servidor de verdad.
    """
    pares = ctx.planificar()
    if args.control:
        pares = [(cd, r) for cd, r in pares if cd.check_id == args.control]
        if not pares:
            print(f"no hay ningun control {args.control} planificado")
            return 1

    # Sin esto, `probar` podria contestar con datos cacheados de hasta quince
    # minutos atras y no mostrar el estado real, que es justo para lo que se usa.
    try:
        from sysop.adapters.apps.plataforma import datos as _d  # noqa: PLC0415
        _d.olvidar()
    except ImportError:
        pass

    print(f"corriendo {len(pares)} control(es) sin notificar...")
    _linea()

    # Se abren los mismos recursos compartidos que abriria un ciclo real. Sin
    # esto, cada control de base abriria su propia conexion y lo que se prueba
    # no seria lo que despues corre.
    deps = {"cfg": ctx.cfg, "conn": ctx.conn}
    cierres = []
    for proveer in ctx.proveedores:
        try:
            cerrar = proveer(pares, deps)
            if cerrar is not None:
                cierres.append(cerrar)
        except Exception as e:  # noqa: BLE001
            print(f" (un proveedor de recursos fallo: {e})")

    try:
        for cd, recurso in pares:
            try:
                r, _ = engine.ejecutar_control(ctx.conn, cd, recurso, deps)
            except Exception as e:  # noqa: BLE001
                print(f" {cd.check_id:<8} {recurso.resource_id:<34} EXPLOTO  {e}")
                continue
            detalle = r.reason_code or ""
            if r.actual:
                detalle = f"{detalle} {r.actual}"
            print(f" {cd.check_id:<8} {recurso.resource_id:<34} "
                  f"{r.state.value:<8} {detalle[:60]}")
    finally:
        for cerrar in reversed(cierres):
            try:
                cerrar()
            except Exception:  # noqa: BLE001
                pass

    _linea()
    print("nada de esto se guardo como incidente ni se mando por correo")
    return 0


def cmd_logs(ctx: Any, args: Any) -> int:
    """Lo que sacaría el extractor, sin mandar nada ni mover los cursores."""
    from sysop import logs as logs_mod                       # noqa: PLC0415

    declarados = ctx.cfg.umbral("logs.extractos", []) or []
    if not declarados:
        print("no hay logs declarados en `logs.extractos` del yaml")
        return 1

    # desde_cero: mirar a mano tiene que mostrar lo que hay, no "nada nuevo
    # desde el ultimo informe".
    barrido = logs_mod.barrer(ctx.conn, declarados, avanzar=False,
                              desde_cero=not args.nuevos)
    if not barrido:
        print("sin errores en los logs declarados")
        return 0

    for l in barrido:
        _linea("=")
        if l.get("error"):
            print(f" {l['ruta']}: {l['error']}")
            continue
        print(f" {l['ruta']}")
        print(f" {l['eventos_relevantes']} evento(s), "
              f"{l['grupos_distintos']} distinto(s)")
        _linea()
        for g in l["grupos"][:args.grupos]:
            print(f"  x{g['veces']:<4} {g['nivel']:9} {g.get('ultima_vez') or ''}")
            for linea in g["ejemplo"].splitlines()[:6]:
                print(f"       {linea[:150]}")
            print()
    return 0



class _SinBase:
    """Un adaptador que nunca contesta, para probar el respaldo de verdad.

    Es la unica forma honesta de verificar que el archivo alcanza: mirarlo no
    prueba nada, porque lo que importa es que el nucleo pueda armar la config
    entera con el y sin la base. Aca no se simula la lectura del archivo, se
    hace; lo unico simulado es la caida.
    """

    def leer_correo(self, spec: Any) -> None:
        return None

    def leer_destinatarios(self, spec: Any) -> None:
        return None

    def leer_agente(self, spec: Any) -> None:
        return None


def cmd_config(ctx: Any, args: Any) -> int:
    """Que configuracion esta usando, y si el respaldo aguanta sin la base."""
    if args.respaldo:
        print("SIMULANDO que la base no responde. Esto es lo que se usaria:")
        _linea()
        cfg = fuentes_mod.resolver(ctx.cfg.fuentes, _SinBase())
    else:
        cfg = ctx.fuentes()

    origen = {"vivo": "la base responde",
              "respaldo": "la base NO responde, se uso el archivo"}
    print(f"origen: {cfg.origen}  ({origen.get(cfg.origen, '')})")
    print()

    c = cfg.cuenta
    print(f"  correo         {c if c else 'FALTA'}")
    print(f"  destinatarios  {', '.join(cfg.destinatarios) or 'FALTA'}")
    a = cfg.agente
    print(f"  agente         {f'{a.nombre}  {a.modelo}  {a.vendor}  '
                              f'prompt {len(a.prompt)} chars' if a else 'FALTA'}")
    print()

    # Las dos preguntas que importan. Un observador que no puede notificar no
    # sirve para nada, y es exactamente lo que hay que poder verificar ANTES de
    # que la base se caiga.
    print(f"  puede notificar    : {'si' if cfg.puede_notificar else 'NO'}")
    print(f"  puede diagnosticar : {'si' if cfg.puede_diagnosticar else 'NO'}")

    if cfg.desactualizados:
        print()
        _linea()
        print("El archivo de respaldo no coincide con la base:")
        for d in cfg.desactualizados:
            print(f"  - {d}")
        print("Hoy manda la base, asi que no molesta. El dia que la base no")
        print("responda se va a usar el archivo tal como esta.")

    if not cfg.puede_notificar:
        print()
        print("SIN CUENTA O SIN DESTINATARIOS EL OBSERVADOR ES MUDO.")
        return 1
    return 0


def cmd_telegram(ctx: Any, args: Any) -> int:
    """Verifica el canal de Telegram sin levantar el servicio."""
    from sysop.bot import ajustes as ajustes_mod             # noqa: PLC0415

    aj = ajustes_mod.cargar()
    if aj.problema:
        print(f"NO se puede usar: {aj.problema}")
        return 1
    if not aj.habilitado:
        print("esta deshabilitado en telegram.yaml (`habilitado: false`).")
        return 0

    print(f"token          : {len(aj.token)} chars leidos de la variable")
    print(f"prompt del chat: {len(aj.prompt)} chars")
    print(f"modelo         : {aj.modelo or '(el mismo del agente)'}")
    print(f"presupuesto    : {aj.presupuesto_diario_usd:.2f} USD/dia, aparte "
          f"del de los diagnosticos")
    if aj.autorizados:
        print("autorizados    : " + ", ".join(
            f"{aj.nombres.get(i, i)} ({i})" for i in sorted(aj.autorizados)))
    else:
        print("autorizados    : NINGUNO. El bot no le va a contestar a nadie.")
        print("                 Escribile y mira el log para ver tu chat id.")

    # El token se verifica CONTRA LA API. Que tenga largo no dice nada: uno
    # revocado tiene el mismo largo que uno bueno.
    from sysop.bot.telegram import Telegram                  # noqa: PLC0415
    try:
        yo = Telegram(aj.token).quien_soy()
    except Exception as e:  # noqa: BLE001
        print(f"\nel token NO sirve: {e}")
        return 1
    print(f"\nel token responde: @{yo.get('username')} ({yo.get('first_name')})")

    if args.mandar:
        if not aj.autorizados:
            print("no hay a quien mandarle.")
            return 1
        api = Telegram(aj.token)
        for cid in sorted(aj.autorizados):
            try:
                api.mandar(cid, f"Prueba del observador de "
                                f"{settings.INSTALLATION}. Si lees esto, el "
                                f"canal funciona.")
                print(f"mensaje de prueba enviado a {cid}")
            except Exception as e:  # noqa: BLE001
                # El error mas comun no es el token sino que la persona nunca
                # le escribio al bot: Telegram no deja iniciar la conversacion
                # del lado del bot, y devuelve "chat not found".
                print(f"no se pudo mandar a {cid}: {e}")
                print("  si dice 'chat not found', esa persona todavia no le "
                      "escribio al bot. Tiene que mandarle /start primero.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sysop-agent", description=__doc__)
    p.add_argument("--host-id", help="sobrescribe SYSOP_HOST_ID")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("estado", help="que ve el observador ahora")

    pi = sub.add_parser("informe", help="arma y manda el informe periodico")
    pi.add_argument("--forzar", action="store_true",
                    help="manda aunque el periodo ya se haya enviado")
    pi.add_argument("--vista-previa", metavar="ARCHIVO",
                    help="lo arma (revalidando) y lo escribe en ARCHIVO, sin mandar")

    pp = sub.add_parser("probar", help="corre un ciclo sin notificar")
    pp.add_argument("--control", help="solo este control (ej: HST-09)")

    pd = sub.add_parser("diagnosticar", help="pide diagnostico al modelo")
    pd.add_argument("--limite", type=int, default=3,
                    help="cuantos incidentes diagnosticar (default 3)")

    pc = sub.add_parser("config", help="de donde sale la configuracion")
    pc.add_argument("--respaldo", action="store_true",
                    help="simula que la base no responde y usa solo el archivo")

    pt = sub.add_parser("telegram", help="verifica el canal de Telegram")
    pt.add_argument("--mandar", action="store_true",
                    help="manda un mensaje de prueba a los autorizados")

    pl = sub.add_parser("logs", help="errores de los logs declarados")
    pl.add_argument("--nuevos", action="store_true",
                    help="solo lo nuevo desde el ultimo informe")
    pl.add_argument("--grupos", type=int, default=4,
                    help="cuantos grupos mostrar por archivo")

    args = p.parse_args(argv)

    # A la terminal nada mas: el CLI lo corre una persona mirando, y escribir
    # al archivo del daemon mezclaria dos fuentes en el mismo lugar.
    log_setup.configurar("DEBUG" if args.verbose else "WARNING", archivo=None)
    logging.getLogger("mysql.connector").setLevel(logging.ERROR)

    if args.host_id:
        settings.HOST_ID = args.host_id
    registro.configurar(f"cli:{args.cmd}")

    try:
        # `crear=False`: solo el daemon crea la base. Con la base borrada, el
        # informe creaba una vacia y mandaba "OBSERVADOR CAIDO" con el daemon
        # vivo (medido el 2026-09-14).
        ctx = contexto.cargar(crear=False)
        # Un lock suele durar segundos (el daemon compactando, el bot). Antes de
        # mandar un informe degradado por eso, se reintenta un par de veces.
        for _ in range(2):
            if ctx.falla_almacen is None or ctx.falla_almacen.clase != db.BLOQUEADO:
                break
            time.sleep(5)
            ctx = contexto.cargar(crear=False)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"error de configuracion: {e}", file=sys.stderr)
        return 2

    if ctx.falla_almacen is not None and args.cmd in ("estado", "diagnosticar", "logs"):
        return _sin_almacen(ctx)

    return {"estado": cmd_estado, "informe": cmd_informe,
            "probar": cmd_probar,
            "diagnosticar": cmd_diagnosticar,
            "config": cmd_config,
            "telegram": cmd_telegram,
            "logs": cmd_logs}[args.cmd](ctx, args)


if __name__ == "__main__":
    sys.exit(main())
