#!/bin/bash
# Envoltorio del informe periodico, para cron.
#
# Existe por una razon concreta: la unidad systemd carga
# /etc/sysop-agent/sysop-agent.env con EnvironmentFile, y cron NO. Sin esto, el
# trabajo corre con el entorno vacio, no encuentra la base, cae a la ultima
# copia buena de la configuracion y manda un informe degradado sin que nadie
# entienda por que.
#
# Tambien mantiene la linea del cron legible, que es la que se edita para
# cambiar la cadencia.
set -euo pipefail

ENV_FILE=/etc/sysop-agent/sysop-agent.env
OPT_DIR=/opt/sysop-agent

if [[ -r "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
else
  echo "$(date -Is) aviso: no se pudo leer ${ENV_FILE}; el informe va a salir degradado" >&2
fi

exec "${OPT_DIR}/venv/bin/sysop-agent" "${@:-informe}"
