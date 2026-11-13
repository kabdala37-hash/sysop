#!/usr/bin/env bash
#
# Instalador del observador. Idempotente: correrlo dos veces no cambia nada.
#
#   bash install.sh --check                  releva y no toca nada
#   bash install.sh --stage base             usuario de sistema y directorios
#   bash install.sh --stage db               usuario MySQL de monitoreo
#   bash install.sh --stage app              codigo, venv y unidad systemd
#   bash install.sh --stage all              las tres
#
# El identificador del host tiene que coincidir con el YAML de
# config/hosts/. NO se deduce de `hostname`: en srv3 el hostname es
# `ip-10-0-0-2` y el YAML se llama `srv3.yaml`, asi que deducirlo hace que el
# servicio no arranque. Se pasa explicito:
#
#   bash install.sh --host-id srv3 --stage all
#
# Pensado para correr como root EN el servidor observado. El mismo script sirve
# para srv3 y para srv2: lo que cambia va en variables, no en el codigo.
#
# Lo que este script NO hace, a proposito:
#   * no ARRANCA el servicio: lo deja instalado y detenido. Arrancarlo es una
#     decision explicita, sobre todo en produccion
#   * no toca /opt/plataforma ni ninguno de sus servicios
#   * no imprime la password que genera
set -euo pipefail

SYS_USER="sysop-agent"
SYS_GROUP="sysop-agent"
ETC_DIR="/etc/sysop-agent"
STATE_DIR="/var/lib/sysop-agent"
LOG_DIR="/var/log/sysop-agent"
OPT_DIR="/opt/sysop-agent"
ENV_FILE="${ETC_DIR}/sysop-agent.env"

DB_MONITOR_USER="sysop_ro"
DB_NAME="${SYSOP_DB_NAME:-plataforma}"

MODE="check"
STAGE="all"
HOST_ID="${SYSOP_HOST_ID:-$(hostname -s)}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)   MODE="check"; shift ;;
    --apply)   MODE="apply"; shift ;;
    --stage)   STAGE="$2"; MODE="apply"; shift 2 ;;
    --host-id) HOST_ID="$2"; shift 2 ;;
    *) echo "uso: $0 [--check] [--stage base|db|app|all] [--host-id <id>]" >&2; exit 2 ;;
  esac
done

env_set() {
  # Upsert de una clave. Hace falta porque el env se escribe una sola vez y
  # despues hay que poder corregir un valor sin borrar la password.
  local k="$1" v="$2"
  touch "$ENV_FILE"
  if grep -q "^${k}=" "$ENV_FILE"; then
    sed -i "s|^${k}=.*|${k}=${v}|" "$ENV_FILE"
  else
    echo "${k}=${v}" >> "$ENV_FILE"
  fi
}

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
info() { printf '  ..    %s\n' "$*"; }
warn() { printf '  \033[33mAVISO\033[0m %s\n' "$*"; }
die()  { printf '  \033[31mFALLA\033[0m %s\n' "$*" >&2; exit 1; }

# El observador se conecta CON TLS. Es a proposito, y va contra la costumbre del
# repo de la plataforma, que usa `--skip-ssl` en todos lados. Dos razones, las
# dos aprendidas rompiendo esto el 2026-08-27 en srv3:
#
#   1. El cliente 8.0.46 de Ubuntu 24.04 no conoce `--skip-ssl` y aborta con
#      "unknown option". Y `mysql --help` no lista `--ssl-mode`, asi que
#      tampoco se puede decidir mirando la ayuda.
#   2. Con `caching_sha2_password` (el default de MySQL 8) una conexion por TCP
#      sin canal seguro falla con error 2061 apenas cambia la password, porque
#      el servidor pierde el cache y la primera autenticacion exige TLS. O sea:
#      deshabilitar SSL funciona hasta que rota la credencial, y ahi se cae.
#
# El servidor ya tiene TLS (have_ssl=YES). No hay nada que ganar apagandolo.
SSL_FLAG="--ssl-mode=REQUIRED"

[[ $EUID -eq 0 ]] || die "hay que correrlo como root"

echo "=== sysop-agent :: instalacion (${MODE}, stage=${STAGE}) en $(hostname) ==="
echo "    cliente mysql: $(mysql --version | awk '{print $3}')  tls: ${SSL_FLAG}"
echo "    host-id: ${HOST_ID}  (tiene que existir config/hosts/${HOST_ID}.yaml)"

# ---------------------------------------------------------------------------
# Etapa base: usuario de sistema y directorios
# ---------------------------------------------------------------------------
etapa_base() {
  echo
  echo "-- usuario de sistema"
  if id "$SYS_USER" &>/dev/null; then
    ok "el usuario ${SYS_USER} ya existe"
  elif [[ "$MODE" == "check" ]]; then
    info "faltaria crear el usuario ${SYS_USER}"
  else
    # Sin shell y sin home: el observador no es una cuenta desde la que se
    # trabaja. Si alguien consigue ejecutar codigo como el, no consigue una
    # sesion.
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SYS_USER"
    ok "usuario ${SYS_USER} creado (sin shell, sin home)"
  fi

  if id "$SYS_USER" &>/dev/null; then
    local sh; sh="$(getent passwd "$SYS_USER" | cut -d: -f7)"
    [[ "$sh" == */nologin || "$sh" == */false ]] \
      && ok "shell = ${sh}" || warn "shell = ${sh} (deberia ser nologin)"
    groups "$SYS_USER" | grep -qw docker \
      && warn "esta en el grupo docker: el socket de docker es root efectivo" \
      || ok "fuera del grupo docker"

    # systemd-journal es de SOLO LECTURA sobre el journal, y sin el los controles
    # de OOM y de errores de disco no ven nada. Peor: `journalctl` falla en
    # silencio devolviendo vacio, que es indistinguible de "no hay errores".
    if groups "$SYS_USER" | grep -qw systemd-journal; then
      ok "en el grupo systemd-journal (puede leer el journal)"
    elif [[ "$MODE" == "check" ]]; then
      info "faltaria agregarlo al grupo systemd-journal"
    else
      usermod -aG systemd-journal "$SYS_USER"
      ok "agregado al grupo systemd-journal"
    fi
  fi

  echo
  echo "-- directorios"
  # ETC lo lee el servicio pero lo escribe root: ahi viven los secretos.
  # STATE y LOG los escribe el servicio.
  local specs=(
    "${ETC_DIR}|0750|root:${SYS_GROUP}"
    "${STATE_DIR}|0750|${SYS_USER}:${SYS_GROUP}"
    "${LOG_DIR}|0750|${SYS_USER}:${SYS_GROUP}"
    "${OPT_DIR}|0755|root:root"
  )
  for spec in "${specs[@]}"; do
    IFS='|' read -r d m o <<<"$spec"
    if [[ -d "$d" ]]; then
      ok "${d} existe ($(stat -c '%a %U:%G' "$d"))"
    elif [[ "$MODE" == "check" ]]; then
      info "faltaria crear ${d} (${m} ${o})"
      continue
    else
      install -d -m "$m" -o "${o%%:*}" -g "${o##*:}" "$d"
      ok "${d} creado (${m} ${o})"
    fi
    # if y no `[[ ]] && {...}`: en modo check la condicion es falsa, el compuesto
    # devuelve 1, y como es lo ultimo del for la funcion sale con 1. Con
    # `set -e` eso aborta el script SIN ERROR VISIBLE, justo despues de los
    # directorios: `--check` decia haber relevado todo y nunca miraba MySQL.
    if [[ "$MODE" == "apply" ]]; then
      chmod "$m" "$d"; chown "$o" "$d"
    fi
  done
}

# ---------------------------------------------------------------------------
# Etapa db: usuario MySQL de monitoreo
# ---------------------------------------------------------------------------
etapa_db() {
  echo
  echo "-- usuario MySQL de monitoreo"

  mysql -N -e "SELECT 1" &>/dev/null \
    || die "no puedo entrar a MySQL como root local"

  local existe
  existe="$(mysql -N -e "SELECT COUNT(*) FROM mysql.user WHERE user='${DB_MONITOR_USER}'")"

  if [[ "$MODE" == "check" ]]; then
    [[ "$existe" -gt 0 ]] && ok "el usuario ${DB_MONITOR_USER} ya existe" \
                          || info "faltaria crear ${DB_MONITOR_USER}"
  else
    local pass
    if [[ -f "$ENV_FILE" ]] && grep -q '^SYSOP_DB_PASSWORD=' "$ENV_FILE"; then
      # Reusar la password ya guardada. Regenerarla en cada corrida romperia
      # el servicio en marcha, que es exactamente lo que no puede pasar en srv2.
      pass="$(grep '^SYSOP_DB_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"
      info "reusando la password que ya estaba en ${ENV_FILE}"
    else
      pass="$(openssl rand -base64 30 | tr -d '/+=' | head -c 32)"
      info "password nueva generada (no se imprime)"
    fi

    # PROCESS y SELECT sobre performance_schema NO son opcionales. Verificado el
    # 2026-08-27 contra srv2 con un usuario de solo SELECT: performance_schema da
    # error 1142 y information_schema.processlist devuelve 1 fila de las 232
    # conexiones reales. Sin esto, cinco controles de base de datos (DB-03 a
    # DB-08) no se pueden implementar.
    #
    # Dos hosts porque 'localhost' en MySQL matchea el socket, y el observador
    # se conecta por TCP a 127.0.0.1.
    for h in localhost 127.0.0.1; do
      mysql <<SQL
CREATE USER IF NOT EXISTS '${DB_MONITOR_USER}'@'${h}' IDENTIFIED BY '${pass}';
ALTER USER '${DB_MONITOR_USER}'@'${h}' IDENTIFIED BY '${pass}';
GRANT SELECT ON \`${DB_NAME}\`.*     TO '${DB_MONITOR_USER}'@'${h}';
GRANT SELECT ON performance_schema.* TO '${DB_MONITOR_USER}'@'${h}';
GRANT PROCESS ON *.*                 TO '${DB_MONITOR_USER}'@'${h}';
SQL
    done
    mysql -e "FLUSH PRIVILEGES"
    ok "usuario ${DB_MONITOR_USER} creado/actualizado en localhost y 127.0.0.1"

    # El env: root escribe, el grupo del servicio lee.
    umask 027
    if [[ -f "$ENV_FILE" ]]; then
      sed -i "/^SYSOP_DB_PASSWORD=/d" "$ENV_FILE"
    else
      cat > "$ENV_FILE" <<EOF
# Entorno del observador. Lo escribe install.sh. Contiene secretos.
SYSOP_INSTALLATION=${HOST_ID}
SYSOP_HOST_ID=${HOST_ID}
SYSOP_DB_HOST=127.0.0.1
SYSOP_DB_PORT=3306
SYSOP_DB_USER=${DB_MONITOR_USER}
SYSOP_DB_NAME=${DB_NAME}
SYSOP_STATE_DIR=${STATE_DIR}
SYSOP_CONFIG_DIR=${OPT_DIR}/config
EOF
    fi
    echo "SYSOP_DB_PASSWORD=${pass}" >> "$ENV_FILE"
    env_set SYSOP_HOST_ID "${HOST_ID}"
    env_set SYSOP_CONFIG_DIR "${OPT_DIR}/config"

    # El panel web se dio de baja el 2026-08-28: ya no se genera SYSOP_API_KEY.
    # Si quedo de una instalacion anterior, se puede borrar del env a mano.

    # La del back se copia de su env, para consumir /api/internal/observability.
    if [[ -f /etc/plataforma/plataforma.env ]]; then
      k="$(grep -E '^INTERNAL_API_KEY=' /etc/plataforma/plataforma.env \
           | cut -d= -f2- | tr -d '"' || true)"
      if [[ -n "$k" ]]; then
        env_set INTERNAL_API_KEY "$k"
        ok "INTERNAL_API_KEY copiada del env de la plataforma"
      else
        warn "no se encontro INTERNAL_API_KEY: el pack caera a SQL directo"
      fi
    fi
    env_set SYSOP_INSTALLATION "${HOST_ID}"
    chown "root:${SYS_GROUP}" "$ENV_FILE"; chmod 0640 "$ENV_FILE"
    ok "${ENV_FILE} escrito (0640 root:${SYS_GROUP})"
  fi

  # Verificacion real: no alcanza con que el GRANT no haya dado error.
  if [[ -f "$ENV_FILE" ]] && grep -q '^SYSOP_DB_PASSWORD=' "$ENV_FILE"; then
    echo
    echo "-- verificacion de permisos"
    local p; p="$(grep '^SYSOP_DB_PASSWORD=' "$ENV_FILE" | cut -d= -f2-)"
    local M=(mysql -h 127.0.0.1 -u "$DB_MONITOR_USER" "-p${p}" -N)
    [[ -n "$SSL_FLAG" ]] && M+=("$SSL_FLAG")

    "${M[@]}" -e "SELECT 1" &>/dev/null && ok "conecta" || die "no conecta"

    local n
    n="$("${M[@]}" -e "SELECT COUNT(*) FROM performance_schema.threads" 2>/dev/null || echo ERR)"
    [[ "$n" == "ERR" ]] && die "no lee performance_schema (DB-04 y DB-05 quedan imposibles)" \
                        || ok "lee performance_schema (${n} hilos)"

    # PROCESS se verifica por el GRANT, no contando conexiones. Contar es un
    # test debil: en un server con poca carga el usuario ve su propia conexion y
    # el numero da igual con permiso y sin permiso, o sea verde falso.
    if "${M[@]}" -e "SHOW GRANTS" 2>/dev/null | grep -q "PROCESS ON \*\.\*"; then
      ok "tiene PROCESS (verificado en SHOW GRANTS)"
    else
      die "le falta PROCESS: sin el, DB-04 y DB-06 no ven nada fuera de su sesion"
    fi
    # Y ademas que sirva de verdad: tiene que ver mas usuarios que el suyo.
    local usuarios
    usuarios="$("${M[@]}" -e "SELECT COUNT(DISTINCT user) FROM information_schema.processlist" 2>/dev/null || echo 0)"
    [[ "$usuarios" -gt 1 ]] \
      && ok "ve conexiones de ${usuarios} usuarios distintos" \
      || warn "solo ve 1 usuario: el server esta casi vacio, no prueba nada"

    # Y lo mas importante: que NO pueda escribir.
    if "${M[@]}" -e "CREATE TABLE \`${DB_NAME}\`.__sysop_probe (i INT)" &>/dev/null; then
      mysql -e "DROP TABLE IF EXISTS \`${DB_NAME}\`.__sysop_probe"
      die "PUEDE ESCRIBIR en ${DB_NAME}. El observador no puede tener escritura."
    else
      ok "no puede escribir (rechazado, como corresponde)"
    fi
  fi
}

# ---------------------------------------------------------------------------
# Etapa app: codigo, venv y unidad systemd
# ---------------------------------------------------------------------------
etapa_app() {
  echo
  echo "-- codigo y entorno"

  local origen="${SYSOP_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  [[ -d "${origen}/sysop" ]] || die "no encuentro el codigo en ${origen}"

  if [[ "$MODE" == "check" ]]; then
    info "copiaria ${origen} -> ${OPT_DIR}"
    [[ -x "${OPT_DIR}/venv/bin/python" ]] && ok "el venv ya existe" \
                                          || info "faltaria crear el venv"
    [[ -f /etc/systemd/system/sysop-agent.service ]] && ok "la unidad ya existe" \
                                                    || info "faltaria instalar la unidad"
    return 0
  fi

  # Se copian solo las piezas que el servicio necesita. Sin tests ni .git: lo
  # que no corre en produccion no tiene por que estar ahi.
  for d in sysop config install; do
    [[ -d "${origen}/${d}" ]] || continue
    rm -rf "${OPT_DIR}/${d}.nuevo"
    cp -r "${origen}/${d}" "${OPT_DIR}/${d}.nuevo"
    find "${OPT_DIR}/${d}.nuevo" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    rm -rf "${OPT_DIR}/${d}"
    mv "${OPT_DIR}/${d}.nuevo" "${OPT_DIR}/${d}"
  done
  cp "${origen}/pyproject.toml" "${origen}/requirements.txt" "${OPT_DIR}/" 2>/dev/null || true
  chown -R root:root "${OPT_DIR}"
  ok "codigo copiado a ${OPT_DIR}"

  if [[ ! -x "${OPT_DIR}/venv/bin/python" ]]; then
    python3 -m venv "${OPT_DIR}/venv"
    ok "venv creado"
  fi
  "${OPT_DIR}/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
  "${OPT_DIR}/venv/bin/pip" install -q -r "${OPT_DIR}/requirements.txt"

  # El paquete en modo editable, para que exista el comando `sysop-agent`. No es
  # comodidad: el cron del informe lo invoca por nombre, asi que sin esto el
  # informe periodico no sale y nadie se entera (que es justo lo que el informe
  # existe para evitar).
  "${OPT_DIR}/venv/bin/pip" install -q -e "${OPT_DIR}" 2>/dev/null \
    && ok "comando sysop-agent instalado" \
    || warn "no se pudo instalar el paquete: el cron del informe no va a andar"
  ok "dependencias instaladas"

  # Smoke test ANTES de tocar systemd: si el codigo no importa, no tiene
  # sentido instalar una unidad que va a entrar en bucle de reinicio.
  if "${OPT_DIR}/venv/bin/python" -c "import sys; sys.path.insert(0,'${OPT_DIR}'); import sysop.daemon" 2>/dev/null; then
    ok "el codigo importa"
  else
    die "el codigo NO importa: no se instala la unidad"
  fi

  # El YAML del host tiene que existir, o el servicio arranca y muere. Es mejor
  # descubrirlo aca que en el journal despues de cinco reinicios.
  if [[ -f "${OPT_DIR}/config/hosts/${HOST_ID}.yaml" ]]; then
    ok "expectativas encontradas para ${HOST_ID}"
  else
    die "falta config/hosts/${HOST_ID}.yaml: el servicio no arrancaria"
  fi

  install -m 0644 "${origen}/install/sysop-agent.service" \
          /etc/systemd/system/sysop-agent.service
  systemctl daemon-reload
  ok "unidad instalada (systemctl start sysop-agent para arrancarla)"

  if [[ -x "${OPT_DIR}/venv/bin/sysop-agent" ]]; then
    ok "el comando sysop-agent responde"
  else
    die "falta ${OPT_DIR}/venv/bin/sysop-agent: el cron del informe fallaria en silencio"
  fi

  # El informe periodico va por cron y NO por el daemon: si el daemon se muere,
  # el trabajo corre igual y avisa que se murio.
  #
  # No se pisa si ya existe: la cadencia se edita a mano en ese archivo, y una
  # reinstalacion que la vuelva a poner en "diario" borraria la decision de
  # quien la haya cambiado.
  if [[ -f "${origen}/install/logrotate/sysop-agent" ]]; then
    install -m 0644 "${origen}/install/logrotate/sysop-agent" \
            /etc/logrotate.d/sysop-agent
    ok "logrotate instalado (solo informe.log y los ya rotados: daemon.log lo"
    info "   rota el propio daemon, y dos rotadores se pisan en silencio)"
  fi

  install -d -m 0755 "${OPT_DIR}/bin"
  install -m 0755 "${origen}/bin/informe.sh" "${OPT_DIR}/bin/informe.sh"
  ok "envoltorio del informe instalado"

  if [[ -f /etc/cron.d/sysop-agent ]]; then
    ok "el cron del informe ya existe (cadencia: $(grep -oE '^[0-9*/, -]+' /etc/cron.d/sysop-agent | tail -1 | tr -s ' '))"
  else
    install -m 0644 "${origen}/install/sysop-agent.cron" /etc/cron.d/sysop-agent
    ok "cron del informe instalado (editar /etc/cron.d/sysop-agent para la cadencia)"
  fi

  systemctl is-active --quiet sysop-agent \
    && warn "el servicio esta CORRIENDO con el codigo viejo: hace falta restart" \
    || info "el servicio esta detenido"
}

case "$STAGE" in
  base) etapa_base ;;
  db)   etapa_db ;;
  app)  etapa_app ;;
  all)  etapa_base; etapa_db; etapa_app ;;
  *)    die "stage desconocido: ${STAGE}" ;;
esac

echo
echo "=== fin ==="
