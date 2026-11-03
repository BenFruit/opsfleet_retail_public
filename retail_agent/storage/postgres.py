"""One place that knows how to connect to the application PostgreSQL.

Reports, user memory and the Golden index share this database. Each feature
receives a connection factory instead of reaching into another feature's
repository. Failures surface as StorageError with a fixed message: driver
errors may contain connection details and are never shown or logged.
"""

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from retail_agent.settings import Settings


class StorageError(RuntimeError):
    """Controlled storage failure; the outcome of a write is unknown."""


STORAGE_UNAVAILABLE = "Application storage is unavailable or the operation failed. No success is confirmed."


def connection_factory(
    settings: Settings, *, user: str | None = None, password: str | None = None, statement_timeout_ms: int = 5000
):
    """Return a context-manager factory yielding a dict-row connection.

    The chat role comes from settings; the Golden ingestion job passes its own
    writer role through user/password.
    """
    params = {
        "host": settings.reports_postgres_host,
        "port": settings.reports_postgres_port,
        "dbname": settings.reports_postgres_database,
        "user": user or settings.reports_postgres_user,
        "password": password or settings.reports_postgres_password.get_secret_value(),
    }

    @contextmanager
    def connection():
        try:
            with psycopg.connect(
                **params,
                connect_timeout=3,
                options=f"-c statement_timeout={statement_timeout_ms} -c timezone=UTC",
                row_factory=dict_row,
            ) as conn:
                yield conn
        except (psycopg.Error, ValueError):
            raise StorageError(STORAGE_UNAVAILABLE) from None

    return connection


def golden_ingest_connection_factory(settings: Settings):
    """Writer role for the offline Golden ingestion job; the chat agent only reads."""
    return connection_factory(
        settings,
        user=settings.golden_ingest_postgres_user,
        password=settings.golden_ingest_postgres_password.get_secret_value(),
        statement_timeout_ms=15000,
    )
