"""Shared demo entitlements for tests (same shape as access.example.json, fictional brands)."""

from retail_agent.security.access import Entitlements

DEMO_ENTITLEMENTS = Entitlements(
    {
        "manager_a": ["Brand A", "Brand B"],
        "manager_b": ["Brand C"],
    }
)


def patch_agent_loggers(test_case):
    """Route every agent-package logger into one mock so log-safety checks see all events."""
    from unittest.mock import Mock, patch

    logger = Mock()
    for module in ("orchestrator", "answer", "commands"):
        test_case.enterContext(patch(f"retail_agent.agent.{module}.logger", logger))
    return logger
