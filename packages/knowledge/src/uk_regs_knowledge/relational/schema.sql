-- ============================================================================
-- UK Regs Pipeline — PostgreSQL + pgvector schema
-- ============================================================================
-- Idempotent: safe to re-run. Application connects as the `ukregs` role.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---- Documents & versions --------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    doc_id      TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    publisher   TEXT NOT NULL DEFAULT 'gov.uk',
    doc_type    TEXT NOT NULL,
    source_url  TEXT,
    sha256      TEXT NOT NULL,
    license     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_versions (
    doc_id          TEXT NOT NULL REFERENCES documents(doc_id),
    version         TEXT NOT NULL,
    published_date  DATE NOT NULL,
    effective_from  DATE,
    object_key      TEXT NOT NULL,
    pages           INT,
    PRIMARY KEY (doc_id, version)
);

-- ---- Rules (bitemporal) ----------------------------------------------------
CREATE TABLE IF NOT EXISTS rules (
    rule_id            TEXT PRIMARY KEY,
    doc_id             TEXT NOT NULL REFERENCES documents(doc_id),
    version            TEXT NOT NULL,
    section_path       TEXT NOT NULL,
    category           TEXT NOT NULL,
    title              TEXT,
    text               TEXT NOT NULL,
    normalized_form    TEXT NOT NULL,
    parameters         JSONB NOT NULL DEFAULT '{}'::jsonb,
    references         TEXT[] NOT NULL DEFAULT '{}',
    valid_from         DATE NOT NULL,
    valid_to           DATE,
    recorded_at        TIMESTAMPTZ NOT NULL,
    status             TEXT NOT NULL,
    -- citation (denormalised for fast retrieval)
    citation_doc_id    TEXT NOT NULL,
    citation_version   TEXT NOT NULL,
    citation_page      INT NOT NULL,
    citation_raw_text  TEXT NOT NULL,
    citation_url       TEXT,
    -- QA
    confidence         REAL NOT NULL,
    extraction_method  TEXT NOT NULL,
    -- semantic vector for the rule text
    embedding          vector(1536)
);

CREATE INDEX IF NOT EXISTS idx_rules_status_valid ON rules (status, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_rules_section       ON rules (section_path);
CREATE INDEX IF NOT EXISTS idx_rules_category      ON rules (category);
CREATE INDEX IF NOT EXISTS idx_rules_refs          ON rules USING GIN (references);
CREATE INDEX IF NOT EXISTS idx_rules_embedding     ON rules USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ---- Figures ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS figures (
    figure_id          TEXT PRIMARY KEY,
    doc_id             TEXT NOT NULL REFERENCES documents(doc_id),
    version            TEXT NOT NULL,
    page               INT NOT NULL,
    caption            TEXT,
    image_key          TEXT NOT NULL,
    description        TEXT NOT NULL,
    elements           JSONB NOT NULL DEFAULT '[]'::jsonb,
    illustrates_rules  TEXT[] NOT NULL DEFAULT '{}',
    citation_page      INT NOT NULL,
    citation_raw_text  TEXT
);

CREATE INDEX IF NOT EXISTS idx_figs_rules ON figures USING GIN (illustrates_rules);

-- ---- ChangeSets & deltas ---------------------------------------------------
CREATE TABLE IF NOT EXISTS change_sets (
    change_set_id  TEXT PRIMARY KEY,
    doc_id         TEXT NOT NULL,
    version        TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL,
    status         TEXT NOT NULL,
    reviewed_by    TEXT,
    reviewed_at    TIMESTAMPTZ,
    review_notes   TEXT
);

CREATE TABLE IF NOT EXISTS rule_deltas (
    change_set_id    TEXT NOT NULL REFERENCES change_sets(change_set_id),
    new_rule_id      TEXT NOT NULL,
    matched_rule_id  TEXT,
    decision         TEXT NOT NULL,
    confidence       REAL NOT NULL,
    rationale        TEXT NOT NULL,
    diff_summary     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (change_set_id, new_rule_id)
);

-- ---- Supersession edges (also in Neo4j; mirrored here for SQL queries) -----
CREATE TABLE IF NOT EXISTS supersessions (
    old_rule_id   TEXT NOT NULL REFERENCES rules(rule_id),
    new_rule_id   TEXT NOT NULL REFERENCES rules(rule_id),
    change_set_id TEXT NOT NULL REFERENCES change_sets(change_set_id),
    reason        TEXT,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (old_rule_id, new_rule_id)
);

-- ---- Audit log -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_events (
    id        BIGSERIAL PRIMARY KEY,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    entity    TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload   JSONB NOT NULL DEFAULT '{}'::jsonb,
    at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---- Source monitoring (auto-ingestion) ------------------------------------
-- Tracks which documents the sourcing monitor has already seen/ingested, per
-- source. Enables diffing so only new/changed PDFs are re-ingested.
CREATE TABLE IF NOT EXISTS source_documents (
    source_id        TEXT NOT NULL,
    document_id      TEXT NOT NULL,
    version          TEXT NOT NULL,
    title            TEXT NOT NULL,
    url              TEXT NOT NULL,
    sha256           TEXT,
    size_bytes       BIGINT,
    published_at     TIMESTAMPTZ,
    first_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status           TEXT NOT NULL DEFAULT 'ingested',
    PRIMARY KEY (source_id, document_id)
);

CREATE INDEX IF NOT EXISTS idx_source_documents_status ON source_documents (status);

-- Per-source poll timestamps (for throttle/throttle decisions).
CREATE TABLE IF NOT EXISTS source_poll_state (
    source_id        TEXT PRIMARY KEY,
    last_polled_at   TIMESTAMPTZ NOT NULL,
    last_status      TEXT,
    last_error       TEXT,
    last_ingested    INT NOT NULL DEFAULT 0
);
