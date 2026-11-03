import unittest
from datetime import date
from decimal import Decimal

from retail_agent.data.query_service import QueryResult
from retail_agent.presentation import remove_markdown_tables, render_observation
from retail_agent.safety.errors import ResultGuardError
from retail_agent.safety.result_guard import make_observation
from retail_agent.safety.sensitive_text import check_text
from retail_agent.sql.validator import validate_query

SQL = "SELECT p.name FROM `bigquery-public-data.thelook_ecommerce.products` AS p"


def result(rows, truncated=False):
    return QueryResult("id", tuple(rows), truncated, 10, 10, 10)


class ResultGuardTests(unittest.TestCase):
    def test_rendering_uses_actual_values_and_escapes_table_separators(self):
        text = render_observation({"rows": [{"brand": "A|B", "revenue": 12.5}], "truncated": False})
        self.assertIn("| brand | revenue |", text)
        self.assertIn("| A\\|B | 12.50 |", text)

    def test_all_23_rows_are_printed_without_invented_pagination(self):
        text = render_observation({"rows": [{"category": f"Category {i}"} for i in range(23)]})
        self.assertIn("| Category 22 |", text)
        self.assertNotIn("Showing 20", text)

    def test_sql_context_is_service_owned_not_a_handwritten_predicate_interpretation(self):
        base = "SELECT SUM(oi.sale_price) AS revenue FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi"
        data = result([{"revenue": 10}])
        from dataclasses import replace

        data = replace(data, columns=("revenue",), query_context={"tool": "sql", "executed_sql": base})
        observation = make_observation(base, data, max_chars=1000)
        self.assertEqual(observation["query_context"]["executed_sql"], base)

    def test_model_table_is_removed_when_authoritative_table_will_be_appended(self):
        text = "Findings\n\n| revenue |\n| --- |\n| 999 |\n\nInterpretation."
        cleaned = remove_markdown_tables(text)
        self.assertIn("Findings", cleaned)
        self.assertIn("Interpretation", cleaned)
        self.assertNotIn("999", cleaned)

    def test_financial_looking_strings_are_not_expanded_as_arbitrary_decimals(self):
        text = render_observation({"rows": [{"revenue_label": "1E100000000"}]})
        self.assertIn("1E100000000", text)
        self.assertLess(len(text), 200)

    def test_rendering_marks_truncated_samples(self):
        text = render_observation({"rows": [{"id": 1}, {"id": 2}], "truncated": True}, max_rows=1)
        self.assertIn("Showing 1 of 2", text)
        self.assertIn("not necessarily a complete ranking", text)

    def test_scalar_normalization(self):
        # Use actual catalog columns, assigning a date to this fixture's name value.
        sql = "SELECT p.id, p.cost, p.name FROM `bigquery-public-data.thelook_ecommerce.products` AS p"
        observation = make_observation(
            sql, result([{"id": 1, "cost": Decimal("1.23"), "name": date(2026, 1, 1)}]), max_chars=1000
        )
        self.assertEqual(observation["rows"], [{"id": 1, "cost": "1.23", "name": "2026-01-01"}])

    def test_empty_result(self):
        self.assertEqual(make_observation(SQL, result([]), max_chars=1000)["status"], "empty")

    def test_sample_truncation_is_explicit(self):
        observation = make_observation(SQL, result([{"name": "x" * 100}] * 10), max_chars=400)
        self.assertTrue(observation["truncated"])
        self.assertGreater(len(observation["rows"]), 0)
        self.assertLess(len(observation["rows"]), 10)

    def test_original_truncation_flag_is_preserved(self):
        self.assertTrue(make_observation(SQL, result([{"name": "p"}], True), max_chars=1000)["truncated"])

    def test_one_oversized_row_is_rejected(self):
        with self.assertRaises(ResultGuardError):
            make_observation(SQL, result([{"name": "x" * 1000}]), max_chars=200)

    def test_unsupported_or_sensitive_values_rejected(self):
        for value in ("person@example.com", "+1 (555) 123-4567", [1, 2], {"x": 1}, b"data", float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ResultGuardError):
                make_observation(SQL, result([{"name": value}]), max_chars=1000)

    def test_phone_pattern_blocks_numbers_but_not_dated_analytical_deltas(self):
        for text in ("+1 (555) 123-4567", "+7 (7172) 12-34-56", "call +44 20 7946 0958", "+15551234567"):
            with self.subTest(text=text), self.assertRaises(ResultGuardError):
                check_text(text)
        for text in (
            "Revenue grew +5 (2024-01-31) versus the prior month.",
            "Orders +12 (2023-09-01 - 2023-09-30).",
            "Buyers +150 (2024 - 2023).",
            "Growth +15% (2024-10).",
        ):
            with self.subTest(text=text):
                self.assertEqual(check_text(text), text)

    def test_unexpected_missing_or_duplicate_columns_rejected(self):
        for row in ({"email": "x"}, {}, {"name": "p", "extra": 1}):
            with self.subTest(row=row), self.assertRaises(ResultGuardError):
                make_observation(SQL, result([row]), max_chars=1000)
        with self.assertRaises(ResultGuardError):
            make_observation(
                "SELECT p.id AS x, p.name AS x FROM `bigquery-public-data.thelook_ecommerce.products` AS p",
                result([]),
                max_chars=1000,
            )

    def test_unnamed_computed_columns_get_stable_aliases(self):
        from retail_agent.sql.validator import validate_query

        tree = validate_query("SELECT COUNT(*) FROM `bigquery-public-data.thelook_ecommerce.products` AS p")
        self.assertEqual(len(tree.named_selects), 1)
        self.assertTrue(tree.named_selects[0])

    def test_monthly_revenue_and_count_are_supported(self):
        validate_query("""
            SELECT DATE_TRUNC(DATE(oi.created_at), MONTH) AS month,
                   SUM(oi.sale_price) AS revenue, COUNT(*) AS units
            FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
            WHERE oi.created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
            GROUP BY month ORDER BY month
        """)

    def test_output_names_do_not_define_source_column_policy(self):
        validate_query("SELECT u.id AS age FROM `bigquery-public-data.thelook_ecommerce.users` AS u GROUP BY age")
        with self.assertRaises(ValueError):
            validate_query("SELECT u.age AS id FROM `bigquery-public-data.thelook_ecommerce.users` AS u GROUP BY id")

    def test_star_projection_still_rejected(self):
        with self.assertRaises(ValueError):
            validate_query("SELECT p.* FROM `bigquery-public-data.thelook_ecommerce.products` AS p")
