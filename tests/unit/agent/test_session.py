import unittest

from retail_agent.agent.models import AgentLimits
from retail_agent.agent.session import ChatSession
from support.agent_state import answer_action, checked, failed, turn_state


def finished(answer="Revenue is 30.", *observations, **fields):
    return turn_state(*observations, action=answer_action(answer), answer=answer, **fields)


class ChatSessionTests(unittest.TestCase):
    def test_first_question_and_previous_question(self):
        session = ChatSession()
        self.assertIsNone(session.previous_question)
        session.start_question("Q1")
        session.record_turn("Q1", finished(), brands=["Brand A"], limits=AgentLimits())
        session.start_question("Q2")
        self.assertEqual(session.first_question, "Q1")
        self.assertEqual(session.previous_question, "Q1")

    def test_only_saveable_answers_become_the_report_snapshot(self):
        session = ChatSession()
        session.record_turn(
            "Q1",
            finished("Revenue is 30.", checked({"revenue": 30}), queries=1),
            brands=["Brand A"],
            limits=AgentLimits(),
        )
        self.assertEqual((session.last_answer, session.last_answer_brands), ("Revenue is 30.", ["Brand A"]))
        session.record_turn("Q2", finished("Failed.", failed(), queries=1), brands=["Brand B"], limits=AgentLimits())
        self.assertEqual(session.last_answer, "Revenue is 30.")  # Not replaced by an answer after failures.
        self.assertEqual(len(session.history), 2)

    def test_truncation_is_remembered_and_sessions_are_distinct(self):
        session = ChatSession()
        limits = AgentLimits(max_history_turns=1)
        session.record_turn("Q1", finished(), brands=[], limits=limits)
        self.assertFalse(session.history_truncated)
        session.record_turn("Q2", finished(), brands=[], limits=limits)
        self.assertTrue(session.history_truncated)
        self.assertNotEqual(ChatSession().conversation_id, session.conversation_id)
