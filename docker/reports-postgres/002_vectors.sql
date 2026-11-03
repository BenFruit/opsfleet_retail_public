-- Idempotent migration; run explicitly as postgres on an existing volume.
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE saved_reports ADD COLUMN IF NOT EXISTS embedding vector(768);
ALTER TABLE saved_reports ADD COLUMN IF NOT EXISTS embedding_model TEXT;
ALTER TABLE saved_reports ADD COLUMN IF NOT EXISTS brand_scope TEXT[];
-- Exact owner-filtered search is sufficient for the small MVP library.
-- Legacy rows without brand provenance are not exposed by the application.
