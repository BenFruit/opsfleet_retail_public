import logging
import random
import time
from typing import TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from retail_agent.llm.errors import describe_model_error, response_text
from retail_agent.observability.tracing import trace_generation

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)

# Planning calls are read-only, so transient provider failures may be retried.
# Timeouts are NOT retried on the same model: another 30 s wait rarely helps.
_RETRY_SAME_MODEL = frozenset({"model_unavailable", "model_rate_limited", "model_connection_failed"})
# Failures where a different configured model may still answer.
_TRY_FALLBACK = _RETRY_SAME_MODEL | {"model_timeout", "model_daily_quota", "model_not_found"}


class GeminiClient:
    """Gemini wrapper with bounded application-level retries and optional fallback.

    SDK transport retries stay disabled so every retry is explicit, logged and
    bounded here: at most ``max_attempts`` calls to the primary model, then at
    most one call to the fallback model when configured. Only provider/transport
    failures are retried; invalid JSON/actions are returned to the orchestrator,
    which owns its own correction budget.
    """

    max_attempts = 2
    base_delay_seconds = 1.0
    max_delay_seconds = 8.0

    def __init__(self, api_key: str, model: str, *, fallback_model: str = "", sleep=time.sleep):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not configured")
        self.model = model
        if not self.model:
            raise ValueError("Set GEMINI_MODEL to a model ID enabled in your AI Studio project")
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=30_000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        self.fallback_model = fallback_model.strip() or None
        if self.fallback_model == self.model:
            self.fallback_model = None
        self._sleep = sleep
        self.last_usage: dict[str, int | str] = {}

    @classmethod
    def from_settings(cls, settings, *, model: str = "") -> "GeminiClient":
        """model overrides GEMINI_MODEL (the evaluation judge uses its own)."""
        api_key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
        return cls(api_key, model or settings.gemini_model, fallback_model=settings.gemini_fallback_model)

    def generate_structured(
        self,
        prompt: str,
        response_schema: type[T],
        *,
        system_instruction: str | None = None,
    ) -> T:
        self.last_usage = {}
        model, attempts_on_model, attempt = self.model, 1, 1
        while True:
            try:
                return self._generate_once(model, prompt, response_schema, system_instruction)
            except Exception as error:
                code, _, metadata = describe_model_error(error)
                next_model = self._next_model(model, attempts_on_model, code, metadata)
                if next_model is None:
                    raise
                delay = self._delay(attempts_on_model, metadata) if next_model == model else 0.0
                logger.warning(
                    "model_retry",
                    extra={
                        "error_code": code,
                        "attempt": attempt,
                        "delay_ms": round(delay * 1000),
                        "fallback": next_model != model,
                        "model": next_model,
                        **{key: metadata[key] for key in ("error_type", "http_status") if key in metadata},
                    },
                )
                if delay:
                    self._sleep(delay)
                attempts_on_model = attempts_on_model + 1 if next_model == model else 1
                model, attempt = next_model, attempt + 1

    def _next_model(self, model: str, attempts_on_model: int, code: str, metadata: dict) -> str | None:
        """Same model (bounded), then the fallback once; never back to the primary."""
        if model != self.model:
            return None  # The fallback model gets exactly one attempt.
        retry_after = metadata.get("retry_after_seconds")
        if (
            code in _RETRY_SAME_MODEL
            and attempts_on_model < self.max_attempts
            # A long Retry-After means waiting is worse than trying the fallback.
            and (retry_after is None or retry_after <= self.max_delay_seconds)
        ):
            return model
        if self.fallback_model and code in _TRY_FALLBACK:
            return self.fallback_model
        return None

    def _delay(self, attempt: int, metadata: dict) -> float:
        retry_after = metadata.get("retry_after_seconds")
        if retry_after is not None:
            return float(min(retry_after, self.max_delay_seconds))
        backoff = self.base_delay_seconds * 2 ** (attempt - 1)
        return min(backoff, self.max_delay_seconds) * random.uniform(0.8, 1.2)

    def _generate_once(self, model: str, prompt: str, response_schema: type[T], system_instruction: str | None) -> T:
        with trace_generation(model, prompt, system_instruction) as observation:
            response = self.client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    system_instruction=system_instruction,
                    max_output_tokens=4096,
                    response_mime_type="application/json",
                    # Preserve JSON Schema as-is; typed SDK conversion previously
                    # produced additional_properties rejected by this endpoint.
                    response_json_schema=response_schema.model_json_schema(),
                ),
            )
            self.last_usage = {"model": model}
            usage = response.usage_metadata
            if usage is not None:
                self.last_usage = {
                    "model": model,
                    "input_tokens": usage.prompt_token_count or 0,
                    "output_tokens": usage.candidates_token_count or 0,
                    "total_tokens": usage.total_token_count or 0,
                }
            action = response_schema.model_validate_json(response_text(response))
            if observation is not None:
                observation.update(
                    output=action.model_dump(mode="json"),
                    usage_details={
                        "input": self.last_usage.get("input_tokens", 0),
                        "output": self.last_usage.get("output_tokens", 0),
                        "total": self.last_usage.get("total_tokens", 0),
                    },
                )
            return action

    def close(self) -> None:
        self.client.close()
