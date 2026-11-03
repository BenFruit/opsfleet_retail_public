import io
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from retail_agent.settings import Settings


class StartupDiagnosticsTests(unittest.TestCase):
    """cli.main with every external dependency of the application replaced."""

    def setUp(self):
        from retail_agent import application, cli

        self.application, self.cli = application, cli
        self.settings = Settings(_env_file=None, gemini_api_key="fake", gemini_model="m")
        for name in (
            "configure_logging",
            "configure_tracing",
            "SchemaService",
            "build_agent",
            "connection_factory",
            "Embedder",
            "QueryService",
        ):
            self.enterContext(patch.object(application, name))
        self.shutdown_tracing = self.enterContext(patch.object(application, "shutdown_tracing"))
        self.entitlements = self.enterContext(patch.object(application, "Entitlements"))
        self.gemini = self.enterContext(patch.object(application, "GeminiClient"))
        self.bigquery = self.enterContext(patch.object(application, "BigQueryRunner"))
        self.logger = self.enterContext(patch.object(application, "logger"))
        self.run_chat = self.enterContext(patch.object(cli, "run_chat"))
        self.stdout = self.enterContext(patch("sys.stdout", new_callable=io.StringIO))

    def run_main(self, settings=None):
        with patch.object(self.cli, "Settings", return_value=settings or self.settings):
            return self.cli.main([])

    def test_failure_reports_stage_and_error_class_but_not_exception_text(self):
        self.bigquery.side_effect = RuntimeError("SECRET credentials path")
        self.assertEqual(self.run_main(), 1)
        self.assertIn("stage 'bigquery_client' (RuntimeError)", self.stdout.getvalue())
        self.assertIn("gcloud auth application-default login", self.stdout.getvalue())
        self.logger.error.assert_called_once_with(
            "startup_failed",
            extra={"stage": "bigquery_client", "error_type": "RuntimeError"},
        )
        self.assertNotIn("SECRET", self.stdout.getvalue() + repr(self.logger.mock_calls))
        self.run_chat.assert_not_called()
        self.shutdown_tracing.assert_called_once()
        self.gemini.from_settings.return_value.close.assert_called_once()

    def test_access_config_problems_get_direct_messages(self):
        for error, expected in (
            (FileNotFoundError("SECRET path"), "Access config not found"),
            (PermissionError("SECRET"), "no authorized brands"),
        ):
            with self.subTest(error=type(error).__name__):
                self.entitlements.from_file.side_effect = error
                self.assertEqual(self.run_main(), 1)
                self.assertIn(expected, self.stdout.getvalue())
                self.assertNotIn("SECRET", self.stdout.getvalue())
        self.logger.error.assert_not_called()
        self.gemini.from_settings.assert_not_called()

    def test_missing_gemini_setting_stops_before_clients(self):
        self.assertEqual(self.run_main(Settings(_env_file=None, gemini_api_key="fake")), 1)
        self.assertIn("Set GEMINI_MODEL in your .env", self.stdout.getvalue())
        self.gemini.from_settings.assert_not_called()

    def test_successful_start_runs_chat_then_closes_clients(self):
        self.assertEqual(self.run_main(), 0)
        self.run_chat.assert_called_once_with(self.application.build_agent.return_value)
        self.gemini.from_settings.return_value.close.assert_called_once()
        self.bigquery.return_value.client.close.assert_called_once()
        self.shutdown_tracing.assert_called_once()
        self.assertEqual(self.logger.info.call_args.args[0], "startup_completed")

    def test_invalid_settings_and_cohort_flag(self):
        with patch.object(
            self.cli,
            "Settings",
            side_effect=ValidationError.from_exception_data(
                "Settings", [{"type": "int_parsing", "loc": ("reports_postgres_port",), "input": "SECRET"}]
            ),
        ):
            self.assertEqual(self.cli.main([]), 1)
        self.assertIn("REPORTS_POSTGRES_PORT", self.stdout.getvalue())
        self.assertNotIn("SECRET", self.stdout.getvalue())
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            self.cli.parse_options(self.settings, ["--min-cohort-size", "1"])
        options = self.cli.parse_options(self.settings, ["--manager", "manager_b"])
        self.assertEqual((options.manager, options.min_cohort_size), ("manager_b", 5))
