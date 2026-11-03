import unittest
from unittest.mock import Mock

from retail_agent.agent.commands import is_command, run_command
from retail_agent.memory.models import UserMemoryError


def run(command, *, memory=None, reports=None):
    return run_command(command, memory=memory, reports=reports, conversation_id="conversation-1")


class CommandTests(unittest.TestCase):
    def test_only_exact_commands_bypass_the_model(self):
        for command in ("/memories", "/forget-all", "/forget abc", "/confirm", "/cancel"):
            self.assertTrue(is_command(command), command)
        for text in ("yes", "confirm", "/confirm please", "/forget", "please /cancel"):
            self.assertFalse(is_command(text), text)

    def test_confirm_and_cancel_use_the_report_library(self):
        reports = Mock()
        reports.confirm.return_value = "Deleted 1 of 1 previewed reports."
        self.assertEqual(run("/confirm", reports=reports), "Deleted 1 of 1 previewed reports.")
        self.assertEqual(run("/cancel", reports=reports), "Deletion cancelled.")
        reports.cancel.assert_called_once()
        self.assertEqual(run("/confirm"), "Report library is not configured.")

    def test_memory_commands_cancel_a_pending_deletion(self):
        reports, memory = Mock(), Mock()
        memory.load.return_value = [{"memory_id": "a" * 32, "text": "Prefers bullets."}]
        self.assertEqual(run("/memories", memory=memory, reports=reports), "a" * 32 + " | Prefers bullets.")
        reports.cancel.assert_called_once()
        memory.forget.return_value = "Forgot 1 memory entries."
        run("/forget " + "b" * 32, memory=memory)
        run("/forget-all", memory=memory)
        self.assertEqual([c.args for c in memory.forget.call_args_list], [("b" * 32,), (None,)])

    def test_memory_errors_become_messages(self):
        memory = Mock()
        memory.load.return_value = []
        self.assertEqual(run("/memories", memory=memory), "No saved user memories.")
        memory.forget.side_effect = UserMemoryError("Use /forget followed by a memory ID from /memories.")
        self.assertEqual(run("/forget bad", memory=memory), "Use /forget followed by a memory ID from /memories.")
        self.assertEqual(run("/memories"), "User memory is not configured.")


if __name__ == "__main__":
    unittest.main()
