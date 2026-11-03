from enum import StrEnum

from pydantic import BaseModel


class DataAccessLevel(StrEnum):
    # Can be used normally in analysis and returned at row level.
    ALLOWED = "allowed"

    # Can be used only in aggregated analysis.
    # Must not be exposed as individual-level customer attributes.
    AGGREGATE_ONLY = "aggregate_only"

    # Personally identifiable information.
    # Must be removed or masked before any data reaches the LLM.
    BLOCKED_PII = "blocked_pii"

    # Not personal data, but the value summarizes rows of every brand
    # (e.g. a whole-order item count), so brand scoping cannot apply to it.
    BLOCKED_UNSCOPED = "blocked_unscoped"


class ColumnPolicy(BaseModel):
    access_level: DataAccessLevel
    description: str = ""
