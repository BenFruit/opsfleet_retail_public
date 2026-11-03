"""Offline Golden Knowledge ingestion: bucket files -> validated -> embedded -> index.

    uv run python -m retail_agent.golden.ingest --check-only
    uv run python -m retail_agent.golden.ingest --bigquery-dry-run
    uv run python -m retail_agent.golden.ingest

Every run validates the WHOLE bucket first and publishes nothing if any trio
fails. Only changed trios are re-embedded (content hash), and withdrawn or
deleted trios are removed from the index in the same transaction.
"""

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from retail_agent.golden.models import GoldenError, GoldenTrio
from retail_agent.llm.embeddings import EmbeddingError
from retail_agent.safety.errors import ResultGuardError
from retail_agent.safety.sensitive_text import check_text
from retail_agent.sql.scope import apply_brand_scope
from retail_agent.sql.validator import SQLValidationError
from retail_agent.storage.postgres import StorageError

DEFAULT_BUCKET = Path("golden/trios")


def load_bucket(directory: Path) -> list[GoldenTrio]:
    """Load and fully validate every trio, or raise with all problems listed."""
    problems, trios = [], []
    files = sorted(directory.glob("*.json"))
    if not files:
        raise GoldenError(f"No trio files found in {directory}.")
    for path in files:
        try:
            trio = GoldenTrio.model_validate_json(path.read_text(encoding="utf-8"))
            if path.stem != trio.id:
                raise ValueError(f"file name must equal id '{trio.id}'")
            # Reviewed SQL must pass the SAME policy as model SQL: no PII, aggregate-only
            # shapes for demographics, and successful brand-scope rewriting.
            apply_brand_scope(trio.sql)
            check_text(trio.search_text())
            check_text(trio.analyst_report)
            trios.append(trio)
        except ValidationError as error:
            fields = sorted({".".join(str(part) for part in issue["loc"]) for issue in error.errors()})
            problems.append(f"{path.name}: invalid fields {fields}")
        except SQLValidationError as error:
            problems.append(f"{path.name}: SQL rejected by policy: {error}")
        except ResultGuardError:
            problems.append(f"{path.name}: contains a contact-detail pattern")
        except ValueError as error:
            problems.append(f"{path.name}: {error}")
    ids = [trio.id for trio in trios]
    problems += [f"duplicate id '{trio_id}'" for trio_id in sorted({i for i in ids if ids.count(i) > 1})]
    if problems:
        raise GoldenError("Golden bucket validation failed:\n- " + "\n- ".join(problems))
    return trios


@dataclass
class IngestPlan:
    upserts: list[GoldenTrio] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)


def plan_ingest(trios: list[GoldenTrio], indexed: dict[str, tuple[str, str]], model: str) -> IngestPlan:
    plan = IngestPlan()
    approved = {trio.id: trio for trio in trios if trio.status == "approved"}
    for trio_id, trio in approved.items():
        if indexed.get(trio_id) == (trio.content_hash(), model):
            plan.unchanged.append(trio_id)
        else:
            plan.upserts.append(trio)  # New, edited, or embedded with another model.
    plan.remove = sorted(set(indexed) - set(approved))
    return plan


def ingest(trios: list[GoldenTrio], repository, embedder) -> IngestPlan:
    plan = plan_ingest(trios, repository.indexed_hashes(), embedder.model)
    # Embed everything BEFORE the write transaction so a provider outage publishes nothing.
    upserts = [
        (
            trio,
            embedder.embed(trio.search_text(), document=True, title=trio.question, observation_name="golden_embedding"),
        )
        for trio in plan.upserts
    ]
    if upserts or plan.remove:
        repository.publish(upserts=upserts, remove=plan.remove, model=embedder.model)
    return plan


def bigquery_dry_run(trios: list[GoldenTrio], runner) -> list[str]:
    """Check that every reviewed SQL compiles in BigQuery after brand scoping (free dry run)."""
    failures = []
    for trio in trios:
        try:
            runner.dry_run(apply_brand_scope(trio.sql), allowed_brands=("__golden_dry_run__",), api_timeout_seconds=15)
        except Exception as error:
            failures.append(f"{trio.id}: {type(error).__name__}")
    return failures


def main(argv=None) -> int:
    from retail_agent.settings import Settings

    settings = Settings()
    parser = argparse.ArgumentParser(description="Validate and index the Golden Knowledge bucket")
    parser.add_argument("--bucket", type=Path, default=DEFAULT_BUCKET)
    parser.add_argument("--check-only", action="store_true", help="validate files only; no network calls")
    parser.add_argument("--bigquery-dry-run", action="store_true", help="also dry-run every SQL in BigQuery")
    args = parser.parse_args(argv)
    try:
        trios = load_bucket(args.bucket)
        print(f"Validated {len(trios)} trio files ({sum(t.status == 'approved' for t in trios)} approved).")
        if args.bigquery_dry_run:
            from retail_agent.data.bigquery_runner import BigQueryRunner

            failures = bigquery_dry_run(trios, BigQueryRunner(project_id=settings.google_cloud_project))
            if failures:
                print("BigQuery dry run failed:\n- " + "\n- ".join(failures))
                return 1
            print("BigQuery dry run passed for all trios.")
        if args.check_only or args.bigquery_dry_run:
            return 0
        from retail_agent.golden.repository import GoldenRepository
        from retail_agent.llm.embeddings import Embedder
        from retail_agent.storage.postgres import golden_ingest_connection_factory

        embedder = Embedder.from_settings(settings)
        try:
            plan = ingest(trios, GoldenRepository(golden_ingest_connection_factory(settings)), embedder)
        finally:
            embedder.close()
        print(f"Indexed: {len(plan.upserts)} new/changed, {len(plan.unchanged)} unchanged, {len(plan.remove)} removed.")
        return 0
    except (GoldenError, EmbeddingError, StorageError) as error:
        print(error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
