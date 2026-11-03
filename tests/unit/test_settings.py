import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from retail_agent.settings import Settings, invalid_settings
from support.paths import REPO_ROOT


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.env_file = Path(self.enterContext(tempfile.TemporaryDirectory())) / ".env"

    def load(self, text: str) -> Settings:
        self.env_file.write_text(text, encoding="utf-8")
        return Settings(_env_file=self.env_file)

    def test_defaults_match_local_compose_and_env_example(self):
        settings = Settings(_env_file=None)
        self.assertEqual(settings.reports_postgres_port, 5433)
        self.assertEqual(settings.reports_postgres_password.get_secret_value(), "local-reports-demo")
        self.assertEqual(settings.reports_embedding_model, "gemini-embedding-2")
        self.assertTrue(settings.golden_retrieval_enabled)
        self.assertEqual(settings.golden_min_similarity, 0.65)
        self.assertFalse(settings.langfuse_tracing_enabled)
        self.assertIsNone(settings.gemini_api_key)

    def test_env_file_is_read_and_environment_wins(self):
        os.environ["GEMINI_MODEL"] = "from-environment"
        settings = self.load(
            "GEMINI_API_KEY=key\nGEMINI_MODEL=from-file\nGOLDEN_RETRIEVAL_ENABLED=false\nUNRELATED_VARIABLE=ignored\n"
        )
        self.assertEqual(settings.gemini_api_key.get_secret_value(), "key")
        self.assertEqual(settings.gemini_model, "from-environment")
        self.assertFalse(settings.golden_retrieval_enabled)

    def test_empty_values_use_defaults(self):
        settings = self.load("REPORTS_POSTGRES_PORT=\nGOLDEN_MIN_SIMILARITY=\nGEMINI_FALLBACK_MODEL=\n")
        self.assertEqual(settings.reports_postgres_port, 5433)
        self.assertEqual(settings.golden_min_similarity, 0.65)
        self.assertEqual(settings.gemini_fallback_model, "")

    def test_secrets_are_masked_and_invalid_values_report_names_only(self):
        settings = self.load("GEMINI_API_KEY=SECRET-KEY\n")
        self.assertNotIn("SECRET-KEY", repr(settings))
        with self.assertRaises(ValidationError) as caught:
            self.load("REPORTS_POSTGRES_PORT=SECRET-PORT\nGOLDEN_MIN_SIMILARITY=high\n")
        message = invalid_settings(caught.exception)
        self.assertIn("GOLDEN_MIN_SIMILARITY, REPORTS_POSTGRES_PORT", message)
        self.assertNotIn("SECRET", message)

    def test_settings_are_immutable(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None).gemini_model = "other"

    def test_components_do_not_read_the_environment(self):
        root = REPO_ROOT / "retail_agent"
        offenders = [
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if any(token in path.read_text(encoding="utf-8") for token in ("os.getenv", "os.environ", "load_dotenv"))
        ]
        self.assertEqual(offenders, [])
