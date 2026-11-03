"""The last data boundary before observations reach Gemini."""

import json
import math
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

from retail_agent.observations import ObservationStatus
from retail_agent.safety.errors import ResultGuardError
from retail_agent.safety.sensitive_text import check_text
from retail_agent.sql.demographics import COHORT_COLUMN
from retail_agent.sql.validator import SQLValidationError, validate_query

if TYPE_CHECKING:  # Type only: the guard does not depend on the data layer at runtime.
    from retail_agent.data.query_service import QueryResult


def _scalar(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, (float, Decimal)):
        if not math.isfinite(value):
            raise ResultGuardError("Nonfinite numbers are unsupported")
        return str(value) if isinstance(value, Decimal) else value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, str):
        return check_text(value)
    raise ResultGuardError("Only scalar query results are supported")


def make_observation(sql: str, result: "QueryResult", *, max_chars: int) -> dict:
    """Service-owned columns/context are not supplied by the model as proof."""
    minimum = result.minimum_cohort_size
    if minimum is not None and (type(minimum) is not int or minimum < 2 or not result.columns):
        raise ResultGuardError("Demographic results require trusted columns and cohort policy")
    try:
        columns = result.columns or tuple(validate_query(sql).named_selects)
    except SQLValidationError:
        raise ResultGuardError("Query output metadata cannot be verified") from None
    if len(columns) != len(set(columns)):
        raise ResultGuardError("Output column names must be unique")
    for name in columns:
        check_text(name)
    expected = set(columns) | ({COHORT_COLUMN} if minimum is not None else set())
    context = result.query_context
    check_text(json.dumps(context, ensure_ascii=False))
    observation = {
        "status": ObservationStatus.EMPTY,
        "query_id": result.query_id,
        "rows": [],
        "truncated": result.truncated,
        "query_context": context,
    }
    if minimum is not None:
        observation["privacy_note"] = (
            f"Only groups with at least {minimum} distinct contributing buyers are publishable. "
            "Rankings cover publishable groups only. Empty output may mean no data OR a suppressed small group; do not conclude zero buyers."
        )
    if len(json.dumps(observation, ensure_ascii=False)) > max_chars:
        raise ResultGuardError("Observation metadata exceeds its budget")
    # Validate every row even if the observation budget only permits a prefix.
    full = False
    for row in result.rows:
        if set(row) != expected:
            raise ResultGuardError("Returned columns do not match the prepared query")
        if minimum is not None:
            count = row[COHORT_COLUMN]
            if type(count) is not int or count < 0:
                raise ResultGuardError("Invalid trusted cohort count")
            if count < minimum:
                continue
        safe = {name: _scalar(row[name]) for name in columns}
        if full:
            continue
        observation["rows"].append(safe)
        observation["status"] = ObservationStatus.OK
        if len(json.dumps(observation, ensure_ascii=False)) > max_chars:
            observation["rows"].pop()
            if not observation["rows"]:
                raise ResultGuardError("One result row exceeds the observation budget")
            observation["truncated"] = full = True
    return observation
