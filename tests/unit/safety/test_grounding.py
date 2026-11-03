import unittest

from retail_agent.safety.grounding import figures, ungrounded_figures

ROWS = (
    {"category": "Suits", "revenue": 3403.2},
    {"category": "Outerwear", "revenue": 2420.77},
    {"category": "Sweaters", "revenue": "1351.06"},
)
OBS = [{"status": "ok", "rows": list(ROWS)}]


class GroundingTests(unittest.TestCase):
    def test_checked_values_sums_and_differences_are_grounded(self):
        text = "Suits earned $3,403.20, Outerwear $2,420.77; together $5,823.97, a gap of $982.43."
        self.assertEqual(ungrounded_figures(text, OBS), [])

    def test_mental_arithmetic_over_many_rows_is_flagged(self):
        # 3403.20 + 2420.77 + 1351.06 = 7175.03 is a three-row total: must come from SQL.
        self.assertEqual(
            ungrounded_figures("Quarter revenue was $7,175.03 from 172 orders.", OBS), ["$7,175.03", "172"]
        )

    def test_rounding_dates_years_percent_ranks_and_ids_are_ignored(self):
        text = (
            "Revenue about $3,403 (Q3 2026, 2026-07-01 to 2026-09-30) grew 33.1% for the top 5 "
            "categories; win back within 60 days. Method: product-comparison v2"
        )
        self.assertEqual(ungrounded_figures(text, OBS), [])
        self.assertEqual([raw for raw, _, _ in figures("99.57% and 99.57 percent")], [])

    def test_numbers_the_user_stated_and_no_evidence_are_not_flagged(self):
        self.assertEqual(
            ungrounded_figures("You asked for the top 250 customers.", OBS, context="top 250 customers"), []
        )
        self.assertEqual(ungrounded_figures("Revenue was $9,999.99", []), [])
