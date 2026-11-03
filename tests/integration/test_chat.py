import io
import json
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from retail_agent.agent.models import AgentAction
from retail_agent.agent.orchestrator import AnalyticalAgent
from retail_agent.cli import run_chat
from retail_agent.data.bigquery_runner import BigQueryRunner, QueryExecution
from retail_agent.data.query_service import QueryService
from retail_agent.llm.analyst import Analyst
from retail_agent.llm.client import GeminiClient
from retail_agent.observability.logging import SafeJsonFormatter
from retail_agent.schema.catalog import TABLE_POLICIES
from retail_agent.security.access import Entitlements
from support.demo_access import DEMO_ENTITLEMENTS


class ChatIntegrationTests(unittest.TestCase):
    def test_real_graph_analyst_service_scope_and_guard_with_fake_backends(self):
        llm = Mock(spec=GeminiClient)
        llm.last_usage = {}
        llm.generate_structured.side_effect = [
            AgentAction(
                action="query",
                sql="SELECT DISTINCT oi.status FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi",
            ),
            AgentAction(
                action="query",
                sql="SELECT SUM(oi.sale_price) AS revenue FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi WHERE oi.status = 'Complete'",
            ),
            AgentAction(action="answer", answer="Completed-item revenue is 30."),
        ]
        schema = {
            table: [{"name": name, "type": "STRING", "description": "DO_NOT_FORWARD"} for name in columns]
            for table, columns in TABLE_POLICIES.items()
        }
        runner = Mock(spec=BigQueryRunner)
        runner.dry_run.return_value = 50
        runner.execute_scoped_query.side_effect = [
            QueryExecution(({"status": "Complete"},), False, 50, 50),
            QueryExecution(({"revenue": 30},), False, 50, 50),
        ]
        agent = AnalyticalAgent(
            Analyst(llm, schema), QueryService(runner), manager_id="manager_a", entitlements=DEMO_ENTITLEMENTS
        )
        turn = agent.ask("What is completed revenue?")
        self.assertTrue(turn.answer.startswith("Completed-item revenue is 30."))
        self.assertIn("| revenue |", turn.answer)
        self.assertIn("| 30.00 |", turn.answer)
        self.assertEqual(turn.queries, 2)
        self.assertEqual(turn.model_calls, 3)
        for call in runner.execute_scoped_query.call_args_list:
            self.assertIn("@allowed_brands", call.args[0])
            self.assertEqual(call.kwargs["allowed_brands"], ("Brand A", "Brand B"))
        last_prompt = json.loads(llm.generate_structured.call_args.args[0])
        self.assertEqual(last_prompt["observations"][1]["rows"], [{"revenue": 30}])
        user_columns = {column["name"] for column in last_prompt["schema"]["users"]}
        self.assertIn("age", user_columns)
        self.assertIn("state", user_columns)
        self.assertNotIn("email", user_columns)
        self.assertNotIn("first_name", user_columns)
        self.assertNotIn("DO_NOT_FORWARD", llm.generate_structured.call_args.args[0])

    def test_cli_survives_failed_request_and_accepts_next_question(self):
        agent = Mock()
        agent.ask.side_effect = [RuntimeError("SECRET"), SimpleNamespace(answer="30", queries=1, model_calls=2)]
        output = io.StringIO()
        with patch("builtins.input", side_effect=["Question 1", "Question 2", "/exit"]), patch("sys.stdout", output):
            run_chat(agent)
        self.assertEqual(agent.ask.call_count, 2)
        self.assertIn("Analyst: 30", output.getvalue())
        self.assertNotIn("SECRET", output.getvalue())

    def test_cli_reset_help_and_empty_input(self):
        agent = Mock()
        with patch("builtins.input", side_effect=["", "/help", "/reset", "/exit"]), patch("sys.stdout", io.StringIO()):
            run_chat(agent)
        agent.reset.assert_called_once()
        agent.ask.assert_not_called()

    def test_access_config_is_fully_validated(self):
        with patch("pathlib.Path.read_text", return_value='{"demo": ["A"], "empty": []}'):
            entitlements = Entitlements.from_file("unused.json")
        self.assertEqual(entitlements.brands_for("demo"), frozenset({"A"}))
        for data in ("[]", "{}", '{"demo": "A"}', '{"demo": [null]}', '{"demo": [" "]}'):
            with (
                self.subTest(data=data),
                patch("pathlib.Path.read_text", return_value=data),
                self.assertRaises(ValueError),
            ):
                Entitlements.from_file("unused.json")

    def test_unknown_or_empty_identity_is_denied(self):
        entitlements = Entitlements({"demo": ["A"], "empty": []})
        for manager in ("unknown", "empty"):
            with self.subTest(manager=manager), self.assertRaises(PermissionError):
                entitlements.brands_for(manager)

    def test_json_formatter_drops_arbitrary_extras_and_exception_text(self):
        record = logging.LogRecord("retail_agent.agent", logging.INFO, "file", 1, "agent_event", (), None)
        record.turn_id = "id"
        record.prompt = "SECRET"
        record.exc_info = (RuntimeError, RuntimeError("SECRET"), None)
        text = SafeJsonFormatter().format(record)
        self.assertNotIn("SECRET", text)
        self.assertEqual(json.loads(text)["turn_id"], "id")
