import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from retail_agent.data.bigquery_runner import BigQueryRunner


class BigQueryRunnerTests(unittest.TestCase):
    def setUp(self):
        # Bypass the constructor: no ADC, network, or real Google project needed.
        self.runner = BigQueryRunner.__new__(BigQueryRunner)
        self.runner.client = Mock()
        self.job = self.runner.client.query.return_value
        self.job.total_bytes_processed = 50
        self.job.total_bytes_billed = 100
        self.set_rows([{"id": 1}])

    def set_rows(self, rows, columns=("id",)):
        iterator = MagicMock()
        iterator.schema = [SimpleNamespace(name=name) for name in columns]
        iterator.__iter__.return_value = iter(rows)
        self.job.result.return_value = iterator

    def execute(self, **overrides):
        args = dict(
            allowed_brands=("Brand A", "Brand B"),
            maximum_bytes_billed=1000,
            max_rows=2,
            timeout_seconds=3,
            api_timeout_seconds=1,
            query_id="test123",
        )
        args.update(overrides)
        return self.runner.execute_scoped_query("SCOPED SQL", **args)

    def test_dry_run_parameters_and_no_execution(self):
        self.assertEqual(
            self.runner.dry_run(
                "SCOPED SQL",
                allowed_brands=("Brand A",),
                api_timeout_seconds=1,
            ),
            50,
        )
        kwargs = self.runner.client.query.call_args.kwargs
        config = kwargs["job_config"]
        self.assertTrue(config.dry_run)
        self.assertFalse(config.use_legacy_sql)
        self.assertFalse(config.use_query_cache)
        parameter = config.query_parameters[0]
        self.assertEqual(parameter.name, "allowed_brands")
        self.assertEqual(parameter.array_type, "STRING")
        self.assertEqual(parameter.values, ["Brand A"])
        self.assertIsNone(kwargs["retry"])
        self.assertIsNone(kwargs["job_retry"])
        self.job.result.assert_not_called()

    def test_execution_has_server_limits_and_parameters(self):
        result = self.execute()
        kwargs = self.runner.client.query.call_args.kwargs
        config = kwargs["job_config"]
        self.assertEqual(config.maximum_bytes_billed, 1000)
        # This SDK represents jobTimeoutMs as a JSON string.
        self.assertEqual(config.to_api_repr()["jobTimeoutMs"], "3000")
        self.assertFalse(config.use_legacy_sql)
        self.assertEqual(config.query_parameters[0].values, ["Brand A", "Brand B"])
        self.assertEqual(kwargs["job_id"], "retail_test123")
        self.assertIsNone(kwargs["retry"])
        self.assertIsNone(kwargs["job_retry"])
        self.job.result.assert_called_once_with(
            timeout=3,
            max_results=3,
            page_size=3,
            retry=None,
            job_retry=None,
        )
        self.assertEqual(result.rows, ({"id": 1},))
        self.assertFalse(result.truncated)
        self.assertEqual(result.bytes_billed, 100)

    def test_one_extra_row_detects_truncation(self):
        self.set_rows([{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}])
        result = self.execute()
        self.assertEqual(result.rows, ({"id": 1}, {"id": 2}))
        self.assertTrue(result.truncated)

    def test_exact_row_limit_not_marked_truncated(self):
        self.set_rows([{"id": 1}, {"id": 2}])
        self.assertFalse(self.execute().truncated)

    def test_empty_result(self):
        self.set_rows([])
        result = self.execute()
        self.assertEqual(result.rows, ())
        self.assertFalse(result.truncated)

    def test_duplicate_output_names_rejected(self):
        self.set_rows([], columns=("id", "id"))
        with self.assertRaises(ValueError):
            self.execute()
        self.runner.client.cancel_job.assert_called_once()

    def test_wait_timeout_triggers_cancellation(self):
        self.job.result.side_effect = TimeoutError("PRIVATE")
        with self.assertRaises(TimeoutError):
            self.execute()
        self.runner.client.cancel_job.assert_called_once_with("retail_test123", timeout=1, retry=None)

    def test_submission_timeout_uses_known_job_id_for_cancellation(self):
        self.runner.client.query.side_effect = TimeoutError("PRIVATE")
        with self.assertRaises(TimeoutError):
            self.execute()
        self.runner.client.cancel_job.assert_called_once_with("retail_test123", timeout=1, retry=None)

    def test_cancellation_failure_does_not_hide_original_error(self):
        self.job.result.side_effect = TimeoutError("original")
        self.runner.client.cancel_job.side_effect = RuntimeError("cancel failed")
        with self.assertRaises(TimeoutError):
            self.execute()

    def test_empty_and_invalid_brands_rejected_before_query(self):
        for brands in ((), ("",), (" ",), (None,)):
            with self.subTest(brands=brands), self.assertRaises(PermissionError):
                self.execute(allowed_brands=brands)
        self.runner.client.query.assert_not_called()
