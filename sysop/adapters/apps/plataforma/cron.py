"""Parser de expresiones cron y cálculo de la corrida esperada.

Hace falta para responder "¿este trabajo tendría que haber corrido y no
corrió?", que es el control JOB-05. Sin saber cuándo se lo esperaba, la única
alternativa es mirar cuánto hace que no corre, y eso da falsos positivos en todo
lo que corre una vez por semana.

Se implementa a mano y no con una librería porque son cinco campos y las
expresiones de la plataforma son simples (`*/15 * * * *`, `55 9 * * *`,
`0 11 * * 1,4`). Una dependencia más es una dependencia más que actualizar en un
servicio cuyo trabajo es no romperse.

**Todo se calcula en la zona horaria del job**, que en `rc_cron_jobs` está en la
columna `timezone` y hoy es UTC en los 17. Si algún día alguien pone
`America/Argentina/Buenos_Aires` en uno, calcular en UTC lo daría por atrasado
tres horas por día.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CronInvalido(ValueError):
    """La expresión no se pudo interpretar.

    Se distingue de "no toca ahora" a propósito: un cron que no se entiende es
    un hallazgo (JOB-02: la definición no coincide con su fuente), no un job
    que simplemente no tenía que correr.
    """


@dataclass(frozen=True)
class Campos:
    minutos: frozenset[int]
    horas: frozenset[int]
    dias_mes: frozenset[int]
    meses: frozenset[int]
    dias_semana: frozenset[int]
    # Cron trata día-del-mes y día-de-semana con OR cuando los dos están
    # restringidos, no con AND. Es una rareza histórica que hay que respetar o
    # el cálculo se desvía.
    dom_restringido: bool
    dow_restringido: bool


def _expandir(campo: str, minimo: int, maximo: int) -> frozenset[int]:
    valores: set[int] = set()
    for parte in campo.split(","):
        parte = parte.strip()
        if not parte:
            raise CronInvalido(f"campo vacio en {campo!r}")

        paso = 1
        if "/" in parte:
            parte, paso_s = parte.split("/", 1)
            try:
                paso = int(paso_s)
            except ValueError as e:
                raise CronInvalido(f"paso invalido en {campo!r}") from e
            if paso <= 0:
                raise CronInvalido(f"paso <= 0 en {campo!r}")

        if parte in ("*", ""):
            desde, hasta = minimo, maximo
        elif "-" in parte:
            a, _, b = parte.partition("-")
            try:
                desde, hasta = int(a), int(b)
            except ValueError as e:
                raise CronInvalido(f"rango invalido en {campo!r}") from e
        else:
            try:
                desde = hasta = int(parte)
            except ValueError as e:
                raise CronInvalido(f"valor invalido en {campo!r}") from e

        if desde < minimo or hasta > maximo or desde > hasta:
            raise CronInvalido(f"fuera de rango en {campo!r} ({minimo}-{maximo})")
        valores.update(range(desde, hasta + 1, paso))

    if not valores:
        raise CronInvalido(f"sin valores en {campo!r}")
    return frozenset(valores)


def parsear(expr: str) -> Campos:
    partes = expr.split()
    if len(partes) != 5:
        raise CronInvalido(f"se esperaban 5 campos y hay {len(partes)}: {expr!r}")
    m, h, dom, mes, dow = partes

    dias_semana = _expandir(dow, 0, 7)
    # Domingo se escribe 0 o 7. Se normaliza a 0 para poder comparar con
    # `weekday()` de Python después de convertirlo.
    if 7 in dias_semana:
        dias_semana = frozenset(dias_semana - {7} | {0})

    return Campos(
        minutos=_expandir(m, 0, 59),
        horas=_expandir(h, 0, 23),
        dias_mes=_expandir(dom, 1, 31),
        meses=_expandir(mes, 1, 12),
        dias_semana=dias_semana,
        dom_restringido=dom.strip() != "*",
        dow_restringido=dow.strip() != "*",
    )


def _coincide(c: Campos, t: datetime) -> bool:
    if t.minute not in c.minutos or t.hour not in c.horas:
        return False
    if t.month not in c.meses:
        return False

    # weekday(): lunes=0..domingo=6. cron: domingo=0..sabado=6.
    dow = (t.weekday() + 1) % 7
    en_dom = t.day in c.dias_mes
    en_dow = dow in c.dias_semana

    if c.dom_restringido and c.dow_restringido:
        return en_dom or en_dow      # OR, no AND: es como funciona cron
    if c.dom_restringido:
        return en_dom
    if c.dow_restringido:
        return en_dow
    return True


def _tz(nombre: str):
    if not nombre or nombre.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(nombre)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return timezone.utc


def ultima_esperada(expr: str, ahora: datetime, tz_nombre: str = "UTC",
                    limite_dias: int = 40) -> Optional[datetime]:
    """El último momento en que el trabajo tendría que haber arrancado.

    Devuelve None si no hay ninguno dentro del límite, que en la práctica sólo
    pasa con expresiones muy esporádicas (un día del año). El límite existe
    para que la búsqueda no sea infinita ante una expresión que nunca coincide.
    """
    c = parsear(expr)
    tz = _tz(tz_nombre)
    t = ahora.astimezone(tz).replace(second=0, microsecond=0)

    for _ in range(limite_dias * 24 * 60):
        if _coincide(c, t):
            return t
        t -= timedelta(minutes=1)
    return None


def proxima_esperada(expr: str, ahora: datetime, tz_nombre: str = "UTC",
                     limite_dias: int = 40) -> Optional[datetime]:
    c = parsear(expr)
    tz = _tz(tz_nombre)
    t = (ahora.astimezone(tz).replace(second=0, microsecond=0)
         + timedelta(minutes=1))

    for _ in range(limite_dias * 24 * 60):
        if _coincide(c, t):
            return t
        t += timedelta(minutes=1)
    return None


def periodo_seg(expr: str, tz_nombre: str = "UTC") -> Optional[int]:
    """Cada cuánto corre, aproximado, para dimensionar la ventana de gracia.

    Un trabajo que corre cada 5 minutos y se atrasa 10 es un problema; uno
    semanal atrasado 10 minutos no. Sin esto, la gracia tendría que ser un
    número fijo y estaría mal para alguno de los dos.
    """
    base = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)   # un lunes
    try:
        a = proxima_esperada(expr, base, tz_nombre)
        if a is None:
            return None
        b = proxima_esperada(expr, a, tz_nombre)
        if b is None:
            return None
    except CronInvalido:
        return None
    return int((b - a).total_seconds())
