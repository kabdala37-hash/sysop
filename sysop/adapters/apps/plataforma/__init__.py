"""Adaptador de la aplicación Plataforma.

Todo lo que sabe de ESTA plataforma vive acá: sus trabajos programados, sus
workers de voz, sus colas, su base MySQL, y de dónde saca el observador el
agente, los destinatarios y la cuenta de correo.

El núcleo no importa nada de este paquete. Se lo pasan armado.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sysop.adapters.apps.plataforma import (checks_db, checks_jobs,
                                                 checks_ops, checks_queues,
                                                 checks_workers, cron, datos)
from sysop.adapters.apps.plataforma import mysql as mysql_col
from sysop.core.fuentes import Agente, CuentaSMTP
from sysop.core.models import Resource, ResourceState

log = logging.getLogger("sysop.nac")

NOMBRE = "plataforma"
VERSION = 1


# ---------------------------------------------------------------------------
# Observación
# ---------------------------------------------------------------------------

def planificar(cfg: Any, host_id: str, deps: Optional[dict[str, Any]] = None) -> list:
    """Los pares (control, recurso) de esta plataforma.

    `deps` hace falta para los controles por trabajo: la lista de trabajos sale
    del back o de la base, así que la planificación de esa familia depende de
    poder consultarla. Sin `deps` se planifica todo lo demás, que es lo correcto
    al arrancar antes de tener una conexión.
    """
    pares = list(checks_db.planificar(cfg, host_id))
    pares += checks_workers.planificar(cfg, host_id)
    pares += checks_queues.planificar(cfg, host_id)
    pares += checks_ops.planificar(cfg, host_id)
    if deps is not None:
        pares += checks_jobs.planificar(cfg, host_id, deps)
    else:
        # Sin deps no se puede saber qué trabajos hay, pero los tres controles
        # globales de la familia (inventario, bloqueos, planificador) no
        # dependen de esa lista y tienen que correr igual.
        host = Resource(resource_id=f"host:{host_id}", host_id=host_id,
                        kind="host", state=ResourceState.MANAGED)
        pares += [(cd, host) for cd in checks_jobs.GLOBALES.values()]
    return pares


def descubrir(cfg: Any, host_id: str, deps: Optional[dict[str, Any]] = None) -> list:
    if deps is None:
        return []
    return checks_jobs.descubrir(deps, host_id)


def tipos_de_recurso() -> tuple[str, ...]:
    return ("db", "job")


def proveer(controles: Optional[list],
            deps: dict[str, Any]) -> Optional[Callable[[], None]]:
    """Una sola conexión MySQL para todo el ciclo.

    Sin esto eran cuatro handshakes TLS por vuelta, medidos en srv3 como la mitad
    del presupuesto de CPU del observador.

    `controles=None` significa "todavía no se sabe cuáles": pasa al planificar,
    y ahí la conexión hace falta igual, porque la lista de trabajos sale de la
    base.

    QUIÉN LA NECESITA LO DICE CADA CONTROL, y no se deduce del tipo de recurso.
    Antes esto miraba `r.kind == "db"`, que dejaba afuera a los 14 controles que
    consultan la base sobre un recurso que NO es una base: las colas, los
    trabajos y los workers se miran con un `SELECT` pero su recurso es un host o
    un job. El efecto medido en srv3 sobre 330.848 mediciones: QUE-02, QUE-04 y
    QUE-10 quedaban en UNKNOWN el 66% de las veces y JOB-06 el 59%, porque su
    ciclo no traía ningún control de base que abriera la conexión. QUE-01, que
    corre cada 30 s igual que los DB-*, coincidía siempre con ellos y por eso
    nunca quedó ciego: ese contraste es lo que delató el mecanismo.

    Con la dependencia declarada, un control nuevo que consulte la base la pide
    y la tiene, sin que nadie se acuerde de tocar este gate.
    """
    if controles is not None and not any(
            "mysql" in (c.dependencies or ()) for c, _ in controles):
        return None
    # No se usa `with`: la conexión tiene que vivir todo el ciclo.
    cm = mysql_col.conexion(5.0)
    conexion = cm.__enter__()
    if conexion is not None:
        deps["mysql"] = conexion

    def cerrar() -> None:
        cm.__exit__(None, None, None)

    return cerrar


# ---------------------------------------------------------------------------
# Fuentes de datos
# ---------------------------------------------------------------------------
# Las tres se usan sólo cuando `app.yaml` declara `tipo: mysql`. Cada una
# devuelve None si no pudo leer, y el núcleo cae a su copia local.

def leer_correo(spec: dict[str, Any]) -> Optional[CuentaSMTP]:
    """La cuenta SMTP, de `client_config_vars` del tenant administrador."""
    tenant = str(spec.get("tenant", "acme"))
    with mysql_col.conexion(5.0) as conn:
        if conn is None:
            return None
        filas = mysql_col.consultar(
            conn,
            "SELECT config_key, config_value FROM client_config_vars "
            "WHERE client_id = %s AND config_key LIKE 'MAIL%%'",
            (tenant,),
        )
    kv = {f["config_key"]: _desenvolver(f["config_value"]) for f in filas}
    if not {"MAIL_HOST", "MAIL_USER", "MAIL_PASSWORD", "MAIL_FROM"} <= kv.keys():
        log.warning("el tenant %s no tiene la cuenta MAIL_* completa", tenant)
        return None
    return CuentaSMTP(
        host=kv["MAIL_HOST"],
        port=int(kv.get("MAIL_PORT", 587)),
        secure=str(kv.get("MAIL_SECURE", "false")).lower() in ("true", "1"),
        user=kv["MAIL_USER"],
        password=kv["MAIL_PASSWORD"],
        from_addr=kv["MAIL_FROM"],
    )


def leer_destinatarios(spec: dict[str, Any]) -> Optional[tuple[str, ...]]:
    """De `email_report_recipients`, la misma pantalla que los demás reportes.

    **No se filtra por tenant, a propósito.** El informe del observador es de
    infraestructura: quien lo asigna tiene que ser un platform admin, pero el
    destinatario puede estar en cualquier company. Los mails de las personas
    viven en el tenant de su company para recibir los demás reportes, y no tiene
    sentido duplicar a la misma persona en el tenant plataforma sólo para que le
    llegue el sysop. Todo el que tenga el scope lo recibe.

    El email sale con el mismo `COALESCE` que usa el back
    (`repositories/email_reports_repo.py`, `_list`): un destinatario que es
    usuario de la plataforma tiene `usu_id` cargado y la columna `email` en NULL,
    así que leer sólo `email` los saltea en silencio.

    Requiere el ALTER de `install/sql/002`: sin él, 'sysop' no existe en el ENUM
    del scope. Eso no es una falla del observador, es una instalación
    incompleta, y por eso se registra distinto.
    """
    scope = str(spec.get("scope", "sysop"))
    try:
        with mysql_col.conexion(5.0) as conn:
            if conn is None:
                return None
            filas = mysql_col.consultar(
                conn,
                "SELECT DISTINCT COALESCE(u.usuEmail, r.email) AS email "
                "FROM email_report_recipients r "
                "LEFT JOIN usuarios u ON u.id = r.usu_id "
                "WHERE r.scope = %s",
                (scope,),
            )
        return tuple(f["email"] for f in filas if f.get("email"))
    except Exception as e:  # noqa: BLE001
        log.warning("no se pudieron leer destinatarios "
                    "(revisar si falta el ALTER 002): %s", e)
        return None


def leer_agente(spec: dict[str, Any]) -> Optional[Agente]:
    """El agente que diagnostica, de la tabla `agents` con su credencial.

    Se lee igual que lo hace la propia plataforma (`chat_service.get_agent_config`):
    `agents` con LEFT JOIN a `llm_credentials`. Consumir, no reimplementar.
    """
    tenant = str(spec.get("tenant", "acme"))
    agent_id = str(spec.get("agent_id", "SYSOP"))
    with mysql_col.conexion(5.0) as conn:
        if conn is None:
            return None
        filas = mysql_col.consultar(
            conn,
            "SELECT a.agent_id, a.full_name, a.training_prompt, a.model_name, "
            "       a.adapter, c.api_key, c.base_url "
            "FROM agents a "
            "LEFT JOIN llm_credentials c "
            "  ON a.credentials_id COLLATE utf8mb4_unicode_ci "
            "   = c.credentials_id COLLATE utf8mb4_unicode_ci "
            "WHERE a.agent_id = %s AND a.client_id = %s LIMIT 1",
            (agent_id, tenant),
        )
    if not filas:
        log.warning("no existe el agente %s en el tenant %s", agent_id, tenant)
        return None
    f = filas[0]
    return Agente(
        prompt=f.get("training_prompt") or "",
        # El modelo se guarda sin el prefijo `models/`, igual que en el resto
        # de la plataforma.
        modelo=(f.get("model_name") or "").replace("models/", ""),
        api_key=f.get("api_key") or "",
        vendor=f.get("adapter") or "gemini",
        base_url=f.get("base_url") or "",
        nombre=f.get("agent_id") or agent_id,
    )


def _desenvolver(v: Any) -> str:
    """`client_config_vars.config_value` guarda los valores como JSON.

    O sea que el host llega literalmente como `"smtp.gmail.com"`, con comillas
    incluidas. Pasarlo tal cual a smtplib produce un fallo de DNS que no dice
    nada sobre la causa.
    """
    if not isinstance(v, str):
        return str(v)
    v = v.strip()
    if v.startswith('"') and v.endswith('"'):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v.strip('"')
    return v


# ---------------------------------------------------------------------------
# Lo que el agente puede preguntarle a esta plataforma
# ---------------------------------------------------------------------------
# Sin esto el agente solo leia archivos. Medido en srv2 el 2026-09-17: para
# decidir si la ingesta de clienteb estaba congelada no pudo ver que el sync
# habia dado `success` las 80 corridas del dia, busco en un log y diagnostico un
# 401 que no existia. La respuesta estaba en dos tablas.
#
# SQL LIBRE Y DE SOLO LECTURA. Lo que impide escribir no es este filtro sino el
# usuario de la base (`install/sql/001`: SELECT y PROCESS, nada mas); el filtro
# esta para contestar claro en vez de con un error de permisos, y para no
# mandar al servidor lo que no tiene sentido.

_SQL_LECTURA = re.compile(r"^\s*(select|with|show|describe|desc|explain)\b", re.I)
_SQL_PROHIBIDO = re.compile(
    r"\binto\s+(outfile|dumpfile)\b|\bfor\s+update\b|\block\s+in\s+share\b"
    r"|\bsleep\s*\(|\bbenchmark\s*\(", re.I)
CONSULTA_MAX_FILAS = 100
# El tope lo pone el servidor (`max_execution_time`), igual que en los
# controles: cortar del lado del cliente deja la consulta corriendo en MySQL.
CONSULTA_TIMEOUT_SEG = 10.0
CONSULTA_MAX_VALOR = 300
CONSULTA_MAX_BYTES = 8_000


def consultar(sql: str, limite: int = 50) -> dict[str, Any]:
    """Una consulta de lectura contra la base de la plataforma. Nunca lanza."""
    from sysop.llm.contexto import redactar                   # noqa: PLC0415

    texto = (sql or "").strip().rstrip(";").strip()
    if not texto:
        return {"error": "falta la consulta"}
    if ";" in texto:
        return {"error": "una sola sentencia por consulta, sin `;` en el medio"}
    if not _SQL_LECTURA.match(texto):
        return {"error": "solo lectura: SELECT, WITH, SHOW, DESCRIBE o EXPLAIN"}
    if _SQL_PROHIBIDO.search(texto):
        return {"error": "esa construccion no se permite en una consulta de lectura"}
    try:
        limite = max(1, min(int(limite or 50), CONSULTA_MAX_FILAS))
    except (TypeError, ValueError):
        limite = 50

    with mysql_col.conexion(5.0) as conn:
        if conn is None:
            return {"error": "la base de la plataforma no responde"}
        cur = None
        try:
            cur = conn.cursor()
            cur.execute("SET SESSION max_execution_time = %d"
                        % int(CONSULTA_TIMEOUT_SEG * 1000))
            cur.execute(texto)
            columnas = [d[0] for d in (cur.description or [])]
            filas = cur.fetchmany(limite + 1) if columnas else []
        except Exception as e:  # noqa: BLE001
            if getattr(e, "errno", None) == 3024:
                return {"error": "la consulta tardo mas de %d s y el servidor la "
                                 "corto. Acotala: client_id, rango de fechas, "
                                 "LIMIT" % CONSULTA_TIMEOUT_SEG}
            return {"error": ("MySQL: %s" % e)[:400]}
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:  # noqa: BLE001
                    pass

    recortado = len(filas) > limite
    filas = filas[:limite]
    # En TEXTO y no como lista de diccionarios: el redactor corta las listas a
    # 20 elementos, y una fila por linea es lo que el agente cita tal cual.
    lineas = "\n".join(" | ".join(f"{c}={_valor(v)}" for c, v in zip(columnas, f))
                       for f in filas)
    out: dict[str, Any] = {"columnas": columnas, "cuantas": len(filas),
                           "hay_mas": recortado}
    if len(lineas) > CONSULTA_MAX_BYTES:
        lineas = lineas[:CONSULTA_MAX_BYTES]
        out["truncado"] = "el resultado paso de %d bytes" % CONSULTA_MAX_BYTES
    out["filas"] = redactar(lineas, max_texto=CONSULTA_MAX_BYTES)
    return out


def _valor(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray)):
        return "<%d bytes>" % len(v)
    t = str(v).replace("\n", " ")
    return t if len(t) <= CONSULTA_MAX_VALOR else t[:CONSULTA_MAX_VALOR] + "..."


DECL_CONSULTAR = {
    "name": "consultar",
    "description": (
        "SQL de SOLO LECTURA contra la base MySQL 8 de la plataforma "
        "(plataforma). Una sola sentencia: SELECT, WITH, SHOW, DESCRIBE o "
        "EXPLAIN. Corta a los 10 s. Las fechas estan en UTC. Tablas utiles: "
        "rc_cron_jobs (trabajos: id, client_id, name, schedule_expression, "
        "is_active, last_run_at, last_status), rc_cron_runs (cada corrida: "
        "job_id, client_id, started_at, finished_at, status, exit_code, stderr), "
        "rc_call_log (llamadas: client_id, start_time, duration_seconds, "
        "direction; es ENORME, filtra siempre por client_id y un rango de "
        "start_time), rc_call_processing (cola: call_id, client_id, status, "
        "attempts, last_error, updated_at), communication_media (lineas), "
        "clients. Si no sabes las columnas: SHOW COLUMNS FROM <tabla>. Usa "
        "LIMIT y agrupa (COUNT, MAX) en vez de traer filas sueltas."),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {"type": "string", "description": "la consulta"},
            "limite": {"type": "integer",
                       "description": "filas a devolver, 1 a 100. Por defecto 50"},
        },
        "required": ["sql"],
    },
}


def herramientas() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Las herramientas que esta plataforma le suma al agente."""
    return [DECL_CONSULTAR], {"consultar": consultar}


# ---------------------------------------------------------------------------
# Cuando se puede confirmar un arreglo
# ---------------------------------------------------------------------------

# Cuanto despues de la proxima corrida se espera. La corrida tiene que terminar
# y el control tiene que verla: un repaso semanal tarda horas.
CONFIRMAR_MARGEN_MIN_SEG = 2 * 3600
CONFIRMAR_MARGEN_MAX_SEG = 12 * 3600


def confirmacion(resource_id: str,
                 ahora: Optional[datetime] = None) -> Optional[datetime]:
    """Desde cuando se puede dar por confirmado un arreglo sobre este recurso.

    Solo sabe de trabajos programados: la proxima corrida segun su cron, mas
    un margen. Para lo demas devuelve None y el que pregunta pone el plazo.
    """
    m = re.fullmatch(r"job:[^:]+:(\d+)", resource_id or "")
    if not m:
        return None
    ahora = ahora or datetime.now(timezone.utc)
    with mysql_col.conexion(5.0) as conn:
        r = datos.contratos_jobs({"mysql": conn} if conn is not None else {})
    for j in r.datos or []:
        if str(j.get("job_id")) != m.group(1):
            continue
        expr, tzn = j.get("schedule") or "", j.get("timezone") or "UTC"
        try:
            prox = cron.proxima_esperada(expr, ahora, tzn)
            periodo = cron.periodo_seg(expr, tzn) or 0
        except cron.CronInvalido:
            return None
        if prox is None:
            return None
        margen = min(max(periodo * 0.5, CONFIRMAR_MARGEN_MIN_SEG),
                     CONFIRMAR_MARGEN_MAX_SEG)
        return (prox + timedelta(seconds=margen)).astimezone(timezone.utc)
    return None
