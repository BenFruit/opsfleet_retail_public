"""Chat application: builds the agent in named startup stages and owns its resources.

A failed stage becomes a StartupError whose message is safe to print: it names
the stage, the exception class and an operator hint, never the exception text
(driver and credential errors may contain paths, hosts or keys).
"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass

from retail_agent.agent.orchestrator import AnalyticalAgent
from retail_agent.bootstrap import build_agent
from retail_agent.data.bigquery_runner import BigQueryRunner
from retail_agent.data.query_service import QueryLimits, QueryService
from retail_agent.llm.client import GeminiClient
from retail_agent.llm.embeddings import Embedder
from retail_agent.observability.logging import configure_logging
from retail_agent.observability.tracing import configure_tracing, shutdown_tracing
from retail_agent.schema.service import SchemaService
from retail_agent.security.access import Entitlements
from retail_agent.settings import Settings
from retail_agent.storage.postgres import connection_factory

logger = logging.getLogger("retail_agent.startup")

# Startup stage -> safe operator hint.
STARTUP_HINTS = {
    "logging": "Check that the log file directory is writable.",
    "access_config": "Copy access.example.json to access.local.json and edit the brands.",
    "gemini_client": "Check GEMINI_API_KEY and GEMINI_MODEL in .env.",
    "bigquery_client": "Run 'gcloud auth application-default login' and set GOOGLE_CLOUD_PROJECT.",
    "bigquery_schema": "Check Google ADC, the quota project and BigQuery API access to the public dataset.",
    "agent_init": "Check the optional reports/memory/persona settings in .env and the README.",
}
# Expected access-config problems get a direct message instead of a stage report.
ACCESS_CONFIG_MESSAGES = (
    (FileNotFoundError, "Access config not found. Copy access.example.json to access.local.json and edit the brands."),
    (PermissionError, "This demo identity has no authorized brands. Check --manager and the access config."),
)


class StartupError(RuntimeError):
    """Startup cannot continue; the message is safe to show the operator."""


@dataclass(frozen=True)
class ChatOptions:
    """Per-run choices from the command line (Settings holds the .env values)."""

    project: str | None
    manager: str
    access_config: str = "access.local.json"
    log_file: str = "logs/agent.jsonl"
    min_cohort_size: int = 5


@contextmanager
def startup_stage(stage: str):
    try:
        yield
    except Exception as error:
        if stage == "access_config":
            for error_type, message in ACCESS_CONFIG_MESSAGES:
                if isinstance(error, error_type):
                    raise StartupError(message) from None
        logger.error("startup_failed", extra={"stage": stage, "error_type": type(error).__name__})
        raise StartupError(
            f"Startup failed at stage '{stage}' ({type(error).__name__}). {STARTUP_HINTS[stage]}"
        ) from None


class ChatApplication:
    """Use as a context manager: start() builds the agent, exit closes clients and tracing."""

    def __init__(self, settings: Settings, options: ChatOptions):
        self.settings = settings
        self.options = options
        self.llm: GeminiClient | None = None
        self.runner: BigQueryRunner | None = None
        self.embedder: Embedder | None = None

    def start(self) -> AnalyticalAgent:
        settings, options = self.settings, self.options
        with startup_stage("logging"):
            configure_logging(options.log_file)
        with startup_stage("access_config"):
            entitlements = Entitlements.from_file(options.access_config)
            entitlements.brands_for(options.manager)  # Fail fast for an unknown identity.
        for name, value in (("GEMINI_API_KEY", settings.gemini_api_key), ("GEMINI_MODEL", settings.gemini_model)):
            if not value:
                raise StartupError(f"Set {name} in your .env before starting the chat.")
        configure_tracing(settings)  # Never raises: tracing failures are logged and tracing stays off.
        with startup_stage("gemini_client"):
            self.llm = GeminiClient.from_settings(settings)
        with startup_stage("bigquery_client"):
            self.runner = BigQueryRunner(project_id=options.project)
        with startup_stage("bigquery_schema"):
            schema = SchemaService(self.runner).load_analysis_schema()
        with startup_stage("agent_init"):
            self.embedder = Embedder.from_settings(settings)
            golden = settings.golden_retrieval_enabled
            agent = build_agent(
                self.llm,
                schema,
                QueryService(self.runner, QueryLimits(minimum_cohort_size=options.min_cohort_size)),
                manager_id=options.manager,
                entitlements=entitlements,
                connect=connection_factory(settings),
                embedder=self.embedder,
                golden=golden,
                memory=True,
                persona_doc_id=settings.persona_google_doc_id,
                golden_min_similarity=settings.golden_min_similarity,
            )
        logger.info(
            "startup_completed",
            extra={
                "model": self.llm.model,
                "fallback": self.llm.fallback_model is not None,
                "status": "golden_on" if golden else "golden_off",
            },
        )
        return agent

    def close(self) -> None:
        shutdown_tracing()
        if self.llm is not None:
            self.llm.close()
        if self.embedder is not None:
            self.embedder.close()
        if self.runner is not None:
            self.runner.client.close()

    def __enter__(self) -> "ChatApplication":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
