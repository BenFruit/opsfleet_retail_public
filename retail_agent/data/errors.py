"""Safe classification of BigQuery failures for QueryService.

Codes are a stable contract for the orchestrator's repair budget and the logs;
messages are fixed strings. BadRequest text is inspected only to recognize two
common date/time mistakes and return a hint the model can act on in a repair.
"""

from google.api_core import exceptions as google_errors

# Exception classes -> (code, message), checked in order.
_ERROR_CLASSES = (
    ((TimeoutError, google_errors.DeadlineExceeded), "timeout", "Query timed out. Try a smaller scope."),
    ((google_errors.BadRequest,), "query_rejected", "BigQuery rejected the query. It may need to be revised."),
    ((google_errors.Forbidden,), "backend_denied", "BigQuery access or quota is unavailable."),
    (
        (google_errors.TooManyRequests, google_errors.ResourceExhausted),
        "rate_limited",
        "BigQuery quota or rate limit reached. Try again later.",
    ),
    (
        (google_errors.ServiceUnavailable, google_errors.InternalServerError),
        "backend_unavailable",
        "BigQuery is temporarily unavailable.",
    ),
)
_UNKNOWN_ERROR = ("query_failed", "Query could not be completed.")

# Repair hints for BadRequest: every group must have at least one marker in the
# upper-cased BigQuery message. The code stays "query_rejected".
_BAD_REQUEST_HINTS = (
    (
        (("TIMESTAMP_SUB", "TIMESTAMP_ADD"), ("DOES NOT SUPPORT",)),
        "BigQuery TIMESTAMP_SUB/ADD do not support calendar MONTH/QUARTER/YEAR arithmetic. Use DATE_SUB/DATE_ADD and compare DATE values for calendar periods.",
    ),
    (
        (("NO MATCHING SIGNATURE",), ("TIMESTAMP",), ("DATE",)),
        "BigQuery date/time types do not match. Convert timestamps with DATE(...) when comparing them with DATE boundaries.",
    ),
)


def classify_backend_error(error: Exception) -> tuple[str, str]:
    """(code, message) for a runner failure; never includes the exception text."""
    for error_types, code, message in _ERROR_CLASSES:
        if isinstance(error, error_types):
            if isinstance(error, google_errors.BadRequest):
                message = _bad_request_hint(error) or message
            return code, message
    return _UNKNOWN_ERROR


def _bad_request_hint(error: google_errors.BadRequest) -> str | None:
    text = (error.message or "").upper()
    for groups, hint in _BAD_REQUEST_HINTS:
        if all(any(marker in text for marker in group) for group in groups):
            return hint
    return None
