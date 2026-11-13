"""Configuración del proceso, leída del entorno.

**Este archivo SI va a git**, al revés que el `settings.py` de la plataforma: no
tiene una sola credencial, sólo nombres de variables de entorno. Los secretos
viven en `/etc/sysop-agent/sysop-agent.env`, que no se versiona.

Y por eso mismo no tiene lógica: la plataforma ya tuvo el problema de meter
lógica en un settings.py no versionado y descubrirlo cuando el destino no
levantaba. Acá el archivo viaja, pero la costumbre de dejarlo tonto se mantiene.

El override local, si hace falta, va en `settings_local.py`, que sí está
ignorado.
"""
import os
from pathlib import Path

APP_NAME = "sysop-agent"

# Identidad de la instalación. Aparece en el asunto de cada correo.
INSTALLATION = os.getenv("SYSOP_INSTALLATION", "srv3")
HOST_ID = os.getenv("SYSOP_HOST_ID", INSTALLATION)

# Almacén local
STATE_DIR = Path(os.getenv("SYSOP_STATE_DIR", "/var/lib/sysop-agent"))
DB_PATH = STATE_DIR / "sysop.db"

# Logs propios. Van con rotacion, como pide CONVENCION-DE-LOGS.md.
LOG_DIR = Path(os.getenv("SYSOP_LOG_DIR", "/var/log/sysop-agent"))

# Base de negocio, solo lectura
DB_HOST = os.getenv("SYSOP_DB_HOST", "")
DB_PORT = int(os.getenv("SYSOP_DB_PORT", "3306"))
DB_USER = os.getenv("SYSOP_DB_USER", "sysop_ro")
DB_PASSWORD = os.getenv("SYSOP_DB_PASSWORD", "")
DB_NAME = os.getenv("SYSOP_DB_NAME", "plataforma")

# Back-end de la plataforma, para los endpoints internos
BACKEND_URL = os.getenv("SYSOP_BACKEND_URL", "http://127.0.0.1:10997")
# Para CONSUMIR los endpoints internos del back. Es la clave de la plataforma.
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")

# Tenant administrador de la plataforma: de ahí salen credenciales y destinatarios
PLATFORM_TENANT = os.getenv("SYSOP_PLATFORM_TENANT", "acme")

# Presupuesto del modelo, en dólares
LLM_DAILY_BUDGET_USD = float(os.getenv("SYSOP_LLM_DAILY_BUDGET_USD", "5"))

# Las expectativas viven CON el codigo porque van a git y se versionan junto a
# los controles que las usan. /etc/sysop-agent guarda secretos y overrides
# locales, que por definicion no se versionan.
CONFIG_DIR = Path(os.getenv("SYSOP_CONFIG_DIR", "/opt/sysop-agent/config"))
