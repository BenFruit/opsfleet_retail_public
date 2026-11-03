import unittest

from retail_agent.agent.models import AgentAction
from retail_agent.agent.orchestrator import route_after_plan


class RouteAfterPlanTests(unittest.TestCase):
    def test_routes(self):
        cases = (
            ({"answer": "done", "action": None}, "end"),
            ({"answer": None, "action": None}, "plan"),
            ({"answer": None, "action": AgentAction(action="remember", remember={"memories": ["x"]})}, "remember"),
            (
                {"answer": None, "action": AgentAction(action="report", report={"operation": "list", "query": ""})},
                "reports",
            ),
            ({"answer": None, "action": AgentAction(action="query", sql="SELECT 1")}, "query"),
        )
        for state, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(route_after_plan(state), expected)
