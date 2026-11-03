-- Idempotent migration: Golden Knowledge retrieval index.
-- golden/trios/*.json is the source of truth (the "bucket"); this table is a
-- rebuildable index of APPROVED trios only. Withdrawn trios are removed.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS golden_trios (
    trio_id VARCHAR(64) COLLATE "C" PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version >= 1),
    content_hash CHAR(64) NOT NULL,
    document JSONB NOT NULL,
    search_text TEXT NOT NULL,
    search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', search_text)) STORED,
    embedding vector(768) NOT NULL,
    embedding_model TEXT NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS golden_trios_search_tsv ON golden_trios USING GIN (search_tsv);

-- Separate writer: the chat agent can only READ the index, so a compromised or
-- confused agent cannot rewrite expert knowledge. Demo password, localhost only.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'golden_ingest') THEN
        CREATE ROLE golden_ingest LOGIN PASSWORD 'local-golden-ingest-demo';
    END IF;
END
$$;
GRANT CONNECT ON DATABASE retail_agent TO golden_ingest;
GRANT USAGE ON SCHEMA public TO golden_ingest;
GRANT SELECT, INSERT, UPDATE, DELETE ON golden_trios TO golden_ingest;
GRANT SELECT ON golden_trios TO retail_agent;
