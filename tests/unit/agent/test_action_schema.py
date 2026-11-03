import json
import unittest
from typing import get_args

from retail_agent.agent.models import AgentAction
from retail_agent.reports.models import DeleteScope, ReportOperation, ReportRequest
from support.paths import REPO_ROOT

SNAPSHOT = REPO_ROOT / "tests/support/agent_action_schema.json"


class ActionSchemaTests(unittest.TestCase):
    def test_model_facing_schema_is_unchanged(self):
        # Gemini receives this JSON schema with every planning call. Docstrings and
        # field descriptions become part of it, so refactoring must not change it.
        # After a deliberate prompt change, regenerate the snapshot and review the diff.
        expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        self.assertEqual(AgentAction.model_json_schema(), expected)

    def test_report_literals_match_the_enums(self):
        fields = ReportRequest.model_fields
        self.assertEqual(set(get_args(fields["operation"].annotation)), {item.value for item in ReportOperation})
        self.assertEqual(set(get_args(fields["scope"].annotation)), {item.value for item in DeleteScope})


if __name__ == "__main__":
    unittest.main()
