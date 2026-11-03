import unittest
from unittest.mock import patch

from retail_agent.agent.answer import PARTIAL_PREFIX, finalize_answer, partial_answer, query_failures
from retail_agent.agent.models import AgentLimits
from support.agent_state import checked, failed, turn_state

LIMITS = AgentLimits()


def finalize(state, text, **kwargs):
    with patch("retail_agent.agent.answer.logger"):
        return finalize_answer(
            state,
            text,
            {"model_calls": state["model_calls"]},
            limits=LIMITS,
            authorized_brand_count=kwargs.get("brands", 2),
        )


class QueryFailureTests(unittest.TestCase):
    def test_controller_feedback_is_not_a_failed_query(self):
        observations = [failed(), failed("repair_required", "x"), failed("ungrounded_numbers", "x"), checked({"a": 1})]
        self.assertEqual([item["code"] for item in query_failures(observations)], ["query_rejected"])

    def test_partial_answer_names_last_issue_and_last_checked_result(self):
        state = turn_state(checked({"revenue": 30}), failed("timeout", "Query timed out."))
        text = partial_answer(state, "Budget reached.")
        self.assertTrue(text.startswith(f"{PARTIAL_PREFIX} Budget reached."))
        self.assertIn("Last query issue: [timeout] Query timed out.", text)
        self.assertIn("| revenue |", text)


class FinalizeAnswerTests(unittest.TestCase):
    def test_answer_after_only_failures_is_sent_back_for_repair(self):
        update = finalize(turn_state(failed(), queries=1, consecutive_errors=1), "Revenue is 30.")
        self.assertIsNone(update["action"])
        self.assertEqual(update["action_retries"], 1)
        self.assertEqual(update["observations"][-1]["code"], "repair_required")
        self.assertNotIn("answer", update)

    def test_answer_after_only_failures_without_budget_is_partial(self):
        state = turn_state(failed(), queries=1, consecutive_errors=LIMITS.max_consecutive_errors)
        update = finalize(state, "Revenue is 30.")
        self.assertTrue(update["answer"].startswith(PARTIAL_PREFIX))
        self.assertIn("No new query completed successfully.", update["answer"])

    def test_checked_answer_replaces_model_tables_and_adds_scope(self):
        model_text = "Revenue is 30.\n\n| revenue |\n| --- |\n| 999 |"
        update = finalize(turn_state(checked({"revenue": 30})), model_text, brands=3)
        answer = update["answer"]
        self.assertTrue(answer.startswith("Revenue is 30."))
        self.assertNotIn("999", answer)  # Only the app-rendered table remains.
        self.assertIn("Scope: authorized brands only (configured: 3).", answer)
        self.assertIn("Result 1:\n| revenue |", answer)

    def test_ungrounded_figure_gets_one_repair_then_a_visible_note(self):
        state = turn_state(checked({"revenue": 30}))
        first = finalize(state, "Revenue is 4,321.")
        self.assertEqual(first["observations"][-1]["code"], "ungrounded_numbers")
        self.assertIn("4,321", first["observations"][-1]["message"])
        exhausted = turn_state(checked({"revenue": 30}), action_retries=LIMITS.max_action_retries)
        second = finalize(exhausted, "Revenue is 4,321.")
        self.assertIn("could not be matched to the checked results", second["answer"])

    def test_figures_from_earlier_turns_count_as_evidence(self):
        history = [{"question": "Total?", "answer": "", "observations": [checked({"revenue": 4321})]}]
        update = finalize(turn_state(checked({"revenue": 30}), history=history), "Down from 4,321 to 30.")
        self.assertIn("answer", update)
        self.assertNotIn("could not be matched", update["answer"])

    def test_report_and_memory_results_are_appended(self):
        reports = {
            "status": "report",
            "operation": "list",
            "reports": [{"report_id": "a" * 32, "title": "Q1", "created_at": "2026-11-03"}],
            "note": "",
        }
        memory = {"status": "memory", "message": "Memory saved: 1 new entries."}
        answer = finalize(turn_state(reports, memory), "Here are your reports.")["answer"]
        self.assertIn("Report sources:\n" + "a" * 32 + " | Q1", answer)
        self.assertTrue(answer.endswith("Memory saved: 1 new entries."))
        self.assertNotIn("Scope:", answer)


if __name__ == "__main__":
    unittest.main()
