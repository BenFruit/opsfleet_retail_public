"""Opt-in Langfuse tracing. Payload redaction is deferred; demo inputs only."""

import logging
from contextlib import nullcontext

logger = logging.getLogger(__name__)
_client = None
_public_key = ""


def configure_tracing(settings) -> None:
    """Initialize once at startup, only on opt-in; never fall back to a cloud endpoint."""
    global _client, _public_key
    if not settings.langfuse_tracing_enabled:
        return
    if not (settings.langfuse_public_key and settings.langfuse_secret_key and settings.langfuse_base_url):
        logger.warning("tracing_unavailable", extra={"error_code": "tracing_configuration"})
        return
    try:
        from langfuse import Langfuse

        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            base_url=settings.langfuse_base_url,
        )
        _public_key = settings.langfuse_public_key
    except Exception:
        logger.warning("tracing_unavailable", extra={"error_code": "tracing_initialization"})


def get_callbacks() -> list:
    if _client is None:
        return []
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler(public_key=_public_key)]
    except Exception:
        logger.warning("tracing_unavailable", extra={"error_code": "tracing_initialization"})
        return []


def trace_generation(model: str, prompt: str, system_instruction: str | None, *, name: str = "Gemini"):
    if _client is None:
        return nullcontext(None)
    return _client.start_as_current_observation(
        as_type="generation",
        name=name,
        model=model,
        input={"prompt": prompt, "system_instruction": system_instruction},
    )


def trace_operation(name: str, inputs: dict):
    if _client is None:
        return nullcontext(None)
    return _client.start_as_current_observation(as_type="tool", name=name, input=inputs)


def shutdown_tracing() -> None:
    global _client
    if _client is not None:
        try:
            _client.shutdown()
        except Exception:
            logger.warning("tracing_unavailable", extra={"error_code": "tracing_shutdown"})
        finally:
            _client = None
