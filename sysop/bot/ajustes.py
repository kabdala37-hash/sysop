"""Lee `config/telegram.yaml`. Sin base, sin respaldo, sin sorpresas.

Es el unico modulo de configuracion del observador que NO tiene caida a otra
fuente, y es deliberado: este es el canal por el que se pregunta que paso
cuando algo se cayo, asi que no puede depender de nada que se pueda caer.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from sysop import settings

log = logging.getLogger("sysop.bot.ajustes")

ARCHIVO = "telegram.yaml"


@dataclass(frozen=True)
class Ajustes:
    habilitado: bool = False
    token: str = field(default="", repr=False)
    autorizados: frozenset = frozenset()
    nombres: dict = field(default_factory=dict)
    prompt: str = ""
    modelo: str = ""
    presupuesto_diario_usd: float = 1.0
    timeout_seg: float = 60.0
    espera_polling_seg: int = 30
    max_turnos: int = 12
    max_chars_historial: int = 80000
    ttl_horas: float = 3.0
    ventana_tokens: int = 1_000_000
    pct_ventana: float = 90.0
    # Por que no se puede usar, si no se puede. Se contesta una vez y con el
    # motivo concreto, en vez de arrancar y fallar callado.
    problema: Optional[str] = None

    @property
    def utilizable(self) -> bool:
        return self.problema is None and self.habilitado


def cargar(config_dir: Optional[Any] = None) -> Ajustes:
    d = Path(config_dir or settings.CONFIG_DIR)
    p = d / ARCHIVO

    if not p.exists():
        return Ajustes(problema=f"no existe {p}")
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        return Ajustes(problema=f"no se pudo leer {p}: {e}")

    habilitado = bool(cfg.get("habilitado", False))

    var = str(cfg.get("token_env") or "SYSOP_TELEGRAM_TOKEN")
    token = os.getenv(var, "")
    if habilitado and not token:
        return Ajustes(habilitado=habilitado,
                       problema=f"la variable {var} esta vacia o no existe en "
                                f"/etc/sysop-agent/sysop-agent.env")

    ids, nombres = set(), {}
    for a in (cfg.get("autorizados") or []):
        if isinstance(a, dict):
            cid, nom = a.get("id"), a.get("nombre", "")
        else:
            cid, nom = a, ""
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            log.warning("autorizado con id no numerico, se ignora: %r", a)
            continue
        # El 0 es el valor de ejemplo del archivo. Aceptarlo seria peor que
        # ignorarlo: nadie tiene chat_id 0, asi que solo sirve para creer que
        # la lista esta cargada cuando no lo esta.
        if cid == 0:
            continue
        ids.add(cid)
        nombres[cid] = nom or str(cid)

    if habilitado and not ids:
        log.warning("la lista de autorizados esta vacia: el bot no le va a "
                    "contestar a nadie. Escribile y mira el log para ver tu id.")

    prompt = str(cfg.get("prompt") or "")
    if prompt:
        pp = Path(prompt)
        if not pp.is_absolute():
            pp = d / pp
        if pp.exists():
            prompt = pp.read_text(encoding="utf-8")
        else:
            return Ajustes(habilitado=habilitado,
                           problema=f"el prompt declarado no existe: {pp}")

    conv = cfg.get("conversacion") or {}

    return Ajustes(
        habilitado=habilitado,
        token=token,
        autorizados=frozenset(ids),
        nombres=nombres,
        prompt=prompt,
        modelo=str(cfg.get("modelo") or ""),
        presupuesto_diario_usd=float(cfg.get("presupuesto_diario_usd", 1.0)),
        timeout_seg=float(cfg.get("timeout_seg", 60)),
        espera_polling_seg=int(cfg.get("espera_polling_seg", 30)),
        max_turnos=int(conv.get("max_turnos", 12)),
        max_chars_historial=int(conv.get("max_chars_historial", 80000)),
        ttl_horas=float(conv.get("ttl_horas", 3)),
        ventana_tokens=int(conv.get("ventana_modelo_tokens", 1_000_000)),
        pct_ventana=float(conv.get("max_pct_ventana", 90)),
    )
