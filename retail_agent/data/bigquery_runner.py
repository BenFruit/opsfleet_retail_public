from contextlib import suppress
from dataclasses import dataclass, field
from itertools import islice
from typing import Any

from google.cloud import bigquery


@dataclass(frozen=True)
class QueryExecution:
    """Internal bounded result; not yet approved for delivery to an LLM."""

    rows: tuple[dict[str, Any], ...] = field(repr=False)
    truncated: bool
    bytes_processed: int | None = None
    bytes_billed: int | None = None


def brand_parameters(allowed_brands: tuple[str, ...]) -> list:
    """The trusted brand list as the @allowed_brands query parameter used by scoped SQL."""
    if not allowed_brands or any(not isinstance(brand, str) or not brand.strip() for brand in allowed_brands):
        raise PermissionError("Nonempty trusted brand entitlements are required")
    return [bigquery.ArrayQueryParameter("allowed_brands", "STRING", list(allowed_brands))]


class BigQueryRunner:
    """Thin BigQuery adapter: scoped dry run, bounded execution, table schema.

    Only QueryService calls it, after validation and brand scoping. There is
    deliberately no method that runs arbitrary SQL. Errors propagate unchanged;
    callers translate them into safe codes (no exception text is logged here).
    """

    def __init__(
        self, project_id: str | None = None, dataset_id: str = "bigquery-public-data.thelook_ecommerce"
    ) -> None:
        self.client = bigquery.Client(project=project_id)
        self.dataset_id = dataset_id

    def dry_run(
        self,
        sql: str,
        *,
        allowed_brands: tuple[str, ...],
        api_timeout_seconds: float,
    ) -> int | None:
        """Estimate the SAME scoped SQL and parameters that will be executed."""
        config = bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,
            use_legacy_sql=False,
            query_parameters=brand_parameters(allowed_brands),
        )
        job = self.client.query(
            sql,
            job_config=config,
            timeout=api_timeout_seconds,
            retry=None,
            job_retry=None,
        )
        return job.total_bytes_processed

    def execute_scoped_query(
        self,
        sql: str,
        *,
        allowed_brands: tuple[str, ...],
        maximum_bytes_billed: int,
        max_rows: int,
        timeout_seconds: float,
        api_timeout_seconds: float,
        query_id: str,
    ) -> QueryExecution:
        """Internal QueryService adapter, not an independently safe SQL tool.

        No pandas conversion or unbounded result materialization. Job timeout
        and cancellation are best-effort server controls, not hard billing or
        wall-clock guarantees. No automatic query resubmission is enabled.
        """
        config = bigquery.QueryJobConfig(
            use_legacy_sql=False,
            use_query_cache=False,
            query_parameters=brand_parameters(allowed_brands),
            maximum_bytes_billed=maximum_bytes_billed,
            job_timeout_ms=max(1, int(timeout_seconds * 1000)),
            labels={"application": "retail-agent"},
        )
        # Known ID allows best-effort cancellation even if submission times out.
        job_id = f"retail_{query_id}"
        try:
            job = self.client.query(
                sql,
                job_config=config,
                job_id=job_id,
                timeout=api_timeout_seconds,
                retry=None,
                job_retry=None,
            )
            result = job.result(
                timeout=timeout_seconds,
                max_results=max_rows + 1,
                page_size=min(max_rows + 1, 100),
                retry=None,
                job_retry=None,
            )
            names = [column.name for column in result.schema]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate output column names are unsupported")
            rows = tuple(dict(row.items()) for row in islice(result, max_rows + 1))
            return QueryExecution(
                rows=rows[:max_rows],
                truncated=len(rows) > max_rows,
                bytes_processed=job.total_bytes_processed,
                bytes_billed=job.total_bytes_billed,
            )
        except Exception:
            # Do not log raw SQL, data, or BigQuery exception text here.
            # Cancellation failure must not hide the original error.
            with suppress(Exception):
                self.client.cancel_job(job_id, timeout=api_timeout_seconds, retry=None)
            raise

    def get_table_schema(self, table_name: str) -> list[dict[str, Any]]:
        """Column name, type, mode and description of one dataset table."""
        table = self.client.get_table(f"{self.dataset_id}.{table_name}")
        return [
            {
                "name": column.name,
                "type": column.field_type,
                "mode": column.mode,
                "description": column.description or "",
            }
            for column in table.schema
        ]
