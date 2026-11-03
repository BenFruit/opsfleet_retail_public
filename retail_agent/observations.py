"""Status of a tool observation the model reads in its next planning step.

StrEnum members serialize as their plain string values, so the JSON the model
sees is unchanged.
"""

from enum import StrEnum


class ObservationStatus(StrEnum):
    OK = "ok"  # A checked query result with publishable rows.
    EMPTY = "empty"  # A checked query result with no publishable rows.
    ERROR = "error"  # A failed tool call or controller feedback (see agent/feedback.py).
    REPORT = "report"  # A report library result.
    MEMORY = "memory"  # A user memory write outcome.


CHECKED_RESULT = {ObservationStatus.OK, ObservationStatus.EMPTY}
