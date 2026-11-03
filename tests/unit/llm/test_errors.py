import json
import unittest
from types import SimpleNamespace

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from langsmith import tracing_context

from retail_agent.agent.models import AgentAction
from retail_agent.llm.errors import ModelResponseError, describe_model_error, response_text


class ModelErrorTests(unittest.TestCase):
    def test_real_sdk_serializes_action_schema_with_offline_transport(self):
        def handler(request):
            config = json.loads(request.content)["generationConfig"]
            self.assertNotIn("responseSchema", config)
            self.assertFalse(config["responseJsonSchema"]["additionalProperties"])
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {"text": '{"action":"answer","answer":"I am a retail analyst."}'},
                                ],
                            },
                            "finishReason": "STOP",
                        }
                    ],
                },
            )

        transport = httpx.Client(transport=httpx.MockTransport(handler))
        client = genai.Client(
            api_key="offline-test-key",
            http_options=types.HttpOptions(
                httpx_client=transport,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        self.addCleanup(client.close)
        with tracing_context(enabled=False):
            response = client.models.generate_content(
                model="offline-test-model",
                contents="hello",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=AgentAction.model_json_schema(),
                ),
            )
        self.assertEqual(AgentAction.model_validate_json(response_text(response)).action, "answer")

    def test_provider_failures_are_distinguishable_and_do_not_expose_raw_body(self):
        cases = [
            (503, "SECRET", "model_unavailable"),
            (429, "SECRET", "model_rate_limited"),
            (429, "Quota perDay exceeded SECRET", "model_daily_quota"),
            (401, "SECRET", "model_access_denied"),
            (403, "SECRET", "model_access_denied"),
            (404, "SECRET", "model_not_found"),
            (400, "API key not valid SECRET", "model_invalid_key"),
            (400, "response_schema additionalProperties SECRET", "model_schema_rejected"),
            (400, "SECRET", "model_request_rejected"),
            (418, "SECRET", "model_api_error"),
        ]
        for status, provider_message, expected in cases:
            with self.subTest(status=status, expected=expected):
                error = genai_errors.APIError(status, {"error": {"message": provider_message}})
                code, message, metadata = describe_model_error(error)
                self.assertEqual(code, expected)
                self.assertEqual(metadata["http_status"], status)
                self.assertNotIn("SECRET", message + json.dumps(metadata))

    def test_numeric_retry_after_is_allowed_but_arbitrary_header_is_not_exposed(self):
        for header in ("25", "SECRET", "-1", "Mon, 01 Jan 2027", "9" * 100):
            with self.subTest(header=header):
                response = httpx.Response(429, headers={"Retry-After": header})
                error = genai_errors.APIError(429, {"error": {"message": "SECRET"}}, response)
                code, message, metadata = describe_model_error(error)
                self.assertEqual(code, "model_rate_limited")
                if header == "25":
                    self.assertEqual(metadata["retry_after_seconds"], 25)
                else:
                    self.assertNotIn("retry_after_seconds", metadata)
                self.assertNotIn("SECRET", message + json.dumps(metadata))

    def test_invalid_json_and_invalid_action_are_different(self):
        for text, expected in (
            ("SECRET invalid JSON", "model_invalid_json"),
            ('{"action":"SECRET","answer":"sensitive input"}', "model_invalid_action"),
            ('{"action":"query","sql":"","answer":"SECRET"}', "model_invalid_action"),
        ):
            with self.subTest(expected=expected):
                try:
                    AgentAction.model_validate_json(text)
                except ValueError as error:
                    code, message, metadata = describe_model_error(error)
                else:
                    self.fail("Expected a validation error")
                self.assertEqual(code, expected)
                self.assertNotIn("SECRET", message + json.dumps(metadata))
                self.assertNotIn("sensitive input", message + json.dumps(metadata))
                self.assertGreater(metadata["validation_error_count"], 0)

    def test_transport_and_local_failures_are_distinguished(self):
        for error, expected in (
            (TimeoutError("SECRET"), "model_timeout"),
            (httpx.ReadTimeout("SECRET"), "model_timeout"),
            (httpx.ConnectError("SECRET"), "model_connection_failed"),
            (ValueError("SECRET"), "model_configuration_error"),
            (RuntimeError("SECRET"), "model_failed"),
        ):
            with self.subTest(expected=expected):
                code, message, metadata = describe_model_error(error)
                self.assertEqual(code, expected)
                self.assertNotIn("SECRET", message + json.dumps(metadata))

    def test_empty_blocked_and_token_limited_responses_are_distinguished(self):
        for finish, text, expected in (
            (None, None, "model_empty_response"),
            ("MAX_TOKENS", '{"action":', "model_output_limit"),
            ("SAFETY", None, "model_response_blocked"),
            ("SPII", None, "model_response_blocked"),
        ):
            with self.subTest(expected=expected):
                response = SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=finish)])
                with self.assertRaises(ModelResponseError) as caught:
                    response_text(response)
                self.assertEqual(describe_model_error(caught.exception)[0], expected)

    def test_prompt_block_without_candidates_is_reported(self):
        response = SimpleNamespace(text=None, candidates=[], prompt_feedback=SimpleNamespace(block_reason="SAFETY"))
        with self.assertRaises(ModelResponseError) as caught:
            response_text(response)
        self.assertEqual(caught.exception.code, "model_response_blocked")
