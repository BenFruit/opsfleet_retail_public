"""Only parameterized library operations; this is NOT an LLM SQL tool."""

from contextlib import contextmanager

from retail_agent.llm.embeddings import vector_literal
from retail_agent.reports.models import MAX_DELETE_IDS, DeleteScope, ReportError, ReportRequest
from retail_agent.storage.postgres import StorageError

_ACCESS = "owner_id = %s AND brand_scope <@ %s::text[] AND cardinality(brand_scope) > 0"
_SUMMARY = "report_id, title, conversation_id, created_at"
_CONTAINS_QUERY = "strpos(lower(title || ' ' || content), lower(%s)) > 0"

MAX_SEARCH_RESULTS = 10
MAX_LISTED = 20

# Deletion scope -> extra WHERE clause and the value bound to it.
_DELETE_FILTERS = {
    DeleteScope.MATCHING: (f" AND {_CONTAINS_QUERY}", lambda request, conversation_id: request.query),
    DeleteScope.CONVERSATION: (" AND conversation_id = %s", lambda request, conversation_id: conversation_id),
    DeleteScope.ID: (" AND report_id = %s", lambda request, conversation_id: request.report_id),
    DeleteScope.ALL: ("", None),
}


class ReportRepository:
    def __init__(self, connection_factory):
        self._connect = connection_factory

    @contextmanager
    def connection(self):
        """Storage failures become ReportError so the library keeps one error contract."""
        try:
            with self._connect() as conn:
                yield conn
        except StorageError:
            raise ReportError(
                "Report storage is unavailable or an operation failed. No success is confirmed; check the library before retrying a write."
            ) from None

    def save(self, *, report_id, owner, brands, conversation_id, title, content, vector, model):
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO saved_reports (report_id, owner_id, conversation_id, title, content, embedding, embedding_model, brand_scope) "
                "VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s)",
                (report_id, owner, conversation_id, title, content, vector_literal(vector), model, brands),
            )

    def search(self, *, owner, brands, query, vector, model):
        with self.connection() as conn:
            return conn.execute(
                f"SELECT {_SUMMARY}, content, 1 - (embedding <=> %s::vector) AS similarity FROM saved_reports "
                f"WHERE {_ACCESS} AND embedding_model = %s AND embedding IS NOT NULL "
                f"ORDER BY ({_CONTAINS_QUERY}) DESC, embedding <=> %s::vector, report_id LIMIT %s",
                (vector_literal(vector), owner, brands, model, query, vector_literal(vector), MAX_SEARCH_RESULTS),
            ).fetchall()

    def list(self, *, owner, brands):
        with self.connection() as conn:
            return conn.execute(
                f"SELECT {_SUMMARY} FROM saved_reports WHERE {_ACCESS} ORDER BY created_at DESC, report_id LIMIT %s",
                (owner, brands, MAX_LISTED),
            ).fetchall()

    def open(self, report_id, *, owner, brands):
        with self.connection() as conn:
            return conn.execute(
                f"SELECT {_SUMMARY}, content FROM saved_reports WHERE {_ACCESS} AND report_id = %s",
                (owner, brands, report_id),
            ).fetchone()

    def preview_delete(self, request: ReportRequest, *, owner, brands, conversation_id):
        """Up to MAX_DELETE_IDS + 1 rows: one extra row tells the caller the set is too large."""
        clause, value_for = _DELETE_FILTERS[request.scope]
        values = [value_for(request, conversation_id)] if value_for else []
        with self.connection() as conn:
            # Fail rather than silently preview/delete a partial match set.
            return conn.execute(
                f"SELECT {_SUMMARY} FROM saved_reports WHERE {_ACCESS}{clause} ORDER BY report_id LIMIT %s",
                (owner, brands, *values, MAX_DELETE_IDS + 1),
            ).fetchall()

    def delete(self, ids: tuple[str, ...], *, owner, brands):
        with self.connection() as conn:
            rows = conn.execute(
                f"DELETE FROM saved_reports WHERE {_ACCESS} AND report_id = ANY(%s::text[]) RETURNING report_id",
                (owner, brands, list(ids)),
            ).fetchall()
            return len(rows)
