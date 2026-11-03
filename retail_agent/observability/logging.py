import json
import logging
from datetime import UTC, datetime
from pathlib import Path

_FIELDS = (
    "query_id",
    "turn_id",
    "stage",
    "action",
    "status",
    "error_code",
    "duration_ms",
    "row_count",
    "truncated",
    "result_bytes",
    "estimated_bytes",
    "bytes_processed",
    "bytes_billed",
    "query_count",
    "model_calls",
    "scan_bytes_used",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "error_type",
    "http_status",
    "retry_after_seconds",
    "validation_fields",
    "validation_error_count",
    "validation_reason",
    "report_calls",
    "persona_version",
    "memory_calls",
    "model",
    "attempt",
    "delay_ms",
    "fallback",
    "candidate_count",
    "golden_ids",
    "top_similarity",
)


class SafeJsonFormatter(logging.Formatter):
    def format(self, record):
        # Explicit field allowlist; never serialize record.__dict__ or exc_info.
        payload = {
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.msg,
            **{name: getattr(record, name) for name in _FIELDS if hasattr(record, name)},
        }
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(target, encoding="utf-8")
    handler.setFormatter(SafeJsonFormatter())
    logger = logging.getLogger("retail_agent")
    for old_handler in logger.handlers[:]:
        logger.removeHandler(old_handler)
        old_handler.close()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # No implicit export through a root logger.
