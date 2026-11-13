"""Acceso al almacén local. SQLite con WAL.

WAL importa por una razón concreta: el loop de checks escribe todo el tiempo y
la API lee para el panel. Sin WAL, un `SELECT` del panel bloquea al recolector.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sysop import settings

_SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: Optional[Path] = None, crear: bool = True,
            espera: float = 10.0) -> sqlite3.Connection:
    """Abre el almacén. Con `crear=False` falla si el archivo no existe.

    SOLO EL DAEMON CREA LA BASE. El informe y el bot la abren con `crear=False`:
    medido el 2026-09-14, con la base borrada el informe creaba una vacía y
    mandaba "OBSERVADOR CAIDO" con cero incidentes mientras el daemon seguía
    vivo y con un crítico abierto. Que no exista es algo a informar, no a tapar.
    """
    path = Path(path) if path else settings.DB_PATH
    if crear:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=espera)
    else:
        # `as_uri()` escapa la ruta. Armada a mano, un `#` o un `?` en la ruta
        # cortaba la URI y sqlite abria (y CREABA) otro archivo, ignorando
        # justo el `mode=rw` que existe para no crear nada.
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True,
                               isolation_level=None, timeout=espera)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.Error:
        # Una base corrupta falla aca. Sin cerrar, el archivo queda abierto y
        # apartarlo despues deja un descriptor colgado sobre el inodo viejo.
        conn.close()
        raise

    # 0600, y no es de mas: este archivo guarda la copia local de la
    # configuracion, y ahi adentro esta la PASSWORD SMTP en claro — sin ella el
    # observador no podria avisar por correo justo cuando la base no responde,
    # que es cuando mas hace falta. SQLite lo crea con 0644 (legible por
    # cualquier usuario del sistema) y en srv3 quedo asi; lo unico que lo tapaba
    # era que el directorio fuera 0750. Un permiso de directorio es una cosa que
    # alguien afloja sin pensarlo; el del archivo tiene que valerse solo.
    #
    # Los sidecars del WAL van igual: el `-wal` tiene las paginas todavia no
    # consolidadas, o sea el mismo contenido.
    for f in (path, path.with_name(path.name + "-wal"),
              path.with_name(path.name + "-shm")):
        try:
            if f.exists():
                f.chmod(0o600)
        except OSError:  # noqa: PERF203
            # Sin permiso para cambiarlo (otro dueño) se sigue igual: no poder
            # endurecer no es razon para no arrancar.
            pass
    return conn


def corte(**delta: float) -> str:
    """El umbral de una ventana temporal, en el MISMO formato que la columna.

    USAR ESTO Y NUNCA `datetime('now', '-N hours')` DE SQLITE.

    Las fechas se guardan con `datetime.isoformat()`, o sea
    `2026-09-02T20:10:31.064003+00:00`, con T y con offset. La funcion
    `datetime()` de SQLite devuelve `2026-09-02 20:15:54`, con un espacio y sin
    offset. SQLite no tiene tipo fecha: compara los dos como TEXTO, y 'T'
    (0x54) es mayor que ' ' (0x20).

    El resultado es que toda fila del mismo dia pasa el filtro sin importar la
    hora. Medido en srv3 el 2026-09-02: una ventana de 2 minutos devolvia 85.655
    filas donde habia 185. No falla ruidosamente -- devuelve de mas y parece
    andar -- y por eso estuvo en once consultas sin que nadie lo notara.

    Excepcion: `datetime('now', 'start of day')` funciona igual, porque ahi el
    corte cae a medianoche y la comparacion se decide por la fecha. Se cambio
    igual, para no dejar dos criterios conviviendo.
    """
    return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Cuando el almacén falla
# ---------------------------------------------------------------------------
#
# Medido el 2026-09-14 rompiendo la base de verdad: con disco lleno el daemon
# seguia vivo y NO mandaba la alerta de un critico; con la base corrupta, sin
# permiso o con los sidecars de root morian daemon, informe y bot sin un correo.
# De aca sale lo que necesitan para no depender de que la base ande.

CORRUPTO = "CORRUPTO"
BLOQUEADO = "BLOQUEADO"
SIN_ESCRITURA = "SIN_ESCRITURA"
NO_ABRE = "NO_ABRE"
PERDIDO = "PERDIDO"

# Cuanto espera la sonda un lock. Con los 10 s de `connect`, tres consultas
# bloqueadas trababan el tick 30 s y el propio observador se veia interrumpido.
SONDA_ESPERA_MS = 1000


@dataclass(frozen=True)
class Falla:
    clase: str
    mensaje: str

    def __str__(self) -> str:
        return f"{self.clase}: {self.mensaje}"


@dataclass
class Almacen:
    conn: Optional[sqlite3.Connection]
    falla: Optional[Falla] = None
    inodo: Optional[int] = None
    # Si se aparto una base corrupta, a donde fue a parar.
    rotada: Optional[str] = None
    # Con una corrupta que no se pudo apartar: la falla de fondo, sin el motivo
    # de por que no se aparto (que cambia en cada intento).
    causa: Optional[Falla] = None


def clasificar(e: BaseException) -> str:
    """La clase de falla, con los mensajes reales que da sqlite.

    SOLO `CORRUPTO` autoriza a tocar el archivo. Un problema de permisos o de
    disco nunca justifica apartar una base sana: `disk I/O error` va a
    SIN_ESCRITURA a proposito, aunque a veces venga de un disco que se rompe.
    """
    if isinstance(e, sqlite3.ProgrammingError):
        # "Cannot operate on a closed database": la conexion ya no sirve y hay
        # que reabrir. Como SIN_ESCRITURA no se reabria nunca y el daemon
        # quedaba degradado hasta reiniciarlo (encontrado en la revision).
        return NO_ABRE
    if isinstance(e, OSError):
        return NO_ABRE
    m = str(e).lower()
    if "not a database" in m or "malformed" in m or "corrupt" in m:
        return CORRUPTO
    if "locked" in m or "busy" in m:
        return BLOQUEADO
    if "unable to open" in m:
        return NO_ABRE
    return SIN_ESCRITURA


def inodo(path: Optional[Path] = None) -> Optional[int]:
    try:
        return os.stat(path or settings.DB_PATH).st_ino
    except OSError:
        return None


def firma(path: Optional[Path] = None) -> Optional[tuple[int, int]]:
    """Tamaño y mtime: para no volver a verificar una base que no cambió."""
    try:
        st = os.stat(path or settings.DB_PATH)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def abrir(path: Optional[Path] = None, crear: bool = True,
          verificar: bool = False, rotar: bool = False,
          espera: float = 10.0) -> Almacen:
    """Abre el almacén sin lanzar nunca. Si falla, dice por qué.

    `verificar` corre `PRAGMA quick_check` (0,88 s sobre 544 MiB): una base con
    una pagina rota sigue contestando unas consultas y fallando otras, asi que
    abrir bien no prueba que este sana. `rotar` aparta una base corrupta y
    arranca con una nueva; lo usa solo el daemon.
    """
    path = Path(path) if path else settings.DB_PATH
    try:
        if not crear and not path.exists():
            return Almacen(None, Falla(NO_ABRE, f"la base {path} no existe"))
        alm = _abrir_una_vez(path, crear, verificar, espera)
        if alm.falla is None or alm.falla.clase != CORRUPTO or not rotar:
            return alm
        return apartar_y_abrir(path, alm.falla, crear, verificar, espera)
    except OSError as e:
        # "Sin lanzar nunca" incluye al filesystem. Un directorio sin permiso
        # hacia que el `rename` de la rotacion o el `mkdir` salieran como
        # PermissionError y el daemon moria al arrancar sin mandar un correo
        # (encontrado en la revision, verificado).
        return Almacen(None, Falla(NO_ABRE, f"{type(e).__name__}: {e}"))


def apartar_y_abrir(path: Path, falla: Falla, crear: bool = True,
                    verificar: bool = True, espera: float = 10.0) -> Almacen:
    """Aparta la base corrupta y abre una nueva. Si no se puede, lo dice."""
    try:
        destino, motivo = rotar_corrupta(path)
    except OSError as e:
        return Almacen(None, Falla(CORRUPTO, f"{falla.mensaje} (no se pudo "
                                             f"apartar: {type(e).__name__}: {e})"),
                       causa=falla)
    if destino is None:
        return Almacen(None, Falla(CORRUPTO, f"{falla.mensaje} ({motivo})"), causa=falla)
    nueva = _abrir_una_vez(path, crear, verificar, espera)
    nueva.rotada = destino
    return nueva


def _abrir_una_vez(path: Path, crear: bool, verificar: bool,
                   espera: float = 10.0) -> Almacen:
    conn = None
    try:
        conn = connect(path, crear=crear, espera=espera)
        init_schema(conn)
        if verificar:
            r = conn.execute("PRAGMA quick_check").fetchone()[0]
            if r != "ok":
                conn.close()
                return Almacen(None, Falla(CORRUPTO, f"quick_check: {str(r)[:200]}"))
        # Una apertura de reintento espera poco un lock; una vez abierta, la
        # conexion vuelve a los 10 s de siempre.
        conn.execute("PRAGMA busy_timeout = 10000")
        return Almacen(conn, None, inodo(path))
    except (sqlite3.Error, OSError) as e:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        return Almacen(None, Falla(clasificar(e), str(e)))


def rotar_corrupta(path: Path) -> tuple[Optional[str], str]:
    """Aparta una base corrupta a `sysop.db.roto-<fecha>`. Una sola copia.

    Solo si queda libre el doble de lo que ocupa: la base nueva va a crecer en
    el mismo disco, y en srv2 ese disco es el de todo el servidor (74% el
    2026-09-14). Sin espacio no se toca nada y se avisa: borrar la base rota
    para hacer lugar seria perder lo unico que queda para mirar que paso.
    """
    partes = [path, path.with_name(path.name + "-wal"),
              path.with_name(path.name + "-shm")]
    ocupa = sum(p.stat().st_size for p in partes if p.exists())
    viejas = sorted(path.parent.glob(path.name + ".roto-*"))
    libre = shutil.disk_usage(path.parent).free + sum(
        v.stat().st_size for v in viejas if v.exists())
    if libre < 2 * ocupa:
        return None, (f"no se aparto: ocupa {ocupa // 1048576} MiB y hay "
                      f"{libre // 1048576} MiB libres, hace falta el doble")
    for v in viejas:
        v.unlink(missing_ok=True)
    marca = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    destino = path.with_name(f"{path.name}.roto-{marca}")
    # Primero los sidecars y al final la base: si un rename falla a mitad, lo
    # que queda con el nombre original es la base corrupta entera (se vuelve a
    # detectar), y no una base nueva al lado de un `-wal` que no es suyo.
    for p in reversed(partes):
        if p.exists():
            p.rename(destino.with_name(destino.name + p.name[len(path.name):]))
    return str(destino), "apartada"


def sondear(conn: Optional[sqlite3.Connection], path: Optional[Path] = None,
            inodo_abierto: Optional[int] = None) -> Optional[Falla]:
    """¿Se puede escribir en el almacén ahora? None si sí.

    Pregunta explicitamente en vez de inferirlo de que las consultas no fallen.
    Y mira el INODO: con la base borrada, sqlite sigue escribiendo en el archivo
    que ya no existe sin dar un solo error (medido), y todo lo escrito se pierde
    al reiniciar.
    """
    path = Path(path) if path else settings.DB_PATH
    if conn is None:
        return Falla(NO_ABRE, "el almacen no esta abierto")
    actual = inodo(path)
    if actual is None:
        return Falla(PERDIDO, f"{path} ya no existe")
    if inodo_abierto is not None and actual != inodo_abierto:
        return Falla(PERDIDO, f"{path} es otro archivo que el que se abrio")
    try:
        conn.execute(f"PRAGMA busy_timeout = {SONDA_ESPERA_MS}")
        try:
            conn.execute(
                "INSERT INTO cursor (key, value, updated_at) VALUES "
                "('almacen:sonda', ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (datetime.now(timezone.utc).isoformat(),
                 datetime.now(timezone.utc).isoformat()))
        finally:
            conn.execute("PRAGMA busy_timeout = 10000")
    except sqlite3.Error as e:
        return Falla(clasificar(e), str(e))
    return None


def query(conn: sqlite3.Connection, sql: str,
          params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()
