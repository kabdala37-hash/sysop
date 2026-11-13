"""De donde sale la configuracion, y que pasa cuando la base no esta.

Es el test del unico camino que importa cuando todo lo demas se rompio: si la
base de negocio se cae, el observador tiene que poder mandar el correo IGUAL y
decir que lo mando degradado. Hasta el 2026-09-02 eso lo cubria una cache en
SQLite que se llenaba sola; ahora lo cubre un archivo declarado.

Lo que se verifica, en orden de importancia:

  1. Con la base viva manda la base.
  2. Con la base caida manda el archivo, y `puede_notificar` sigue en True.
  3. Sin base y sin archivo NO inventa nada, y lo dice.
  4. La red de seguridad: con la base viva, avisa si el archivo quedo viejo.

El 3 es el que justifica el cambio. Con la cache, ese caso daba False y nadie se
enteraba hasta el dia que hacia falta.
"""
import os
import shutil
import tempfile
from pathlib import Path

from sysop.core import fuentes as F


def ok(n, c, e=""):
    print("  %s %s%s" % ("OK  " if c else "MAL ", n, "" if c else " :: %s" % (e,)))
    return c


BASE_YAML = """
agente:
  nombre: SYSOP
  prompt: prompts/p.md
  modelo: gemini-3-flash-preview
  vendor: gemini_text
  api_key_env: TEST_LLM_KEY
correo:
  host: smtp.gmail.com
  port: 587
  secure: false
  user: ops@example.com
  password_env: TEST_SMTP_PASS
  from: ops@example.com
destinatarios:
  - alguien@example.com
"""

SPEC = {
    "correo": {"tipo": "mysql", "tenant": "acme", "respaldo": "base.yaml"},
    "destinatarios": {"tipo": "mysql", "tenant": "acme", "scope": "sysop",
                      "respaldo": "base.yaml"},
    "agente": {"tipo": "mysql", "tenant": "acme", "agent_id": "SYSOP",
               "respaldo": "base.yaml"},
}


class Base:
    """Un adaptador que contesta lo que se le diga. None = la base no responde."""

    def __init__(self, correo=None, dest=None, agente=None):
        self._c, self._d, self._a = correo, dest, agente

    def leer_correo(self, spec):
        return self._c

    def leer_destinatarios(self, spec):
        return self._d

    def leer_agente(self, spec):
        return self._a


def dir_config(base_yaml=BASE_YAML, prompt="Sos el observador."):
    d = Path(tempfile.mkdtemp())
    (d / "base.yaml").write_text(base_yaml, encoding="utf-8")
    (d / "prompts").mkdir()
    (d / "prompts" / "p.md").write_text(prompt, encoding="utf-8")
    return d


def main():
    r = []
    tmp = []
    os.environ["TEST_LLM_KEY"] = "clave-del-modelo"
    os.environ["TEST_SMTP_PASS"] = "clave-smtp"

    vivo_cuenta = F.CuentaSMTP(host="smtp.gmail.com", port=587, secure=False,
                               user="ops@example.com", password="clave-smtp",
                               from_addr="ops@example.com")
    vivo_agente = F.Agente(prompt="Sos el observador.",
                           modelo="gemini-3-flash-preview",
                           api_key="clave-del-modelo", vendor="gemini_text",
                           nombre="SYSOP")

    # -- 1. la base responde ------------------------------------------------
    d = dir_config(); tmp.append(d)
    c = F.resolver(SPEC, Base(vivo_cuenta, ("alguien@example.com",), vivo_agente), d)
    r.append(ok("con la base viva, el origen es 'vivo'", c.origen == "vivo", c.origen))
    r.append(ok("con la base viva, puede notificar", c.puede_notificar))
    r.append(ok("con la base viva, puede diagnosticar", c.puede_diagnosticar))
    r.append(ok("archivo y base iguales: no avisa nada",
                c.desactualizados == (), c.desactualizados))

    # -- 2. la base NO responde, hay archivo --------------------------------
    c = F.resolver(SPEC, Base(None, None, None), d)
    r.append(ok("sin base, el origen es 'respaldo'", c.origen == "respaldo", c.origen))
    r.append(ok("sin base, PUEDE NOTIFICAR IGUAL", c.puede_notificar))
    r.append(ok("sin base, PUEDE DIAGNOSTICAR IGUAL", c.puede_diagnosticar))
    r.append(ok("sin base, la cuenta sale del archivo",
                c.cuenta and c.cuenta.user == "ops@example.com"))
    r.append(ok("sin base, el destinatario sale del archivo",
                c.destinatarios == ("alguien@example.com",), c.destinatarios))
    r.append(ok("la password sale de la variable de entorno, no del yaml",
                c.cuenta and c.cuenta.password == "clave-smtp"))
    r.append(ok("la api key sale de la variable de entorno, no del yaml",
                c.agente and c.agente.api_key == "clave-del-modelo"))
    r.append(ok("el prompt se lee del archivo que apunta la ruta relativa",
                c.agente and c.agente.prompt == "Sos el observador.",
                c.agente.prompt if c.agente else None))

    # -- 3. ni base ni archivo: no inventa, y lo dice ------------------------
    # El caso que la cache dejaba pasar: instalacion nueva, base caida, mudo.
    sin_respaldo = {k: {x: y for x, y in v.items() if x != "respaldo"}
                    for k, v in SPEC.items()}
    c = F.resolver(sin_respaldo, Base(None, None, None), d)
    r.append(ok("sin base y sin archivo, NO puede notificar",
                not c.puede_notificar))
    r.append(ok("sin base y sin archivo, no inventa destinatarios",
                c.destinatarios == (), c.destinatarios))

    # Y un respaldo declarado que no existe tampoco puede romper el arranque.
    roto = {k: dict(v, respaldo="no-existe.yaml") for k, v in SPEC.items()}
    c = F.resolver(roto, Base(None, None, None), d)
    r.append(ok("un respaldo declarado que falta no rompe, solo deja mudo",
                not c.puede_notificar))

    # -- 4. la red de seguridad ---------------------------------------------
    # La base responde, pero el archivo que la reemplazaria quedo viejo.
    otra_cuenta = F.CuentaSMTP(host="smtp.office365.com", port=587, secure=False,
                               user="otro@example.com", password="clave-smtp")
    c = F.resolver(SPEC, Base(otra_cuenta, ("nuevo@example.com",), vivo_agente), d)
    txt = " | ".join(c.desactualizados)
    r.append(ok("avisa que el correo del archivo quedo viejo",
                "correo" in txt and "host" in txt and "user" in txt, txt))
    r.append(ok("avisa el destinatario que le falta al archivo",
                "nuevo@example.com" in txt, txt))
    r.append(ok("avisa el destinatario que el archivo tiene de mas",
                "alguien@example.com" in txt, txt))
    r.append(ok("con la base viva, sigue mandando la base y no el archivo",
                c.cuenta.user == "otro@example.com", c.cuenta.user))
    r.append(ok("el aviso NUNCA muestra el valor de una credencial",
                "clave-smtp" not in txt and "clave-del-modelo" not in txt, txt))

    # El nombre no cuenta: el agente vivo trae su agent_id y el archivo su rotulo.
    c = F.resolver(SPEC, Base(vivo_cuenta, ("alguien@example.com",),
                              F.Agente(prompt="Sos el observador.",
                                       modelo="gemini-3-flash-preview",
                                       api_key="clave-del-modelo",
                                       vendor="gemini_text", nombre="OTRO")), d)
    r.append(ok("un nombre distinto no se reporta como desactualizado",
                c.desactualizados == (), c.desactualizados))

    # Y un CRLF tampoco: el prompt viaja entre MySQL, Windows y el repo.
    d2 = dir_config(prompt="Sos el observador."); tmp.append(d2)
    c = F.resolver(SPEC, Base(vivo_cuenta, ("alguien@example.com",),
                              F.Agente(prompt="Sos el observador.\r\n",
                                       modelo="gemini-3-flash-preview",
                                       api_key="clave-del-modelo",
                                       vendor="gemini_text", nombre="SYSOP")), d2)
    r.append(ok("un CRLF de diferencia en el prompt no dispara el aviso",
                c.desactualizados == (), c.desactualizados))

    # -- 5. una plataforma SIN base ninguna ---------------------------------
    # `tipo: archivo` en las tres: ni adaptador ni MySQL.
    solo = {
        "correo": {"tipo": "archivo", "host": "smtp.gmail.com", "port": 587,
                   "user": "ops@example.com", "password_env": "TEST_SMTP_PASS"},
        "destinatarios": {"tipo": "archivo", "lista": ["alguien@example.com"]},
        "agente": {"tipo": "archivo", "prompt": str(d / "prompts" / "p.md"),
                   "modelo": "gemini-3-flash-preview",
                   "api_key_env": "TEST_LLM_KEY"},
    }
    c = F.resolver(solo, None, d)
    r.append(ok("sin adaptador y sin base, el modo archivo alcanza solo",
                c.puede_notificar and c.puede_diagnosticar))
    r.append(ok("en modo archivo el origen es 'vivo', no 'respaldo'",
                c.origen == "vivo", c.origen))

    for x in tmp:
        shutil.rmtree(x, ignore_errors=True)
    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
