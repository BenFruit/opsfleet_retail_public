import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from retail_agent.llm.embeddings import Embedder, EmbeddingError, vector_literal
from retail_agent.safety.errors import ResultGuardError
from retail_agent.settings import Settings

VECTOR = [1.0] + [0.0] * 767


class EmbeddingTests(unittest.TestCase):
    def test_vector_validation(self):
        for v in ([], [0.0] * 768, [float("nan")] * 768, [True] * 768):
            with self.subTest(v=str(v[:1])), self.assertRaises(EmbeddingError):
                vector_literal(v)

    def test_google_request_format_dimensions_and_size_guard(self):
        client = Mock()
        client.models.embed_content.return_value = SimpleNamespace(embeddings=[SimpleNamespace(values=VECTOR)])
        embedder = Embedder(client)
        self.assertEqual(embedder.embed("document", document=True, title="Title"), VECTOR)
        call = client.models.embed_content.call_args.kwargs
        self.assertEqual(call["contents"], "title: Title | text: document")
        self.assertEqual(call["config"].output_dimensionality, 768)
        embedder.embed("Nike", document=False)
        self.assertEqual(client.models.embed_content.call_args.kwargs["contents"], "task: search result | query: Nike")
        with self.assertRaises(EmbeddingError):
            embedder.embed("x" * 8001, document=True)

    def test_own_sdk_client_from_settings_and_sensitive_text_guard(self):
        settings = Settings(_env_file=None, gemini_api_key="fake")
        with patch("retail_agent.llm.embeddings.genai.Client") as sdk:
            embedder = Embedder.from_settings(settings)
        self.assertEqual(sdk.call_args.kwargs["api_key"], "fake")
        self.assertEqual(sdk.call_args.kwargs["http_options"].retry_options.attempts, 1)
        embedder.close()
        sdk.return_value.close.assert_called_once()
        with self.assertRaises(ValueError):
            Embedder.from_settings(Settings(_env_file=None))
        with self.assertRaises(ResultGuardError):
            embedder.embed("Contact me at person@example.com", document=False)
        sdk.return_value.models.embed_content.assert_not_called()
