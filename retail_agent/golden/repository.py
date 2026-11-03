"""Parameterized access to the Golden Knowledge index; never an LLM SQL tool."""

from psycopg.types.json import Jsonb

from retail_agent.golden.models import GoldenTrio
from retail_agent.llm.embeddings import vector_literal


class GoldenRepository:
    def __init__(self, connection_factory):
        self.connection = connection_factory

    def search(self, *, vector: list[float], query_text: str, model: str, limit: int = 10) -> list[dict]:
        """Vector candidates with a lexical OR-match rank for hybrid re-scoring."""
        literal = vector_literal(vector)
        with self.connection() as conn:
            return conn.execute(
                "SELECT trio_id, version, document, 1 - (embedding <=> %s::vector) AS similarity, "
                "ts_rank(search_tsv, NULLIF(replace(plainto_tsquery('english', %s)::text, '&', '|'), '')::tsquery) AS lexical "
                "FROM golden_trios WHERE embedding_model = %s "
                "ORDER BY embedding <=> %s::vector, trio_id LIMIT %s",
                (literal, query_text, model, literal, limit),
            ).fetchall()

    def indexed_hashes(self) -> dict[str, tuple[str, str]]:
        """trio_id -> (content_hash, embedding_model) of what is currently indexed."""
        with self.connection() as conn:
            rows = conn.execute("SELECT trio_id, content_hash, embedding_model FROM golden_trios").fetchall()
        return {row["trio_id"]: (row["content_hash"], row["embedding_model"]) for row in rows}

    def publish(self, *, upserts: list[tuple[GoldenTrio, list[float]]], remove: list[str], model: str) -> None:
        """Apply one ingestion run atomically: readers see the old or the new index, never a mix."""
        with self.connection() as conn, conn.transaction():
            for trio, vector in upserts:
                conn.execute(
                    "INSERT INTO golden_trios (trio_id, version, content_hash, document, search_text, embedding, embedding_model) "
                    "VALUES (%s, %s, %s, %s, %s, %s::vector, %s) "
                    "ON CONFLICT (trio_id) DO UPDATE SET version = EXCLUDED.version, content_hash = EXCLUDED.content_hash, "
                    "document = EXCLUDED.document, search_text = EXCLUDED.search_text, embedding = EXCLUDED.embedding, "
                    "embedding_model = EXCLUDED.embedding_model, ingested_at = CURRENT_TIMESTAMP",
                    (
                        trio.id,
                        trio.version,
                        trio.content_hash(),
                        Jsonb(trio.model_dump(mode="json")),
                        trio.search_text(),
                        vector_literal(vector),
                        model,
                    ),
                )
            if remove:
                conn.execute("DELETE FROM golden_trios WHERE trio_id = ANY(%s::text[])", (remove,))
