import unittest
from unittest.mock import Mock, patch

from google.api_core import exceptions as google_errors

from retail_agent.data.bigquery_runner import BigQueryRunner, QueryExecution
from retail_agent.data.query_service import QueryLimits, QueryService, QueryServiceError

BRANDS_A = frozenset({"Brand A", "Brand B"})


SQL = "SELECT SUM(oi.sale_price) AS revenue FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi"


class QueryServiceTests(unittest.TestCase):
    def setUp(self):
        self.runner = Mock(spec=BigQueryRunner)
        self.runner.dry_run.return_value = 50
        self.runner.execute_scoped_query.return_value = QueryExecution(
            rows=({"revenue": 30},),
            truncated=False,
            bytes_processed=50,
            bytes_billed=100,
        )
        self.limits = QueryLimits(maximum_bytes_billed=1000, max_rows=2, max_result_bytes=1000)
        self.service = QueryService(self.runner, self.limits)
        self.logger = self.enterContext(patch("retail_agent.data.query_service.logger"))

    def assert_error(self, code, sql=SQL, allowed_brands=BRANDS_A):
        with self.assertRaises(QueryServiceError) as caught:
            self.service.execute(sql, allowed_brands=allowed_brands)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_authorized_scoped_execution(self):
        result = self.service.execute(SQL, allowed_brands=BRANDS_A)
        self.assertEqual(result.rows, ({"revenue": 30},))
        self.assertEqual(result.estimated_bytes, 50)
        dry_args, dry_kwargs = self.runner.dry_run.call_args
        args, kwargs = self.runner.execute_scoped_query.call_args
        self.assertEqual(dry_args, args)  # Exactly the same rewritten SQL.
        self.assertIn("@allowed_brands", args[0])
        self.assertNotEqual(args[0], SQL)
        self.assertEqual(dry_kwargs["allowed_brands"], ("Brand A", "Brand B"))
        self.assertEqual(kwargs["allowed_brands"], dry_kwargs["allowed_brands"])
        self.assertEqual(kwargs["maximum_bytes_billed"], 1000)
        self.assertEqual(kwargs["max_rows"], 2)
        self.assertEqual(kwargs["query_id"], result.query_id)

    def test_other_manager_uses_other_entitlements(self):
        self.service.execute(SQL, allowed_brands=frozenset({"Brand C"}))
        self.assertEqual(self.runner.dry_run.call_args.kwargs["allowed_brands"], ("Brand C",))

    def test_missing_or_malformed_entitlements_never_reach_bigquery(self):
        for brands in (frozenset(), [], None, "Brand A", [""], ["Brand A", None]):
            with self.subTest(brands=brands):
                self.assert_error("access_denied", allowed_brands=brands)
        self.runner.dry_run.assert_not_called()
        self.runner.execute_scoped_query.assert_not_called()

    def test_pii_and_unsupported_sql_never_reach_bigquery(self):
        for sql in (
            "SELECT u.email FROM `bigquery-public-data.thelook_ecommerce.users` AS u",
            "SELECT u.age FROM `bigquery-public-data.thelook_ecommerce.users` AS u",
            "DELETE FROM x",
            "SELECT 1; SELECT 2",
            "SELECT (",
            "",
            None,
        ):
            with self.subTest(sql=sql):
                self.assert_error("invalid_sql", sql=sql)
        self.runner.dry_run.assert_not_called()
        self.runner.execute_scoped_query.assert_not_called()

    def test_oversized_sql_is_rejected_before_parser_or_bigquery(self):
        self.assert_error("invalid_sql", sql="SELECT 1 " + " " * self.limits.max_sql_chars)
        self.runner.dry_run.assert_not_called()

    def test_validation_feedback_is_specific_but_does_not_echo_sql_values(self):
        sql = "SELECT unknown_function('SECRET_VALUE') AS value FROM `bigquery-public-data.thelook_ecommerce.products` AS p"
        error = self.assert_error("invalid_sql", sql=sql)
        self.assertIn("functions are not allowed", str(error))
        self.assertNotIn("SECRET_VALUE", str(error) + repr(self.logger.mock_calls))
        self.assertIn("validation_reason", repr(self.logger.mock_calls))
        self.runner.dry_run.assert_not_called()

    def test_cost_limit_stops_execution(self):
        self.runner.dry_run.return_value = 1001
        self.assert_error("cost_limit")
        self.runner.execute_scoped_query.assert_not_called()

    def test_cost_limit_boundary_is_allowed(self):
        self.runner.dry_run.return_value = 1000
        self.service.execute(SQL, allowed_brands=BRANDS_A)
        self.runner.execute_scoped_query.assert_called_once()

    def test_remaining_question_budget_caps_both_estimate_and_execution(self):
        self.service.execute(SQL, allowed_brands=BRANDS_A, scan_budget_bytes=100)
        self.assertEqual(self.runner.execute_scoped_query.call_args.kwargs["maximum_bytes_billed"], 100)
        self.runner.execute_scoped_query.reset_mock()
        with self.assertRaises(QueryServiceError) as caught:
            self.service.execute(SQL, allowed_brands=BRANDS_A, scan_budget_bytes=49)
        self.assertEqual(caught.exception.code, "cost_limit")
        self.runner.execute_scoped_query.assert_not_called()

    def test_exhausted_question_budget_never_reaches_bigquery(self):
        for budget in (0, -1, True):
            with self.subTest(budget=budget), self.assertRaises(QueryServiceError):
                self.service.execute(SQL, allowed_brands=BRANDS_A, scan_budget_bytes=budget)
        self.runner.dry_run.assert_not_called()

    def test_unknown_estimate_fails_closed(self):
        for estimate in (None, -1, "50", True):
            with self.subTest(estimate=estimate):
                self.runner.dry_run.return_value = estimate
                self.assert_error("estimate_unavailable")
        self.runner.execute_scoped_query.assert_not_called()

    def test_zero_estimate_is_valid(self):
        self.runner.dry_run.return_value = 0
        self.service.execute(SQL, allowed_brands=BRANDS_A)

    def test_dry_run_failure_never_executes_or_retries(self):
        self.runner.dry_run.side_effect = google_errors.BadRequest("SECRET query text")
        error = self.assert_error("query_rejected")
        self.assertNotIn("SECRET", str(error))
        self.runner.dry_run.assert_called_once()
        self.runner.execute_scoped_query.assert_not_called()

    def test_calendar_timestamp_error_has_safe_actionable_feedback_and_stage(self):
        self.runner.dry_run.side_effect = google_errors.BadRequest(
            "TIMESTAMP_SUB does not support the MONTH date part when the argument is TIMESTAMP type at [12:3] SECRET"
        )
        error = self.assert_error("query_rejected")
        self.assertIn("DATE_SUB", str(error))
        self.assertEqual(error.stage, "dry_run")
        self.assertNotIn("SECRET", str(error) + repr(self.logger.mock_calls))
        self.runner.execute_scoped_query.assert_not_called()

    def test_date_timestamp_comparison_error_has_safe_feedback(self):
        self.runner.dry_run.side_effect = google_errors.BadRequest(
            "No matching signature for operator >= for argument types TIMESTAMP, DATE SECRET"
        )
        error = self.assert_error("query_rejected")
        self.assertIn("DATE(...)", str(error))
        self.assertEqual(error.stage, "dry_run")
        self.assertNotIn("SECRET", str(error))

    def test_backend_errors_have_safe_codes_and_no_automatic_retry(self):
        cases = [
            (TimeoutError, "timeout"),
            (google_errors.DeadlineExceeded, "timeout"),
            (google_errors.BadRequest, "query_rejected"),
            (google_errors.Forbidden, "backend_denied"),
            (google_errors.TooManyRequests, "rate_limited"),
            (google_errors.ResourceExhausted, "rate_limited"),
            (google_errors.ServiceUnavailable, "backend_unavailable"),
            (google_errors.InternalServerError, "backend_unavailable"),
            (RuntimeError, "query_failed"),
        ]
        for error_type, code in cases:
            with self.subTest(code=code, error_type=error_type):
                self.runner.execute_scoped_query.reset_mock()
                self.runner.execute_scoped_query.side_effect = error_type("SECRET SQL or data")
                error = self.assert_error(code)
                self.assertNotIn("SECRET", str(error))
                self.runner.execute_scoped_query.assert_called_once()

    def test_empty_result_is_valid_not_an_exception(self):
        self.runner.execute_scoped_query.return_value = QueryExecution(rows=(), truncated=False)
        result = self.service.execute(SQL, allowed_brands=BRANDS_A)
        self.assertEqual(result.rows, ())
        self.assertFalse(result.truncated)

    def test_truncation_is_preserved(self):
        self.runner.execute_scoped_query.return_value = QueryExecution(rows=({"id": 1},), truncated=True)
        self.assertTrue(self.service.execute(SQL, allowed_brands=BRANDS_A).truncated)

    def test_runner_row_overflow_fails_closed(self):
        self.runner.execute_scoped_query.return_value = QueryExecution(
            rows=({"id": 1}, {"id": 2}, {"id": 3}),
            truncated=False,
        )
        self.assert_error("result_limit")

    def test_result_size_limit(self):
        self.runner.execute_scoped_query.return_value = QueryExecution(
            rows=({"text": "x" * 2000},),
            truncated=False,
        )
        self.assert_error("result_limit")

    def test_result_size_uses_utf8_bytes(self):
        self.runner.execute_scoped_query.return_value = QueryExecution(
            rows=({"text": "\u0430" * 600},),
            truncated=False,
        )
        self.assert_error("result_limit")

    def test_result_repr_does_not_include_rows(self):
        self.runner.execute_scoped_query.return_value = QueryExecution(
            rows=({"secret": "DO_NOT_LOG"},),
            truncated=False,
        )
        result = self.service.execute(SQL, allowed_brands=BRANDS_A)
        self.assertNotIn("DO_NOT_LOG", repr(result))
        self.assertNotIn("DO_NOT_LOG", repr(self.runner.execute_scoped_query.return_value))

    def test_logs_do_not_include_sql_identity_brands_rows_or_backend_text(self):
        self.runner.execute_scoped_query.return_value = QueryExecution(
            rows=({"text": "SECRET_ROW"},),
            truncated=False,
        )
        self.service.execute(SQL, allowed_brands=BRANDS_A)
        self.runner.execute_scoped_query.side_effect = RuntimeError("SECRET_EXCEPTION")
        self.assert_error("query_failed")
        emitted = repr(self.logger.mock_calls)
        for forbidden in (SQL, "manager_a", "Brand A", "SECRET_ROW", "SECRET_EXCEPTION"):
            self.assertNotIn(forbidden, emitted)
        self.assertIn("query_completed", emitted)
        self.assertIn("query_failed", emitted)
        self.assertIn("error_code", emitted)

    def test_each_failure_is_tagged_with_its_stage_in_error_and_log(self):
        cases = (
            ("authorization", "access_denied", {"allowed_brands": []}, None),
            ("authorization", "cost_limit", {"scan_budget_bytes": 0}, None),
            ("validation", "invalid_sql", {"sql": "DELETE FROM x"}, None),
            ("dry_run", "cost_limit", {}, lambda: setattr(self.runner.dry_run, "return_value", 5000)),
            (
                "dry_run",
                "backend_denied",
                {},
                lambda: setattr(self.runner.dry_run, "side_effect", google_errors.Forbidden("x")),
            ),
            (
                "execution",
                "timeout",
                {},
                lambda: setattr(self.runner.execute_scoped_query, "side_effect", TimeoutError()),
            ),
            (
                "result_bounds",
                "result_limit",
                {},
                lambda: setattr(
                    self.runner.execute_scoped_query,
                    "return_value",
                    QueryExecution(rows=({"a": 1},) * 3, truncated=False),
                ),
            ),
        )
        for stage, code, kwargs, arrange in cases:
            with self.subTest(stage=stage, code=code):
                self.setUp()
                if arrange:
                    arrange()
                arguments = {"allowed_brands": BRANDS_A, **kwargs}
                with self.assertRaises(QueryServiceError) as caught:
                    self.service.execute(arguments.pop("sql", SQL), **arguments)
                self.assertEqual((caught.exception.stage, caught.exception.code), (stage, code))
                failed = self.logger.warning.call_args.kwargs["extra"]
                self.assertEqual((failed["stage"], failed["error_code"]), (stage, code))
                self.assertEqual(failed["query_id"], caught.exception.query_id)
                self.assertEqual(failed["validation_reason"] is not None, stage == "validation")

    def test_every_query_event_carries_turn_and_query_ids(self):
        self.service.execute(SQL, allowed_brands=BRANDS_A, turn_id="turn-1")
        self.runner.execute_scoped_query.side_effect = google_errors.ServiceUnavailable("SECRET")
        with self.assertRaises(QueryServiceError) as caught:
            self.service.execute(SQL, allowed_brands=BRANDS_A, turn_id="turn-2")
        self.assertRegex(caught.exception.query_id, r"^[0-9a-f]{32}$")
        events = {}
        for call in self.logger.info.call_args_list + self.logger.warning.call_args_list:
            events.setdefault(call.args[0], []).append(call.kwargs["extra"])
        for name in ("query_started", "query_dry_run_completed", "query_completed", "query_failed"):
            with self.subTest(event=name):
                for extra in events[name]:
                    self.assertIn(extra["turn_id"], {"turn-1", "turn-2"})
                    self.assertRegex(extra["query_id"], r"^[0-9a-f]{32}$")
        failed = events["query_failed"][0]
        self.assertEqual(failed["turn_id"], "turn-2")
        self.assertEqual(failed["query_id"], caught.exception.query_id)
        self.assertEqual(failed["error_type"], "ServiceUnavailable")
        self.assertNotIn("SECRET", repr(events))

    def test_limits_are_validated(self):
        cases = [
            {"maximum_bytes_billed": 0},
            {"max_rows": -1},
            {"max_rows": True},
            {"max_result_bytes": 0},
            {"max_sql_chars": 0},
            {"timeout_seconds": 0},
            {"timeout_seconds": float("inf")},
            {"api_timeout_seconds": float("nan")},
            {"api_timeout_seconds": True},
            {"minimum_cohort_size": 1},
            {"minimum_cohort_size": True},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                QueryLimits(**kwargs)
