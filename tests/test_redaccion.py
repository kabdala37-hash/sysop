"""Que no se le manden secretos al modelo.

Son tres capas y las tres tenian un agujero:

  1. el redactor solo miraba las CLAVES de un diccionario, asi que una
     asignacion adentro de un texto —que es la forma en que llega el contenido
     de un archivo— pasaba intacta;
  2. la lista de archivos vedados filtraba por nombre y dejaba pasar el almacen
     del propio observador, `/proc/<pid>/environ` y `settings.py`;
  3. el almacen, que guarda la password SMTP en claro, se creaba 0644.

Cada caso de aca corresponde a uno de esos agujeros, verificado contra el
codigo real antes de arreglarlo.
"""
import os
import shutil
import tempfile
from pathlib import Path

from sysop.llm.contexto import redactar
from sysop.llm.herramientas import _es_secreto
from sysop.store import db


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


def main():
    r = []

    # --- 1. el secreto adentro de un texto ---
    SECRETOS_EN_TEXTO = [
        ('JWT_SECRET = "s3cr3t-de-produccion-abc123"', "s3cr3t-de-produccion-abc123"),
        ("DB_PASSWORD=Sup3rP4ss!", "Sup3rP4ss!"),
        ("INTERNAL_API_KEY=dc08a46d1122", "dc08a46d1122"),
        ("export GEMINI_API_KEY=AIzaSyXXXXXXXXXXXX", "AIzaSyXXXXXXXXXXXX"),
        ("password: hunter2", "hunter2"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("connection_string=mysql://u:clave@host/db", "clave"),
        ("TWILIO_AUTH_TOKEN='a1b2c3d4'", "a1b2c3d4"),
        ("private_key=-----BEGIN", "BEGIN"),
        ("SYSOP_DB_PASSWORD=xyz123", "xyz123"),
        ("cookie=session=abcdef", "abcdef"),
    ]
    for texto, filtrado in SECRETOS_EN_TEXTO:
        out = redactar(texto)
        r.append(ok("no filtra el valor de %r" % texto[:34],
                    filtrado not in out, "salio: %r" % out))

    r.append(ok("pero deja ver QUE clave era (sirve para diagnosticar)",
                "JWT_SECRET" in redactar('JWT_SECRET = "x"'),
                redactar('JWT_SECRET = "x"')))

    # --- lo que NO tiene que romper ---
    INTACTOS = [
        "nada raro aca, solo un log normal",
        "2026-08-31T12:00:00-03:00 INFO arranco el servicio",
        "el token vencio y hay que renovarlo",
        "Traceback (most recent call last):",
        "connection refused to host db-1",
        "GET /api/health 200 en 12ms",
    ]
    for texto in INTACTOS:
        r.append(ok("no toca un texto sin secretos: %r" % texto[:34],
                    redactar(texto) == texto, redactar(texto)))

    # --- la capa vieja sigue funcionando: claves de diccionario ---
    d = redactar({"password": "x", "api_key": "y", "host": "db-1"})
    r.append(ok("una clave prohibida en un dict se sigue redactando",
                d["password"] == "[redactado]" and d["api_key"] == "[redactado]", d))
    r.append(ok("y lo que no es secreto queda", d["host"] == "db-1", d))

    # --- 2. los archivos ---
    BLOQUEADOS = [
        "/var/lib/sysop-agent/sysop.db",
        "/var/lib/sysop-agent/sysop.db-wal",
        "/var/lib/sysop-agent/sysop.db-shm",
        "/proc/self/environ",
        "/proc/1234/environ",
        "/opt/plataforma/build/settings.py",
        "/opt/plataforma/acmeBot/config.py",
        "/etc/sysop-agent/sysop-agent.env",
        "/etc/shadow",
        "/root/.ssh/id_rsa",
        "/home/x/.my.cnf",
    ]
    for ruta in BLOQUEADOS:
        r.append(ok("no se lee %s" % ruta, _es_secreto(Path(ruta))))

    PERMITIDOS = [
        "/var/log/plataforma/webapp.log",
        "/var/log/syslog",
        "/etc/cron.d/sysop-agent",
        "/opt/plataforma/build/main.py",
        "/proc/meminfo",
        "/proc/1234/status",
    ]
    for ruta in PERMITIDOS:
        r.append(ok("SI se lee %s (si no, no se puede diagnosticar)" % ruta,
                    not _es_secreto(Path(ruta))))

    # --- 3. los permisos del almacen ---
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "t.db"
    conn = db.connect(p)
    db.init_schema(conn)
    conn.execute("CREATE TABLE x (a)")
    conn.commit()
    for f in (p, p.with_name(p.name + "-wal")):
        if f.exists():
            r.append(ok("%s queda en 0600, no 0644" % f.name,
                        oct(os.stat(f).st_mode)[-3:] == "600",
                        oct(os.stat(f).st_mode)[-3:]))
    # y al reabrir uno que ya existe, tambien
    os.chmod(p, 0o644)
    db.connect(p)
    r.append(ok("y si ya existia con 0644, al abrirlo se corrige",
                oct(os.stat(p).st_mode)[-3:] == "600", oct(os.stat(p).st_mode)[-3:]))

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
