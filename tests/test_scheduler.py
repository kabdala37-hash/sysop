"""El planificador: que respete cadencias y no se ponga al dia a las corridas."""
from sysop.core.models import CheckDefinition, CheckOutcome, Resource, State
from sysop.core.scheduler import Scheduler, _jitter_estable


def _cd(cid, seg, jitter=0):
    return CheckDefinition(check_id=cid, version=1, titulo=cid, selector="*",
                           expectation_id=None, schedule_sec=seg,
                           jitter_pct=jitter,
                           evaluate=lambda ctx: CheckOutcome(state=State.OK))


R = Resource(resource_id="r1", host_id="h", kind="x")
R2 = Resource(resource_id="r2", host_id="h", kind="x")


def ok(nombre, cond, extra=""):
    print("  %s %s%s" % ("OK  " if cond else "MAL ", nombre,
                         "" if cond else " :: " + str(extra)))
    return cond


def main():
    r = []

    # 1. primera vez corre ya, aunque la cadencia sea de horas
    s = Scheduler()
    pares = [(_cd("A", 21600), R)]
    r.append(ok("un control nuevo corre en el primer tick",
                len(s.vencidos(pares, 0.0)) == 1))

    # 2. no vuelve a correr antes de tiempo
    r.append(ok("y no vuelve a correr antes de su cadencia",
                len(s.vencidos(pares, 100.0)) == 0))
    r.append(ok("pero si despues",
                len(s.vencidos(pares, 21601.0)) == 1))

    # 3. cadencias distintas no se pisan
    s = Scheduler()
    pares = [(_cd("R", 15), R), (_cd("L", 300), R)]
    s.vencidos(pares, 0.0)
    en_60 = [cd.check_id for cd, _ in s.vencidos(pares, 60.0)]
    r.append(ok("a los 60s corre el de 15s y no el de 300s",
                en_60 == ["R"], en_60))

    # 4. jitter estable: mismo par -> mismo desfase, siempre
    a = _jitter_estable("A", "r1", 30, 10)
    b = _jitter_estable("A", "r1", 30, 10)
    c = _jitter_estable("A", "r2", 30, 10)
    r.append(ok("el jitter es estable entre corridas", a == b))
    r.append(ok("y distinto por recurso, para no alinear picos", a != c))
    r.append(ok("y acotado al porcentaje declarado", 0 <= a <= 3.0, a))

    # 5. tras una caida larga NO dispara una rafaga de atrasadas
    s = Scheduler()
    pares = [(_cd("A", 30), R)]
    s.vencidos(pares, 0.0)
    n = len(s.vencidos(pares, 3600.0))     # una hora despues
    r.append(ok("tras una hora caido corre UNA vez, no 120", n == 1, n))
    r.append(ok("y la siguiente se agenda desde ahora",
                len(s.vencidos(pares, 3610.0)) == 0))

    # 6. dormir lo justo
    s = Scheduler()
    s.vencidos([(_cd("A", 30), R)], 0.0)
    r.append(ok("sabe cuanto falta para el proximo",
                29 <= s.proximo_en(1.0) <= 33, s.proximo_en(1.0)))

    print("\n%d/%d" % (sum(r), len(r)))
    return 0 if all(r) else 1


if __name__ == "__main__":
    raise SystemExit(main())
