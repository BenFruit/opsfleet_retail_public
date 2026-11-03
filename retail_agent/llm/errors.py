"""Safe classification of Gemini failures.

Every failure becomes (code, message, metadata). Codes drive retries/fallback in
GeminiClient and are logged; messages are fixed strings from the tables below and
may be shown to the user. Provider bodies, keys and inputs are never returned or
logged: provider text is only inspected for the known markers listed here.
"""

import httpx
from google.genai import errors as genai_errors
from pydantic import ValidationError


class ModelResponseError(RuntimeError):
    """A controlled failure with no raw provider response attached."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# HTTP status of a Gemini APIError -> (code, message). "{status}" is filled in.
_HTTP_ERRORS = {
    429: (
        "model_rate_limited",
        "Gemini quota or rate limit was reached (HTTP 429). Check AI Studio quotas and try later.",
    ),
    **dict.fromkeys(
        (500, 502, 503, 504),
        ("model_unavailable", "Gemini is temporarily unavailable or overloaded (HTTP {status}). Try again later."),
    ),
    **dict.fromkeys(
        (401, 403),
        (
            "model_access_denied",
            "Gemini denied access (HTTP {status}). Check the API key, project restrictions, and model access.",
        ),
    ),
    404: (
        "model_not_found",
        "Gemini model or endpoint was not found (HTTP 404). Check GEMINI_MODEL and its API availability.",
    ),
    400: (
        "model_request_rejected",
        "Gemini rejected the request (HTTP 400). Check model capabilities and request configuration.",
    ),
}
_OTHER_HTTP_ERROR = ("model_api_error", "Gemini API returned HTTP {status}. Check model/API configuration.")

# A more specific cause recognized from lower-cased provider-message markers:
# (status, markers, code, message). The first rule whose status matches and
# whose message contains any marker wins.
_MESSAGE_RULES = (
    (
        429,
        ("perday", "per_day", "per day", "daily"),
        "model_daily_quota",
        "Gemini daily quota is exhausted (HTTP 429). Check AI Studio quotas or use another enabled model.",
    ),
    (
        400,
        ("api_key_invalid", "api key not valid", "api key expired"),
        "model_invalid_key",
        "Gemini rejected the API key (HTTP 400). Check GEMINI_API_KEY.",
    ),
    (
        400,
        ("schema", "additionalproperties", "response_mime_type", "responsemimetype"),
        "model_schema_rejected",
        "Gemini rejected the structured-output schema or configuration (HTTP 400). Check model support and the response schema.",
    ),
)

# Failures without an HTTP status, checked in order (httpx timeouts are also
# transport errors, so they come first).
_EXCEPTION_ERRORS = (
    (
        (TimeoutError, httpx.TimeoutException),
        "model_timeout",
        "The Gemini request timed out. Try again later or use a faster enabled model.",
    ),
    (
        (httpx.TransportError,),
        "model_connection_failed",
        "Could not connect to Gemini. Check the network or proxy and try again.",
    ),
    (
        (ValueError, TypeError),
        "model_configuration_error",
        "The model call failed local SDK/request validation. Check SDK compatibility and response-schema configuration.",
    ),
)
_UNEXPECTED_ERROR = (
    "model_failed",
    "The model is unavailable or an unexpected local error occurred. See the safe error type in logs/agent.jsonl.",
)

# Candidate finish reasons that mean the text is missing or unusable.
_FINISH_REASON_ERRORS = {
    # Text may be present, but the JSON action is cut off.
    "MAX_TOKENS": ("model_output_limit", "Gemini reached its output-token limit before completing the response."),
    **dict.fromkeys(
        ("SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION"),
        ("model_response_blocked", "Gemini withheld the response under its safety/content policy."),
    ),
}
# The API's default prompt_feedback value: it means "not blocked".
_NOT_BLOCKED = "BLOCKED_REASON_UNSPECIFIED"


def describe_model_error(error: Exception) -> tuple[str, str, dict]:
    """Classify failures without exposing provider bodies, keys, or input values."""
    metadata = {"error_type": type(error).__name__}
    if isinstance(error, ModelResponseError):
        return error.code, str(error), metadata
    if isinstance(error, genai_errors.APIError):
        return _describe_api_error(error, metadata)
    if isinstance(error, ValidationError):  # Before ValueError: it is a subclass.
        return _describe_validation_error(error, metadata)
    for error_types, code, message in _EXCEPTION_ERRORS:
        if isinstance(error, error_types):
            return code, message, metadata
    return *_UNEXPECTED_ERROR, metadata


def _describe_api_error(error: genai_errors.APIError, metadata: dict) -> tuple[str, str, dict]:
    status = error.code
    metadata["http_status"] = status
    # Inspect only to select fixed messages. Never return/log raw contents.
    provider_message = (error.message or "").lower()
    code, message = next(
        (
            (code, message)
            for rule_status, markers, code, message in _MESSAGE_RULES
            if rule_status == status and any(marker in provider_message for marker in markers)
        ),
        _HTTP_ERRORS.get(status, _OTHER_HTTP_ERROR),
    )
    message = message.format(status=status)
    seconds = _retry_after_seconds(error.response)
    if seconds is not None:
        metadata["retry_after_seconds"] = seconds
        message += f" Retry after approximately {seconds} seconds."
    return code, message, metadata


def _retry_after_seconds(response) -> int | None:
    """Only a short decimal Retry-After is used; HTTP dates and other values are ignored."""
    if response is None:
        return None
    value = response.headers.get("Retry-After", "")
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 5:
        return min(int(value), 3600)
    return None


def _describe_validation_error(error: ValidationError, metadata: dict) -> tuple[str, str, dict]:
    issues = error.errors(include_input=False, include_context=False, include_url=False)
    # Field names/types only. Never include input, Pydantic messages, or ctx.
    metadata["validation_fields"] = sorted(
        {
            str(part) if part in {"action", "sql", "answer"} else "<other>"
            for issue in issues[:5]
            for part in issue["loc"]
        }
    )
    metadata["validation_error_count"] = len(issues)
    if any(issue["type"] == "json_invalid" for issue in issues):
        return "model_invalid_json", "Gemini returned invalid or incomplete JSON, not a usable action.", metadata
    return (
        "model_invalid_action",
        "Gemini returned a structured response that does not match the required tool/answer format.",
        metadata,
    )


def response_text(response) -> str:
    """Return the model text or raise ModelResponseError naming why there is none.

    Gemini reports a missing answer in three places, checked from most to least
    specific (attributes are read with getattr because they are optional):
    1. candidates[0].finish_reason: generation started but stopped early, at the
       output-token limit or by a safety/recitation filter.
    2. prompt_feedback.block_reason: the prompt itself was blocked, so there are
       no candidates at all.
    3. Neither: the text is simply empty. Overload and quota problems never reach
       this point; they arrive as APIError and are classified above.
    """
    candidates = getattr(response, "candidates", None) or []
    finish = getattr(candidates[0], "finish_reason", None) if candidates else None
    finish = getattr(finish, "value", finish)  # SDK enum or plain string.
    if finish in _FINISH_REASON_ERRORS:
        raise ModelResponseError(*_FINISH_REASON_ERRORS[finish])
    block_reason = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
    if block_reason and getattr(block_reason, "value", block_reason) != _NOT_BLOCKED:
        raise ModelResponseError(
            "model_response_blocked", "Gemini blocked the request under its safety/content policy."
        )
    if not response.text:
        raise ModelResponseError(
            "model_empty_response", "Gemini returned no text. This is different from an API overload or quota error."
        )
    return response.text
