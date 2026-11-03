"""Trusted, bounded query path. Results still require a pre-LLM result guard."""

import json
import logging
import math
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import monotonic
from typing import Any
from uuid import uuid4

from retail_agent.data.bigquery_runner import BigQueryRunner
from retail_agent.data.errors import classify_backend_error
from retail_agent.sql.scope import scope_query
from retail_agent.sql.validator import SQLValidationError, validate_query

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueryLimits:
    maximum_bytes_billed: int = 100_000_000
    max_rows: int = 200
    max_result_bytes: int = 256_000
    max_sql_chars: int = 20_000
    timeout_seconds: float = 30.0
    api_timeout_seconds: float = 10.0
    minimum_cohort_size: int = 5

    def __post_init__(self):
        if type(self.minimum_cohort_size) is not int or self.minimum_cohort_size < 2:
            raise ValueError("minimum_cohort_size must be an integer of at least 2")
        for name in ("maximum_bytes_billed", "max_rows", "max_result_bytes", "max_sql_chars"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("timeout_seconds", "api_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")


class QueryServiceError(Exception):
    """Stable, safe error contract; never includes SQL/backend exception text."""

    def __init__(self, code: str, message: str, *, stage: str = "unknown", query_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.query_id = query_id


@dataclass(frozen=True)
class QueryResult:
    """Internal data, NOT a promise that rows are safe to send to an LLM."""

    query_id: str
    rows: tuple[dict[str, Any], ...] = field(repr=False)
    truncated: bool
    estimated_bytes: int
    bytes_processed: int | None
    bytes_billed: int | None
    minimum_cohort_size: int | None = None
    columns: tuple[str, ...] = ()
    query_context: dict = field(default_factory=dict, repr=False)


_DEMOGRAPHIC_POLICY = (
    "Restricted aggregate SQL; AVG(age) is purchase-weighted with purchase joins. Group only at the "
    "requested grain; never invent re-binned totals from suppressed/truncated rows."
)


@dataclass(frozen=True)
class _ValidatedQuery:
    scoped_sql: str
    columns: tuple[str, ...]
    context: dict
    demographic: bool


class _QueryRun:
    """Correlation, timing and stage-tagged failure handling for one execute() call."""

    def __init__(self, turn_id: str | None):
        self.query_id = uuid4().hex
        self.started = monotonic()
        # turn_id links every query event to the agent turn in logs/agent.jsonl.
        self.correlation = {"query_id": self.query_id, "turn_id": turn_id}
        logger.info("query_started", extra=self.correlation)

    def duration_ms(self) -> int:
        return round((monotonic() - self.started) * 1000)

    @contextmanager
    def stage(self, name: str):
        """Tag failures with the stage; turn backend exceptions into safe codes."""
        try:
            yield
        except QueryServiceError as error:
            error.stage, error.query_id = name, self.query_id
            self._log_failure(name, error.code, validation_reason=str(error) if name == "validation" else None)
            raise
        except Exception as error:
            code, message = classify_backend_error(error)
            self._log_failure(name, code, error_type=type(error).__name__)
            raise QueryServiceError(code, message, stage=name, query_id=self.query_id) from None

    def _log_failure(
        self, stage: str, code: str, *, validation_reason: str | None = None, error_type: str | None = None
    ) -> None:
        extra = {
            **self.correlation,
            "stage": stage,
            "error_code": code,
            "duration_ms": self.duration_ms(),
            "validation_reason": validation_reason,
        }
        if error_type is not None:
            extra["error_type"] = error_type  # Exception class name only, never its text.
        logger.warning("query_failed", extra=extra)


class QueryService:
    def __init__(self, runner: BigQueryRunner, limits: QueryLimits | None = None):
        self.runner = runner
        self.limits = limits if limits is not None else QueryLimits()

    def execute(
        self,
        sql: str,
        *,
        allowed_brands,
        scan_budget_bytes: int | None = None,
        turn_id: str | None = None,
    ) -> QueryResult:
        """Run model SQL within the caller's trusted brand entitlements.

        allowed_brands comes from the application's identity/entitlements,
        never from the model. QueryService does not know about managers.

        Errors are safe for caller-controlled handling and carry the failed
        stage (authorization, validation, dry_run, execution, result_bounds).
        No retries or model calls occur here. The orchestrator must enforce an
        additional per-session query/retry/cost budget. The limits apply to
        ONE request only.
        """
        run = _QueryRun(turn_id)
        with run.stage("authorization"):
            byte_limit = self._byte_limit(scan_budget_bytes)
            brands = _authorized_brands(allowed_brands)
        with run.stage("validation"):
            query = self._validate(sql)
        with run.stage("dry_run"):
            estimated_bytes = self._dry_run(query.scoped_sql, brands, byte_limit)
            logger.info("query_dry_run_completed", extra={**run.correlation, "estimated_bytes": estimated_bytes})
        with run.stage("execution"):
            execution = self.runner.execute_scoped_query(
                query.scoped_sql,
                allowed_brands=brands,
                maximum_bytes_billed=byte_limit,
                max_rows=self.limits.max_rows,
                timeout_seconds=self.limits.timeout_seconds,
                api_timeout_seconds=self.limits.api_timeout_seconds,
                query_id=run.query_id,
            )
        with run.stage("result_bounds"):
            size = self._result_size(execution.rows)

        result = QueryResult(
            query_id=run.query_id,
            rows=execution.rows,
            truncated=execution.truncated,
            estimated_bytes=estimated_bytes,
            bytes_processed=execution.bytes_processed,
            bytes_billed=execution.bytes_billed,
            minimum_cohort_size=self.limits.minimum_cohort_size if query.demographic else None,
            columns=query.columns,
            query_context=query.context,
        )
        logger.info(
            "query_completed",
            extra={
                **run.correlation,
                "row_count": len(result.rows),
                "truncated": result.truncated,
                "result_bytes": size,
                "bytes_processed": result.bytes_processed,
                "bytes_billed": result.bytes_billed,
                "duration_ms": run.duration_ms(),
            },
        )
        return result

    def _byte_limit(self, scan_budget_bytes: int | None) -> int:
        """Per-query cap, lowered to what remains of the question's scan budget."""
        if scan_budget_bytes is None:
            return self.limits.maximum_bytes_billed
        if type(scan_budget_bytes) is not int or scan_budget_bytes <= 0:
            raise QueryServiceError("cost_limit", "The question scan budget is exhausted.")
        return min(self.limits.maximum_bytes_billed, scan_budget_bytes)

    def _validate(self, sql: str) -> _ValidatedQuery:
        """Safety policy check, then the trusted brand (and cohort) scope rewrite."""
        if not isinstance(sql, str) or len(sql) > self.limits.max_sql_chars:
            raise QueryServiceError("invalid_sql", "SQL is missing or exceeds the supported size.")
        try:
            tree = validate_query(sql)
            demographic = bool(tree.meta.get("demographic_subject"))
            columns = tuple(tree.named_selects)
            context = {"tool": "sql", "executed_sql": tree.sql(dialect="bigquery")}
            if demographic:
                context["demographic_policy"] = _DEMOGRAPHIC_POLICY
            # Read columns and context first: scoping rewrites the tree in place.
            scoped_sql = scope_query(tree, minimum_cohort_size=self.limits.minimum_cohort_size)
        except SQLValidationError as error:
            raise QueryServiceError("invalid_sql", f"SQL rejected: {error}") from None
        except (ValueError, RecursionError):
            raise QueryServiceError("invalid_sql", "SQL is invalid or outside the supported safety policy.") from None
        return _ValidatedQuery(scoped_sql, columns, context, demographic)

    def _dry_run(self, scoped_sql: str, brands: tuple[str, ...], byte_limit: int) -> int:
        """Estimate the exact SQL and parameters that will run; refuse over-budget scans."""
        estimated_bytes = self.runner.dry_run(
            scoped_sql,
            allowed_brands=brands,
            api_timeout_seconds=self.limits.api_timeout_seconds,
        )
        if type(estimated_bytes) is not int or estimated_bytes < 0:
            raise QueryServiceError("estimate_unavailable", "Could not verify the query scan estimate.")
        if estimated_bytes > byte_limit:
            raise QueryServiceError("cost_limit", "Query exceeds the configured scan budget.")
        return estimated_bytes

    def _result_size(self, rows: tuple[dict, ...]) -> int:
        """Serialized size in bytes, after checking row and size limits."""
        if len(rows) > self.limits.max_rows:
            raise QueryServiceError("result_limit", "Query returned too many rows.")
        try:
            # BQ dates/decimals use their string form for this size estimate.
            size = len(json.dumps(rows, default=str, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            raise QueryServiceError("invalid_result", "Query result has an unsupported format.") from None
        if size > self.limits.max_result_bytes:
            raise QueryServiceError("result_limit", "Query result exceeds the configured response budget.")
        return size


def _authorized_brands(allowed_brands) -> tuple[str, ...]:
    """Trusted entitlements as a sorted tuple; a bare string or blank brand is refused."""
    if (
        isinstance(allowed_brands, str)
        or not allowed_brands
        or any(not isinstance(brand, str) or not brand.strip() for brand in allowed_brands)
    ):
        raise QueryServiceError("access_denied", "No authorized brands for this user.")
    return tuple(sorted(allowed_brands))
