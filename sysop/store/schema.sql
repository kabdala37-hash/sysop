-- Almacén local del observador. SQLite con WAL.
--
-- Vive acá y no en la base de negocio a propósito: si MySQL se cae, el
-- observador tiene que poder seguir registrando y mandar el correo que avisa
-- que se cayó. Ese es el principio 8 de la especificación, y es la razón por la
-- que este archivo existe.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

-- ---------------------------------------------------------------------------
-- Inventario
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS resource (
    resource_id   TEXT PRIMARY KEY,
    host_id       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    scope_json    TEXT NOT NULL DEFAULT '{}',
    state         TEXT NOT NULL DEFAULT 'DISCOVERED',
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    attrs_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_resource_kind ON resource (host_id, kind, state);

-- Clasificaciones puestas a mano: "esto es nuestro, no avises".
--
-- Tabla aparte y no sólo el `state` de `resource` porque lo que importa no es el
-- veredicto sino QUIEN lo puso, POR QUE y HASTA CUANDO. Un silencio sin autor es
-- una decisión que dentro de seis meses nadie puede revisar, y uno sin fecha es
-- un control borrado (`core/config.py`): por eso `vence_at` es NOT NULL.
--
-- Se guardan también las revocadas y las vencidas: la historia de por qué algo
-- estuvo en silencio es evidencia.
CREATE TABLE IF NOT EXISTS resource_clasificacion (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id  TEXT NOT NULL,
    host_id      TEXT NOT NULL,
    estado       TEXT NOT NULL,           -- IGNORED | MANAGED | EXTERNAL
    motivo       TEXT NOT NULL,
    quien        TEXT NOT NULL,           -- de donde vino: "telegram:<nombre>"
    at           TEXT NOT NULL,
    vence_at     TEXT NOT NULL,
    revocada_at  TEXT,
    revocada_por TEXT
);
CREATE INDEX IF NOT EXISTS ix_clasif_vigente
    ON resource_clasificacion (host_id, resource_id, revocada_at, vence_at);

CREATE TABLE IF NOT EXISTS expectation (
    expectation_id      TEXT NOT NULL,
    revision            INTEGER NOT NULL DEFAULT 1,
    resource_selector   TEXT NOT NULL,
    desired_json        TEXT NOT NULL,
    source              INTEGER NOT NULL,       -- ExpectationSource: menor gana
    severity_if_missing TEXT NOT NULL DEFAULT 'major',
    author              TEXT,
    reason              TEXT,
    valid_until         TEXT,
    source_hash         TEXT,
    source_at           TEXT,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (expectation_id, revision)
);

-- Una contradicción entre fuentes no se resuelve en silencio: se registra con
-- las dos evidencias y se pide reconciliación.
CREATE TABLE IF NOT EXISTS expectation_conflict (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id   TEXT NOT NULL,
    ganadora_json TEXT NOT NULL,
    perdedora_json TEXT NOT NULL,
    detected_at   TEXT NOT NULL,
    resolved_at   TEXT
);

-- ---------------------------------------------------------------------------
-- Controles y observaciones
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS check_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id       TEXT NOT NULL,
    check_id      TEXT NOT NULL,
    scheduled_at  TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    duration_ms   INTEGER,
    error         TEXT,
    -- Idempotencia: una corrida programada se ejecuta una sola vez aunque el
    -- proceso reinicie en el medio.
    UNIQUE (host_id, check_id, scheduled_at)
);

CREATE TABLE IF NOT EXISTS observation (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id       TEXT NOT NULL,
    source        TEXT NOT NULL,
    observed_at   TEXT NOT NULL,
    payload_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_observation_at ON observation (observed_at);

CREATE TABLE IF NOT EXISTS check_result (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id         TEXT NOT NULL,
    resource_id      TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    state            TEXT NOT NULL,
    severity         TEXT NOT NULL,
    expected_json    TEXT,
    actual_json      TEXT,
    reason_code      TEXT,
    evidence_id      INTEGER REFERENCES observation (id),
    evidence_quality TEXT NOT NULL DEFAULT 'FULL',
    rule_version     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_result_recurso ON check_result (resource_id, observed_at);
CREATE INDEX IF NOT EXISTS ix_result_check  ON check_result (check_id, observed_at);
-- Sin este indice, borrar una observacion obliga a SQLite a recorrer
-- check_result entero para verificar la FK, y lo hace POR CADA fila borrada.
-- En srv2 (2026-09-14) la retencion trababa el tick 30 s cada hora.
CREATE INDEX IF NOT EXISTS ix_result_evidencia ON check_result (evidence_id);

-- ---------------------------------------------------------------------------
-- Incidentes
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS incident (
    incident_id     TEXT PRIMARY KEY,
    fingerprint     TEXT NOT NULL,
    resource_id     TEXT NOT NULL,
    check_id        TEXT NOT NULL,
    opened_at       TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'OPEN',
    severity        TEXT NOT NULL,
    last_seen       TEXT,          -- ultima vez que se VIO el problema
    recovered_at    TEXT,          -- cuando dejo de verse (distinto de last_seen)
    closed_at       TEXT,
    cause_json      TEXT NOT NULL DEFAULT '{}',
    causa_raiz_de   TEXT REFERENCES incident (incident_id)
);
-- Un solo incidente abierto por fingerprint. Es lo que impide que un control que
-- corre cada 15 segundos abra 240 incidentes por hora.
CREATE UNIQUE INDEX IF NOT EXISTS ux_incident_abierto
    ON incident (fingerprint) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS incident_event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  TEXT NOT NULL REFERENCES incident (incident_id),
    at           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

-- ---------------------------------------------------------------------------
-- Trabajos programados
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS job_run (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key          TEXT NOT NULL,          -- "job:clientea:2"
    scheduled_at     TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    exit_code        INTEGER,
    duration_ms      INTEGER,
    outcome          TEXT,                   -- OK | MISSING_OUTCOME | UNKNOWN
    evidence_quality TEXT NOT NULL DEFAULT 'PARTIAL',
    UNIQUE (job_key, scheduled_at)
);
CREATE INDEX IF NOT EXISTS ix_jobrun_key ON job_run (job_key, scheduled_at);

-- ---------------------------------------------------------------------------
-- Línea base
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS baseline (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_key  TEXT NOT NULL,
    dow         INTEGER NOT NULL,      -- separar día de semana no es refinamiento:
    hour_bucket INTEGER NOT NULL,      -- el costo de un domingo es 20x menor que el de un lunes
    median      REAL NOT NULL,
    mad         REAL NOT NULL,         -- desviación absoluta mediana, no desvío estándar
    n           INTEGER NOT NULL,
    segment     TEXT NOT NULL DEFAULT 'default',   -- un cambio de versión abre segmento nuevo
    updated_at  TEXT NOT NULL,
    UNIQUE (metric_key, dow, hour_bucket, segment)
);

-- ---------------------------------------------------------------------------
-- Mantenimiento y silencios
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS maintenance_window (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    selector    TEXT NOT NULL,
    starts_at   TEXT NOT NULL,
    ends_at     TEXT NOT NULL,
    author      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    version     TEXT
);

CREATE TABLE IF NOT EXISTS suppression (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    selector    TEXT NOT NULL,
    until       TEXT NOT NULL,     -- siempre vence: un silencio eterno es un control borrado
    author      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Correo
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    report_type     TEXT NOT NULL,        -- informe | urgente
    period_start    TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|SENT|FAILED
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    message_id      TEXT,                 -- se reusa en los reintentos: sin esto, duplica
    recipients_json TEXT NOT NULL DEFAULT '[]',
    subject         TEXT,
    body_html       TEXT,
    created_at      TEXT NOT NULL,
    sent_at         TEXT,
    smtp_response   TEXT,
    last_error      TEXT,
    -- La clave del asunto: un período se manda una sola vez. Reiniciar el
    -- servicio en medio de un envío no puede producir dos correos.
    UNIQUE (report_type, period_start)
);
CREATE INDEX IF NOT EXISTS ix_outbox_pendiente ON outbox (state, next_attempt_at);

-- ---------------------------------------------------------------------------
-- Copia local de la configuración de negocio
-- ---------------------------------------------------------------------------

-- La copia local de la configuracion se saco el 2026-09-02. El respaldo de
-- cuando la base no responde ahora es un ARCHIVO declarado (`config/base.yaml`,
-- via `respaldo:` en app.yaml): se versiona, se lee de un `cat` y no se llena
-- solo. El motivo completo esta en el docstring de `sysop/core/fuentes.py`.
--
-- El DROP no es cosmetico y por eso corre en cada arranque: la tabla guardaba
-- la password SMTP y la API key del modelo EN CLARO, ademas de claves de
-- diseños anteriores que ya nadie leia. Dejar de escribirla no las borra.
DROP TABLE IF EXISTS config_cache;

-- ---------------------------------------------------------------------------
-- Auditoría
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

-- ---------------------------------------------------------------------------
-- Cursores y estado interno
-- ---------------------------------------------------------------------------

-- Watermarks del journal y de las tablas de negocio. Es lo que hace que los
-- controles lean deltas y no tablas enteras.
CREATE TABLE IF NOT EXISTS cursor (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_version (version, applied_at)
     VALUES (1, datetime('now'));

-- ---------------------------------------------------------------------------
-- Diagnostico asistido por modelo
-- ---------------------------------------------------------------------------
-- Un diagnostico por incidente. El modelo NUNCA fija un estado: dice si el
-- problema le parece real y cual es la causa probable. La severidad y el estado
-- los sigue decidiendo el plano deterministico.
CREATE TABLE IF NOT EXISTS diagnostico (
    incident_id  TEXT PRIMARY KEY REFERENCES incident (incident_id),
    at           TEXT NOT NULL,
    es_real      INTEGER NOT NULL,      -- 0 = el modelo lo da por falso positivo
    confianza    TEXT,                  -- alta | media | baja
    causa        TEXT,
    accion       TEXT,
    descarte     TEXT,                  -- por que lo descarta, si es_real = 0
    modelo       TEXT,
    filtrado     INTEGER NOT NULL DEFAULT 1   -- 0 = paso sin filtro por critico
);

-- Cada llamada al modelo, para poder responder cuanto sale por dia y por
-- incidente sin depender de la tabla de la plataforma (a la que el observador
-- solo tiene lectura).
CREATE TABLE IF NOT EXISTS llm_call (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    incident_id TEXT,
    modelo      TEXT,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    costo_usd   REAL NOT NULL DEFAULT 0,
    ms          INTEGER,
    ok          INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_llm_call_at ON llm_call (at);

-- ---------------------------------------------------------------------------
-- Revalidacion antes del informe
-- ---------------------------------------------------------------------------
-- El diagnostico de arriba se hace al ABRIR y no se vuelve a mirar. Esta tabla
-- guarda la ultima revalidacion de cada incidente: la que se hace antes de
-- cada informe, mirando el servidor en ese momento.
--
-- SIN FK a `incident`, igual que `llm_call`. Una FK sin `ON DELETE` fue la que
-- trabo la retencion en srv2 (2026-09-14): lo que no hace falta enlazar, no se
-- enlaza.
CREATE TABLE IF NOT EXISTS revalidacion (
    incident_id    TEXT PRIMARY KEY,
    at             TEXT NOT NULL,
    veredicto      TEXT NOT NULL,   -- real | falsa_alarma | arreglado_pendiente | no_se_pudo_validar
    confianza      TEXT,
    causa          TEXT,
    accion         TEXT,
    motivo         TEXT,
    nota           TEXT,            -- lo que el codigo le corrigio a la respuesta
    evidencia_json TEXT NOT NULL DEFAULT '[]',
    -- Solo arreglado_pendiente. Se fija la PRIMERA vez y no se corre: si en
    -- cada informe se recalculara "la proxima corrida", nunca llegaria.
    pendiente_desde TEXT,
    confirma_en    TEXT,
    modelo         TEXT
);

-- "Ya lo arregle", dicho por una persona por Telegram.
--
-- No silencia: saca el incidente de la lista principal HASTA `confirma_en`.
-- Si para entonces sigue abierto, vuelve a la lista con la marca a la vista.
-- Se guardan las revocadas: quien dijo que estaba arreglado es evidencia.
CREATE TABLE IF NOT EXISTS marca_arreglado (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  TEXT NOT NULL,
    motivo       TEXT NOT NULL,
    quien        TEXT NOT NULL,
    at           TEXT NOT NULL,
    confirma_en  TEXT NOT NULL,
    revocada_at  TEXT,
    revocada_por TEXT
);
CREATE INDEX IF NOT EXISTS ix_marca_incidente
    ON marca_arreglado (incident_id, revocada_at);
