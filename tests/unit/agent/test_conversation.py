import unittest

from retail_agent.agent.conversation import append_turn, is_snapshot_answer
from retail_agent.agent.models import AgentAction, AgentLimits
from support.agent_state import answer_action, checked, failed, turn_state


class SnapshotAnswerTests(unittest.TestCase):
    def test_only_real_analysis_answers_can_be_saved(self):
        remember = AgentAction(action="remember", remember={"memories": ["x"]})
        cases = (
            (turn_state(checked({"a": 1}), action=answer_action(), queries=1), True),
            (turn_state(action=answer_action()), True),  # Schema/definition answers need no query.
            (turn_state(failed(), checked({"a": 1}), action=answer_action(), queries=2), True),
            (turn_state(failed(), action=answer_action(), queries=1), False),
            (turn_state(action=answer_action(), report_calls=1), False),
            (turn_state(action=answer_action(), memory_calls=1), False),
            (turn_state(action=remember), False),
            (turn_state(action=None), False),
        )
        for state, expected in cases:
            with self.subTest(expected=expected, observations=[o["status"] for o in state["observations"]]):
                self.assertIs(is_snapshot_answer(state), expected)


class AppendTurnTests(unittest.TestCase):
    def result(self, answer="ok", observations=()):
        return {"answer": answer, "observations": list(observations)}

    def test_keeps_last_two_observations_and_bounded_answer(self):
        history = []
        observations = [checked({"n": i}) for i in range(3)]
        self.assertFalse(append_turn(history, "Q", self.result("x" * 9000, observations), AgentLimits()))
        self.assertEqual(len(history[0]["answer"]), 8000)
        self.assertEqual([o["rows"][0]["n"] for o in history[0]["observations"]], [1, 2])

    def test_oldest_turns_are_dropped_by_count_and_size(self):
        history = []
        limits = AgentLimits(max_history_turns=2)
        for question in ("Q1", "Q2"):
            self.assertFalse(append_turn(history, question, self.result(), limits))
        self.assertTrue(append_turn(history, "Q3", self.result(), limits))
        self.assertEqual([turn["question"] for turn in history], ["Q2", "Q3"])
        small = AgentLimits(max_history_chars=300)
        self.assertTrue(append_turn(history, "Q4", self.result("y" * 200), small))
        self.assertEqual([turn["question"] for turn in history], ["Q4"])
        # A single turn larger than the limit is not kept either: history becomes empty.
        self.assertTrue(append_turn(history, "Q5", self.result("y" * 400), small))
        self.assertEqual(history, [])

    def test_large_observations_are_dropped_before_the_turn(self):
        history = []
        big = checked({"text": "z" * 500})
        append_turn(history, "Q", self.result(observations=[big]), AgentLimits(max_history_chars=300))
        self.assertEqual(history, [{"question": "Q", "answer": "ok", "observations": []}])


if __name__ == "__main__":
    unittest.main()
