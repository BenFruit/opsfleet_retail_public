-- Runs once when initializing a new reports_postgres_data volume.
-- This is a separate server/database from the PostgreSQL owned by Langfuse.
CREATE ROLE retail_agent LOGIN PASSWORD 'local-reports-demo';

REVOKE ALL ON DATABASE retail_agent FROM PUBLIC;
GRANT CONNECT ON DATABASE retail_agent TO retail_agent;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO retail_agent;

CREATE TABLE saved_reports (
    report_id VARCHAR(32) COLLATE "C" PRIMARY KEY,
    owner_id VARCHAR(128) COLLATE "C" NOT NULL,
    conversation_id VARCHAR(32) COLLATE "C" NOT NULL,
    title VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX reports_owner_created ON saved_reports (owner_id, created_at);
CREATE INDEX reports_owner_conversation ON saved_reports (owner_id, conversation_id, created_at);

-- The CLI can manage rows, but cannot create/drop tables or manage roles.
GRANT SELECT, INSERT, UPDATE, DELETE ON saved_reports TO retail_agent;
