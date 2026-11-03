import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from retail_agent.observability import tracing
from retail_agent.settings import Settings
from support.demo_access import DEMO_ENTITLEMENTS

LANGFUSE = {
    "langfuse_tracing_enabled": True,
    "langfuse_public_key": "pk-test",
    "langfuse_secret_key": "sk-test",
    "langfuse_base_url": "http://localhost:3000",
}


def settings(**values) -> Settings:
    return Settings(_env_file=None, **values)


class TracingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(tracing, "_client", None))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_disabled_by_default(self):
        with patch("langfuse.Langfuse") as langfuse:
            tracing.configure_tracing(settings())
        langfuse.assert_not_called()
        self.assertEqual(tracing.get_callbacks(), [])
        with tracing.trace_generation("model", "prompt", None) as observation:
            self.assertIsNone(observation)

    def test_missing_configuration_does_not_initialize_or_use_cloud(self):
        with patch("langfuse.Langfuse") as langfuse:
            tracing.configure_tracing(settings(langfuse_tracing_enabled=True))
        langfuse.assert_not_called()
        self.assertEqual(tracing.get_callbacks(), [])

    def test_callback_generation_and_shutdown(self):
        client = Mock()
        callback = Mock()
        with patch("langfuse.Langfuse", return_value=client) as langfuse:
            tracing.configure_tracing(settings(**LANGFUSE))
        langfuse.assert_called_once_with(public_key="pk-test", secret_key="sk-test", base_url="http://localhost:3000")
        with patch("langfuse.langchain.CallbackHandler", return_value=callback) as handler:
            self.assertEqual(tracing.get_callbacks(), [callback])
        handler.assert_called_once_with(public_key="pk-test")
        tracing.trace_generation("test-model", "question", "instructions")
        client.start_as_current_observation.assert_called_once_with(
            as_type="generation",
            name="Gemini",
            model="test-model",
            input={"prompt": "question", "system_instruction": "instructions"},
        )
        tracing.shutdown_tracing()
        client.shutdown.assert_called_once()
        self.assertIsNone(tracing._client)

    def test_shutdown_failure_does_not_crash_cli(self):
        client = Mock()
        client.shutdown.side_effect = RuntimeError("sensitive endpoint error")
        tracing._client = client
        with patch.object(tracing, "logger") as logger:
            tracing.shutdown_tracing()
        self.assertNotIn("sensitive endpoint error", str(logger.mock_calls))
        self.assertIsNone(tracing._client)

    def test_real_langfuse_callback_nests_generation_in_graph_without_network(self):
        from langfuse import Langfuse
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        from retail_agent.agent.models import AgentAction
        from retail_agent.agent.orchestrator import AnalyticalAgent
        from retail_agent.data.query_service import QueryLimits

        os.environ["LANGSMITH_TRACING"] = "true"  # Must not enable a second exporter.
        keys = {
            **LANGFUSE,
            "langfuse_public_key": "pk-offline-span-test",
            "langfuse_secret_key": "sk-offline-span-test",
        }
        exporter = InMemorySpanExporter()
        client = Langfuse(
            public_key=keys["langfuse_public_key"],
            secret_key=keys["langfuse_secret_key"],
            base_url=keys["langfuse_base_url"],
            span_exporter=exporter,
        )
        tracing.configure_tracing(settings(**keys))  # Same public key: reuses the exporter above.
        self.addCleanup(tracing.shutdown_tracing)
        self.addCleanup(client.shutdown)

        analyst = Mock()
        analyst.llm = SimpleNamespace(last_usage={})

        def decide(**kwargs):
            with tracing.trace_generation("offline-model", "demo question", "instructions") as span:
                span.update(output={"action": "answer"}, usage_details={"input": 10, "output": 5})
            return AgentAction(action="answer", answer="Demo answer")

        analyst.decide.side_effect = decide
        service = Mock()
        service.limits = QueryLimits()
        agent = AnalyticalAgent(analyst, service, manager_id="manager_a", entitlements=DEMO_ENTITLEMENTS)
        self.assertEqual(agent.ask("Describe available data").answer, "Demo answer")
        client.flush()
        spans = {span.name: span for span in exporter.get_finished_spans()}
        self.assertIn("retail_question", spans)
        self.assertIn("plan", spans)
        self.assertIn("Gemini", spans)
        self.assertEqual(spans["Gemini"].context.trace_id, spans["retail_question"].context.trace_id)
        self.assertEqual(spans["Gemini"].parent.span_id, spans["plan"].context.span_id)
