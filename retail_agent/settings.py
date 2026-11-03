"""Configuration read once from the environment and `.env` at startup.

Entry points build one `Settings` and pass plain values to constructors; no
component reads the environment itself. Environment variables win over `.env`
(as with python-dotenv), the `.env` file is looked up in the working directory,
and empty values fall back to the defaults below. Variable names are the field
names in upper case and match `.env.example`.
"""

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore", frozen=True
    )

    # Gemini. Required for the chat; checked by the entry point with a clear message.
    gemini_api_key: SecretStr | None = None
    gemini_model: str = ""
    gemini_fallback_model: str = ""
    eval_judge_model: str = ""

    # BigQuery billing project and the default demo identity (CLI flags override both).
    google_cloud_project: str | None = None
    retail_manager_id: str = "manager_a"

    # Application PostgreSQL (reports, memory, Golden index). The REPORTS_ prefix
    # is kept for backwards-compatible .env files. Defaults match local Compose.
    reports_postgres_host: str = "127.0.0.1"
    reports_postgres_port: int = 5433
    reports_postgres_database: str = "retail_agent"
    reports_postgres_user: str = "retail_agent"
    reports_postgres_password: SecretStr = SecretStr("local-reports-demo")
    reports_embedding_model: str = "gemini-embedding-2"

    # Optional style-only Google Doc (ID or URL); empty means the built-in style.
    persona_google_doc_id: str = ""

    golden_retrieval_enabled: bool = True
    golden_min_similarity: float = 0.65
    golden_ingest_postgres_user: str = "golden_ingest"
    golden_ingest_postgres_password: SecretStr = SecretStr("local-golden-ingest-demo")

    # Optional local Langfuse; never falls back to a cloud endpoint.
    langfuse_tracing_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str = ""


def invalid_settings(error: ValidationError) -> str:
    """Names of invalid variables only: validation messages may echo secret values."""
    names = sorted({str(item["loc"][0]).upper() for item in error.errors() if item["loc"]})
    return "Invalid configuration in .env or the environment: " + ", ".join(names)
