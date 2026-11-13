"""De dónde saca el adaptador lo que sabe la plataforma.

Primero el back-end, por sus endpoints internos de observabilidad. Ahí vive la
derivación real: la misma función que usa el supervisor de líneas para decidir
qué workers levantar, la misma elegibilidad que usa el pipeline, el mismo
comando de cada trabajo ya interpretado.

Si el back no responde, se cae a SQL directo con una consulta congelada **y se
marca la evidencia como PARTIAL**. Las dos cosas importan:

- que haya caída, porque el observador tiene que seguir mirando justamente
  cuando el back está caído, que es cuando más hace falta;
- que se marque, porque una consulta congelada es una copia de la lógica de la
  plataforma, y una copia diverge. El informe tiene que poder decir "esto lo
  deduje yo, no me lo dijo la aplicación".

Las respuestas se cachean **por ciclo y entre ciclos**, cada una con su propia
vigencia. Por ciclo, porque cinco controles preguntan por los mismos workers y
no tiene sentido pedirlos cinco veces. Entre ciclos, porque los controles que
comparten una fuente no comparten cadencia: uno cada treinta segundos y otro
cada cinco minutos terminan pidiendo lo mismo por separado.

La vigencia de cada fuente es más corta que la cadencia del control más rápido
que la usa, así que ningún control decide sobre datos más viejos que su propio
intervalo.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from sysop import settings
from sysop.adapters.apps.plataforma import mysql as mysql_col
from sysop.core.models import EvidenceQuality

log = logging.getLogger("sysop.nac.datos")

TIMEOUT = 4.0

# Cuánto vale una respuesta antes de volver a pedirla, en segundos. Más corto
# que la cadencia del control más rápido que la consume.
VIGENCIA = {
    "tenants": 900.0,     # la lista de clientes no cambia en el dia
    "jobs": 90.0,         # JOB-05/06/07 corren cada 60 s
    "workers": 25.0,      # WRK-02 y WRK-04 corren cada 30 s
    "corridas": 25.0,     # idem
    "backlog": 240.0,     # QUE-09 y JOB-08 corren cada 300 s
}
VIGENCIA_POR_DEFECTO = 30.0

# Cache de proceso. El daemon es un solo proceso y un solo hilo de control, asi
# que no hace falta sincronizar. La linea de comandos lo usa dentro de una sola
# corrida y despues muere.
_MEMORIA: dict[str, tuple[float, "Respuesta"]] = {}


@dataclass(frozen=True)
class Respuesta:
    datos: Any
    calidad: EvidenceQuality
    origen: str                    # "back" | "sql" | "nada"
    motivo: str = ""

    @property
    def hay(self) -> bool:
        return self.calidad is not EvidenceQuality.NONE


def _vacia(motivo: str) -> Respuesta:
    return Respuesta(None, EvidenceQuality.NONE, "nada", motivo)


def _pedir(ruta: str, params: Optional[dict[str, Any]] = None) -> Optional[dict]:
    if not settings.INTERNAL_API_KEY:
        return None
    try:
        import httpx                                       # noqa: PLC0415
        url = f"{settings.BACKEND_URL.rstrip('/')}/api/internal/observability/{ruta}"
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(url, params=params or {},
                      headers={"X-Api-Key": settings.INTERNAL_API_KEY})
        if r.status_code != 200:
            log.info("el back respondio %s en %s", r.status_code, ruta)
            return None
        d = r.json()
        return d if d.get("disponible") else None
    except Exception as e:  # noqa: BLE001
        log.info("no se pudo consultar %s: %s", ruta, e)
        return None


def _cache(deps: dict[str, Any], clave: str, fn: Any) -> Respuesta:
    """Resuelve una vez por ciclo, y reusa entre ciclos mientras siga vigente.

    `deps` vive lo que dura el ciclo; `_MEMORIA` sobrevive entre ciclos con la
    vigencia de cada fuente. Una respuesta que no se pudo resolver NO se cachea:
    cachear un fallo esconde la recuperación hasta que vence el plazo.
    """
    caja = deps.setdefault("_nac_cache", {})
    if clave in caja:
        return caja[clave]

    base = clave.split(":")[0]
    vigencia = VIGENCIA.get(base, VIGENCIA_POR_DEFECTO)
    guardado = _MEMORIA.get(clave)
    if guardado is not None and (time.monotonic() - guardado[0]) < vigencia:
        caja[clave] = guardado[1]
        return guardado[1]

    r = fn()
    caja[clave] = r
    if r.hay:
        _MEMORIA[clave] = (time.monotonic(), r)
    else:
        _MEMORIA.pop(clave, None)
    return r


def olvidar() -> None:
    """Tira el caché entre ciclos. Para las pruebas y para el comando `probar`."""
    _MEMORIA.clear()


# ---------------------------------------------------------------------------
# Workers esperados
# ---------------------------------------------------------------------------

def workers_esperados(deps: dict[str, Any]) -> Respuesta:
    return _cache(deps, "workers", lambda: _workers(deps))


def _workers(deps: dict[str, Any]) -> Respuesta:
    d = _pedir("expected-workers")
    if d is not None:
        return Respuesta(d.get("workers") or [], EvidenceQuality.FULL, "back")

    conn = deps.get("mysql")
    if conn is None:
        return _vacia("ni el back ni la base responden")

    # Consulta CONGELADA, copia de `line_workers_repo.list_desired_line_workers`.
    # Es deliberadamente conservadora: si difiere de la original, que sea de
    # menos. Y por eso se marca PARTIAL.
    try:
        filas = mysql_col.consultar(conn, """
            SELECT cm.client_id, cm.id AS media_id, cm.extension, cm.type,
                   cm.direction, b.agent_id,
                   cm.updated_at AS media_updated_at,
                   b.updated_at  AS binding_updated_at
              FROM communication_media cm
              JOIN agent_inbound_bindings b
                ON b.media_id = cm.id AND b.is_active = 1
             WHERE cm.is_active = 1
             ORDER BY cm.id
        """, ())
    except Exception as e:  # noqa: BLE001
        return _vacia(f"fallo la consulta de respaldo: {e}")

    workers = [{"mode": _modo(f.get("direction")), "client_id": f["client_id"],
                "media_id": f["media_id"], "extension": f.get("extension"),
                "type": f.get("type"), "agent_id": f.get("agent_id"),
                "media_updated_at": str(f.get("media_updated_at") or ""),
                "binding_updated_at": str(f.get("binding_updated_at") or "")}
               for f in filas]
    return Respuesta(workers, EvidenceQuality.PARTIAL, "sql",
                     "derivado por SQL: el back no respondio")


def _modo(direction: Any) -> str:
    d = str(direction or "").lower()
    return {"inbound": "inbound", "outbound": "shift", "both": "both"}.get(d, d)


# ---------------------------------------------------------------------------
# Contratos de los trabajos
# ---------------------------------------------------------------------------

def contratos_jobs(deps: dict[str, Any]) -> Respuesta:
    return _cache(deps, "jobs", lambda: _jobs(deps))


def _jobs(deps: dict[str, Any]) -> Respuesta:
    d = _pedir("job-contracts")
    if d is not None:
        return Respuesta(d.get("jobs") or [], EvidenceQuality.FULL, "back")

    conn = deps.get("mysql")
    if conn is None:
        return _vacia("ni el back ni la base responden")
    try:
        filas = mysql_col.consultar(conn, """
            SELECT id, client_id, name, command, schedule_expression, timezone,
                   is_active, prevent_concurrency, last_run_at, last_status,
                   last_exit_code, updated_at
              FROM rc_cron_jobs ORDER BY id
        """, ())
    except Exception as e:  # noqa: BLE001
        return _vacia(f"fallo la consulta de respaldo: {e}")

    jobs = []
    for f in filas:
        # Sin el back no se interpreta el comando: se guarda crudo y los
        # controles que dependen de sus argumentos quedan en UNKNOWN, que es lo
        # correcto. Parsearlo acá seria la segunda copia del mismo parser.
        jobs.append({"job_id": int(f["id"]), "client_id": f["client_id"],
                     "name": f["name"], "schedule": f["schedule_expression"],
                     "timezone": f["timezone"] or "UTC",
                     "is_active": bool(f["is_active"]),
                     "prevent_concurrency": bool(f["prevent_concurrency"]),
                     "last_run_at": str(f["last_run_at"]) if f["last_run_at"] else None,
                     "last_status": f["last_status"],
                     "last_exit_code": f["last_exit_code"],
                     "updated_at": str(f["updated_at"]) if f["updated_at"] else None,
                     "script": None, "args": {}, "posicionales": [],
                     "log": None, "captura_salida": None, "parseable": False,
                     "comando_crudo": (f["command"] or "")[:400],
                     "cron_file": f"/etc/cron.d/plataforma_job_{f['id']}"})
    return Respuesta(jobs, EvidenceQuality.PARTIAL, "sql",
                     "leido por SQL: el back no respondio, el comando no esta interpretado")


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

def tenants(deps: dict[str, Any]) -> Respuesta:
    """Los `client_id` de la plataforma.

    Parece un dato trivial y no lo es: varios índices de la base empiezan por
    `client_id`, así que una consulta que filtra sólo por fecha no los usa y
    termina recorriendo la tabla entera. Teniendo la lista, la misma consulta
    se escribe con `client_id IN (...)` y pasa a usar el índice.
    """
    return _cache(deps, "tenants", lambda: _tenants(deps))


def _tenants(deps: dict[str, Any]) -> Respuesta:
    conn = deps.get("mysql")
    if conn is None:
        return _vacia("la base no responde")
    try:
        filas = mysql_col.consultar(
            conn, "SELECT client_id FROM clients WHERE is_active = 1", ())
    except Exception:  # noqa: BLE001
        try:
            filas = mysql_col.consultar(conn, "SELECT client_id FROM clients", ())
        except Exception as e:  # noqa: BLE001
            return _vacia(f"no se pudo leer la lista de tenants: {e}")
    return Respuesta([f["client_id"] for f in filas], EvidenceQuality.FULL, "sql")


# ---------------------------------------------------------------------------
# Corridas de los trabajos
# ---------------------------------------------------------------------------
# Tres controles (JOB-05, 06 y 07) necesitan el estado de las corridas de CADA
# trabajo. Consultar por trabajo eran tres consultas por cada uno: con los 17 de
# producción, cincuenta y una consultas por ronda. Medido en srv3 con seis
# trabajos, el ciclo ya estaba en 0,97% de un core, justo en el tope del
# criterio de aceptación.
#
# Acá se resuelve con dos consultas agregadas por ciclo, repartidas después. Es
# el mismo patrón que ya se aplicó a la conexión MySQL y a la lectura de
# systemd, y por la misma razón.

def corridas(deps: dict[str, Any]) -> Respuesta:
    """`{job_id: {ultima_iniciada, ultima_terminada, estado, exit_code, ...}}`."""
    return _cache(deps, "corridas", lambda: _corridas(deps))


def _corridas(deps: dict[str, Any]) -> Respuesta:
    conn = deps.get("mysql")
    if conn is None:
        return _vacia("la base no responde")
    try:
        # Ultima corrida iniciada por trabajo, y la ultima ya terminada con su
        # resultado. Se piden juntas porque son la misma tabla.
        ultimas = mysql_col.consultar(conn, """
            SELECT r.job_id, r.status, r.exit_code, r.started_at, r.finished_at,
                   LEFT(COALESCE(r.stderr, ''), 400) AS err
              FROM rc_cron_runs r
              JOIN (SELECT job_id, MAX(id) AS id FROM rc_cron_runs
                     WHERE status <> 'running' GROUP BY job_id) u
                ON u.id = r.id
        """, ())
        activas = mysql_col.consultar(conn, """
            SELECT job_id, id, started_at,
                   TIMESTAMPDIFF(SECOND, started_at, NOW()) AS corriendo_seg
              FROM rc_cron_runs WHERE status = 'running'
        """, ())
        inicios = mysql_col.consultar(conn, """
            SELECT job_id, MAX(started_at) AS ultimo FROM rc_cron_runs
             GROUP BY job_id
        """, ())
    except Exception as e:  # noqa: BLE001
        return _vacia(f"fallo la consulta de corridas: {e}")

    por_job: dict[int, dict[str, Any]] = {}
    for f in inicios:
        por_job.setdefault(int(f["job_id"]), {})["ultimo_inicio"] = f["ultimo"]
    for f in ultimas:
        por_job.setdefault(int(f["job_id"]), {}).update({
            "estado": f["status"], "exit_code": f.get("exit_code"),
            "finished_at": f.get("finished_at"), "stderr": f.get("err")})
    for f in activas:
        por_job.setdefault(int(f["job_id"]), {}).setdefault("en_curso", []).append(
            {"run_id": f["id"], "started_at": f["started_at"],
             "corriendo_seg": int(f.get("corriendo_seg") or 0)})
    return Respuesta(por_job, EvidenceQuality.FULL, "sql")


# ---------------------------------------------------------------------------
# Backlog del pipeline
# ---------------------------------------------------------------------------

def backlog(deps: dict[str, Any], duracion_minima: int = 10) -> Respuesta:
    return _cache(deps, f"backlog:{duracion_minima}",
                  lambda: _backlog(deps, duracion_minima))


def _backlog(deps: dict[str, Any], duracion_minima: int) -> Respuesta:
    d = _pedir("pipeline-backlog", {"duracion_minima": duracion_minima})
    if d is not None:
        return Respuesta({"cohorte": d.get("cohorte") or [],
                          "frescura": d.get("frescura") or [],
                          "ventana": d.get("ventana") or {}},
                         EvidenceQuality.FULL, "back")

    conn = deps.get("mysql")
    if conn is None:
        return _vacia("ni el back ni la base responden")
    try:
        cohorte = mysql_col.consultar(conn, """
            SELECT client_id, COUNT(DISTINCT call_id) AS llamadas,
                   COUNT(DISTINCT CASE WHEN recording_id IS NOT NULL
                          AND (recording_file IS NULL OR recording_file = '')
                         THEN recording_id END) AS sin_bajar,
                   COUNT(DISTINCT CASE WHEN recording_file IS NOT NULL
                          AND recording_file <> '' AND duration_seconds > %s
                          AND (transcripcion IS NULL OR transcripcion = '')
                         THEN recording_file END) AS sin_transcribir
              FROM rc_call_log
             WHERE start_time >= NOW() - INTERVAL 48 HOUR
               AND start_time <  NOW() - INTERVAL 24 HOUR
             GROUP BY client_id
        """, (duracion_minima,))
        frescura = mysql_col.consultar(conn, """
            SELECT client_id, MAX(start_time) AS ultima,
                   TIMESTAMPDIFF(MINUTE, MAX(start_time), NOW()) AS atraso_min
              FROM rc_call_log WHERE start_time >= NOW() - INTERVAL 2 DAY
             GROUP BY client_id
        """, ())
    except Exception as e:  # noqa: BLE001
        return _vacia(f"fallo la consulta de respaldo: {e}")

    return Respuesta(
        {"cohorte": [{k: (int(v) if v is not None and k != "client_id" else v)
                      for k, v in f.items()} for f in cohorte],
         "frescura": [{"client_id": f["client_id"],
                       "ultima": str(f["ultima"]) if f["ultima"] else None,
                       "atraso_min": int(f["atraso_min"] or 0)} for f in frescura],
         "ventana": {"duracion_minima": duracion_minima}},
        EvidenceQuality.PARTIAL, "sql", "calculado por SQL: el back no respondio")
