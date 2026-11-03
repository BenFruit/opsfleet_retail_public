import json
import unittest
from unittest.mock import Mock, patch

from retail_agent.agent.models import AgentAction
from retail_agent.agent.orchestrator import AnalyticalAgent
from retail_agent.llm.analyst import Analyst
from support.demo_access import DEMO_ENTITLEMENTS


class GoldenAgentWiringTests(unittest.TestCase):
    def test_examples_reach_every_planning_step_and_follow_ups_use_history(self):
        analyst = Mock()
        analyst.llm.last_usage = {}
        analyst.decide.side_effect = [
            AgentAction(action="answer", answer="One"),
            AgentAction(action="answer", answer="Two"),
        ]
        golden = Mock()
        golden.retrieve.return_value = {"status": "ready", "items": [{"trio_id": "monthly-revenue-trend"}]}
        with patch("retail_agent.agent.orchestrator.logger"):
            agent = AnalyticalAgent(
                analyst, Mock(), manager_id="manager_a", entitlements=DEMO_ENTITLEMENTS, golden=golden
            )
            agent.ask("Revenue by month for our brands")
            agent.ask("and last year?")
        self.assertEqual(analyst.decide.call_args.kwargs["golden"]["items"][0]["trio_id"], "monthly-revenue-trend")
        first, second = golden.retrieve.call_args_list
        self.assertIsNone(first.kwargs["previous_question"])
        self.assertEqual(second.kwargs["previous_question"], "Revenue by month for our brands")

    def test_prompt_contains_examples_and_precedent_rules(self):
        llm = Mock()
        llm.generate_structured.return_value = AgentAction(action="answer", answer="ok")
        analyst = Analyst(llm, {})
        analyst.decide(
            question="q",
            history=[],
            observations=[],
            budget={},
            golden={"status": "ready", "items": [{"trio_id": "x"}]},
        )
        prompt = json.loads(llm.generate_structured.call_args.args[0])
        self.assertEqual(prompt["golden_examples"]["items"][0]["trio_id"], "x")
        self.assertIn("NOT current data", llm.generate_structured.call_args.kwargs["system_instruction"])
