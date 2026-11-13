#!/bin/bash
# Dice el chat id de quien le escribio al bot de Telegram.
#
# Existe porque el bot **no le contesta nada** a un desconocido: responderle "no
# estas autorizado" le confirma a cualquiera que el bot existe y que hay algo del
# otro lado. Asi que el id de una persona nueva hay que sacarlo por afuera, y
# esto es ese afuera.
#
#   /opt/sysop-agent/bin/chat_id.sh           auto: elige solo el metodo que sirve
#   /opt/sysop-agent/bin/chat_id.sh --api     fuerza preguntarle a la API
#   /opt/sysop-agent/bin/chat_id.sh --log     fuerza leer el journal del bot
#   /opt/sysop-agent/bin/chat_id.sh --log 200 idem, mirando 200 lineas
#
# LOS DOS METODOS, Y POR QUE HAY DOS:
#
#   API   `getUpdates` devuelve los mensajes que el bot todavia no consumio. Es
#         el unico que sirve ANTES de instalar el servicio. Pero si el bot esta
#         corriendo, el daemon los consume el con long polling y Telegram le
#         contesta 409 a cualquier otro que pregunte.
#
#   LOG   el daemon escribe el chat id de cada mensaje no autorizado justamente
#         para esto. Sirve con el bot andando, que es el caso normal.
#
# Por eso sin argumentos elige solo: si el servicio esta activo mira el journal,
# y si no le pregunta a la API.
#
# NUNCA imprime el token. Si no esta en el entorno lo pide por stdin sin eco.
set -euo pipefail

ENV_FILE=${SYSOP_ENV_FILE:-/etc/sysop-agent/sysop-agent.env}
OPT_DIR=${SYSOP_OPT_DIR:-/opt/sysop-agent}
UNIDAD=sysop-agent-bot
MODO=auto
LINEAS=120

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api) MODO=api; shift ;;
    --log) MODO=log; shift
           if [[ ${1:-} =~ ^[0-9]+$ ]]; then LINEAS=$1; shift; fi ;;
    -h|--help) sed -n '2,28p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "opcion desconocida: $1 (probá --help)" >&2; exit 2 ;;
  esac
done

# El entorno no lo carga nadie por nosotros: la unidad systemd usa
# EnvironmentFile, pero un script suelto no hereda nada.
if [[ -r "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi

PY="${OPT_DIR}/venv/bin/python"
[[ -x "$PY" ]] || PY=$(command -v python3)

CONFIG_DIR=${SYSOP_CONFIG_DIR:-${OPT_DIR}/config}

BOT_VIVO=no
systemctl is-active --quiet "$UNIDAD" 2>/dev/null && BOT_VIVO=si

if [[ "$MODO" == "auto" ]]; then
  if [[ "$BOT_VIVO" == "si" ]]; then
    MODO=log
    echo "(${UNIDAD} esta corriendo: se lee el journal, que es lo que sirve" >&2
    echo " cuando el daemon ya consumio los mensajes)" >&2
  else
    MODO=api
  fi
elif [[ "$MODO" == "api" && "$BOT_VIVO" == "si" ]]; then
  # Con `timeout=0` la API no contesta 409 --eso pasa entre dos long polling--
  # pero devuelve vacio, porque el daemon ya se llevo los mensajes con su
  # offset. Sin este aviso, el vacio parece que el script esta roto.
  echo "AVISO: ${UNIDAD} esta corriendo y consume los mensajes apenas llegan," >&2
  echo "       asi que lo mas probable es que esto salga vacio. Usá --log." >&2
  echo >&2
fi

# ---------------------------------------------------------------- los que ya estan
autorizados_actuales() {
  [[ -r "${CONFIG_DIR}/telegram.yaml" ]] || return 0
  "$PY" - "$CONFIG_DIR/telegram.yaml" <<'PY' 2>/dev/null || true
import sys
try:
    import yaml
except ImportError:
    sys.exit(0)
try:
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
except Exception:
    sys.exit(0)
for a in (cfg.get("autorizados") or []):
    if isinstance(a, dict) and a.get("id"):
        print("%s\t%s" % (a["id"], a.get("nombre") or ""))
PY
}

YA=$(autorizados_actuales || true)
if [[ -n "$YA" ]]; then
  echo "Ya autorizados en ${CONFIG_DIR}/telegram.yaml:"
  while IFS=$'\t' read -r i n; do [[ -n "$i" ]] && printf "  %-14s %s\n" "$i" "$n"; done <<< "$YA"
  echo
fi

# ---------------------------------------------------------------------- por el log
if [[ "$MODO" == "log" ]]; then
  echo "Mensajes de chats NO autorizados, segun el journal (ultimas ${LINEAS} lineas):"
  HALLADO=$(journalctl -u "$UNIDAD" -n "$LINEAS" --no-pager 2>/dev/null \
            | grep -i "NO autorizado" | tail -20 || true)
  if [[ -n "$HALLADO" ]]; then
    sed 's/^/  /' <<< "$HALLADO"
    echo
    echo "El numero que va en el YAML es el de chat_id."
  else
    echo "  (ninguno)"
    echo
    echo "  Si esperabas ver algo: escribile al bot AHORA y volve a correr esto."
    echo "  Si no ves ni una linea del servicio, puede que te falte permiso"
    echo "  sobre el journal: corré esto como root o con --api."
  fi
  exit 0
fi

# ---------------------------------------------------------------------- por la API
VAR=${SYSOP_TELEGRAM_TOKEN_VAR:-SYSOP_TELEGRAM_TOKEN}
TOKEN=${!VAR:-}
if [[ -z "$TOKEN" ]]; then
  if [[ -t 0 ]]; then
    read -rsp "token del bot (no se muestra ni queda en el historial): " TOKEN
    echo
  else
    echo "no hay ${VAR} en el entorno y no hay terminal para pedirlo." >&2
    exit 1
  fi
fi
[[ -n "$TOKEN" ]] || { echo "token vacio." >&2; exit 1; }

# timeout=0 para que no se quede en long polling. Sin `offset` no se confirma
# ninguno: el daemon los va a poder leer igual cuando arranque.
RESP=$(curl -sS --max-time 20 \
       "https://api.telegram.org/bot${TOKEN}/getUpdates?timeout=0&limit=100" || true)
unset TOKEN

TMP_PY=$(mktemp)
trap 'rm -f "$TMP_PY"' EXIT
cat > "$TMP_PY" <<'PY'
import json, os, sys

crudo = sys.stdin.read().strip()
if not crudo:
    print("la API no contesto nada. Revisa la salida a internet del server.")
    raise SystemExit(1)
try:
    d = json.loads(crudo)
except ValueError:
    print("la API contesto algo que no es JSON:", crudo[:200])
    raise SystemExit(1)

if not d.get("ok"):
    cod, desc = d.get("error_code"), d.get("description", "")
    if cod == 409:
        print("409: hay otra instancia consumiendo los mensajes de este bot.")
        print("     Es el daemon. Corre esto con --log, o pará el servicio,")
        print("     consultá, y volvé a arrancarlo.")
    elif cod == 401:
        print("401: el token no sirve. Ojo que un token revocado mide lo mismo")
        print("     que uno bueno, asi que el largo no prueba nada.")
    else:
        print("la API dijo que no: %s %s" % (cod, desc))
    raise SystemExit(1)

res = d.get("result") or []
if not res:
    print("No hay mensajes pendientes. Las cuatro causas, en orden:")
    print("  1. todavia no le escribiste al bot. Mandale un /start y repetí.")
    print("  2. el bot esta corriendo y el daemon ya se los llevo -> usá --log.")
    print("  3. pasaron mas de 24 h: Telegram descarta los updates viejos.")
    print("  4. le escribiste al bot de OTRO server (cada token es un bot).")
    raise SystemExit(0)

print("Mensajes pendientes (%d):" % len(res))
vistos = {}
for u in res:
    m = u.get("message") or u.get("edited_message") or {}
    ch, fr = m.get("chat") or {}, m.get("from") or {}
    cid = ch.get("id")
    if cid is None:
        continue
    vistos[cid] = (ch.get("type"), fr.get("username"), fr.get("first_name"))
    print("  chat_id=%-14s tipo=%-10s de=@%-16s %r" % (
        cid, ch.get("type"), fr.get("username") or "?", (m.get("text") or "")[:40]))

print()
print("Para autorizar, en config/telegram.yaml DEL REPO (no en el instalado,")
print("que se borra en cada actualizacion):")
print()
print("autorizados:")
for cid, (tipo, user, nombre) in vistos.items():
    coment = ""
    if isinstance(cid, int) and cid < 0:
        coment = "   # negativo = es un grupo. Ojo con el privacy mode del bot"
    print("  - id: %s%s" % (cid, coment))
    print("    nombre: %s" % (nombre or user or "?"))
PY

"$PY" "$TMP_PY" <<< "$RESP"
