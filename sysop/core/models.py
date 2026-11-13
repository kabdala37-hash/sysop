"""Tipos del motor. Es el contrato del que dependen collectors, packs y notificación.

Tres ideas que están acá y no son negociables, porque son las que hacen que el
observador no mienta:

* Un resultado SIEMPRE lleva la expectativa contra la que se evaluó. Sin
  expectativa no hay observación calificable, solo un número suelto.
* `UNKNOWN` es un estado de primera clase, no un error. La ausencia de evidencia
  nunca colapsa a `OK`.
* `evidence_quality` viaja en cada resultado. Mientras la plataforma no tenga el
  registro de ejecuciones, media docena de controles responden con evidencia
  inferida, y el informe tiene que poder decirlo.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Estados y severidades
# ---------------------------------------------------------------------------

class State(str, enum.Enum):
    """Qué encontró el control. Ortogonal a la severidad: un MISSING puede ser
    crítico o informativo según qué recurso sea."""
    OK = "OK"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    MISSING = "MISSING"
    LATE = "LATE"
    STUCK = "STUCK"
    UNKNOWN = "UNKNOWN"
    UNCLASSIFIED = "UNCLASSIFIED"
    DISABLED_EXPECTED = "DISABLED_EXPECTED"
    SUPPRESSED = "SUPPRESSED"
    RECOVERED = "RECOVERED"

    @property
    def es_problema(self) -> bool:
        """UNKNOWN NO es problema por sí solo: es falta de evidencia, y se
        reporta aparte. Meterlo acá haría que una fuente caída genere una
        tormenta de incidentes falsos."""
        return self in (State.DEGRADED, State.FAILED, State.MISSING,
                        State.LATE, State.STUCK)


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    MAJOR = "major"
    CRITICAL = "critical"

    @property
    def orden(self) -> int:
        return {"info": 0, "warning": 1, "major": 2, "critical": 3}[self.value]


class EvidenceQuality(str, enum.Enum):
    """Qué tan directa es la prueba que sostiene el resultado.

    FULL     el dato viene de la fuente que lo define (ledger, health endpoint)
    PARTIAL  se reconstruyó de una fuente indirecta (journal, proceso vivo)
    INFERRED se dedujo de un efecto de negocio, sin ver el mecanismo
    NONE     no hay evidencia; solo válido con state=UNKNOWN
    """
    FULL = "FULL"
    PARTIAL = "PARTIAL"
    INFERRED = "INFERRED"
    NONE = "NONE"


# ---------------------------------------------------------------------------
# Recursos y expectativas
# ---------------------------------------------------------------------------

class ResourceState(str, enum.Enum):
    """Ciclo de vida de la sección 6.3 de la especificación.

    Descubrir no es aprobar: un recurso nuevo entra UNCLASSIFIED y ahí se queda
    hasta que una regla o una persona diga qué es. El aprendizaje nunca lo mueve.
    """
    DISCOVERED = "DISCOVERED"
    UNCLASSIFIED = "UNCLASSIFIED"
    MANAGED = "MANAGED"
    IGNORED = "IGNORED"
    EXTERNAL = "EXTERNAL"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class Resource:
    """Algo observable. El `resource_id` se compone de tipo + alcance y NUNCA
    incluye el PID: un proceso que reinicia sigue siendo el mismo recurso, y su
    historia tiene que sobrevivir al reinicio."""
    resource_id: str                 # "worker:clienteb:4", "systemd:plataforma-api.service"
    host_id: str
    kind: str                        # systemd | mount | job | worker | queue | endpoint | ...
    scope: dict[str, Any] = field(default_factory=dict)
    state: ResourceState = ResourceState.DISCOVERED
    attrs: dict[str, Any] = field(default_factory=dict)


class ExpectationSource(int, enum.Enum):
    """Precedencia de la sección 6.1, de mayor a menor autoridad.

    El valor numérico ES la precedencia: `min()` gana. Una fuente inferior nunca
    sobrescribe a una superior; cuando se contradicen se emite
    EXPECTATION_CONFLICT con las dos evidencias, en vez de elegir en silencio.
    """
    OPERATOR = 1          # aprobada por una persona
    APP_TRUTH = 2         # fuente transaccional de la aplicación
    OS_DECLARATIVE = 3    # configuración declarativa del sistema operativo
    PACK_CONTRACT = 4     # contrato versionado del pack
    DISCOVERED = 5        # recurso descubierto sin clasificar
    BASELINE = 6          # línea base aprendida


@dataclass(frozen=True)
class Expectation:
    """Qué debería pasar. Sin esto, un control no puede calificar nada."""
    expectation_id: str
    resource_selector: str
    desired: dict[str, Any]
    source: ExpectationSource
    severity_if_missing: Severity = Severity.MAJOR
    author: Optional[str] = None
    reason: Optional[str] = None
    valid_until: Optional[datetime] = None
    revision: int = 1
    # Las derivadas guardan de qué datos salieron y cuándo. Si la fuente no se
    # puede leer, el resultado es UNKNOWN_SOURCE: no se conserva un conjunto
    # viejo haciéndolo pasar por vigente.
    source_hash: Optional[str] = None
    source_at: Optional[datetime] = None


@dataclass(frozen=True)
class ExpectationConflict:
    resource_id: str
    ganadora: Expectation
    perdedora: Expectation
    detectado_en: datetime


# ---------------------------------------------------------------------------
# Controles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QueryBudget:
    """Techo de costo de un control. La especificación prohíbe scans completos en
    el ciclo normal, y esto es lo que lo hace cumplible."""
    max_rows: int = 10_000
    max_window_sec: int = 86_400
    timeout_sec: float = 5.0


@dataclass(frozen=True)
class Hysteresis:
    """Cuántas muestras hacen falta para abrir y para cerrar.

    Asimétrico a propósito: abrir rápido y cerrar lento evita que un síntoma
    intermitente genere veinte incidentes en una hora.
    """
    abre_tras: int = 2
    cierra_tras: int = 3


@dataclass(frozen=True)
class EvidencePolicy:
    """Lista POSITIVA de campos que pueden persistirse o mandarse al modelo.

    Positiva y no negativa a propósito: una lista de prohibidos deja pasar todo
    lo que nadie previó, que es exactamente como se escapa un secreto nuevo.
    """
    campos_permitidos: tuple[str, ...] = ()
    max_bytes: int = 4096
    redactar: bool = True


@dataclass(frozen=True)
class CheckOutcome:
    """Lo que devuelve la función de evaluación de un control."""
    state: State
    actual: Optional[dict[str, Any]] = None
    expected: Optional[dict[str, Any]] = None
    reason_code: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    evidence_quality: EvidenceQuality = EvidenceQuality.FULL
    severity_override: Optional[Severity] = None

    def __post_init__(self) -> None:
        # Un OK sin evidencia es indistinguible de "no miré". Se prohíbe.
        if self.state is State.OK and self.evidence_quality is EvidenceQuality.NONE:
            raise ValueError("un OK no puede tener evidence_quality=NONE")


@dataclass(frozen=True)
class CheckDefinition:
    check_id: str                    # "NAC-JOB-05"
    version: int
    titulo: str
    selector: str                    # qué recursos alcanza
    expectation_id: Optional[str]
    schedule_sec: int
    evaluate: Callable[["CheckContext"], CheckOutcome]
    timeout_sec: float = 5.0
    jitter_pct: int = 10
    budget: QueryBudget = field(default_factory=QueryBudget)
    severity: Severity = Severity.MAJOR
    hysteresis: Hysteresis = field(default_factory=Hysteresis)
    evidence_policy: EvidencePolicy = field(default_factory=EvidencePolicy)
    dependencies: tuple[str, ...] = ()
    runbook: str = ""
    owner: str = "plataforma"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    resource_id: str
    observed_at: datetime
    state: State
    severity: Severity
    expected: Optional[dict[str, Any]]
    actual: Optional[dict[str, Any]]
    reason_code: str
    evidence_refs: tuple[str, ...]
    evidence_quality: EvidenceQuality
    rule_version: int


# ---------------------------------------------------------------------------
# Incidentes
# ---------------------------------------------------------------------------

class IncidentState(str, enum.Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    MITIGATING = "MITIGATING"
    RECOVERED = "RECOVERED"
    CLOSED = "CLOSED"
    SUPPRESSED = "SUPPRESSED"


@dataclass
class Incident:
    """Agrupa resultados que son el mismo problema.

    El `fingerprint` es lo que evita la tormenta de correos: mismo problema,
    mismo incidente, aunque el control lo reporte cada quince segundos. Y es lo
    que permite reabrir en vez de duplicar cuando algo vuelve a pasar.
    """
    incident_id: str
    fingerprint: str
    resource_id: str
    check_id: str
    opened_at: datetime
    severity: Severity
    state: IncidentState = IncidentState.OPEN
    last_seen: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    cause: dict[str, Any] = field(default_factory=dict)
    # Cuando la correlación decide que este incidente es consecuencia de otro.
    # El correo muestra la causa raíz y cuelga los síntomas debajo, en vez de
    # mandar seis alertas de un solo disco lleno.
    causa_raiz_de: Optional[str] = None


@dataclass(frozen=True)
class CheckContext:
    """Lo que recibe la función de evaluación. Se arma por control y por recurso."""
    resource: Resource
    expectation: Optional[Expectation]
    now: datetime
    budget: QueryBudget
    deps: dict[str, Any] = field(default_factory=dict)   # collectors y clientes
