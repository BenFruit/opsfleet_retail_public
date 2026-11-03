import unittest
from types import SimpleNamespace
from unittest.mock import ANY, Mock

from retail_agent.agent.models import AgentAction
from retail_agent.agent.orchestrator import AnalyticalAgent
from retail_agent.data.query_service import QueryLimits, QueryResult, QueryService
from retail_agent.llm.analyst import Analyst
from support.demo_access import DEMO_ENTITLEMENTS, patch_agent_loggers

SQL = (
    "SELECT p.category AS category, SUM(oi.sale_price) AS revenue FROM "
    "`bigquery-public-data.thelook_ecommerce.order_items` AS oi JOIN "
    "`bigquery-public-data.thelook_ecommerce.products` AS p ON oi.product_id = p.id GROUP BY category"
)
TOTAL_SQL = "SELECT SUM(oi.sale_price) AS revenue FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi"
ROWS = (
    {"category": "Suits", "revenue": 3403.2},
    {"category": "Outerwear", "revenue": 2420.77},
    {"category": "Sweaters", "revenue": "1351.06"},
)


class AgentGroundingTests(unittest.TestCase):
    def setUp(self):
        self.analyst = Mock(spec=Analyst)
        self.analyst.llm = SimpleNamespace(last_usage={})
        self.service = Mock(spec=QueryService)
        self.service.limits = QueryLimits(maximum_bytes_billed=1000)
        self.service.execute.side_effect = [
            QueryResult("q1", ROWS, False, 10, 10, 10, columns=("category", "revenue")),
            QueryResult("q2", ({"revenue": 7175.03},), False, 10, 10, 10, columns=("revenue",)),
        ]
        self.agent = AnalyticalAgent(self.analyst, self.service, manager_id="manager_a", entitlements=DEMO_ENTITLEMENTS)
        self.logger = patch_agent_loggers(self)

    def test_ungrounded_total_triggers_one_sql_correction(self):
        self.analyst.decide.side_effect = [
            AgentAction(action="query", sql=SQL),
            AgentAction(action="answer", answer="Total revenue was $7,175.03."),
            AgentAction(action="query", sql=TOTAL_SQL),
            AgentAction(action="answer", answer="Total revenue was $7,175.03."),
        ]
        turn = self.agent.ask("Quarter revenue by category and total")
        feedback = self.analyst.decide.call_args_list[2].kwargs["observations"][-1]
        self.assertEqual(feedback["code"], "ungrounded_numbers")
        self.assertIn("$7,175.03", feedback["message"])
        self.assertNotIn("could not be matched", turn.answer)
        self.assertEqual(turn.queries, 2)
        self.logger.warning.assert_any_call(
            "answer_ungrounded_figures", extra={"turn_id": ANY, "row_count": 1, "status": "repair"}
        )
        self.assertIsNotNone(self.agent.session.last_answer)  # Feedback is not a failed query.

    def test_when_budget_is_spent_the_answer_is_delivered_with_a_note(self):
        self.agent.limits = type(self.agent.limits)(max_action_retries=1)
        self.analyst.decide.side_effect = [
            AgentAction(action="query", sql=SQL),
            AgentAction(action="answer", answer="Total revenue was $7,175.03."),
            AgentAction(action="answer", answer="Total revenue was $7,175.03 again."),
        ]
        turn = self.agent.ask("Quarter revenue by category and total")
        self.assertIn("could not be matched to the checked results", turn.answer)
        self.assertIn("$7,175.03", turn.answer)
        self.assertIn("| Suits |", turn.answer)  # Authoritative tables are still shown.
