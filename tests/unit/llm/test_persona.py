import json
import unittest
from email.message import Message
from unittest.mock import MagicMock, Mock, patch

from retail_agent.agent.models import AgentAction
from retail_agent.agent.orchestrator import AnalyticalAgent
from retail_agent.data.query_service import QueryLimits
from retail_agent.llm.analyst import SYSTEM_INSTRUCTION, Analyst
from retail_agent.llm.persona import DEFAULT_STYLE, GoogleDocReader, PersonaStyle, parse_google_doc_id
from support.demo_access import DEMO_ENTITLEMENTS

DOC_ID = "a" * 40


def doc_style(document):
    return PersonaStyle(GoogleDocReader(document))


def response(text, content_type="text/plain"):
    result = MagicMock()
    result.__enter__.return_value = result
    result.headers = Message()
    result.headers["Content-Type"] = content_type
    result.read.return_value = text.encode("utf-8")
    return result


class PersonaTests(unittest.TestCase):
    def test_no_document_uses_default_without_network(self):
        with patch("retail_agent.llm.persona.urlopen") as fetch:
            persona = PersonaStyle().refresh()
        self.assertEqual(persona.text, DEFAULT_STYLE)
        self.assertEqual(persona.source, "default")
        fetch.assert_not_called()

    def test_google_doc_id_export_timeout_and_bounded_read(self):
        result = response("\ufeffBe brief and formal.")
        with patch("retail_agent.llm.persona.urlopen", return_value=result) as fetch:
            persona = doc_style(DOC_ID).refresh()
        request = fetch.call_args.args[0]
        self.assertEqual(request.full_url, f"https://docs.google.com/document/d/{DOC_ID}/export?format=txt")
        self.assertEqual(fetch.call_args.kwargs["timeout"], 3)
        result.read.assert_called_once_with(16_001)
        self.assertEqual(persona.text, "Be brief and formal.")
        self.assertEqual(persona.source, "google_doc")
        self.assertEqual(len(persona.version), 12)

    def test_last_good_version_survives_network_failure_without_logging_content(self):
        provider = doc_style(DOC_ID)
        with patch("retail_agent.llm.persona.urlopen", side_effect=[response("Style A"), OSError("SECRET URL")]):
            first = provider.refresh()
            with patch("retail_agent.llm.persona.logger") as logger:
                second = provider.refresh()
        self.assertEqual(first, second)
        self.assertNotIn("SECRET", str(logger.mock_calls))
        self.assertNotIn("Style A", str(logger.mock_calls))

    def test_invalid_empty_html_or_oversized_documents_fall_back(self):
        cases = [response(""), response("Please sign in", "text/html"), response("x" * 4001), response("x" * 16001)]
        for result in cases:
            with (
                self.subTest(content_type=result.headers["Content-Type"]),
                patch("retail_agent.llm.persona.urlopen", return_value=result),
                patch("retail_agent.llm.persona.logger"),
            ):
                self.assertEqual(doc_style(DOC_ID).refresh().source, "default")

    def test_arbitrary_urls_and_paths_are_not_fetched(self):
        for value in (
            "http://localhost/private",
            "../../secret",
            "bad id",
            f"https://docs.google.com.evil.example/document/d/{DOC_ID}/edit",
            f"https://docs.google.com@evil.example/document/d/{DOC_ID}/edit",
        ):
            with patch("retail_agent.llm.persona.urlopen") as fetch:
                with patch("retail_agent.llm.persona.logger"):
                    self.assertEqual(doc_style(value).refresh().source, "default")
                fetch.assert_not_called()

    def test_full_google_docs_url_is_normalized_to_safe_export_url(self):
        for suffix in (f"/document/d/{DOC_ID}/edit?tab=t.0#heading=example", f"/document/u/0/d/{DOC_ID}/edit"):
            with patch("retail_agent.llm.persona.urlopen", return_value=response("Write in English")) as fetch:
                persona = doc_style("https://docs.google.com" + suffix).refresh()
            self.assertEqual(persona.source, "google_doc")
            self.assertEqual(
                fetch.call_args.args[0].full_url, f"https://docs.google.com/document/d/{DOC_ID}/export?format=txt"
            )

    def test_default_style_file_keeps_the_previous_text(self):
        self.assertEqual(PersonaStyle().current.version, "4a007cf25a0a")
        self.assertTrue(DEFAULT_STYLE.startswith("Write for nontechnical retail executives"))

    def test_document_id_parsing(self):
        self.assertEqual(parse_google_doc_id(f"  {DOC_ID} "), DOC_ID)
        self.assertEqual(parse_google_doc_id(f"https://docs.google.com/document/d/{DOC_ID}/edit"), DOC_ID)
        for value in (
            "short",
            f"http://docs.google.com/document/d/{DOC_ID}",
            f"https://docs.google.com/spreadsheets/d/{DOC_ID}",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_google_doc_id(value)

    def test_failure_codes_name_the_failed_step(self):
        cases = (
            ("bad id", None, "persona_invalid_config"),
            (DOC_ID, OSError("SECRET"), "persona_fetch_failed"),
            (DOC_ID, response("Please sign in", "text/html"), "persona_invalid_content"),
        )
        for document, outcome, expected in cases:
            with self.subTest(expected=expected):
                fetch = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with (
                    patch("retail_agent.llm.persona.urlopen", **fetch),
                    patch("retail_agent.llm.persona.logger") as logger,
                ):
                    doc_style(document).refresh()
                extra = logger.warning.call_args.kwargs["extra"]
                self.assertEqual(extra["error_code"], expected)
                self.assertNotIn("SECRET", repr(logger.mock_calls))

    def test_style_changes_next_turn_without_changing_policy_or_runtime_context(self):
        llm = Mock()
        llm.generate_structured.return_value = AgentAction(action="answer", answer="Demo")
        analyst = Analyst(llm, {}, persona_style=doc_style(DOC_ID))
        args = dict(
            question="Revenue?",
            history=[],
            observations=[],
            budget={},
            access_context={"authorized_brands": ["Brand A"]},
        )
        with patch(
            "retail_agent.llm.persona.urlopen", side_effect=[response("Style A {question}"), response("Style B")]
        ) as fetch:
            analyst.refresh_persona()
            analyst.decide(**args)
            first = llm.generate_structured.call_args.kwargs["system_instruction"]
            analyst.decide(**args)
            self.assertEqual(llm.generate_structured.call_args.kwargs["system_instruction"], first)
            self.assertEqual(fetch.call_count, 1)
            analyst.refresh_persona()
            analyst.decide(**args)
            second = llm.generate_structured.call_args.kwargs["system_instruction"]
        self.assertNotEqual(first, second)
        self.assertIn("Style A {question}", first)  # No variable interpolation.
        self.assertTrue(first.endswith(SYSTEM_INSTRUCTION))
        self.assertTrue(second.endswith(SYSTEM_INSTRUCTION))
        self.assertNotIn("Brand A", second)  # Runtime data is separate JSON context.
        self.assertEqual(
            json.loads(llm.generate_structured.call_args.args[0])["access_context"]["authorized_brands"], ["Brand A"]
        )

    def test_agent_refreshes_once_per_question_not_each_model_call_or_confirmation(self):
        llm = Mock()
        llm.last_usage = {}
        analyst = Analyst(llm, {}, persona_style=PersonaStyle())
        llm.generate_structured.return_value = AgentAction(action="answer", answer="Demo")
        query_service = Mock()
        query_service.limits = QueryLimits()
        agent = AnalyticalAgent(analyst, query_service, manager_id="manager_a", entitlements=DEMO_ENTITLEMENTS)
        with patch.object(analyst, "refresh_persona", wraps=analyst.refresh_persona) as refresh:
            agent.ask("Question one")
            agent.ask("Question two")
            agent.ask("/confirm")
            self.assertEqual(refresh.call_count, 2)
