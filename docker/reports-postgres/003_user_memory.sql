-- Idempotent migration for the same application DB; separate from Langfuse.
CREATE TABLE IF NOT EXISTS user_memories (
    memory_id VARCHAR(32) COLLATE "C" PRIMARY KEY,
    owner_id VARCHAR(128) COLLATE "C" NOT NULL,
    content VARCHAR(200) NOT NULL,
    normalized_key VARCHAR(64) NOT NULL,
    embedding vector(768) NOT NULL,
    embedding_model TEXT NOT NULL,
    source_turn_id VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner_id, normalized_key)
);
GRANT SELECT, INSERT, DELETE ON user_memories TO retail_agent;
