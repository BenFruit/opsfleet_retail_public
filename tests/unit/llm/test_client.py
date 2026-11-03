import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from google.genai import errors as genai_errors
from pydantic import ValidationError

from retail_agent.agent.models import AgentAction
from retail_agent.llm.client import GeminiClient
from retail_agent.settings import Settings


class GeminiClientTests(unittest.TestCase):
    def test_constructor_model_override_timeout_and_disabled_retries(self):
        with patch("retail_agent.llm.client.genai.Client") as sdk:
            client = GeminiClient("fake", "explicit-model")
            self.assertEqual(client.model, "explicit-model")
            options = sdk.call_args.kwargs["http_options"]
            self.assertEqual(options.timeout, 30_000)
            self.assertEqual(options.retry_options.attempts, 1)
            client.close()
            sdk.return_value.close.assert_called_once()

    def test_structured_action_system_instruction_and_usage(self):
        with patch("retail_agent.llm.client.genai.Client") as sdk:
            generate = sdk.return_value.models.generate_content
            generate.return_value = SimpleNamespace(
                text='{"action": "answer", "answer": "Schema information"}',
                usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5, total_token_count=15),
            )
            client = GeminiClient("fake", "env-model")
            action = client.generate_structured("input", AgentAction, system_instruction="rules")
            self.assertEqual(action.action, "answer")
            self.assertEqual(client.last_usage["total_tokens"], 15)
            config = generate.call_args.kwargs["config"]
            self.assertEqual(config.system_instruction, "rules")
            self.assertEqual(config.max_output_tokens, 4096)
            self.assertIsNone(config.response_schema)
            self.assertFalse(config.response_json_schema["additionalProperties"])

    def test_missing_model_is_not_replaced_by_unverified_default(self):
        for key, model in (("fake", ""), ("", "model")):
            with (
                self.subTest(key=bool(key)),
                patch("retail_agent.llm.client.genai.Client"),
                self.assertRaises(ValueError),
            ):
                GeminiClient(key, model)

    def test_from_settings_passes_key_models_and_judge_override(self):
        settings = Settings(
            _env_file=None, gemini_api_key="fake", gemini_model="primary", gemini_fallback_model="backup"
        )
        with patch("retail_agent.llm.client.genai.Client") as sdk:
            client = GeminiClient.from_settings(settings)
            judge = GeminiClient.from_settings(settings, model="judge")
        self.assertEqual(sdk.call_args.kwargs["api_key"], "fake")
        self.assertEqual((client.model, client.fallback_model), ("primary", "backup"))
        self.assertEqual(judge.model, "judge")


OK_RESPONSE = SimpleNamespace(
    text='{"action": "answer", "answer": "ok"}',
    usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1, total_token_count=2),
)


def api_error(status, message="SECRET", retry_after=None):
    response = httpx.Response(status, headers={"Retry-After": retry_after}) if retry_after else None
    return genai_errors.APIError(status, {"error": {"message": message}}, response)


class GeminiRetryTests(unittest.TestCase):
    def make_client(self, outcomes, fallback=""):
        sdk = self.enterContext(patch("retail_agent.llm.client.genai.Client"))
        self.generate = sdk.return_value.models.generate_content
        self.generate.side_effect = outcomes
        self.sleep = Mock()
        self.logger = self.enterContext(patch("retail_agent.llm.client.logger"))
        return GeminiClient("fake", "primary", fallback_model=fallback, sleep=self.sleep)

    def models_called(self):
        return [call.kwargs["model"] for call in self.generate.call_args_list]

    def test_transient_error_is_retried_once_on_the_same_model(self):
        client = self.make_client([api_error(503), OK_RESPONSE])
        self.assertEqual(client.generate_structured("p", AgentAction).answer, "ok")
        self.assertEqual(self.models_called(), ["primary", "primary"])
        self.sleep.assert_called_once()
        self.assertLessEqual(self.sleep.call_args.args[0], client.max_delay_seconds)
        self.assertEqual(client.last_usage["model"], "primary")

    def test_persistent_outage_switches_to_fallback_and_never_returns_to_primary(self):
        client = self.make_client([api_error(503), api_error(503), OK_RESPONSE], fallback="backup")
        client.generate_structured("p", AgentAction)
        self.assertEqual(self.models_called(), ["primary", "primary", "backup"])
        self.assertEqual(client.last_usage["model"], "backup")
        client = self.make_client([api_error(503), api_error(503), api_error(503)], fallback="backup")
        with self.assertRaises(genai_errors.APIError):
            client.generate_structured("p", AgentAction)
        self.assertEqual(self.models_called(), ["primary", "primary", "backup"])

    def test_without_fallback_the_error_is_raised_after_bounded_attempts(self):
        client = self.make_client([api_error(503), api_error(503), OK_RESPONSE])
        with self.assertRaises(genai_errors.APIError):
            client.generate_structured("p", AgentAction)
        self.assertEqual(self.generate.call_count, client.max_attempts)

    def test_daily_quota_timeout_and_long_retry_after_skip_to_fallback_without_waiting(self):
        for error in (
            api_error(429, "Quota perDay exceeded"),
            httpx.ReadTimeout("SECRET"),
            api_error(429, retry_after="60"),
        ):
            with self.subTest(error=type(error).__name__):
                client = self.make_client([error, OK_RESPONSE], fallback="backup")
                client.generate_structured("p", AgentAction)
                self.assertEqual(self.models_called(), ["primary", "backup"])
                self.sleep.assert_not_called()

    def test_short_retry_after_is_honoured(self):
        client = self.make_client([api_error(429, retry_after="3"), OK_RESPONSE])
        client.generate_structured("p", AgentAction)
        self.sleep.assert_called_once_with(3.0)

    def test_invalid_output_and_permanent_errors_are_not_retried(self):
        invalid = SimpleNamespace(text='{"action": "SECRET"}', usage_metadata=None)
        for outcome in (invalid, api_error(400), api_error(401)):
            with self.subTest(outcome=outcome):
                client = self.make_client([outcome, OK_RESPONSE], fallback="backup")
                with self.assertRaises((ValidationError, genai_errors.APIError)):
                    client.generate_structured("p", AgentAction)
                self.assertEqual(self.generate.call_count, 1)

    def test_retry_log_is_metadata_only(self):
        client = self.make_client([api_error(503, "SECRET body"), OK_RESPONSE])
        client.generate_structured("SECRET prompt", AgentAction)
        (name,) = (call.args[0] for call in self.logger.warning.call_args_list)
        extra = self.logger.warning.call_args.kwargs["extra"]
        self.assertEqual(name, "model_retry")
        self.assertEqual((extra["error_code"], extra["attempt"], extra["http_status"]), ("model_unavailable", 1, 503))
        self.assertNotIn("SECRET", repr(self.logger.mock_calls))

    def test_fallback_equal_to_primary_is_ignored(self):
        client = self.make_client([], fallback="primary")
        self.assertIsNone(client.fallback_model)
