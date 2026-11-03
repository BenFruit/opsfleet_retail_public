"""Gemini embeddings shared by saved reports, user memory and Golden Knowledge.

All three use one model and dimensionality so vectors stay comparable; changing
the model requires re-embedding every store (see golden.ingest for the index).
"""

import math

from google import genai
from google.genai import types

from retail_agent.observability.tracing import trace_generation
from retail_agent.safety.sensitive_text import check_text

DIMENSIONS = 768
MAX_INPUT_BYTES = 8_000


class EmbeddingError(RuntimeError):
    """Controlled embedding failure; never contains provider response text."""


def vector_literal(values: list[float]) -> str:
    """pgvector text form of a validated embedding."""
    if len(values) != DIMENSIONS or any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
        raise EmbeddingError("Embedding has an invalid shape or nonfinite values.")
    if not any(values):
        raise EmbeddingError("Embedding must not be a zero vector.")
    return "[" + ",".join(str(float(v)) for v in values) + "]"


class Embedder:
    """Text -> vector through the Gemini embeddings API; independent of the chat model client.

    It still checks text for sensitive values: this is the last step before the
    text leaves for an external API, whatever the caller already checked.
    """

    def __init__(self, client: genai.Client, model: str = "gemini-embedding-2"):
        self.client = client  # SDK client; tests pass a fake.
        self.model = model
        if self.model != "gemini-embedding-2":
            raise ValueError("This MVP supports gemini-embedding-2 only; changing models needs reindexing")

    @classmethod
    def from_settings(cls, settings) -> "Embedder":
        if settings.gemini_api_key is None:
            raise ValueError("GEMINI_API_KEY is not configured")
        # Same transport policy as the chat client: bounded timeout, no hidden SDK retries.
        client = genai.Client(
            api_key=settings.gemini_api_key.get_secret_value(),
            http_options=types.HttpOptions(timeout=30_000, retry_options=types.HttpRetryOptions(attempts=1)),
        )
        return cls(client, settings.reports_embedding_model)

    def close(self) -> None:
        self.client.close()

    def embed(self, text: str, *, document: bool, title: str = "", observation_name: str = "embedding") -> list[float]:
        check_text(text)
        check_text(title)
        # Bound conservatively in UTF-8 bytes, never silently truncate.
        formatted = f"title: {title or 'none'} | text: {text}" if document else f"task: search result | query: {text}"
        if len(formatted.encode("utf-8")) > MAX_INPUT_BYTES:
            raise EmbeddingError("Text is too large to embed (8,000-byte limit). Request a shorter answer.")
        try:
            with trace_generation(self.model, formatted, None, name=observation_name) as span:
                result = self.client.models.embed_content(
                    model=self.model,
                    contents=formatted,
                    config=types.EmbedContentConfig(output_dimensionality=DIMENSIONS),
                )
                if not result.embeddings or len(result.embeddings) != 1:
                    raise EmbeddingError("Embedding service returned no usable vector.")
                values = list(result.embeddings[0].values or [])
                vector_literal(values)
                if span is not None:
                    span.update(output={"dimensions": DIMENSIONS})
                return values
        except EmbeddingError:
            raise
        except Exception:
            raise EmbeddingError(
                "Embedding service is unavailable; the operation was not completed. Try again later."
            ) from None
