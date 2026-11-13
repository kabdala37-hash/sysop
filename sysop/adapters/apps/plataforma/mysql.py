"""Acceso de solo lectura a la base de negocio.

Dos cosas que este módulo impone y no son negociables:

* **Timeout en todo.** Una consulta que se cuelga no puede colgar el ciclo del
  observador. Un timeout produce `UNKNOWN`, nunca un `OK` ni una excepción que
  se propague.
* **TLS.** Va contra la costumbre del repo de la plataforma, que usa
  `--skip-ssl`. Con `caching_sha2_password` una conexión TCP sin canal seguro
  falla apenas rota la credencial (error 2061), o sea que funciona hasta que
  alguien cambia la password. Ver install/RUNBOOK.md.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional, Sequence

import mysql.connector

from sysop import settings

log = logging.getLogger("sysop.nac.mysql")


class ConsultaTimeout(Exception):
    """La consulta excedió su presupuesto. Se traduce a UNKNOWN, no a falla."""


@contextmanager
def conexion(timeout_sec: float = 5.0) -> Iterator[Optional[Any]]:
    """Conexión efímera. Devuelve None si no se pudo conectar.

    Devuelve None en vez de lanzar porque "no puedo conectarme a la base" ES un
    resultado observable (el control DB-01), no un error del observador.
    """
    conn = None
    try:
        conn = mysql.connector.connect(
            host=settings.DB_HOST,
            port=settings.DB_PORT,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            database=settings.DB_NAME,
            connection_timeout=int(timeout_sec),
            ssl_disabled=False,
            autocommit=True,
        )
        yield conn
    except mysql.connector.Error as e:
        log.warning("no se pudo conectar a la base de negocio: %s", e)
        yield None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def consultar(conn: Any, sql: str, params: Sequence[Any] = (),
              timeout_sec: float = 5.0,
              max_rows: int = 10_000) -> list[dict[str, Any]]:
    """SELECT acotado por tiempo y por filas.

    El límite de tiempo lo pone el servidor con MAX_EXECUTION_TIME, no el
    cliente: cortar del lado del cliente deja la consulta corriendo en MySQL, y
    un control que se dispara cada 30 segundos terminaría acumulando consultas
    pesadas en el servidor que dice cuidar.
    """
    cur = None
    try:
        cur = conn.cursor(dictionary=True)
        ms = max(1, int(timeout_sec * 1000))
        sql_acotado = sql.strip()
        if sql_acotado.upper().startswith("SELECT"):
            sql_acotado = f"SELECT /*+ MAX_EXECUTION_TIME({ms}) */" + sql_acotado[6:]
        cur.execute(sql_acotado, tuple(params))
        filas = cur.fetchmany(max_rows)
        return [dict(f) for f in filas]
    except mysql.connector.Error as e:
        # 3024 = statement execution time exceeded
        if getattr(e, "errno", None) == 3024:
            raise ConsultaTimeout(str(e)) from e
        raise
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
