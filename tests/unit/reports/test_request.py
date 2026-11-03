import itertools
import unittest

from pydantic import ValidationError

from retail_agent.reports.models import ReportRequest

ID = "a" * 32
VALID = {
    ("save", "matching", "Title", "", ""),
    ("save", "matching", "", "", ""),
    ("search", "matching", "", "nike", ""),
    ("open", "matching", "", "", ID),
    ("list", "matching", "", "", ""),
    ("prepare_delete", "matching", "", "nike", ""),
    ("prepare_delete", "id", "", "", ID),
    ("prepare_delete", "all", "", "", ""),
    ("prepare_delete", "conversation", "", "", ""),
}


class ReportRequestShapeTests(unittest.TestCase):
    def test_only_documented_shapes_are_accepted(self):
        for shape in itertools.product(
            ["save", "search", "open", "list", "prepare_delete"],
            ["matching", "conversation", "all", "id"],
            ["", "Title"],
            ["", " ", "nike"],
            ["", ID, "A" * 32],
        ):
            operation, scope, title, query, report_id = shape
            with self.subTest(shape=shape):
                try:
                    ReportRequest(operation=operation, scope=scope, title=title, query=query, report_id=report_id)
                    accepted = True
                except ValidationError:
                    accepted = False
                self.assertEqual(accepted, shape in VALID)

    def test_errors_name_the_broken_rule(self):
        cases = (
            ({"operation": "search", "query": " "}, "search requires query only"),
            ({"operation": "list", "query": "", "scope": "all"}, "list accepts no filters"),
            ({"operation": "open", "query": "", "report_id": "A" * 32}, "report_id must be a lowercase hex ID"),
            ({"operation": "list", "query": "", "title": "x"}, "title is only used for save"),
            (
                {"operation": "prepare_delete", "query": "x", "scope": "all"},
                "all/conversation deletion accepts no other filters",
            ),
        )
        for fields, message in cases:
            with self.subTest(message=message), self.assertRaises(ValidationError) as caught:
                ReportRequest(**fields)
            self.assertIn(message, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
